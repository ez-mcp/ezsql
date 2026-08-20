"""Pipeline tests for explain_query (plan_phase3 §10, mocked adapter)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.db.errors import DbAdapterError
from ezsql.db.lifecycle import AdapterLifecycle
from ezsql.pipelines.explain import run_explain_query
from ezsql.server.models import ExplainResult, FailureEnvelope

URI = "postgres://role:pw@host/db?sslmode=require"


def _plan_json(cost: float = 10.0) -> str:
    return json.dumps([{
        "Plan": {"Node Type": "Seq Scan", "Relation Name": "t",
                 "Total Cost": cost, "Plan Rows": 1},
        "Planning Time": 0.1,
    }])


def _mock_lifecycle(adapter: Any) -> AdapterLifecycle:
    lifecycle = AdapterLifecycle(max_adapters=2)
    result = MagicMock()
    result.adapter = adapter
    result.failure = None
    lifecycle.acquire = AsyncMock(return_value=result)  # type: ignore[method-assign]
    lifecycle.release = AsyncMock()  # type: ignore[method-assign]
    return lifecycle


def _mock_adapter(plan_json: str | None = _plan_json()) -> Any:
    adapter = MagicMock()
    adapter.identity.fingerprint = "dbfp"
    adapter.server_major_version = 16
    adapter.explain = AsyncMock(return_value=_parse(plan_json))
    adapter.close = AsyncMock()
    return adapter


def _parse(plan_json: str | None) -> Any:
    from ezsql.core.sql.plan import normalize_explain_json
    if plan_json is None:
        return None
    return normalize_explain_json(plan_json)


async def test_explain_success(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    result = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(result, ExplainResult)
    assert result.summary.root_op == "Seq Scan"
    assert result.summary.root_total_cost == 10.0
    assert result.cache_provenance.cache_hit is False
    assert "planner estimates" in result.limitations[0]
    cache.close()


async def test_explain_cache_hit(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    r1 = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r1, ExplainResult)
    assert r1.cache_provenance.cache_hit is False

    r2 = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r2, ExplainResult)
    assert r2.cache_provenance.cache_hit is True
    # Adapter explained only once.
    assert adapter.explain.await_count == 1
    cache.close()


async def test_explain_invalid_uri_rejected(tmp_path: Path) -> None:
    """An invalid URI is rejected with invalid_database_config (§8).

    The no-URL check lives in the tool layer (tools.py returns
    db_unavailable before calling the pipeline); the pipeline itself
    receives only valid URIs from the tool. Here we verify the pipeline
    passes through adapter-config failures from the lifecycle.
    """
    lifecycle = AdapterLifecycle(max_adapters=2)
    result = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, "mysql://bad", lifecycle, None
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "invalid_database_config"


async def test_explain_rejects_non_postgres_dialect(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    result = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, None, dialect="mysql"
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "dialect_not_supported"


async def test_explain_rejects_write(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    result = await run_explain_query(
        "DELETE FROM t", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "statement_blocked"


async def test_explain_rejects_explicit_explain(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    result = await run_explain_query(
        "EXPLAIN SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "statement_blocked"


async def test_explain_rejects_multi_statement(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    result = await run_explain_query(
        "SELECT 1; SELECT 2", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "statement_blocked"


async def test_explain_adapter_failure_mapped(tmp_path: Path) -> None:
    adapter = _mock_adapter()
    adapter.explain = AsyncMock(side_effect=DbAdapterError(
        "explain_timeout", "statement timeout"
    ))
    lifecycle = _mock_lifecycle(adapter)

    result = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "explain_timeout"


async def test_explain_failure_never_cached(tmp_path: Path) -> None:
    """DB failures are never written to the cache (§6)."""
    adapter = _mock_adapter()
    adapter.explain = AsyncMock(side_effect=DbAdapterError(
        "db_connection_failed", "boom"
    ))
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    r1 = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r1, FailureEnvelope)

    # Recovered adapter → retried, not served a cached failure.
    adapter.explain = AsyncMock(return_value=_parse(_plan_json()))
    r2 = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r2, ExplainResult)
    cache.close()


async def test_explain_db_isolation(tmp_path: Path) -> None:
    """DB-A and DB-B never share cached plans (§6)."""
    adapter_a = _mock_adapter()
    adapter_a.identity.fingerprint = "db-a"
    adapter_b = _mock_adapter()
    adapter_b.identity.fingerprint = "db-b"

    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)
    r1 = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, _mock_lifecycle(adapter_a), cache
    )
    assert isinstance(r1, ExplainResult)

    r2 = await run_explain_query(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, _mock_lifecycle(adapter_b), cache
    )
    assert isinstance(r2, ExplainResult)
    assert r2.cache_provenance.cache_hit is False  # different DB → miss
    assert adapter_b.explain.await_count == 1
    cache.close()


async def test_explain_ttl_expiry(tmp_path: Path) -> None:
    """Plan entries expire at TTL."""
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)
    config = EzsqlConfig()
    config.explain_ttl_seconds = 60

    r1 = await run_explain_query(
        "SELECT 1", config, tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r1, ExplainResult)

    # Simulate expiry by backdating the stored entry.
    with cache._lock:  # noqa: SLF001 — test-only manipulation
        if cache._db is not None:
            cache._db.execute(
                "UPDATE entries SET created = created - 3600 WHERE key = ?",
                (r1.cache_provenance.cache_key,),
            )
            cache._db.commit()
            cache._memory.clear()

    r2 = await run_explain_query(
        "SELECT 1", config, tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r2, ExplainResult)
    assert r2.cache_provenance.cache_hit is False
    cache.close()
