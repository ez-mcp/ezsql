"""SQL debugging and error catalog matching pipeline (plan §21 #8, FR-4).

Deterministic-first: matches the error text against the data-driven
error catalog, cross-checks the optional SQL against the repo schema,
and produces ranked hypotheses with next diagnostics. Escalates to the
LLM only when no catalog match clears the threshold (policy (a), D3) —
and only when an API key is configured. The advisory refines, never
replaces, deterministic findings (plan §9).

Caching: the deterministic skeleton is cached under the ``debug``
domain; the escalation advisory is never cached.
"""

import logging
from pathlib import Path

from sqlglot import exp

from ezsql.cache.keys import debug_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.debug.catalog import match_error
from ezsql.core.schema.repository import load_repo_schema
from ezsql.core.sql.parse import InternalFailure, parse
from ezsql.observability import counters
from ezsql.server.models import (
    CacheProvenance,
    CatalogMatchModel,
    DebugResult,
    EscalationResult,
    FailureEnvelope,
    Hypothesis,
    SchemaImpact,
)

logger = logging.getLogger("ezsql.pipelines.debug")

__all__ = ["run_debug_sql"]

# Minimum specificity for a catalog match to count as "conclusive"
# (SQLSTATE-code matches are specificity 2; message-text matches are 1).
_MATCH_THRESHOLD = 2


def _schema_cross_check(
    sql: str | None,
    dialect: str,
    config: EzsqlConfig,
    root: Path,
    cache: CacheStore | None,
) -> SchemaImpact:
    """Cross-check the failing SQL's table/column refs against the repo schema."""
    if sql is None:
        return SchemaImpact(schema_source="none")
    load = load_repo_schema(root, config, cache)
    if load.schema is None:
        return SchemaImpact(schema_source="none")

    parse_result = parse(
        sql,
        dialect=dialect,
        configured_dialect=config.default_dialect,
        max_statements=config.max_statements,
    )
    if isinstance(parse_result, InternalFailure):
        # Cross-check is best-effort: internal parse failure yields an
        # empty impact rather than failing the whole diagnosis.
        return SchemaImpact(schema_source="repo-ddl")
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for stmt in parse_result.statements:
        for tbl in stmt.find_all(exp.Table):
            if tbl.name not in load.schema.tables and tbl.name not in missing_tables:
                missing_tables.append(tbl.name)
        for col in stmt.find_all(exp.Column):
            table_name = col.table or next(
                (t.name for t in stmt.find_all(exp.Table) if t.name in load.schema.tables),
                "",
            )
            if not table_name:
                continue
            table_def = load.schema.tables.get(table_name)
            if table_def is not None and col.name not in table_def.columns:
                qualified = f"{table_name}.{col.name}"
                if qualified not in missing_columns:
                    missing_columns.append(qualified)

    return SchemaImpact(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        schema_source="repo-ddl",
    )


def _build_hypotheses(
    catalog_matches: list[CatalogMatchModel],
    impact: SchemaImpact,
) -> list[Hypothesis]:
    """Rank hypotheses: catalog matches first, then schema evidence."""
    hypotheses: list[Hypothesis] = []
    rank = 1
    for m in catalog_matches:
        hypotheses.append(
            Hypothesis(
                rank=rank,
                statement=m.diagnosis,
                basis="catalog",
                next_diagnostics=[m.fix_guidance],
            )
        )
        rank += 1
    if impact.missing_tables:
        hypotheses.append(
            Hypothesis(
                rank=rank,
                statement=(
                    f"The query references table(s) not present in the repo "
                    f"schema: {', '.join(impact.missing_tables)}. The migration "
                    f"creating them may be missing or unapplied."
                ),
                basis="schema",
                next_diagnostics=[
                    "Check migration ordering and application state.",
                ],
            )
        )
        rank += 1
    if impact.missing_columns:
        hypotheses.append(
            Hypothesis(
                rank=rank,
                statement=(
                    f"The query references column(s) not present in the repo "
                    f"schema: {', '.join(impact.missing_columns)}."
                ),
                basis="schema",
                next_diagnostics=[
                    "Verify column names against the repo DDL (analyze_sql).",
                ],
            )
        )
        rank += 1
    return hypotheses


