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
from ezsql.core.sql.plan import EXPLAIN_OPTIONS_FINGERPRINT, PLAN_MODEL_VERSION

# Version tags embedded in keys so upgrades invalidate cleanly (plan §14, §22).
_LINT_RULESET_VERSION = "1"  # OPT-001 through OPT-004
_OPTIMIZATION_RULESET_VERSION = "1"  # Optimization heuristics
_REWRITE_RULESET_VERSION = "1"  # Rewrite rules (SELECT * expansion)
_RUNTIME_EVIDENCE_VERSION = "1"  # Phase 3 runtime evidence record shape
_DESIGN_RULESET_VERSION = "1"  # Phase 4 design heuristics
_DEBUG_CATALOG_VERSION = "1"  # Phase 4 error catalog entries
_REFACTOR_COMPOSITION_VERSION = "1"  # Phase 4 refactor composition shape


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


def explain_key(
    canonical_sql: str,
    db_identity_fingerprint: str,
    repo_ddl_fingerprint: str,
    server_major_version: int | None,
) -> str:
    """Build the TTL-bound cache key for a normalized EXPLAIN plan.

    Inputs (plan_phase3 §6): canonical SQL hash, PostgreSQL dialect,
    non-secret DB identity fingerprint, repository-DDL fingerprint,
    EXPLAIN option/version fingerprint, plan-model version, sqlglot
    version, and the effective PostgreSQL server major version.
    """
    parts: dict[str, Any] = {
        "domain": "explain",
        "sql_hash": _hash_sql(canonical_sql),
        "dialect": "postgres",
        "db_identity": db_identity_fingerprint,
        "repo_ddl": repo_ddl_fingerprint,
        "explain_options": EXPLAIN_OPTIONS_FINGERPRINT,
        "plan_model_version": PLAN_MODEL_VERSION,
        "server_major": server_major_version if server_major_version is not None else "",
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def runtime_evidence_key(
    static_key: str,
    db_identity_fingerprint: str,
    repo_ddl_fingerprint: str,
    candidate_identities: Sequence[str],
    config_limits: Sequence[tuple[str, int]],
    server_major_version: int | None,
) -> str:
    """Build the TTL-bound cache key for runtime optimization evidence.

    Includes the static optimize key, DB identity, repository-DDL
    fingerprint, a hash of the **exact ordered eligible candidate
    identities**, every result-shaping limit, the EXPLAIN option
    fingerprint, the effective server major version, and the
    runtime-evidence schema version. Evidence can never be applied to a
    different candidate set (plan_phase3 §6).
    """
    parts: dict[str, Any] = {
        "domain": "runtime_evidence",
        "static_key": static_key,
        "db_identity": db_identity_fingerprint,
        "repo_ddl": repo_ddl_fingerprint,
        "candidates": "|".join(candidate_identities),
        "limits": "|".join(f"{k}={v}" for k, v in config_limits),
        "explain_options": EXPLAIN_OPTIONS_FINGERPRINT,
        "server_major": server_major_version if server_major_version is not None else "",
        "runtime_evidence_version": _RUNTIME_EVIDENCE_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def design_key(
    requirements: str,
    dialect: str,
    schema_fingerprint: str | None,
) -> str:
    """Build the cache key for a design_schema deterministic skeleton.

    Inputs: requirements hash, dialect, repo-schema fingerprint,
    design ruleset version, schema model version, sqlglot version.
    The escalation advisory is never part of the cached value.
    """
    parts: dict[str, Any] = {
        "domain": "design",
        "requirements_hash": _hash_sql(requirements),
        "dialect": dialect,
        "schema_fingerprint": schema_fingerprint or "",
        "design_ruleset_version": _DESIGN_RULESET_VERSION,
        "schema_model_version": SCHEMA_MODEL_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def refactor_key(
    target_content: str,
    dialect: str,
    schema_fingerprint: str | None,
) -> str:
    """Build the cache key for a refactor_sql composed result.

    Inputs: target content hash, dialect, repo-schema fingerprint, and
    the versions of every composed ruleset (security + optimization +
    lint + rewrite + schema model) so any composed-rule change
    invalidates refactor caches too.
    """
    parts: dict[str, Any] = {
        "domain": "refactor",
        "target_hash": _hash_sql(target_content),
        "dialect": dialect,
        "schema_fingerprint": schema_fingerprint or "",
        "security_ruleset_version": SECURITY_RULESET_VERSION,
        "optimization_ruleset_version": _OPTIMIZATION_RULESET_VERSION,
        "lint_ruleset_version": _LINT_RULESET_VERSION,
        "rewrite_ruleset_version": _REWRITE_RULESET_VERSION,
        "schema_model_version": SCHEMA_MODEL_VERSION,
        "refactor_composition_version": _REFACTOR_COMPOSITION_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


def debug_key(
    error_text: str,
    sql: str | None,
    dialect: str,
) -> str:
    """Build the cache key for a debug_sql deterministic skeleton.

    Inputs: error-text hash, optional SQL hash, dialect, debug catalog
    version, schema model version, sqlglot version. The escalation
    advisory is never part of the cached value.
    """
    parts: dict[str, Any] = {
        "domain": "debug",
        "error_hash": _hash_sql(error_text),
        "sql_hash": _hash_sql(sql) if sql is not None else "",
        "dialect": dialect,
        "debug_catalog_version": _DEBUG_CATALOG_VERSION,
        "schema_model_version": SCHEMA_MODEL_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


__all__ = [
    "analysis_key",
    "debug_key",
    "design_key",
    "explain_key",
    "optimize_key",
    "refactor_key",
    "runtime_evidence_key",
    "scan_key",
    "schema_key",
    "security_key",
]
