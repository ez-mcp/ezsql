"""SQL security analysis pipeline (plan §5.1, §15).

Flow: cache check → build analysis units → evaluate rules → coverage →
cache store → SecurityScanResult.

Supports two input modes:
- ``sql=`` mode: single SQL string, input_role resolved to ``query``
- ``files=`` mode: list of file paths, each with its own resolved input_role

``[]`` findings with ``coverage`` showing ``evaluated`` rules means "checks
ran and found nothing," not "secure."
"""

import logging
import re
from pathlib import Path

from ezsql.cache.keys import security_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.security.engine import evaluate
from ezsql.core.security.model import AnalysisUnit, InputRole
from ezsql.core.security.rules import SECURITY_RULESET_VERSION, get_rules
from ezsql.observability import counters
from ezsql.server.models import (
    CacheProvenance,
    FailureEnvelope,
    SecurityScanResult,
)

logger = logging.getLogger("ezsql.pipelines.security")

# Migration naming patterns for role resolution
_MIGRATION_PATTERNS = [
    re.compile(r"^\d+_.*\.sql$", re.IGNORECASE),
    re.compile(r"^V\d+__.*\.sql$", re.IGNORECASE),
    re.compile(r".*\.migration\.sql$", re.IGNORECASE),
]


def _resolve_file_role(filename: str) -> InputRole:
    """Resolve input_role for a file based on its name (plan §15.1)."""
    if filename.endswith(".sql"):
        for pattern in _MIGRATION_PATTERNS:
            if pattern.match(filename):
                return "migration"
        return "script"
    if filename.endswith(".py"):
        return "script"
    return "script"


def _validate_file_path(file_str: str, root: Path, max_file_size: int) -> Path | FailureEnvelope:
    """Validate a file path (plan §21.2 — path traversal defense)."""
    try:
        path = Path(file_str).resolve()
    except (OSError, ValueError) as exc:
        return FailureEnvelope(
            kind="invalid_path",
            detail=str(exc),
            recoverable=True,
            next_steps=["Provide a valid file path."],
        )

    if not path.is_absolute():
        return FailureEnvelope(
            kind="invalid_path",
            detail=f"Path is not absolute: {file_str}",
            recoverable=True,
            next_steps=["Provide an absolute file path."],
        )

    try:
        path.relative_to(root)
    except ValueError:
        return FailureEnvelope(
            kind="path_outside_root",
            detail=f"Path is outside project root: {file_str}",
            recoverable=True,
            next_steps=["Provide a file path within the project root."],
        )

    if not path.is_file():
        return FailureEnvelope(
            kind="not_a_file",
            detail=f"Not a file: {path}",
            recoverable=True,
            next_steps=["Provide a file path, not a directory."],
        )

    try:
        if path.stat().st_size > max_file_size:
            return FailureEnvelope(
                kind="file_too_large",
                detail=f"File exceeds max_file_size ({max_file_size}): {path}",
                recoverable=True,
                next_steps=["Reduce the file size or increase max_file_size."],
            )
    except OSError:
        return FailureEnvelope(
            kind="file_unreadable",
            detail=f"Cannot read file: {path}",
            recoverable=True,
            next_steps=["Check file permissions."],
        )

    return path


def _build_units_from_files(
    files: list[str],
    root: Path,
    config: EzsqlConfig,
) -> tuple[list[AnalysisUnit], FailureEnvelope | None]:
    """Build analysis units from file paths."""
    units: list[AnalysisUnit] = []
    total_bytes = 0

    if len(files) > config.max_sec_files:
        return [], FailureEnvelope(
            kind="too_many_files",
            detail=f"Number of files ({len(files)}) exceeds max_sec_files ({config.max_sec_files})",
            recoverable=True,
            next_steps=["Reduce the number of files or increase max_sec_files."],
        )

    for file_str in files:
        result = _validate_file_path(file_str, root, config.max_file_size)
        if isinstance(result, FailureEnvelope):
            return [], result

        path = result
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [], FailureEnvelope(
                kind="file_unreadable",
                detail=f"Cannot read file {path}: {exc}",
                recoverable=True,
                next_steps=["Check file encoding and permissions."],
            )

        total_bytes += len(content.encode("utf-8"))
        if total_bytes > config.max_total_file_bytes:
            return [], FailureEnvelope(
                kind="total_file_bytes_exceeded",
                detail=(
                    "Total file bytes "
                    f"({total_bytes}) exceeds max_total_file_bytes "
                    f"({config.max_total_file_bytes})"
                ),
                recoverable=True,
                next_steps=["Reduce the total file size or increase max_total_file_bytes."],
            )

        # Determine input kind
        input_kind = "python_source" if file_str.endswith(".py") else "sql"

        # Resolve role
        filename = path.name
        role = _resolve_file_role(filename)

        # Relative path for unit_id
        try:
            rel_path = str(path.relative_to(root))
        except ValueError:
            rel_path = file_str

        units.append(AnalysisUnit(
            unit_id=rel_path,
            file=rel_path,
            content=content,
            input_kind=input_kind,  # type: ignore[arg-type]
            input_role=role,
        ))

    return units, None


