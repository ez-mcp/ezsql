"""Unit tests for adapter lifecycle (plan_phase3 §10)."""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ezsql.config import EzsqlConfig
from ezsql.db.errors import DbAdapterError
from ezsql.db.lifecycle import AdapterLifecycle

SECURE_URI = "postgres://role:pw@host/db?sslmode=require"


def _mock_adapter() -> Any:
    adapter = MagicMock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.identity.fingerprint = "fp"
    adapter.server_major_version = 16
    return adapter


@pytest.fixture
def config() -> EzsqlConfig:
    return EzsqlConfig()


async def test_same_root_deduplicates_initialization(config: EzsqlConfig) -> None:
    """Concurrent first calls for the same key await one init task (§7.1)."""
    lifecycle = AdapterLifecycle(max_adapters=4)
    calls = 0

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _mock_adapter()

    with patch.object(lifecycle, "_initialize", side_effect=init):
        results = await asyncio.gather(*[
            lifecycle.acquire(Path("/proj"), SECURE_URI, config)
            for _ in range(5)
        ])

    assert calls == 1
    assert all(r.adapter is not None for r in results)
    assert all(r.failure is None for r in results)


async def test_unrelated_roots_initialize_concurrently(config: EzsqlConfig) -> None:
    """Unrelated roots do not serialize behind a global lock (§7.2)."""
    lifecycle = AdapterLifecycle(max_adapters=4)

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        await asyncio.sleep(0.05)
        return _mock_adapter()

    with patch.object(lifecycle, "_initialize", side_effect=init):
        results = await asyncio.gather(
            lifecycle.acquire(Path("/a"), SECURE_URI, config),
            lifecycle.acquire(Path("/b"), SECURE_URI, config),
        )

    assert all(r.adapter is not None for r in results)


async def test_failed_creation_not_cached(config: EzsqlConfig) -> None:
    """Failed creation is retried on the next call (§7.4)."""
    lifecycle = AdapterLifecycle(max_adapters=4)
    attempts = 0

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DbAdapterError("db_connection_failed", "first attempt fails")
        return _mock_adapter()

    with patch.object(lifecycle, "_initialize", side_effect=init):
        r1 = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        assert r1.failure is not None
        assert r1.failure.category == "db_connection_failed"

        r2 = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        assert r2.adapter is not None  # retried, not sticky-cached


async def test_adapter_cap_enforced(config: EzsqlConfig) -> None:
    """Process-wide cap prevents aggregate pool growth (§7.7)."""
    lifecycle = AdapterLifecycle(max_adapters=2)

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        return _mock_adapter()

    with patch.object(lifecycle, "_initialize", side_effect=init):
        r1 = await lifecycle.acquire(Path("/a"), SECURE_URI, config)
        r2 = await lifecycle.acquire(Path("/b"), SECURE_URI, config)
        assert r1.adapter is not None
        assert r2.adapter is not None

        r3 = await lifecycle.acquire(Path("/c"), SECURE_URI, config)
        assert r3.failure is not None
        assert r3.failure.category == "db_adapter_limit"


async def test_credential_rotation_replaces_adapter(config: EzsqlConfig) -> None:
    """A rotated password creates a new adapter; identity stays stable (§6)."""
    lifecycle = AdapterLifecycle(max_adapters=4)

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        return _mock_adapter()

    with patch.object(lifecycle, "_initialize", side_effect=init):
        r1 = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        assert r1.adapter is not None

        rotated = "postgres://role:newpw@host/db?sslmode=require"
        r2 = await lifecycle.acquire(Path("/proj"), rotated, config)
        assert r2.adapter is not None
        assert r2.adapter is not r1.adapter  # replaced


async def test_waiter_cancellation_does_not_cancel_shared_init(
    config: EzsqlConfig,
) -> None:
    """One waiter's cancellation doesn't cancel shared initialization (§7.8)."""
    lifecycle = AdapterLifecycle(max_adapters=4)
    init_started = asyncio.Event()
    init_release = asyncio.Event()

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        init_started.set()
        await init_release.wait()
        return _mock_adapter()

    with patch.object(lifecycle, "_initialize", side_effect=init):
        # Waiter 1 will be cancelled mid-init.
        task1 = asyncio.create_task(
            lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        )
        await init_started.wait()

        # Waiter 2 arrives while init is in flight.
        task2 = asyncio.create_task(
            lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        )
        await asyncio.sleep(0.01)

        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

        # Release init; waiter 2 must still get its adapter.
        init_release.set()
        r2 = await task2
        assert r2.adapter is not None


async def test_shutdown_closes_adapters(config: EzsqlConfig) -> None:
    """Close cancels initializers and closes all adapters (§7.8)."""
    lifecycle = AdapterLifecycle(max_adapters=4)
    adapter = _mock_adapter()

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        return adapter

    with patch.object(lifecycle, "_initialize", side_effect=init):
        r = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        assert r.adapter is not None

    await lifecycle.close()
    adapter.close.assert_awaited()

    # After close, acquisition fails.
    r2 = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
    assert r2.failure is not None


async def test_invalid_uri_fails_without_network(config: EzsqlConfig) -> None:
    """Invalid config fails before any network I/O."""
    lifecycle = AdapterLifecycle(max_adapters=4)
    r = await lifecycle.acquire(Path("/proj"), "mysql://bad", config)
    assert r.failure is not None
    assert r.failure.category == "invalid_database_config"


async def test_release_then_reacquire(config: EzsqlConfig) -> None:
    """Released adapters are reused, not recreated."""
    lifecycle = AdapterLifecycle(max_adapters=4)
    adapter = _mock_adapter()

    async def init(uri: str, cfg: EzsqlConfig) -> Any:
        return adapter

    with patch.object(lifecycle, "_initialize", side_effect=init):
        r1 = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        assert r1.adapter is not None
        await lifecycle.release(Path("/proj"), SECURE_URI, config, r1.adapter)

        r2 = await lifecycle.acquire(Path("/proj"), SECURE_URI, config)
        assert r2.adapter is r1.adapter
