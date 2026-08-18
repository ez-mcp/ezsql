"""Unit tests for the two-tier cache store."""

import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from ezsql.cache.store import CacheStore


class _TestModel(BaseModel):
    """Simple model for cache round-trip testing."""
    name: str
    value: int


class _TestModelV2(BaseModel):
    """Different schema to test poisoned-entry rejection (T6.1)."""
    different_field: str


@pytest.fixture
def store(tmp_path: Path) -> CacheStore:
    """Create a CacheStore against a temp dir."""
    s = CacheStore(tmp_path, max_entries=4, max_size_mb=1)
    yield s
    s.close()


def test_put_and_get_memory(store: CacheStore) -> None:
    """Basic put→get round-trip via memory tier."""
    model = _TestModel(name="test", value=42)
    store.put("key1", "scan", model)
    result = store.get("key1", _TestModel)
    assert result is not None
    assert result.name == "test"
    assert result.value == 42


def test_get_miss(store: CacheStore) -> None:
    """Missing key returns None."""
    result = store.get("nonexistent", _TestModel)
    assert result is None


def test_sqlite_persistence(tmp_path: Path) -> None:
    """SQLite tier persists across store instances."""
    model = _TestModel(name="persist", value=99)
    store1 = CacheStore(tmp_path, max_entries=4, max_size_mb=1)
    store1.put("key_p", "scan", model)
    store1.close()

    store2 = CacheStore(tmp_path, max_entries=4, max_size_mb=1)
    result = store2.get("key_p", _TestModel)
    store2.close()
    assert result is not None
    assert result.name == "persist"
    assert result.value == 99


def test_memory_lru_eviction(tmp_path: Path) -> None:
    """Memory tier evicts LRU entries when over max_entries.

    Uses a memory-only store (corrupt DB path) to test memory eviction
    in isolation — SQLite tier would otherwise re-serve evicted entries.
    """
    # Create a store with a corrupt DB so it degrades to memory-only
    cache_dir = tmp_path / ".ezsql"
    cache_dir.mkdir()
    (cache_dir / "cache.db").write_bytes(b"not a database")

    store = CacheStore(tmp_path, max_entries=4, max_size_mb=1)
    for i in range(6):  # max_entries=4
        store.put(f"key{i}", "scan", _TestModel(name=f"item{i}", value=i))
    # First 2 should be evicted from memory (LRU)
    assert store.get("key0", _TestModel) is None
    assert store.get("key1", _TestModel) is None
    # Last 4 should still be present
    assert store.get("key4", _TestModel) is not None
    assert store.get("key5", _TestModel) is not None
    store.close()


def test_ttl_expiry(store: CacheStore) -> None:
    """TTL-expired entries are treated as misses."""
    model = _TestModel(name="ttl", value=1)
    store.put("ttl_key", "scan", model, ttl_seconds=0.01)
    time.sleep(0.05)
    result = store.get("ttl_key", _TestModel, ttl_seconds=0.01)
    assert result is None


def test_poisoned_json_rejected(store: CacheStore) -> None:
    """Crafted JSON with wrong schema → rejected → cache miss (T6.1)."""
    # Insert a valid entry
    model = _TestModel(name="valid", value=1)
    store.put("poison_key", "scan", model)

    # Corrupt the SQLite value directly with wrong schema
    if store._db is not None:
        store._db.execute(
            "UPDATE entries SET value = ? WHERE key = ?",
            ('{"different_field": "injected"}', "poison_key"),
        )
        store._db.commit()
        # Clear memory to force SQLite read
        store._memory.clear()

    # get with _TestModel should fail to parse → None
    result = store.get("poison_key", _TestModel)
    assert result is None


def test_corrupt_db_recovery(tmp_path: Path) -> None:
    """Corrupt cache DB → degrade to memory-only, no crash (T6)."""
    cache_dir = tmp_path / ".ezsql"
    cache_dir.mkdir()
    db_path = cache_dir / "cache.db"
    # Write garbage to the DB file
    db_path.write_bytes(b"not a database")

    # Should not crash — degrades to memory-only
    store = CacheStore(tmp_path, max_entries=4, max_size_mb=1)
    # Memory tier still works
    model = _TestModel(name="recovery", value=1)
    store.put("recovery_key", "scan", model)
    result = store.get("recovery_key", _TestModel)
    assert result is not None
    assert result.name == "recovery"
    store.close()


def test_clear(store: CacheStore) -> None:
    """clear() removes all entries from both tiers."""
    model = _TestModel(name="clear", value=1)
    store.put("clear_key", "scan", model)
    store.clear()
    assert store.get("clear_key", _TestModel) is None


def test_promote_from_sqlite(store: CacheStore) -> None:
    """SQLite hit promotes to memory tier."""
    model = _TestModel(name="promote", value=1)
    store.put("promote_key", "scan", model)
    # Clear memory to force SQLite read
    store._memory.clear()
    assert len(store._memory) == 0

    result = store.get("promote_key", _TestModel)
    assert result is not None
    # Should be promoted to memory
    assert len(store._memory) == 1


def test_sqlite_eviction_size_based(tmp_path: Path) -> None:
    """_evict_sqlite evicts by total byte size, not entry count (Gap 4).

    With max_size_mb=1 (1 MiB), insert entries whose combined value size
    exceeds the cap and verify the oldest are evicted until under cap.
    """
    store = CacheStore(tmp_path, max_entries=4096, max_size_mb=1)
    # Each value is ~200 KiB; 10 entries = ~2 MiB > 1 MiB cap.
    big_value = "x" * (200 * 1024)
    for i in range(10):
        store.put(f"size_key_{i}", "scan", _TestModel(name=big_value, value=i))
    # After eviction, total stored bytes must be under the cap.
    if store._db is not None:
        total = store._db.execute(
            "SELECT COALESCE(SUM(LENGTH(value)), 0) FROM entries"
        ).fetchone()[0]
        assert total <= 1 * 1024 * 1024
        # The earliest entries should have been evicted (LRU by last_access)
        count = store._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert count < 10
    store.close()