def run_sql_sec(
    config: EzsqlConfig,
    root: Path,
    cache: CacheStore | None = None,
    *,
    sql: str | None = None,
    files: list[str] | None = None,
    dialect: str | None = None,
    task: str | None = None,  # noqa: ARG001
) -> SecurityScanResult | FailureEnvelope:
    """Run the sql_sec pipeline.

    Args:
        config: The loaded EZSQL config.
        root: The resolved project root (for file path validation).
        cache: Optional cache store.
        sql: SQL string for sql= mode.
        files: File paths for files= mode.
        dialect: Optional explicit dialect.
        task: Optional task ID (no-op in Phase 2).

    Returns:
        ``SecurityScanResult`` on success, or ``FailureEnvelope`` on failure.
    """
    # Note: tool invocation counters are owned by the server wrappers
    # (plan_phase3 §11); the pipeline owns domain events only.

    resolved_dialect = dialect or config.default_dialect

    # Determine input mode
    if sql is not None:
        # sql= mode
        if len(sql.encode("utf-8")) > config.max_sql_input_bytes:
            return FailureEnvelope(
                kind="input_too_large",
                detail=f"SQL input exceeds max_sql_input_bytes ({config.max_sql_input_bytes})",
                recoverable=True,
                next_steps=["Reduce the SQL input size."],
            )

        units = [AnalysisUnit(
            unit_id="sql:0",
            content=sql,
            input_kind="sql",
            input_role="query",
        )]
        input_mode = "sql"
        resolved_role = "query"
        key = security_key(sql, None, resolved_dialect, resolved_role)
    elif files is not None:
        # files= mode
        units, error = _build_units_from_files(files, root, config)
        if error is not None:
            return error

        if not units:
            return SecurityScanResult(
                findings=[],
                coverage=[],
                ruleset_version=SECURITY_RULESET_VERSION,
                input_mode="files",
                input_role_resolved="query",
            )

        # Build file identity for cache key
        file_identity = [
            (u.file or u.unit_id, u.content, u.input_role, u.input_kind)
            for u in units
        ]
        roles = {u.input_role for u in units}
        resolved_role = "mixed" if len(roles) > 1 else roles.pop()
        input_mode = "files"
        key = security_key(None, file_identity, resolved_dialect, resolved_role)
    else:
        return FailureEnvelope(
            kind="no_input",
            detail="No input provided. Pass 'sql' or 'files'.",
            recoverable=True,
            next_steps=["Provide either 'sql' or 'files' parameter."],
        )

    # Cache check
    if cache is not None:
        cached = cache.get(key, SecurityScanResult)
        if cached is not None:
            counters.inc("cache_hits", 1)
            logger.info("sql_sec_cache_hit")
            cached.cache_provenance = CacheProvenance(cache_hit=True, cache_key=key)
            return cached

    counters.inc("cache_misses", 1)

    # Evaluate rules
    rules = get_rules()
    engine_result = evaluate(rules, units, resolved_dialect)

    # Truncate findings if needed
    findings = engine_result.findings
    truncated = False
    suppressed = 0
    if len(findings) > config.max_findings:
        suppressed = len(findings) - config.max_findings
        findings = findings[:config.max_findings]
        truncated = True

    result = SecurityScanResult(
        findings=findings,
        coverage=engine_result.coverage,
        ruleset_version=SECURITY_RULESET_VERSION,
        input_mode=input_mode,  # type: ignore[arg-type]
        input_role_resolved=resolved_role,  # type: ignore[arg-type]
        truncated=truncated,
        suppressed_count=suppressed,
        cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
    )

    # Cache store
    if cache is not None:
        cache.put(key, "security", result)

    logger.info(
        "sql_sec_complete: findings=%d coverage=%d input_mode=%s",
        len(result.findings),
        len(result.coverage),
        input_mode,
    )

    return result


__all__ = ["run_sql_sec"]
