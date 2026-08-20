"""Bounded repository schema loader (plan_phase3 §6).

Discovers migration candidates under one documented convention/root,
builds a ``SchemaModel`` from their sorted root-relative ``.sql`` paths
via the existing pure ``parse_migrations()``, and returns a conservative
fingerprint. Used ONLY by DB-backed enrichment — the no-DB Phase 2 path
never calls this.

Safety properties:
- pruned ``os.walk`` with existing skip directories;
- never follows symlinks; resolved files must remain under root;
- file-count, per-file, and total-byte limits enforced;
- requires one unambiguous migration convention/root;
- returns an explicit unavailable reason rather than a partial model.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ezsql.cache.keys import schema_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.context.scan import DEFAULT_SKIP_DIRS
from ezsql.core.schema.ddl import parse_migrations
from ezsql.core.schema.model import SchemaModel
from ezsql.server.models import FailureEnvelope

logger = logging.getLogger("ezsql.schema.repository")

# Documented migration roots, in priority order. Exactly one must contain
# migration candidates — multiple populated roots are ambiguous.
_MIGRATION_DIRS: tuple[str, ...] = ("migrations", "db/migrations", "sql/migrations")

# Migration filename patterns (same conventions as ddl.py ordering).
_MIGRATION_SUFFIX = ".sql"


@dataclass(frozen=True)
class SchemaLoadResult:
    """Outcome of a repository schema load.

    Exactly one of ``schema``/``unavailable_reason`` is set. ``fingerprint``
    is the conservative manifest fingerprint (empty string when unavailable).
    """

    schema: SchemaModel | None = None
    fingerprint: str = ""
    unavailable_reason: str | None = None
    manifest: tuple[tuple[str, str], ...] = ()
    cache_hit: bool = False


def _discover_migration_files(root: Path, config: EzsqlConfig) -> tuple[list[Path], str | None]:
    """Find migration files under exactly one migration root.

    Returns ``(files, failure_reason)``. Symlinks are never followed and
    resolved paths must stay under root.
    """
    populated_roots: list[Path] = []
    for rel in _MIGRATION_DIRS:
        candidate = root / rel
        if candidate.is_dir() and not candidate.is_symlink():
            populated_roots.append(candidate)

    if not populated_roots:
        return [], "no migration directory found (expected one of: " + ", ".join(
            _MIGRATION_DIRS
        ) + ")"

    if len(populated_roots) > 1:
        return [], "ambiguous migration roots: " + ", ".join(
            str(p.relative_to(root)) for p in populated_roots
        )

    migration_root = populated_roots[0]
    root_resolved = root.resolve()

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
        migration_root, followlinks=False
    ):
        # Prune skip dirs before descent (same policy as context scan).
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIRS]
        for name in sorted(filenames):
            if not name.endswith(_MIGRATION_SUFFIX):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            # Resolved file must remain under root (traversal guard).
            try:
                resolved = path.resolve()
                resolved.relative_to(root_resolved)
            except (ValueError, OSError):
                continue
            files.append(path)
            if len(files) > config.max_schema_files:
                return [], (
                    f"migration file count exceeds max_schema_files "
                    f"({config.max_schema_files})"
                )

    return files, None


def load_repo_schema(
    root: Path,
    config: EzsqlConfig,
    cache: CacheStore | None = None,
) -> SchemaLoadResult:
    """Load the repository schema model with bounds and caching.

    Returns ``SchemaLoadResult``; never raises. When loading cannot safely
    identify an ordered schema, the result carries an explicit
    ``unavailable_reason`` and no schema.
    """
    files, reason = _discover_migration_files(root, config)
    if reason is not None:
        return SchemaLoadResult(unavailable_reason=reason)
    if not files:
        return SchemaLoadResult(unavailable_reason="no migration files found")

    # Read files with per-file and total-byte limits.
    manifest: list[tuple[str, str]] = []
    total_bytes = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            return SchemaLoadResult(unavailable_reason=f"cannot stat {path.name}")
        if size > config.max_schema_file_bytes:
            return SchemaLoadResult(unavailable_reason=(
                f"{path.name} exceeds max_schema_file_bytes"
            ))
        total_bytes += size
        if total_bytes > config.max_schema_total_bytes:
            return SchemaLoadResult(unavailable_reason=(
                "migration set exceeds max_schema_total_bytes"
            ))
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return SchemaLoadResult(unavailable_reason=f"cannot read {path.name}")
        rel = str(path.relative_to(root))
        manifest.append((rel, content))

    # Cache lookup keyed on the ordered manifest.
    key = schema_key(manifest)
    if cache is not None:
        cached = cache.get(key, SchemaModel)
        if cached is not None:
            return SchemaLoadResult(
                schema=cached, fingerprint=key, manifest=tuple(manifest),
                cache_hit=True,
            )

    # Build the model from ordered migrations.
    result = parse_migrations(
        list(manifest), max_parser_warnings=config.max_parser_warnings
    )
    if isinstance(result, FailureEnvelope):
        return SchemaLoadResult(unavailable_reason=result.kind)

    if cache is not None:
        cache.put(key, "schema", result)

    return SchemaLoadResult(
        schema=result, fingerprint=key, manifest=tuple(manifest),
    )


def fingerprint_manifest(manifest: tuple[tuple[str, str], ...]) -> str:
    """Conservative fingerprint of an ordered migration manifest.

    Hashes path + content of every file in order. Any change to any
    migration invalidates — conservative by design (plan_phase3 §17.6).
    """
    parts = "|".join(
        f"{path}:{hashlib.blake2b(content.encode(), digest_size=16).hexdigest()}"
        for path, content in manifest
    )
    return hashlib.blake2b(parts.encode(), digest_size=16).hexdigest()


__all__ = ["SchemaLoadResult", "load_repo_schema", "fingerprint_manifest"]
