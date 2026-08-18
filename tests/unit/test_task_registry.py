"""Unit tests for task registry (plan §22.3, §18)."""

import time

from ezsql.cache.store import CacheStore
from ezsql.server.models import ContextMap
from ezsql.tasks.registry import TaskRegistry


def test_task_auto_vivification() -> None:
    """First reference creates the task."""
    registry = TaskRegistry(default_ttl=60)
    state = registry.get_or_create("my-task")
    assert state.task_id == "my-task"
    assert state.created_at > 0
    registry.clear()


def test_task_add_ref() -> None:
    """add_ref stores a cache key reference."""
    registry = TaskRegistry(default_ttl=60)
    registry.add_ref("my-task", "cache-key-1", "context_map")
    state = registry.get_or_create("my-task")
    assert len(state.refs) == 1
    assert state.refs[0].cache_key == "cache-key-1"
    assert state.refs[0].artifact_type == "context_map"
    registry.clear()


def test_task_resolve_context(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """resolve_context loads cached artifacts."""
    registry = TaskRegistry(default_ttl=60)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    # Store a ContextMap in cache
    cm = ContextMap()
    cache.put("cm-key", "scan", cm)

    # Add ref and resolve
    registry.add_ref("my-task", "cm-key", "context_map")
    ctx = registry.resolve_context("my-task", cache)
    assert ctx.context_map is not None
    assert ctx.schema_model is None

    cache.close()
    registry.clear()


def test_task_resolve_context_dangling_ref(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Dangling ref (cache miss) → None, not error (plan §18.1)."""
    registry = TaskRegistry(default_ttl=60)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    # Add ref to a key that doesn't exist in cache
    registry.add_ref("my-task", "nonexistent-key", "context_map")
    ctx = registry.resolve_context("my-task", cache)
    assert ctx.context_map is None  # dangling ref → None

    cache.close()
    registry.clear()


def test_task_ttl_expiry() -> None:
    """Tasks expire after TTL."""
    registry = TaskRegistry(default_ttl=0.1)  # 100ms TTL
    registry.get_or_create("short-lived")
    assert len(registry._tasks) == 1  # noqa: SLF001

    time.sleep(0.15)  # wait for expiry
    registry.get_or_create("trigger-expiry")  # triggers _expire_locked
    assert "short-lived" not in registry._tasks  # noqa: SLF001
    registry.clear()


def test_task_none_no_context_resolution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """task=None → no context resolution (Phase 1 behavior)."""
    registry = TaskRegistry(default_ttl=60)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    # No task created — resolve returns empty context
    ctx = registry.resolve_context("nonexistent", cache)
    assert ctx.context_map is None
    assert ctx.schema_model is None

    cache.close()
    registry.clear()


def test_task_no_duplicate_refs() -> None:
    """Adding the same cache key twice doesn't duplicate."""
    registry = TaskRegistry(default_ttl=60)
    registry.add_ref("my-task", "key-1", "context_map")
    registry.add_ref("my-task", "key-1", "context_map")
    state = registry.get_or_create("my-task")
    assert len(state.refs) == 1
    registry.clear()


def test_task_ref_artifact_type() -> None:
    """TaskRef uses ArtifactType (not str) for artifact_type (exit criterion §23.29)."""
    registry = TaskRegistry(default_ttl=60)
    registry.add_ref("my-task", "key-1", "schema_model")
    state = registry.get_or_create("my-task")
    assert state.refs[0].artifact_type == "schema_model"
    registry.clear()