def run_debug_sql(
    config: EzsqlConfig,
    root: Path,
    cache: CacheStore | None = None,
    *,
    error: str,
    sql: str | None = None,
    context: str | None = None,  # noqa: ARG001 — reserved for future doc retrieval
    dialect: str | None = None,
    task: str | None = None,
) -> DebugResult | FailureEnvelope:
    """Run the debug_sql pipeline (plan_phase4 FR-4).

    Args:
        config: The loaded EZSQL config.
        root: The resolved project root.
        cache: Optional cache store.
        error: The error text to diagnose (untrusted data).
        sql: Optional failing SQL for AST/schema cross-check.
        context: Optional additional context (reserved).
        dialect: Optional explicit dialect.
        task: Optional task ID (registry wiring, FR-8).

    Returns:
        ``DebugResult`` on success, or ``FailureEnvelope`` on failure.
    """
    # Note: tool invocation counters are owned by the server wrappers
    # (plan_phase3 §11); the pipeline owns domain events only.
    counters.inc("debug_requests", 1)

    if len(error.encode("utf-8")) > config.max_error_input_bytes:
        return FailureEnvelope(
            kind="input_too_large",
            detail=(
                f"Error text exceeds max_error_input_bytes "
                f"({config.max_error_input_bytes})"
            ),
            recoverable=True,
            next_steps=["Trim the error text to the relevant message."],
        )
    if not error.strip():
        return FailureEnvelope(
            kind="no_input",
            detail="Empty error text.",
            recoverable=True,
            next_steps=["Provide the database error message."],
        )

    resolved_dialect = dialect or config.default_dialect

    key = debug_key(error, sql, resolved_dialect)
    if cache is not None:
        cached = cache.get(key, DebugResult)
        if cached is not None:
            counters.inc("cache_hits", 1)
            logger.info("debug_cache_hit")
            cached.cache_provenance = CacheProvenance(cache_hit=True, cache_key=key)
            # Advisory is never cached: re-derive per call when inconclusive.
            if not cached.catalog_matches:
                escalation = _maybe_escalate(error, sql, config)
                if escalation is not None:
                    cached.escalation = escalation
            _record_task_ref(task, key)
            return cached

    counters.inc("cache_misses", 1)

    raw_matches = match_error(error, resolved_dialect)
    catalog_matches = [
        CatalogMatchModel(
            catalog_id=m.catalog_id,
            diagnosis=m.diagnosis,
            fix_guidance=m.fix_guidance,
            severity=m.severity,  # type: ignore[arg-type]
            specificity=m.specificity,
        )
        for m in raw_matches
    ]
    matches_truncated = False
    matches_suppressed = 0
    if len(catalog_matches) > config.max_catalog_matches:
        matches_suppressed = len(catalog_matches) - config.max_catalog_matches
        catalog_matches = catalog_matches[: config.max_catalog_matches]
        matches_truncated = True

    impact = _schema_cross_check(sql, resolved_dialect, config, root, cache)

    hypotheses = _build_hypotheses(catalog_matches, impact)
    hyp_truncated = False
    hyp_suppressed = 0
    if len(hypotheses) > config.max_hypotheses:
        hyp_suppressed = len(hypotheses) - config.max_hypotheses
        hypotheses = hypotheses[: config.max_hypotheses]
        hyp_truncated = True

    next_diagnostics: list[str] = []
    for m in catalog_matches:
        next_diagnostics.append(m.fix_guidance)
    if impact.missing_tables:
        next_diagnostics.append(
            "Run find_context to locate migrations for the missing tables."
        )

    result = DebugResult(
        dialect=resolved_dialect,
        catalog_matches=catalog_matches,
        hypotheses=hypotheses,
        schema_cross_check=impact,
        next_diagnostics=next_diagnostics,
        catalog_matches_truncated=matches_truncated,
        catalog_matches_suppressed=matches_suppressed,
        hypotheses_truncated=hyp_truncated,
        hypotheses_suppressed=hyp_suppressed,
        cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
    )

    # Escalation trigger — policy (a): no catalog match above threshold (D3).
    has_conclusive = any(m.specificity >= _MATCH_THRESHOLD for m in raw_matches)
    if not has_conclusive:
        escalation = _maybe_escalate(error, sql, config)
        if escalation is not None:
            result.escalation = escalation

    if cache is not None:
        # Advisory is never cached: strip before storing.
        cached_copy = result.model_copy(deep=True)
        cached_copy.escalation = EscalationResult()
        cache.put(key, "debug", cached_copy)
    logger.info(
        "debug_complete",
        extra={
            "matches": len(catalog_matches),
            "hypotheses": len(hypotheses),
        },
    )
    _record_task_ref(task, key)
    return result


def _maybe_escalate(
    error: str,
    sql: str | None,
    config: EzsqlConfig,
) -> EscalationResult | None:
    """Escalate when no conclusive catalog match exists (policy a)."""
    from ezsql.llm.escalate import escalate

    prompt_parts = [
        "Diagnose this database error and suggest next diagnostics. "
        "Be concise; your output is advisory only.",
        error[: config.max_error_input_bytes],
    ]
    if sql is not None:
        prompt_parts.append(f"Failing SQL:\n{sql[: config.max_sql_input_bytes]}")
    return escalate(prompt_parts, config.llm_token_budget, config=config)


def _record_task_ref(task: str | None, cache_key: str) -> None:
    """Attach the result to the task registry (plan_phase4 FR-8)."""
    if task is None:
        return
    from ezsql.tasks.registry import get_registry

    registry = get_registry()
    registry.get_or_create(task)
    registry.add_ref(task, cache_key, "debug")
