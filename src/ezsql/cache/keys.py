"""Deterministic content-addressed cache key generators.

All cache keys are built here — this is the single place hashing rules live
(plan §22). Keys are ``blake2b`` of ``domain ∥ inputs ∥ dep_versions``
(plan §14). A changed input *is* a different key — stale-by-construction
is impossible for pure analyses.
"""

import hashlib
from pathlib import Path
from typing import Any

# Version tags embedded in keys so upgrades invalidate cleanly (plan §14, §22).
# sqlglot version affects parse/lint results; ruleset version affects findings.
_RULESET_VERSION = "1"  # Phase 1 scan classification ruleset.


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
        "ruleset_version": _RULESET_VERSION,
        "sqlglot_version": _get_sqlglot_version(),
    }
    return _hash(parts)


__all__ = ["scan_key"]
