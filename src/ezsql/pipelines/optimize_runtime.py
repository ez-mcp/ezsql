"""Runtime optimization evidence orchestration (plan_phase3 §5).

Async wrapper around the synchronous static pipeline. The static pipeline
(``run_optimize_query``) keeps its exact Phase 2 signature, output, and
cache contract; only the wrapper adds live planner evidence. Schema-backed
static results used for enrichment are computed with ``cache=None`` so the
legacy static domain is never polluted (V3-4).

Flow:
  ├─ no adapter → static result via ``asyncio.to_thread`` (unchanged)
  ├─ load repo schema (worker thread)
  ├─ static optimize with schema, cache=None (worker thread)
  ├─ EXPLAIN original; on failure → static result, status="failed"
  ├─ EXPLAIN top N eligible candidates concurrently (TaskGroup)
  ├─ compute typed PlanDelta values
  └─ merge evidence into a copy of the static result
"""

import asyncio
from pathlib import Path

from ezsql.cache.keys import optimize_key, runtime_evidence_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.schema.repository import load_repo_schema
from ezsql.core.sql.explain_gate import (
    GateRejection,
    validate_explainable_query,
)
from ezsql.core.sql.plan import ParsedPlan, PlanDelta, compute_plan_delta
from ezsql.db.errors import DbAdapterError
from ezsql.db.lifecycle import AdapterLifecycle
from ezsql.db.postgres import PostgresAdapter
from ezsql.observability import counters, logger
from ezsql.pipelines.optimize import run_optimize_query
from ezsql.server.models import (
    CacheProvenance,
    FailureEnvelope,
    OptimizeResult,
    RewriteCandidate,
)

# Result-shaping limits embedded in the runtime evidence key (§6).
_KEY_LIMIT_FIELDS = (
    "max_candidates", "max_explain_candidates", "max_findings",
    "max_plan_nodes", "max_plan_depth", "max_plan_condition_chars",
    "max_plan_response_bytes",
)


def _eligible_candidates(
    result: OptimizeResult, config: EzsqlConfig
) -> list[tuple[int, RewriteCandidate]]:
    """Select candidates eligible for live EXPLAIN (plan_phase3 §5).

    Eligibility: validated, not withheld, exactly one gate-accepted query,
    within ``max_explain_candidates`` preserving Phase 2 order.
    """
    eligible: list[tuple[int, RewriteCandidate]] = []
    for candidate in result.candidates:
        if len(eligible) >= config.max_explain_candidates:
            break
        if candidate.validation_status != "validated":
            continue
        if candidate.security_status == "withheld":
            continue
        gate = validate_explainable_query(
            candidate.rewritten_sql, max_bytes=config.max_explain_sql_bytes
        )
        if isinstance(gate, GateRejection):
            continue
        eligible.append((len(eligible), candidate))
    return eligible


def _candidate_identity(candidate: RewriteCandidate) -> str:
    """Stable identity of a candidate for cache keying."""
    return candidate.original_hash + ":" + candidate.rewritten_sql


