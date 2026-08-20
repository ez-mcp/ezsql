"""Analyze SQL pipeline (plan §5.1, §9.6).

Flow: cache check → parse → AST extraction → lint → schema cross-check →
cache store → SqlAnalysis.

The pipeline is impure (orchestration + I/O + cache). Core services are
pure. The pipeline owns cache orchestration (plan §7.2).
"""

import hashlib
import logging

from sqlglot import exp

from ezsql.cache.keys import analysis_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.schema.model import SchemaModel
from ezsql.core.sql.lint import lint
from ezsql.core.sql.parse import InternalFailure, ParseResult, parse
from ezsql.observability import counters
from ezsql.server.models import (
    CacheProvenance,
    FailureEnvelope,
    SqlAnalysis,
)

logger = logging.getLogger("ezsql.pipelines.analyze")


def _schema_hash(schema: SchemaModel | None) -> str | None:
    """Compute a stable content hash of a schema model for cache keying.

    ``None`` (no schema) stays ``None`` so the cache key shape is
    unchanged for schema-less calls. The hash covers the model's
    canonical JSON dump, so any table/column/constraint change yields a
    new key (stale-by-construction is impossible, plan §14).
    """
    if schema is None:
        return None
    return hashlib.blake2b(
        schema.model_dump_json().encode("utf-8"), digest_size=16
    ).hexdigest()


def _extract_ast_facts(
    parse_result: ParseResult,
    max_tables: int,
    max_columns: int,
    max_joins: int,
    max_predicates: int,
    max_statements: int,
) -> SqlAnalysis:
    """Extract AST facts (tables, columns, joins, predicates) from parsed SQL."""
    tables: list[str] = []
    columns: list[str] = []
    joins: list[str] = []
    predicates: list[str] = []
    statements: list[str] = []
    tables_truncated = False
    columns_truncated = False
    joins_truncated = False
    predicates_truncated = False
    statements_truncated = False

    for stmt in parse_result.statements:
        # Render statement
        if len(statements) < max_statements:
            statements.append(stmt.sql())
        else:
            statements_truncated = True

        # Tables
        for tbl in stmt.find_all(exp.Table):
            if len(tables) < max_tables:
                tables.append(tbl.name)
            else:
                tables_truncated = True

        # Columns (by name, not qualified)
        for col in stmt.find_all(exp.Column):
            if not isinstance(col.this, exp.Star):
                if len(columns) < max_columns:
                    columns.append(col.name)
                else:
                    columns_truncated = True

        # Joins
        for join in stmt.find_all(exp.Join):
            if len(joins) < max_joins:
                joins.append(join.sql())
            else:
                joins_truncated = True

        # Predicates (WHERE and HAVING)
        for clause_key in ("where", "having"):
            clause = stmt.args.get(clause_key) if hasattr(stmt, "args") else None
            if clause is not None:
                if len(predicates) < max_predicates:
                    predicates.append(clause.sql())
                else:
                    predicates_truncated = True

    return SqlAnalysis(
        dialect=parse_result.dialect,
        statements=statements,
        tables=tables,
        columns=columns,
        joins=joins,
        predicates=predicates,
        statements_truncated=statements_truncated,
        statements_suppressed=max(0, len(parse_result.statements) - max_statements),
        tables_truncated=tables_truncated,
        columns_truncated=columns_truncated,
        joins_truncated=joins_truncated,
        predicates_truncated=predicates_truncated,
    )


def run_analyze_sql(
    sql: str,
    config: EzsqlConfig,
    cache: CacheStore | None = None,
    *,
    dialect: str | None = None,
    schema: SchemaModel | None = None,
    task: str | None = None,  # noqa: ARG001 — task context in Phase 4
) -> SqlAnalysis | FailureEnvelope:
    """Run the analyze_sql pipeline.

    Args:
        sql: The SQL string to analyze.
        config: The loaded EZSQL config (provides limits).
        cache: Optional cache store.
        dialect: Optional explicit dialect.
        schema: Optional pre-loaded schema model.
        task: Optional task ID (no-op in Phase 2).

    Returns:
        ``SqlAnalysis`` on success, or ``FailureEnvelope`` on failure.
    """
    # Note: tool invocation counters are owned by the server wrappers
    # (plan_phase3 §11); the pipeline owns domain events only.

    # Input size check (plan §11.1)
    if len(sql.encode("utf-8")) > config.max_sql_input_bytes:
        return FailureEnvelope(
            kind="input_too_large",
            detail=f"SQL input exceeds max_sql_input_bytes ({config.max_sql_input_bytes})",
            recoverable=True,
            next_steps=["Reduce the SQL input size.", "Increase max_sql_input_bytes in config."],
        )

    resolved_dialect = dialect or config.default_dialect
    schema_hash = _schema_hash(schema)

    # Cache check
    key = analysis_key(sql, resolved_dialect, schema_hash)
    if cache is not None:
        cached = cache.get(key, SqlAnalysis)
        if cached is not None:
            counters.inc("cache_hits", 1)
            logger.info("analyze_sql_cache_hit")
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
        # All statements failed to parse
        first_error = parse_result.errors[0]
        return FailureEnvelope(
            kind="parse_error",
            detail=first_error.message,
            recoverable=True,
            next_steps=["Fix the SQL syntax error and try again."],
        )

    # Extract AST facts
    analysis = _extract_ast_facts(
        parse_result,
        max_tables=config.max_analysis_tables,
        max_columns=config.max_analysis_columns,
        max_joins=config.max_analysis_joins,
        max_predicates=config.max_analysis_predicates,
        max_statements=config.max_statements,
    )

    # Run lint heuristics
    findings = lint(parse_result, schema=schema, dialect=resolved_dialect)

    # Truncate findings if needed
    if len(findings) > config.max_findings:
        analysis.lint_findings = findings[:config.max_findings]
        # Note: SqlAnalysis doesn't have truncated/suppressed for lint_findings
        # in the current model — those are on SecurityScanResult/OptimizeResult
    else:
        analysis.lint_findings = findings

    # Set schema source
    if schema is not None:
        analysis.schema_source = schema.source

    analysis.cache_provenance = CacheProvenance(cache_hit=False, cache_key=key)

    # Cache store
    if cache is not None:
        cache.put(key, "analysis", analysis)

    logger.info(
        "analyze_sql_complete: dialect=%s statements=%d findings=%d",
        resolved_dialect,
        len(analysis.statements),
        len(analysis.lint_findings),
    )

    return analysis


__all__ = ["run_analyze_sql"]
