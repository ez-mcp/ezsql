"""Explain pipeline: cache orchestration + public result mapping (plan_phase3 §2).

The pipeline is the sole layer that maps typed adapter errors to public
failure fields (plan_phase3 §8). Plans are cached TTL-bound in a separate
``explain`` domain; failures are never cached.
"""

import hashlib

from ezsql.cache.keys import explain_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.sql.explain_gate import GateRejection, validate_explainable_query
from ezsql.core.sql.plan import ParsedPlan, summarize_plan
from ezsql.db.errors import DbAdapterError
from ezsql.db.lifecycle import AdapterLifecycle
from ezsql.observability import counters
from ezsql.server.models import CacheProvenance, ExplainResult, FailureEnvelope


def _failure(kind: str, detail: str, recoverable: bool = True,
             next_steps: list[str] | None = None) -> FailureEnvelope:
    return FailureEnvelope(
        kind=kind, detail=detail, recoverable=recoverable,
        next_steps=next_steps or [],
    )


def _map_adapter_error(exc: DbAdapterError) -> FailureEnvelope:
    """Map a typed adapter error to the public failure envelope (§8)."""
    return _failure(exc.category, exc.detail)


async def run_explain_query(
    sql: str,
    config: EzsqlConfig,
    root: object,
    uri: str,
    lifecycle: AdapterLifecycle,
    cache: CacheStore | None = None,
    *,
    dialect: str | None = None,
) -> ExplainResult | FailureEnvelope:
    """Run the explain_query pipeline.

    Args:
        sql: The raw SQL from the tool call (validated by the gate here).
        config: The loaded per-call config.
        root: The resolved project root (for repo-DDL fingerprint).
        uri: The resolved database URI.
        lifecycle: The adapter lifecycle manager.
        cache: Optional cache store (TTL-bound explain domain).
        dialect: Must resolve to postgres; no silent transpilation.
    """
    counters.inc("explain_requests", 1)

    # Dialect contract: postgres only, no silent transpilation (§1).
    if dialect is not None and dialect.strip().lower() not in ("postgres", "postgresql"):
        return _failure(
            "dialect_not_supported",
            f"explain_query supports PostgreSQL only; got '{dialect}'",
        )

    # Gate 1: exact-one-query validation.
    gate = validate_explainable_query(sql, max_bytes=config.max_explain_sql_bytes)
    if isinstance(gate, GateRejection):
        counters.inc("statement_gate_blocks", 1)
        return _failure(gate.reason, gate.detail)

    # Acquire the shared adapter.
    from pathlib import Path
    acquire_result = await lifecycle.acquire(Path(root), uri, config)  # type: ignore[arg-type]
    if acquire_result.failure is not None or acquire_result.adapter is None:
        counters.inc("db_connection_failures", 1)
        return _map_adapter_error(acquire_result.failure or DbAdapterError(
            "db_connection_failed", "adapter unavailable"
        ))
    adapter = acquire_result.adapter

    try:
        # Cache lookup (TTL-bound, separate domain).
        key = explain_key(
            gate.canonical_sql,
            adapter.identity.fingerprint,
            "",  # repo fingerprint: explain_query uses empty (§6)
            adapter.server_major_version,
        )
        if cache is not None:
            cached = cache.get(key, ParsedPlan, ttl_seconds=config.explain_ttl_seconds)
            if cached is not None:
                counters.inc("explain_cache_hits", 1)
                return ExplainResult(
                    sql_fingerprint=hashlib.blake2b(
                        gate.canonical_sql.encode(), digest_size=16
                    ).hexdigest(),
                    summary=summarize_plan(cached),
                    plan=cached,
                    cache_provenance=CacheProvenance(cache_hit=True, cache_key=key),
                )

        counters.inc("explain_cache_misses", 1)

        # Live EXPLAIN (CPU-bound normalization runs inside the adapter's
        # async context; the JSON payload is bounded by response-size check).
        try:
            plan = await adapter.explain(gate)
        except DbAdapterError as exc:
            counters.inc("explain_failures", 1)
            return _map_adapter_error(exc)

        counters.inc("explain_successes", 1)

        # Cache store — only on success (failures never cached, §6).
        if cache is not None:
            cache.put(key, "explain", plan, ttl_seconds=config.explain_ttl_seconds)

        return ExplainResult(
            sql_fingerprint=hashlib.blake2b(
                gate.canonical_sql.encode(), digest_size=16
            ).hexdigest(),
            summary=summarize_plan(plan),
            plan=plan,
            cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
        )
    finally:
        await lifecycle.release(Path(root), uri, config, adapter)  # type: ignore[arg-type]


__all__ = ["run_explain_query"]
