"""Two-tier content-addressed cache store (plan §14).

Memory tier: ``OrderedDict`` keyed by ``str``, LRU-bounded by ``max_entries``.
SQLite tier: ``<root>/.ezsql/cache.db``, WAL mode, JSON values via
``model_dump_json`` / ``model_validate_json`` (T6 — no pickle, no code
execution on load).

On get: memory miss → SQLite hit → promote to memory.
On put: write both tiers.

Cache is derived data: if corrupt or deleted, everything still works
(re-scan). Documented in plan §26.
"""

import logging
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import TypeVar

from pydantic import BaseModel

logger = logging.getLogger("ezsql.cache")

T = TypeVar("T", bound=BaseModel)

_CACHE_DIR = ".ezsql"
_CACHE_FILE = "cache.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS entries (
    key TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    value TEXT NOT NULL,
    created REAL NOT NULL,
    ttl REAL,
    last_access REAL NOT NULL
)
"""


class CacheStore:
    """Two-tier content-addressed cache (memory + SQLite).

    Thread-safe via a single lock (stdio = one process per workspace,
    plan §19). SQLite WAL mode for concurrent readers.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = 4096,
        max_size_mb: int = 50,
    ) -> None:
        self._root = root
        self._max_entries = max_entries
        self._max_size_mb = max_size_mb
        self._memory: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
        self._lock = Lock()
        self._db_path = root / _CACHE_DIR / _CACHE_FILE
        self._db: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite tier. Degrades gracefully on failure (T6)."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(_CREATE_TABLE)
            self._db.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Cache DB init failed at %s: %s; degrading to memory-only",
                           self._db_path, exc)
            self._db = None

    def get(
        self,
        key: str,
        model_cls: type[T],
        *,
        ttl_seconds: float | None = None,
    ) -> T | None:
        """Get a cached value, deserializing from JSON via pydantic.

        Args:
            key: The content-addressed cache key.
            model_cls: The pydantic model class to deserialize into.
            ttl_seconds: If set, entries older than this are treated as misses.

        Returns:
            The deserialized model, or None on miss/expiry/parse-error (T6.1).
        """
        with self._lock:
            # Memory tier
            mem_entry = self._memory.get(key)
            if mem_entry is not None:
                domain, value_json, created = mem_entry
                if ttl_seconds is not None and (time.time() - created) > ttl_seconds:
                    # Expired in memory
                    self._memory.pop(key, None)
                else:
                    try:
                        return model_cls.model_validate_json(value_json)
                    except (ValueError, TypeError) as exc:
                        logger.warning("Memory cache parse error for key %s: %s", key, exc)
                        self._memory.pop(key, None)

            # SQLite tier
            if self._db is not None:
                row = self._db.execute(
                    "SELECT value, created, ttl FROM entries WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is not None:
                    value_json, created, ttl = row
                    # Check TTL
                    effective_ttl = ttl_seconds if ttl_seconds is not None else ttl
                    if effective_ttl is not None and (time.time() - created) > effective_ttl:
                        # Expired
                        self._db.execute("DELETE FROM entries WHERE key = ?", (key,))
                        self._db.commit()
                        return None
                    # Update last_access
                    self._db.execute(
                        "UPDATE entries SET last_access = ? WHERE key = ?",
                        (time.time(), key),
                    )
                    self._db.commit()
                    # Parse (T6.1 — schema validation rejects poisoned values)
                    try:
                        result = model_cls.model_validate_json(value_json)
                        # Promote to memory
                        self._promote(key, "", value_json, created)
                        return result
                    except (ValueError, TypeError) as exc:
                        logger.warning("SQLite cache parse error for key %s: %s", key, exc)
                        self._db.execute("DELETE FROM entries WHERE key = ?", (key,))
                        self._db.commit()
                        return None

            return None

    def put(
        self,
        key: str,
        domain: str,
        value: BaseModel,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        """Store a value in both tiers.

        Args:
            key: The content-addressed cache key.
            domain: The cache domain (e.g. "scan").
            value: The pydantic model to cache (serialized as JSON — T6).
            ttl_seconds: Optional TTL; entries older than this are misses.
        """
        value_json = value.model_dump_json()
        created = time.time()
        ttl_val = ttl_seconds if ttl_seconds is not None else None

        with self._lock:
            # Memory tier
            self._memory[key] = (domain, value_json, created)
            self._memory.move_to_end(key)
            self._evict_memory()

            # SQLite tier
            if self._db is not None:
                try:
                    self._db.execute(
                        "INSERT OR REPLACE INTO entries "
                        "(key, domain, value, created, ttl, last_access) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (key, domain, value_json, created, ttl_val, created),
                    )
                    self._db.commit()
                    self._evict_sqlite()
                except sqlite3.Error as exc:
                    logger.warning("SQLite cache write failed for key %s: %s", key, exc)

    def _promote(self, key: str, domain: str, value_json: str, created: float) -> None:
        """Promote a SQLite entry to the memory tier."""
        self._memory[key] = (domain, value_json, created)
        self._memory.move_to_end(key)
        self._evict_memory()

    def _evict_memory(self) -> None:
        """Evict LRU entries from memory if over max_entries."""
        while len(self._memory) > self._max_entries:
            self._memory.popitem(last=False)

    def _evict_sqlite(self) -> None:
        """Evict LRU entries from SQLite when over ``max_size_mb`` (T3.2).

        Size is measured as the sum of ``LENGTH(value)`` across all entries
        (the dominant per-entry cost). When the total exceeds
        ``max_size_mb``, entries are evicted oldest-first by ``last_access``
        until the cap is satisfied. This makes ``max_size_mb`` load-bearing
        (previously it was unused — count-based eviction used
        ``max_entries`` instead).
        """
        if self._db is None:
            return
        max_bytes = self._max_size_mb * 1024 * 1024
        try:
            row = self._db.execute(
                "SELECT COALESCE(SUM(LENGTH(value)), 0) FROM entries"
            ).fetchone()
            total_bytes = row[0] if row is not None else 0
            if total_bytes <= max_bytes:
                return
            # Evict oldest by last_access until under cap. Delete in batches
            # keyed by rowid to avoid holding a cursor over mutations.
            while total_bytes > max_bytes:
                victim = self._db.execute(
                    "SELECT key, LENGTH(value) FROM entries "
                    "ORDER BY last_access ASC LIMIT 1"
                ).fetchone()
                if victim is None:
                    break  # empty table
                victim_key, victim_len = victim
                self._db.execute("DELETE FROM entries WHERE key = ?", (victim_key,))
                self._db.commit()
                total_bytes -= victim_len if victim_len is not None else 0
        except sqlite3.Error as exc:
            logger.warning("SQLite eviction failed: %s", exc)

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def clear(self) -> None:
        """Clear all entries from both tiers (for testing/corrupt recovery)."""
        with self._lock:
            self._memory.clear()
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM entries")
                    self._db.commit()
                except sqlite3.Error:
                    pass


def create_cache_store(root: Path, *, max_entries: int = 4096, max_size_mb: int = 50) -> CacheStore:
    """Factory for CacheStore. Handles corrupt-DB recovery (T6)."""
    return CacheStore(root, max_entries=max_entries, max_size_mb=max_size_mb)


__all__ = ["CacheStore", "create_cache_store"]
