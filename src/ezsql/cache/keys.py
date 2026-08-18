"""Deterministic content-addressed cache key generators.

All cache keys are built here — this is the single place hashing rules live
(plan §22). Keys are ``blake2b`` of ``domain ∥ inputs ∥ dep_versions``
(plan §14). A changed input *is* a different key — stale-by-construction
is impossible for pure analyses.

Per-domain ruleset versions (plan §19.1): changing an optimization rule
doesn't invalidate security caches. Each domain has its own version tag.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ezsql.core.schema.ddl import SCHEMA_MODEL_VERSION
from ezsql.core.security.rules import SECURITY_RULESET_VERSION

# Version tags embedded in keys so upgrades invalidate cleanly (plan §14, §22).
_LINT_RULESET_VERSION = "1"  # OPT-001 through OPT-004
_OPTIMIZATION_RULESET_VERSION = "1"  # Optimization heuristics
_REWRITE_RULESET_VERSION = "1"  # Rewrite rules (SELECT * expansion)


def _get_sqlglot_version() -> str:
    """Get the installed sqlglot version for key embedding."""
    try:
        from importlib.metadata import version
        return version("sqlglot")
    except Exception:  # noqa: BLE001 — version lookup is best-effort
        return "unknown"


def _hash(parts: dict[str, Any]) -> str:
    """Build a blake2b hash from a dict of key→value parts.

    Keys are sorted for determinism. Values are stringified.
    """
    serialized = "|".join(
        f"{k}={v}" for k, v in sorted(parts.items())
    )
    return hashlib.blake2b(
        serialized.encode("utf-8"),
        digest_size=32,  # 256-bit
    ).hexdigest()


def _hash_sql(sql: str) -> str:
    """Hash a SQL string for cache keying."""
    return hashlib.blake2b(
        sql.encode("utf-8"), digest_size=16
    ).hexdigest()


def _hash_schema(schema_hash: str | None) -> str:
    """Get the schema hash component (empty string if no schema)."""
    return schema_hash or ""


def scan_key(root: Path) -> str:
    """Build the cache key for a scan result.

    The key is content-addressed on the resolved root path, the
    classification ruleset version, and the sqlglot version. A changed
    input *is* a different key.

    Note: file-level freshness (mtime+size) is NOT in the key — it is
    checked separately by the pipeline via ``files_manifest`` on cache
    retrieval (plan §14 — "mtime+size fast guard"). This keeps the key
    cheap to compute (no filesystem walk) while still preventing stale
    results: a manifest mismatch downgrades a hit to a miss.
    """
    parts: dict[str, Any] = {
        "domain": "scan",
        "root": str(root.resolve()),
        "ruleset_version": _LINT_RULESET_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def analysis_key(
    sql: str,
    dialect: str,
    schema_hash: str | None,
) -> str:
    """Build the cache key for an analyze_sql result (plan §19.2).

    Inputs: sql_hash, dialect, schema_hash, lint_ruleset_version,
    schema_model_version, sqlglot_version.
    """
    parts: dict[str, Any] = {
        "domain": "analysis",
        "sql_hash": _hash_sql(sql),
        "dialect": dialect,
        "schema_hash": _hash_schema(schema_hash),
        "lint_ruleset_version": _LINT_RULESET_VERSION,
        "schema_model_version": SCHEMA_MODEL_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def security_key(
    sql: str | None,
    files: Sequence[tuple[str, str, str, str]] | None,
    dialect: str,
    resolved_input_role: str,
) -> str:
    """Build the cache key for a sql_sec result (plan §19.2).

    For ``sql`` mode: ``(sql_content_hash, "sql", resolved_input_role)``.
    For ``files`` mode: ordered ``[(relative_path, content_hash, resolved_role, input_kind), ...]``.
    Plus dialect, security_ruleset_version, sqlglot_version.
    """
    parts: dict[str, Any] = {
        "domain": "security",
        "dialect": dialect,
        "security_ruleset_version": SECURITY_RULESET_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }

    if sql is not None:
        parts["input_mode"] = "sql"
        parts["sql_hash"] = _hash_sql(sql)
        parts["resolved_role"] = resolved_input_role
    elif files is not None:
        parts["input_mode"] = "files"
        parts["files"] = "|".join(
            f"{path}:{_hash_sql(content)}:{role}:{kind}"
            for path, content, role, kind in files
        )
    else:
        parts["input_mode"] = "empty"

    return _hash(parts)


def optimize_key(
    sql: str,
    dialect: str,
    schema_hash: str | None,
) -> str:
    """Build the cache key for an optimize_query result (plan §19.2).

    Inputs: sql_hash, dialect, schema_hash, optimization_ruleset_version,
    lint_ruleset_version, rewrite_ruleset_version, schema_model_version,
    sqlglot_version.
    """
    parts: dict[str, Any] = {
        "domain": "optimize",
        "sql_hash": _hash_sql(sql),
        "dialect": dialect,
        "schema_hash": _hash_schema(schema_hash),
        "optimization_ruleset_version": _OPTIMIZATION_RULESET_VERSION,
        "lint_ruleset_version": _LINT_RULESET_VERSION,
        "rewrite_ruleset_version": _REWRITE_RULESET_VERSION,
        "schema_model_version": SCHEMA_MODEL_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def schema_key(
    migration_manifest: list[tuple[str, str]],
) -> str:
    """Build the cache key for a schema model (plan §19.2).

    Inputs: ordered ``[(path, content_hash), ...]``, schema_model_version,
    sqlglot_version.
    """
    parts: dict[str, Any] = {
        "domain": "schema",
        "manifest": "|".join(
            f"{path}:{_hash_sql(content)}"
            for path, content in migration_manifest
        ),
        "schema_model_version": SCHEMA_MODEL_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


__all__ = [
    "analysis_key",
    "optimize_key",
    "scan_key",
    "schema_key",
    "security_key",
]
