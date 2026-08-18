"""Context pipeline for repo scanning and document correlation.

Flow (plan §7): cache check → freshness guard → scan → classify → cache
store → ContextMap. The ``task`` parameter is accepted but ignored (no-op,
§17 Q7) — the task registry is implemented in Phase 2+ when multiple tools
share task context.

Freshness (plan §14 — "mtime+size fast guard"): the cache key is
content-addressed on (root, ruleset, sqlglot) and is cheap to compute.
On a cache hit, the pipeline recomputes a file manifest via
``build_file_manifest`` (``os.walk`` + ``stat``, no content reads for
``.sql``) and compares it to the manifest stored in the cached
``ScanMetadata``. A mismatch downgrades the hit to a miss (re-scan). This
keeps the key cheap while preventing stale results.
"""

from pathlib import Path

from ezsql.cache.keys import scan_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.context.scan import build_file_manifest, scan_with_classification
from ezsql.observability import counters, logger
from ezsql.server.models import CacheProvenance, ContextFile, ContextMap, ScanMetadata


def _manifests_match(
    cached: dict[str, list[int]],
    current: dict[str, tuple[float, int]],
) -> bool:
    """Compare a cached manifest (JSON-roundtripped) to a fresh one.

    ``cached`` values are ``[mtime_ns, size]`` lists (JSON has no tuples).
    ``current`` values are ``(mtime_ns, size)`` tuples from
    ``build_file_manifest``. Same keys + same values → fresh.
    """
    if set(cached.keys()) != set(current.keys()):
        return False
    for path, (mtime_ns, size) in current.items():
        cached_entry = cached[path]
        if len(cached_entry) != 2 or cached_entry[0] != mtime_ns or cached_entry[1] != size:
            return False
    return True


def run_find_context(
    root_path: Path,
    config: EzsqlConfig,
    cache: CacheStore | None = None,
    *,
    query: str | None = None,
    task: str | None = None,  # noqa: ARG001 — no-op in Phase 1 (§17 Q7)
) -> ContextMap:
    """Scan repository for SQL-bearing files and return structured context map.

    Args:
        root_path: The resolved project root directory.
        config: The loaded EZSQL config (provides scan limits).
        cache: Optional cache store. If provided, results are cached and
            a freshness manifest guards against stale entries.
        query: Optional filter query (currently unused — Phase 2+).
        task: Optional task ID (no-op in Phase 1, §17 Q7).

    Returns:
        A ``ContextMap`` with classified files grouped by directory, scan
        metadata (files_seen, files_skipped, truncated, files_manifest),
        and cache provenance (cache_hit, cache_key).
    """
    key = scan_key(root_path)

    # Cache check + freshness guard
    if cache is not None:
        cached = cache.get(key, ContextMap)
        if cached is not None:
            # Freshness guard (plan §14): recompute manifest and compare.
            current_manifest = build_file_manifest(root_path)
            if _manifests_match(
                cached.scan_metadata.files_manifest, current_manifest
            ):
                counters.inc("cache_hits", 1)
                logger.info("find_context_cache_hit", root=str(root_path))
                cached.cache_provenance.cache_hit = True
                cached.cache_provenance.cache_key = key
                return cached
            # Stale — fall through to re-scan
            counters.inc("cache_stale", 1)
            logger.info("find_context_cache_stale", root=str(root_path))

    counters.inc("cache_misses", 1)

    # Scan with classification
    scan_result = scan_with_classification(
        root_path,
        max_file_size=config.max_file_size,
        max_files_per_scan=config.max_files_per_scan,
        max_total_bytes=config.max_total_bytes,
        max_scan_depth=config.max_scan_depth,
    )

    # Build ContextMap
    files_by_dir: dict[str, list[ContextFile]] = {}
    total_files = 0
    for dir_path, files in scan_result.by_dir.items():
        context_files = [
            ContextFile(name=name, classification=cls)
            for name, cls in files
        ]
        files_by_dir[dir_path] = context_files
        total_files += len(context_files)

    counters.inc("scan_files_seen", total_files)

    # Build the freshness manifest for the new entry.
    fresh_manifest = build_file_manifest(root_path)

    result = ContextMap(
        files_by_dir=files_by_dir,
        scan_metadata=ScanMetadata(
            files_seen=total_files,
            files_skipped=scan_result.files_skipped,
            truncated=scan_result.truncated,
            scan_root=str(root_path),
            files_manifest={
                path: [int(mtime_ns), int(size)]
                for path, (mtime_ns, size) in fresh_manifest.items()
            },
        ),
        cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
    )

    # Cache store
    if cache is not None:
        cache.put(key, "scan", result)

    logger.info(
        "find_context_complete",
        root=str(root_path),
        files_seen=total_files,
        files_skipped=scan_result.files_skipped,
        truncated=scan_result.truncated,
        dirs=len(files_by_dir),
        query=query,  # logged but not used for filtering yet
    )

    return result


__all__ = ["run_find_context"]
