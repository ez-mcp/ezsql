"""SQL refactoring and multi-service composition pipeline (plan §21 #7).

Composes security + optimization + schema impact **internally** — as
plain Python function calls into the existing pipelines, never as a
chain of MCP tool calls (plan §5.1). Deterministic only: this module
never imports the escalation layer (test-enforced invariant).

Flow: input validation → cache check → run_sql_sec + run_optimize_query
→ schema impact (target refs vs repo SchemaModel) → proposed changes →
cache store → RefactorResult.
"""

import logging
from pathlib import Path

from sqlglot import exp

from ezsql.cache.keys import refactor_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.schema.repository import load_repo_schema
from ezsql.core.sql.parse import InternalFailure, parse
from ezsql.observability import counters
from ezsql.pipelines.optimize import run_optimize_query
from ezsql.pipelines.security import run_sql_sec
from ezsql.server.models import (
    CacheProvenance,
    FailureEnvelope,
    RefactorResult,
    SchemaImpact,
)

logger = logging.getLogger("ezsql.pipelines.refactor")

__all__ = ["run_refactor_sql"]


def _schema_fingerprint(schema_present: bool, fingerprint: str) -> str | None:
    """Fingerprint for cache keying; ``None`` when no schema was loaded."""
    return fingerprint if schema_present and fingerprint else None


def _target_content(
    sql: str | None,
    files: list[str] | None,
    root: Path,
    config: EzsqlConfig,
) -> tuple[str | None, FailureEnvelope | None]:
    """Resolve the refactor target content (sql text or concatenated files)."""
    if sql is not None:
        if len(sql.encode("utf-8")) > config.max_sql_input_bytes:
            return None, FailureEnvelope(
                kind="input_too_large",
                detail=f"SQL input exceeds max_sql_input_bytes ({config.max_sql_input_bytes})",
                recoverable=True,
                next_steps=["Reduce the SQL input size."],
            )
        return sql, None
    if files is not None:
        if len(files) > config.max_sec_files:
            return None, FailureEnvelope(
                kind="too_many_files",
                detail=f"Too many files ({len(files)} > max_sec_files={config.max_sec_files})",
                recoverable=True,
                next_steps=["Reduce the number of files."],
            )
        parts: list[str] = []
        total = 0
        for rel in files:
            path = (root / rel).resolve()
            if not path.is_relative_to(root):
                return None, FailureEnvelope(
                    kind="path_outside_root",
                    detail=f"File path escapes the project root: {rel}",
                    recoverable=True,
                    next_steps=["Use paths relative to the project root."],
                )
            if not path.is_file():
                return None, FailureEnvelope(
                    kind="not_a_file",
                    detail=f"Not a file: {rel}",
                    recoverable=True,
                    next_steps=["Check the file path."],
                )
            size = path.stat().st_size
            if size > config.max_file_size:
                return None, FailureEnvelope(
                    kind="file_too_large",
                    detail=f"File exceeds max_file_size: {rel}",
                    recoverable=True,
                    next_steps=["Reduce the file size or exclude it."],
                )
            total += size
            if total > config.max_total_file_bytes:
                return None, FailureEnvelope(
                    kind="total_file_bytes_exceeded",
                    detail="Total file bytes exceed max_total_file_bytes",
                    recoverable=True,
                    next_steps=["Reduce the total input size."],
                )
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                return None, FailureEnvelope(
                    kind="file_unreadable",
                    detail=f"File is unreadable: {rel}",
                    recoverable=True,
                    next_steps=["Check file permissions and encoding."],
                )
        return "\n\n".join(parts), None
    return None, FailureEnvelope(
        kind="no_input",
        detail="No target provided: pass sql= or files=.",
        recoverable=True,
        next_steps=["Provide the SQL text or file paths to refactor."],
    )


def _schema_impact(
    target_content: str,
    dialect: str,
    config: EzsqlConfig,
    root: Path,
    cache: CacheStore | None,
) -> SchemaImpact:
    """Diff the target's table/column references against the repo schema."""
    load = load_repo_schema(root, config, cache)
    if load.schema is None:
        return SchemaImpact(schema_source="none")

    parse_result = parse(
        target_content,
        dialect=dialect,
        configured_dialect=config.default_dialect,
        max_statements=config.max_statements,
    )
    if isinstance(parse_result, InternalFailure):
        # Schema impact is best-effort: an internal parse failure yields
        # an empty impact rather than failing the whole refactor.
        return SchemaImpact(schema_source="repo-ddl")
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for stmt in parse_result.statements:
        for tbl in stmt.find_all(exp.Table):
            name = tbl.name
            if name not in load.schema.tables:
                if name not in missing_tables:
                    missing_tables.append(name)
                continue
            table_def = load.schema.tables[name]
            for col in stmt.find_all(exp.Column):
                if col.table and col.table != name:
                    continue
                if col.name not in table_def.columns and col.name not in missing_columns:
                    missing_columns.append(f"{name}.{col.name}")

    return SchemaImpact(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        schema_source="repo-ddl",
    )


