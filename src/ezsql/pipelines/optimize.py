"""SQL query optimization pipeline (plan §5.1, §16).

Flow: cache check → parse → lint → rewrite → cache store → OptimizeResult.

Static-only in Phase 2: no EXPLAIN, no runtime evidence. Findings carry
``evidence: static`` or ``evidence: schema``. ``plan_delta`` on candidates
is always ``None``.
"""

import hashlib
import logging

from ezsql.cache.keys import optimize_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.schema.model import SchemaModel
from ezsql.core.sql.lint import lint
from ezsql.core.sql.parse import InternalFailure, parse
from ezsql.core.sql.rewrite import rewrite
from ezsql.observability import counters
from ezsql.server.models import (
    CacheProvenance,
    FailureEnvelope,
    OptimizeResult,
)

logger = logging.getLogger("ezsql.pipelines.optimize")


def _schema_hash(schema: SchemaModel | None) -> str | None:
    """Compute a stable content hash of a schema model for cache keying.

    Mirrors ``pipelines/analyze.py`` — same hash of the model's canonical
    JSON dump so analyze and optimize keys vary identically with schema
    content. ``None`` stays ``None`` (schema-less key shape unchanged).
    """
    if schema is None:
        return None
    return hashlib.blake2b(
        schema.model_dump_json().encode("utf-8"), digest_size=16
    ).hexdigest()


def run_optimize_query(
    sql: str,
    config: EzsqlConfig,
    cache: CacheStore | None = None,
    *,
    dialect: str | None = None,
    schema: SchemaModel | None = None,
    task: str | None = None,  # noqa: ARG001
) -> OptimizeResult | FailureEnvelope:
    """Run the optimize_query pipeline.

    Args:
        sql: The SQL string to optimize.
        config: The loaded EZSQL config (provides limits).
        cache: Optional cache store.
        dialect: Optional explicit dialect.
        schema: Optional pre-loaded schema model.
        task: Optional task ID (no-op in Phase 2).

    Returns:
        ``OptimizeResult`` on success, or ``FailureEnvelope`` on failure.
    """
    # Note: tool invocation counters are owned by the server wrappers
    # (plan_phase3 §11); the pipeline owns domain events only.

    # Input size check (plan §11.1)
    if len(sql.encode("utf-8")) > config.max_sql_input_bytes:
        return FailureEnvelope(
            kind="input_too_large",
            detail=f"SQL input exceeds max_sql_input_bytes ({config.max_sql_input_bytes})",
            recoverable=True,
            next_steps=["Reduce the SQL input size."],
        )

    resolved_dialect = dialect or config.default_dialect
    schema_hash = _schema_hash(schema)

    # Cache check
    key = optimize_key(sql, resolved_dialect, schema_hash)
    if cache is not None:
        cached = cache.get(key, OptimizeResult)
        if cached is not None:
            counters.inc("cache_hits", 1)
            logger.info("optimize_query_cache_hit")
            cached.cache_provenance = CacheProvenance(cache_hit=True, cache_key=key)
            return cached

    counters.inc("cache_misses", 1)

    # Parse
    parse_result = parse(sql, dialect=dialect, configured_dialect=config.default_dialect,
                         max_statements=config.max_statements)
    if isinstance(parse_result, InternalFailure):
        return FailureEnvelope(
            kind="internal_error",
            detail=parse_result.detail,
            recoverable=False,
            next_steps=["Report this as an internal error."],
        )

    # Check for parse errors
    if parse_result.errors and not parse_result.statements:
        first_error = parse_result.errors[0]
        return FailureEnvelope(
            kind="parse_error",
            detail=first_error.message,
            recoverable=True,
            next_steps=["Fix the SQL syntax error and try again."],
        )

    # Run lint heuristics
    findings = lint(parse_result, schema=schema, dialect=resolved_dialect)

    # Run rewrites
    candidates = []
    for i, stmt in enumerate(parse_result.statements):
        candidates.extend(rewrite(stmt, schema, resolved_dialect, i))

    # Truncate findings if needed
    truncated = False
    suppressed = 0
    if len(findings) > config.max_findings:
        suppressed = len(findings) - config.max_findings
        findings = findings[:config.max_findings]
        truncated = True

    # Truncate candidates if needed
    candidates_truncated = False
    candidates_suppressed = 0
    if len(candidates) > config.max_candidates:
        candidates_suppressed = len(candidates) - config.max_candidates
        candidates = candidates[:config.max_candidates]
        candidates_truncated = True

    result = OptimizeResult(
        dialect=resolved_dialect,
        findings=findings,
        candidates=candidates,
        schema_source=schema.source if schema is not None else "none",
        truncated=truncated,
        suppressed_count=suppressed,
        candidates_truncated=candidates_truncated,
        candidates_suppressed=candidates_suppressed,
        cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
    )

    # Cache store
    if cache is not None:
        cache.put(key, "optimize", result)

    logger.info(
        "optimize_query_complete: dialect=%s findings=%d candidates=%d",
        resolved_dialect,
        len(result.findings),
        len(result.candidates),
    )

    return result


__all__ = ["run_optimize_query"]
