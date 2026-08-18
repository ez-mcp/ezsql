"""Cache lifecycle management (plan §14, D3).

A module-level dict holds one ``CacheStore`` per resolved project root,
lazily created on first use and reused across tool calls. This avoids
reopening SQLite (and WAL checkpoint overhead) on every call. All stores
are closed on server shutdown via ``close_all_caches()``.

Rationale: stdio transport runs one process per workspace (plan §19), so a
single root is the common case. A lock guards the dict for safety under
any future concurrent use.
"""

import threading
from pathlib import Path

from ezsql.cache.store import CacheStore, create_cache_store
from ezsql.config import EzsqlConfig
from ezsql.observability import logger

_stores: dict[Path, CacheStore] = {}
_lock = threading.Lock()


def get_cache(root: Path, config: EzsqlConfig) -> CacheStore:
    """Get (or lazily create) the shared ``CacheStore`` for ``root``.

    The store is sized from the loaded config (``cache_max_entries``,
    ``cache_max_size_mb``). Reused across calls; closed on shutdown.
    """
    with _lock:
        store = _stores.get(root)
        if store is None:
            store = create_cache_store(
                root,
                max_entries=config.cache_max_entries,
                max_size_mb=config.cache_max_size_mb,
            )
            _stores[root] = store
            logger.info(
                "cache_store_created",
                root=str(root),
                max_entries=config.cache_max_entries,
                max_size_mb=config.cache_max_size_mb,
            )
        return store


def close_all_caches() -> None:
    """Close all cached ``CacheStore`` instances (call on shutdown)."""
    with _lock:
        for root, store in _stores.items():
            try:
                store.close()
            except Exception:  # noqa: BLE001 — shutdown best-effort
                logger.warning("cache_store_close_failed", root=str(root))
        _stores.clear()


__all__ = ["get_cache", "close_all_caches"]
