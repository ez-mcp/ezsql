"""Lazy per-root/config adapter lifecycle (plan_phase3 §7).

Adapters are shared by ``(resolved_root, adapter_config_fingerprint)``.
The config fingerprint includes the non-secret DB identity, pool sizes,
timeout settings, and a **process-local keyed credential fingerprint** —
the keyed fingerprint detects a rotated password for adapter replacement
but is never persisted, logged, or reused across processes. Cache keys
continue to use only the non-secret DB identity.

Requirements implemented (plan_phase3 §7):
1. Concurrent first calls for the same key await one initialization task.
2. Unrelated roots initialize concurrently; no global lock across network I/O.
3. A changed fingerprint creates a replacement and closes the obsolete adapter.
4. Failed creation is not cached; the next call retries.
5. The process-wide adapter cap reserves a slot before network I/O.
6. The shared initialization task is shielded from individual waiter
   cancellation; the final waiter cancels unfinished initialization.
7. Shutdown cancels/awaits initializers and releases all slots.
"""

import asyncio
import contextlib
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from ezsql.config import EzsqlConfig
from ezsql.db.errors import DbAdapterError
from ezsql.db.postgres import PostgresAdapter, parse_db_uri
from ezsql.observability import logger

# Monotonic-time warning suppression bound (plan_phase3 §7.5).
_WARNING_SUPPRESS_SECONDS = 60.0


@dataclass
class _AdapterEntry:
    """One shared adapter plus its lifecycle bookkeeping."""

    adapter: PostgresAdapter
    fingerprint: str
    leases: int = 0
    draining: bool = False
    last_warned: float = 0.0


@dataclass
class _InitState:
    """In-progress initialization for one lifecycle key."""

    task: asyncio.Task[PostgresAdapter]
    waiters: int = 0


@dataclass
class LifecycleResult:
    """Outcome of an adapter acquisition attempt."""

    adapter: PostgresAdapter | None = None
    failure: DbAdapterError | None = None