def run_refactor_sql(
    config: EzsqlConfig,
    root: Path,
    cache: CacheStore | None = None,
    *,
    sql: str | None = None,
    files: list[str] | None = None,
    dialect: str | None = None,
    task: str | None = None,
) -> RefactorResult | FailureEnvelope:
    """Run the refactor_sql pipeline (internal composition, plan §5.1).

    Args:
        config: The loaded EZSQL config.
        root: The resolved project root.
        cache: Optional cache store.
        sql: SQL text target.
        files: File-path targets (relative to root).
        dialect: Optional explicit dialect.
        task: Optional task ID (registry wiring, plan_phase4 FR-8).

    Returns:
        ``RefactorResult`` on success, or ``FailureEnvelope`` on failure.
    """
    # Note: tool invocation counters are owned by the server wrappers
    # (plan_phase3 §11); the pipeline owns domain events only.
    counters.inc("refactor_requests", 1)

    resolved_dialect = dialect or config.default_dialect

    content, failure = _target_content(sql, files, root, config)
    if failure is not None or content is None:
        return failure if failure is not None else FailureEnvelope(
            kind="no_input", detail="No target provided.", recoverable=True,
            next_steps=["Pass sql= or files=."],
        )

    # Schema load (for fingerprint + impact) before cache keying.
    schema_load = load_repo_schema(root, config, cache)
    fingerprint = _schema_fingerprint(schema_load.schema is not None, schema_load.fingerprint)

    key = refactor_key(content, resolved_dialect, fingerprint)
    if cache is not None:
        cached = cache.get(key, RefactorResult)
        if cached is not None:
            counters.inc("cache_hits", 1)
            logger.info("refactor_cache_hit")
            cached.cache_provenance = CacheProvenance(cache_hit=True, cache_key=key)
            _record_task_ref(task, key)
            return cached

    counters.inc("cache_misses", 1)

    # Internal composition (plan §5.1): plain function calls, never
    # MCP-call chaining.
    security_result = run_sql_sec(
        config, root, cache=None, sql=content, dialect=resolved_dialect,
    )
    if isinstance(security_result, FailureEnvelope):
        return security_result

    optimize_result = run_optimize_query(
        content, config, cache=None, dialect=resolved_dialect,
        schema=schema_load.schema,
    )
    if isinstance(optimize_result, FailureEnvelope):
        return optimize_result

    impact = _schema_impact(content, resolved_dialect, config, root, cache)

    # Proposed changes: security fixes first, then optimization candidates.
    proposed: list[str] = []
    for finding in security_result.findings:
        if finding.fix_suggestion:
            proposed.append(
                f"[{finding.rule_id}] {finding.fix_suggestion}"
            )
    for candidate in optimize_result.candidates:
        proposed.append(f"Apply rewrite: {candidate.rewritten_sql}")

    truncated = False
    suppressed = 0
    if len(proposed) > config.max_findings:
        suppressed = len(proposed) - config.max_findings
        proposed = proposed[: config.max_findings]
        truncated = True

    result = RefactorResult(
        dialect=resolved_dialect,
        security_findings=security_result.findings,
        optimize_findings=optimize_result.findings,
        candidates=optimize_result.candidates,
        schema_impact=impact,
        proposed_changes=proposed,
        truncated=truncated,
        suppressed_count=suppressed,
        candidates_truncated=optimize_result.candidates_truncated,
        candidates_suppressed=optimize_result.candidates_suppressed,
        cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
    )

    if cache is not None:
        cache.put(key, "refactor", result)
    logger.info(
        "refactor_complete",
        extra={
            "security_findings": len(security_result.findings),
            "optimize_findings": len(optimize_result.findings),
            "candidates": len(optimize_result.candidates),
        },
    )
    _record_task_ref(task, key)
    return result


def _record_task_ref(task: str | None, cache_key: str) -> None:
    """Attach the result to the task registry (plan_phase4 FR-8)."""
    if task is None:
        return
    from ezsql.tasks.registry import get_registry

    registry = get_registry()
    registry.get_or_create(task)
    registry.add_ref(task, cache_key, "refactor")
