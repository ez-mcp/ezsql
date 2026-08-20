"""Auto-vivified task registry with TTL expiry (plan §18).

Tasks are auto-vivified: any tool accepts ``task: str``; first use creates
it; TTL expires it; underlying cache entries persist beyond task expiry.

Task refs are **hints**, not authority (plan §18.1). The canonical source
of truth is content-addressed cache identity. If a task ref points to a
stale cache entry, the cache lookup will miss and the pipeline recomputes.

Task refs may dangle (cache entries evicted by LRU). This is a recoverable
miss: ``resolve_context`` returns ``None``, the pipeline recomputes.
"""

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

from pydantic import BaseModel

from ezsql.cache.store import CacheStore
from ezsql.core.schema.model import SchemaModel
from ezsql.server.models import ContextMap

logger = logging.getLogger("ezsql.tasks")

ArtifactType = Literal[
    "context_map",
    "schema_model",
    "analysis",
    "security",
    "optimize",
    "design",
    "refactor",
    "debug",
]


class TaskRef(BaseModel):
    """A task reference to a cached artifact (plan §9.7)."""

    cache_key: str
    artifact_type: ArtifactType


@dataclass
class TaskState:
    """Internal task state (not serialized directly)."""

    task_id: str
    created_at: float
    ttl: float
    refs: list[TaskRef] = field(default_factory=list)


@dataclass
class TaskContext:
    """Resolved task context — hints from prior calls (plan §18.2).

    ``None`` values mean the artifact hasn't been cached yet (or the ref
    dangled — cache entry evicted). Pipelines check for ``None`` and
    compute if needed.
    """

    task_id: str
    context_map: ContextMap | None = None
    schema_model: SchemaModel | None = None


class TaskRegistry:
    """Auto-vivified task registry with TTL expiry.

    Thread-safe via a single lock (stdio = one process per workspace).
    """

    def __init__(self, default_ttl: float = 3600.0) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._lock = Lock()
        self._default_ttl = default_ttl

    def get_or_create(self, task_id: str, ttl: float | None = None) -> TaskState:
        """Get an existing task or create a new one.

        Auto-vivified: first reference creates the task. TTL expires it.
        """
        with self._lock:
            self._expire_locked()
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(
                    task_id=task_id,
                    created_at=time.time(),
                    ttl=ttl if ttl is not None else self._default_ttl,
                )
                self._tasks[task_id] = state
                logger.debug("task_created: %s", task_id)
            return state

    def add_ref(self, task_id: str, cache_key: str, artifact_type: ArtifactType) -> None:
        """Add a cache reference to a task.

        The registry doesn't interpret cache semantics — it stores the key
        and type as opaque metadata. The resolver uses the type to
        deserialize correctly.
        """
        with self._lock:
            self._expire_locked()
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(
                    task_id=task_id,
                    created_at=time.time(),
                    ttl=self._default_ttl,
                )
                self._tasks[task_id] = state
            # Don't add duplicate refs
            for ref in state.refs:
                if ref.cache_key == cache_key:
                    return
            state.refs.append(TaskRef(cache_key=cache_key, artifact_type=artifact_type))

    def resolve_context(self, task_id: str, cache: CacheStore) -> TaskContext:
        """Resolve task context: load cached artifacts if available.

        Returns ``TaskContext`` with whatever artifacts are cached. ``None``
        values mean the artifact hasn't been cached yet or the ref dangled
        (cache entry evicted). Pipelines check for ``None`` and compute.

        Task refs are hints. Cache identity (content-addressed) is the
        source of truth. If a ref is stale, the cache lookup misses and
        the pipeline recomputes.
        """
        with self._lock:
            self._expire_locked()
            state = self._tasks.get(task_id)
            if state is None:
                return TaskContext(task_id=task_id)

        context_map: ContextMap | None = None
        schema_model: SchemaModel | None = None

        for ref in state.refs:
            if ref.artifact_type == "context_map":
                cm = cache.get(ref.cache_key, ContextMap)
                if cm is not None:
                    context_map = cm
            elif ref.artifact_type == "schema_model":
                sm = cache.get(ref.cache_key, SchemaModel)
                if sm is not None:
                    schema_model = sm

        return TaskContext(
            task_id=task_id,
            context_map=context_map,
            schema_model=schema_model,
        )

    def _expire_locked(self) -> None:
        """Expire tasks past their TTL (must be called with lock held)."""
        now = time.time()
        expired = [
            tid for tid, state in self._tasks.items()
            if now - state.created_at > state.ttl
        ]
        for tid in expired:
            del self._tasks[tid]
            logger.debug("task_expired: %s", tid)

    def clear(self) -> None:
        """Clear all tasks (for testing)."""
        with self._lock:
            self._tasks.clear()


# Module-level singleton (plan §6 — one registry per process).
_registry: TaskRegistry | None = None
_registry_lock = Lock()


def get_registry() -> TaskRegistry:
    """Get the module-level TaskRegistry singleton."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = TaskRegistry()
        return _registry


def reset_registry() -> None:
    """Reset the registry (for testing)."""
    global _registry
    with _registry_lock:
        _registry = None


__all__ = [
    "ArtifactType",
    "TaskContext",
    "TaskRef",
    "TaskRegistry",
    "TaskState",
    "get_registry",
    "reset_registry",
]