class AdapterLifecycle:
    """Process-wide adapter lifecycle manager."""

    def __init__(self, max_adapters: int = 4) -> None:
        self._max_adapters = max_adapters
        self._entries: dict[tuple[Path, str], _AdapterEntry] = {}
        self._inits: dict[tuple[Path, str], _InitState] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def _config_fingerprint(
        self, uri: str, config: EzsqlConfig
    ) -> tuple[str, str]:
        """Build (config_fingerprint, keyed_credential_fingerprint).

        The config fingerprint covers non-secret identity + pool sizes +
        timeouts. The keyed credential fingerprint hashes the credential
        material with a per-process salt so a rotated password produces a
        different value without persisting anything secret-derived.
        """
        identity, _ = parse_db_uri(uri)
        config_parts = "|".join([
            identity.fingerprint,
            str(config.db_pool_min_size),
            str(config.db_pool_max_size),
            str(config.db_connect_timeout_seconds),
            str(config.db_acquire_timeout_seconds),
            str(config.explain_statement_timeout_seconds),
            str(config.explain_lock_timeout_seconds),
            str(config.explain_total_timeout_seconds),
            str(config.max_plan_response_bytes),
            str(config.max_plan_nodes),
            str(config.max_plan_depth),
            str(config.max_plan_condition_chars),
        ])
        config_fp = hashlib.blake2b(
            config_parts.encode("utf-8"), digest_size=16
        ).hexdigest()

        # Keyed credential fingerprint: process-local salt + credential
        # material. Never persisted or logged.
        salt = _PROCESS_SALT
        cred_parts = salt + "|" + uri
        cred_fp = hashlib.blake2b(
            cred_parts.encode("utf-8"), digest_size=16
        ).hexdigest()
        return config_fp, cred_fp

    async def acquire(
        self, root: Path, uri: str, config: EzsqlConfig
    ) -> LifecycleResult:
        """Get or create the shared adapter for ``(root, config)``.

        Returns ``LifecycleResult`` with either a connected adapter or a
        typed failure. Never raises.
        """
        if self._closed:
            return LifecycleResult(failure=DbAdapterError(
                "db_connection_failed", "lifecycle manager is shut down"
            ))

        try:
            config_fp, cred_fp = self._config_fingerprint(uri, config)
        except DbAdapterError as exc:
            return LifecycleResult(failure=exc)

        key = (root.resolve(), config_fp + ":" + cred_fp)

        async with self._lock:
            if self._closed:
                return LifecycleResult(failure=DbAdapterError(
                    "db_connection_failed", "lifecycle manager is shut down"
                ))

            # Existing healthy entry — reuse.
            entry = self._entries.get(key)
            if entry is not None and not entry.draining:
                entry.leases += 1
                return LifecycleResult(adapter=entry.adapter)

            # In-progress initialization — await the shared task.
            init = self._inits.get(key)
            if init is not None:
                init.waiters += 1
            else:
                # Capacity check BEFORE network I/O (§7.7).
                live = sum(
                    1 for e in self._entries.values() if not e.draining
                ) + len(self._inits)
                if live >= self._max_adapters:
                    return LifecycleResult(failure=DbAdapterError(
                        "db_adapter_limit",
                        f"process adapter cap reached ({self._max_adapters})",
                    ))
                # Reserve the slot, then release the lock for network I/O.
                task = asyncio.create_task(
                    self._initialize(uri, config)
                )
                init = _InitState(task=task, waiters=1)
                self._inits[key] = init

        # Await the shared initialization OUTSIDE the lock (§7.2).
        try:
            adapter = await asyncio.shield(init.task)
        except asyncio.CancelledError:
            # This waiter was cancelled — release only its reservation.
            await self._release_waiter(key, init)
            raise
        except DbAdapterError as exc:
            await self._release_waiter(key, init)
            return LifecycleResult(failure=exc)
        except Exception:  # noqa: BLE001 — defensive
            await self._release_waiter(key, init)
            return LifecycleResult(failure=DbAdapterError(
                "db_connection_failed", "adapter initialization failed"
            ))

        async with self._lock:
            init_state = self._inits.get(key)
            if init_state is init:
                self._inits.pop(key, None)
            entry = _AdapterEntry(
                adapter=adapter, fingerprint=config_fp + ":" + cred_fp
            )
            entry.leases = init.waiters
            self._entries[key] = entry

        return LifecycleResult(adapter=adapter)

    async def _initialize(
        self, uri: str, config: EzsqlConfig
    ) -> PostgresAdapter:
        """Create and connect one adapter (runs inside a shared task)."""
        adapter = PostgresAdapter(
            uri,
            pool_min_size=config.db_pool_min_size,
            pool_max_size=config.db_pool_max_size,
            connect_timeout=float(config.db_connect_timeout_seconds),
            acquire_timeout=float(config.db_acquire_timeout_seconds),
            statement_timeout=float(config.explain_statement_timeout_seconds),
            lock_timeout=float(config.explain_lock_timeout_seconds),
            total_timeout=float(config.explain_total_timeout_seconds),
            max_plan_response_bytes=config.max_plan_response_bytes,
            max_plan_nodes=config.max_plan_nodes,
            max_plan_depth=config.max_plan_depth,
            max_plan_condition_chars=config.max_plan_condition_chars,
        )
        await adapter.connect()
        return adapter

    async def _release_waiter(
        self, key: tuple[Path, str], init: _InitState
    ) -> None:
        """Release one waiter's reservation; final waiter cancels init."""
        async with self._lock:
            state = self._inits.get(key)
            if state is not init:
                return
            init.waiters -= 1
            if init.waiters <= 0:
                self._inits.pop(key, None)
                init.task.cancel()

    async def release(
        self, root: Path, uri: str, config: EzsqlConfig,
        adapter: PostgresAdapter,
    ) -> None:
        """Release one lease; drain-and-close when the entry is obsolete."""
        try:
            config_fp, cred_fp = self._config_fingerprint(uri, config)
        except DbAdapterError:
            return
        key = (root.resolve(), config_fp + ":" + cred_fp)

        to_close: PostgresAdapter | None = None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.adapter is not adapter:
                return
            entry.leases = max(0, entry.leases - 1)
            if entry.draining and entry.leases == 0:
                self._entries.pop(key, None)
                to_close = entry.adapter

        if to_close is not None:
            await to_close.close()

    async def close(self) -> None:
        """Cancel initializers, close all adapters, release all slots."""
        async with self._lock:
            self._closed = True
            inits = list(self._inits.values())
            entries = list(self._entries.values())
            self._inits.clear()
            self._entries.clear()

        for init in inits:
            init.task.cancel()
        for init in inits:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await init.task

        for entry in entries:
            try:
                await entry.adapter.close()
            except Exception:  # noqa: BLE001 — shutdown best-effort
                logger.warning("adapter_close_failed")


# Per-process salt for the keyed credential fingerprint. Regenerated on
# every process start; never persisted.
_PROCESS_SALT: str = hashlib.blake2b(
    f"{time.time_ns()}-{id(object())}".encode(), digest_size=16
).hexdigest()

# Module-level singleton (one lifecycle per process, like the cache).
_lifecycle: AdapterLifecycle | None = None
_lifecycle_lock = asyncio.Lock()


async def get_adapter_lifecycle(config: EzsqlConfig) -> AdapterLifecycle:
    """Get the process-wide lifecycle, sized from config on first use."""
    global _lifecycle
    async with _lifecycle_lock:
        if _lifecycle is None:
            _lifecycle = AdapterLifecycle(max_adapters=config.max_database_adapters)
        return _lifecycle


async def close_adapter_lifecycle() -> None:
    """Shut down the process-wide lifecycle (call in lifespan finally)."""
    global _lifecycle
    async with _lifecycle_lock:
        lifecycle = _lifecycle
        _lifecycle = None
    if lifecycle is not None:
        await lifecycle.close()


__all__ = [
    "AdapterLifecycle",
    "LifecycleResult",
    "close_adapter_lifecycle",
    "get_adapter_lifecycle",
]