async def run_optimize_query_with_runtime(
    sql: str,
    config: EzsqlConfig,
    root: Path,
    uri: str | None,
    lifecycle: AdapterLifecycle | None,
    cache: CacheStore | None = None,
    *,
    dialect: str | None = None,
    task: str | None = None,
) -> OptimizeResult | FailureEnvelope:
    """Async optimize wrapper: static result + optional live planner evidence.

    No DB configured → the exact Phase 2 static path (via worker thread).
    DB configured → schema-backed static analysis (cache=None) plus
    TTL-bound runtime evidence merged at return time.
    """
    # No DB → unchanged static path, off the event loop (V3-5).
    if uri is None or lifecycle is None:
        return await asyncio.to_thread(
            run_optimize_query, sql, config, cache,
            dialect=dialect, task=task,
        )

    # Resolve dialect the same way the static pipeline does.
    resolved_dialect = dialect or config.default_dialect
    if resolved_dialect != "postgres":
        # Non-Postgres dialects get static-only results (§8).
        return await asyncio.to_thread(
            run_optimize_query, sql, config, cache,
            dialect=dialect, task=task,
        )

    # Load repository schema (worker thread; bounded loader).
    schema_result = await asyncio.to_thread(load_repo_schema, root, config, cache)
    schema = schema_result.schema
    repo_fp = schema_result.fingerprint if schema is not None else ""

    # Schema-backed static analysis with cache=None — the legacy static
    # domain must never see schema-backed results (V3-4).
    static_result = await asyncio.to_thread(
        run_optimize_query, sql, config, None,
        dialect=dialect, schema=schema, task=task,
    )
    if isinstance(static_result, FailureEnvelope):
        return static_result

    # Acquire the shared adapter.
    acquire = await lifecycle.acquire(root, uri, config)
    if acquire.failure is not None or acquire.adapter is None:
        failure = acquire.failure
        logger.info(
            "runtime_evidence_unavailable: kind=%s", failure.category if failure else "?"
        )
        static_result.runtime_evidence_status = "unavailable"
        static_result.runtime_evidence_detail = (
            failure.detail if failure else "adapter unavailable"
        )
        return static_result
    adapter = acquire.adapter

    try:
        # Runtime evidence cache lookup — covers both the enriched and the
        # negative "no eligible candidates" case (§6).
        eligible = _eligible_candidates(static_result, config)
        candidate_identities = [_candidate_identity(c) for _, c in eligible]
        static_key = optimize_key(sql, resolved_dialect, repo_fp or None)
        limits = [(f, getattr(config, f)) for f in _KEY_LIMIT_FIELDS]
        rt_key = runtime_evidence_key(
            static_key, adapter.identity.fingerprint, repo_fp,
            candidate_identities, limits, adapter.server_major_version,
        )

        if cache is not None:
            cached = cache.get(
                rt_key, OptimizeResult, ttl_seconds=config.explain_ttl_seconds
            )
            if cached is not None:
                cached.cache_provenance = CacheProvenance(
                    cache_hit=True, cache_key=rt_key
                )
                return cached

        # Gate the original query.
        original_gate = validate_explainable_query(
            sql, max_bytes=config.max_explain_sql_bytes
        )
        if isinstance(original_gate, GateRejection):
            static_result.runtime_evidence_status = "unavailable"
            static_result.runtime_evidence_detail = (
                f"original query not explainable: {original_gate.detail}"
            )
            return static_result

        # EXPLAIN the original first; failure → no candidate EXPLAINs (§5).
        try:
            async with asyncio.timeout(config.runtime_enrichment_timeout_seconds):
                original_plan = await adapter.explain(original_gate)
        except DbAdapterError as exc:
            counters.inc("explain_failures", 1)
            static_result.runtime_evidence_status = "failed"
            static_result.runtime_evidence_detail = exc.detail
            return static_result
        except TimeoutError:
            static_result.runtime_evidence_status = "failed"
            static_result.runtime_evidence_detail = "runtime enrichment timed out"
            return static_result

        # EXPLAIN eligible candidates concurrently. _explain_candidate
        # converts adapter errors to return values, so the TaskGroup only
        # raises on timeout or cancellation — both propagate correctly.
        deltas: dict[int, PlanDelta] = {}
        failures: dict[int, str] = {}
        tasks: dict[int, asyncio.Task[ParsedPlan | DbAdapterError]] = {}
        if eligible:
            counters.inc("runtime_candidates_attempted", len(eligible))
            try:
                async with asyncio.timeout(config.runtime_enrichment_timeout_seconds):
                    async with asyncio.TaskGroup() as tg:
                        tasks = {
                            idx: tg.create_task(_explain_candidate(
                                adapter, candidate, config
                            ))
                            for idx, candidate in eligible
                        }
            except TimeoutError:
                static_result.runtime_evidence_status = "partial"
                static_result.runtime_evidence_detail = (
                    "runtime enrichment timed out; partial evidence only"
                )
                # Fall through with whatever completed.

            for idx, task_obj in tasks.items():
                if task_obj.cancelled():
                    continue  # cancelled by the timeout — not a candidate failure
                if not task_obj.done():
                    continue
                try:
                    outcome = task_obj.result()
                except DbAdapterError as exc:  # defensive: never raised today
                    failures[idx] = exc.detail
                    continue
                if isinstance(outcome, ParsedPlan):
                    deltas[idx] = compute_plan_delta(original_plan, outcome)
                elif isinstance(outcome, DbAdapterError):
                    failures[idx] = outcome.detail

        # Merge evidence into a copy of the static result.
        merged = static_result.model_copy(deep=True)
        verified = 0
        for idx, _candidate in eligible:
            delta = deltas.get(idx)
            if delta is not None:
                merged.candidates[idx].plan_delta = delta
                merged.candidates[idx].evidence = "runtime"
                verified += 1
                if delta.cardinality_changed:
                    merged.candidates[idx].preconditions.append(
                        "semantic-safety: root cardinality changed materially "
                        "between original and rewritten plans — verify correctness, "
                        "this is not a performance claim"
                    )
            elif idx in failures:
                merged.candidates[idx].runtime_failure = failures[idx]

        counters.inc("runtime_candidates_verified", verified)

        if verified == len(eligible) or not eligible:
            merged.runtime_evidence_status = "available"
        else:
            merged.runtime_evidence_status = "partial"
            merged.runtime_evidence_detail = (
                f"{len(eligible) - verified} candidate(s) could not be verified live"
            )

        # Cache the enriched record. DB failures returned early above, so
        # only success/partial evidence reaches this point (§6 — failures
        # are never written to the runtime cache domain).
        if cache is not None:
            cache.put(
                rt_key, "runtime_evidence", merged,
                ttl_seconds=config.explain_ttl_seconds,
            )

        return merged
    finally:
        await lifecycle.release(root, uri, config, adapter)


async def _explain_candidate(
    adapter: PostgresAdapter, candidate: RewriteCandidate, config: EzsqlConfig
) -> ParsedPlan | DbAdapterError:
    """Explain one candidate; translate narrow adapter errors to outcomes.

    ``CancelledError`` is never converted — it propagates (§5).
    """
    gate = validate_explainable_query(
        candidate.rewritten_sql, max_bytes=config.max_explain_sql_bytes
    )
    if isinstance(gate, GateRejection):
        return DbAdapterError("statement_blocked", gate.detail)
    try:
        return await adapter.explain(gate)
    except DbAdapterError as exc:
        return exc


__all__ = ["run_optimize_query_with_runtime"]
