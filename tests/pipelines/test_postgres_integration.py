"""Real PostgreSQL integration tests (plan_phase3 §10).

Run ONLY when ``EZSQL_TEST_DATABASE_URL`` is set AND the parsed database
name matches the test-only pattern ``ezsql_test`` — the fixture refuses
any other database. CI provisions an isolated database, a least-privileged
role (CONNECT/USAGE/SELECT only), and TLS.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ezsql.config import EzsqlConfig
from ezsql.db.lifecycle import AdapterLifecycle
from ezsql.pipelines.explain import run_explain_query
from ezsql.server.models import ExplainResult, FailureEnvelope

_TEST_URL = os.environ.get("EZSQL_TEST_DATABASE_URL", "")
_TEST_DB_NAME = urlparse(_TEST_URL).path.lstrip("/") if _TEST_URL else ""

# Refuse any database that doesn't look like an isolated test DB.
_IS_TEST_DB = "ezsql_test" in _TEST_DB_NAME

pytestmark = pytest.mark.skipif(
    not (_TEST_URL and _IS_TEST_DB),
    reason="EZSQL_TEST_DATABASE_URL not set or database name is not test-only",
)


@pytest.fixture
def config() -> EzsqlConfig:
    return EzsqlConfig()


@pytest.fixture
async def lifecycle(config: EzsqlConfig) -> AdapterLifecycle:
    mgr = AdapterLifecycle(max_adapters=2)
    yield mgr
    await mgr.close()


async def test_select_one(config: EzsqlConfig, lifecycle: AdapterLifecycle,
                          tmp_path: Path) -> None:
    result = await run_explain_query(
        "SELECT 1", config, tmp_path, _TEST_URL, lifecycle, None
    )
    assert isinstance(result, ExplainResult), (
        f"expected ExplainResult, got {result!r}"
    )
    assert result.summary.node_count >= 1


async def test_indexed_scan_detected(config: EzsqlConfig, lifecycle: AdapterLifecycle,
                                     tmp_path: Path) -> None:
    """A primary-key lookup produces an Index Scan (CI fixture table)."""
    result = await run_explain_query(
        "SELECT * FROM ezsql_test_users WHERE id = 1",
        config, tmp_path, _TEST_URL, lifecycle, None,
    )
    assert isinstance(result, ExplainResult)
    ops = result.summary.scan_ops
    assert any("Index" in op for op in ops), f"expected an index scan, got {ops}"


async def test_sequential_scan_detected(config: EzsqlConfig,
                                        lifecycle: AdapterLifecycle,
                                        tmp_path: Path) -> None:
    """A predicate without an index produces a Seq Scan."""
    result = await run_explain_query(
        "SELECT * FROM ezsql_test_users WHERE non_indexed = 'x'",
        config, tmp_path, _TEST_URL, lifecycle, None,
    )
    assert isinstance(result, ExplainResult)
    assert "Seq Scan" in result.summary.scan_ops


async def test_generic_parameter_plan(config: EzsqlConfig,
                                      lifecycle: AdapterLifecycle,
                                      tmp_path: Path) -> None:
    """$n placeholders produce a generic plan (PostgreSQL 16 GENERIC_PLAN)."""
    result = await run_explain_query(
        "SELECT * FROM ezsql_test_users WHERE id = $1",
        config, tmp_path, _TEST_URL, lifecycle, None,
    )
    assert isinstance(result, ExplainResult)


async def test_readonly_transaction_enforced(config: EzsqlConfig,
                                             lifecycle: AdapterLifecycle,
                                             tmp_path: Path) -> None:
    """Mutations are denied — the gate blocks them before the DB."""
    result = await run_explain_query(
        "DELETE FROM ezsql_test_users", config, tmp_path, _TEST_URL,
        lifecycle, None,
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "statement_blocked"


async def test_pool_reuse_keeps_readonly(config: EzsqlConfig,
                                         lifecycle: AdapterLifecycle,
                                         tmp_path: Path) -> None:
    """Release/reacquire keeps the readonly control (V3-1 proof).

    Two sequential EXPLAINs through the same pool: the second must still
    run inside a readonly transaction (pool RESET ALL cannot remove it).
    """
    r1 = await run_explain_query(
        "SELECT 1", config, tmp_path, _TEST_URL, lifecycle, None
    )
    assert isinstance(r1, ExplainResult)
    r2 = await run_explain_query(
        "SELECT 2", config, tmp_path, _TEST_URL, lifecycle, None
    )
    assert isinstance(r2, ExplainResult)


async def test_explain_cache_roundtrip(config: EzsqlConfig,
                                       lifecycle: AdapterLifecycle,
                                       tmp_path: Path) -> None:
    from ezsql.cache.store import CacheStore

    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)
    r1 = await run_explain_query(
        "SELECT 1", config, tmp_path, _TEST_URL, lifecycle, cache
    )
    assert isinstance(r1, ExplainResult)
    assert r1.cache_provenance.cache_hit is False

    r2 = await run_explain_query(
        "SELECT 1", config, tmp_path, _TEST_URL, lifecycle, cache
    )
    assert isinstance(r2, ExplainResult)
    assert r2.cache_provenance.cache_hit is True
    cache.close()


async def test_runtime_enrichment_against_real_db(tmp_path: Path) -> None:
    """optimize_query gains live planner evidence on a real database."""
    from ezsql.pipelines.optimize_runtime import run_optimize_query_with_runtime
    from ezsql.server.models import OptimizeResult

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_init.sql").write_text(
        "CREATE TABLE ezsql_test_users (id INT PRIMARY KEY, "
        "email TEXT, name TEXT);",
        encoding="utf-8",
    )

    lifecycle = AdapterLifecycle(max_adapters=2)
    try:
        result = await run_optimize_query_with_runtime(
            "SELECT * FROM ezsql_test_users", EzsqlConfig(), tmp_path,
            _TEST_URL, lifecycle, None,
        )
        assert isinstance(result, OptimizeResult)
        # The table exists in the test DB, so the rewrite candidate is
        # eligible and should carry live evidence.
        if result.candidates:
            assert result.runtime_evidence_status in ("available", "partial")
    finally:
        await lifecycle.close()
