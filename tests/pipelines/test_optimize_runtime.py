"""Pipeline tests for runtime optimization evidence (plan_phase3 §10, mocked).

The existing synchronous optimize tests (test_optimize_query.py) remain
unchanged — that is the Phase 2 compatibility proof.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.sql.plan import normalize_explain_json
from ezsql.db.errors import DbAdapterError
from ezsql.db.lifecycle import AdapterLifecycle
from ezsql.pipelines.optimize_runtime import run_optimize_query_with_runtime
from ezsql.server.models import FailureEnvelope, OptimizeResult

URI = "postgres://role:pw@host/db?sslmode=require"

SCHEMA_SQL = "CREATE TABLE users (id INT PRIMARY KEY, email TEXT, name TEXT);"


def _plan(cost: float = 10.0, rows: int = 1) -> Any:
    return normalize_explain_json(json.dumps([{
        "Plan": {"Node Type": "Seq Scan", "Relation Name": "users",
                 "Total Cost": cost, "Plan Rows": rows},
        "Planning Time": 0.1,
    }]))


def _mock_lifecycle(adapter: Any) -> AdapterLifecycle:
    lifecycle = AdapterLifecycle(max_adapters=2)
    result = MagicMock()
    result.adapter = adapter
    result.failure = None
    lifecycle.acquire = AsyncMock(return_value=result)  # type: ignore[method-assign]
    lifecycle.release = AsyncMock()  # type: ignore[method-assign]
    return lifecycle


def _mock_adapter() -> Any:
    adapter = MagicMock()
    adapter.identity.fingerprint = "dbfp"
    adapter.server_major_version = 16
    adapter.explain = AsyncMock(return_value=_plan())
    adapter.close = AsyncMock()
    return adapter


def _make_repo(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir(exist_ok=True)
    (migrations / "001_init.sql").write_text(SCHEMA_SQL, encoding="utf-8")


# --- No-DB path: exact Phase 2 behavior ---

async def test_no_db_returns_static_result(tmp_path: Path) -> None:
    """No adapter → the unchanged static path (V3-5)."""
    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, None, None
    )
    assert isinstance(result, OptimizeResult)
    assert result.runtime_evidence_status == "unavailable"


async def test_no_db_static_cache_unchanged(tmp_path: Path) -> None:
    """No-DB static cache JSON retains the Phase 2 shape (§6)."""
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)
    r1 = await run_optimize_query_with_runtime(
        "SELECT 1", EzsqlConfig(), tmp_path, None, None, cache
    )
    assert isinstance(r1, OptimizeResult)
    assert r1.cache_provenance.cache_hit is False

    r2 = await run_optimize_query_with_runtime(
        "SELECT 1", EzsqlConfig(), tmp_path, None, None, cache
    )
    assert isinstance(r2, OptimizeResult)
    assert r2.cache_provenance.cache_hit is True
    cache.close()


async def test_no_db_parse_error_is_failure(tmp_path: Path) -> None:
    result = await run_optimize_query_with_runtime(
        "SELECT FROM WHERE", EzsqlConfig(), tmp_path, None, None
    )
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "parse_error"


# --- Runtime enrichment ---

async def test_runtime_enriches_eligible_candidate(tmp_path: Path) -> None:
    """A validated candidate gains a typed plan delta + runtime evidence."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    # Original plan expensive, candidate cheap.
    adapter.explain = AsyncMock(side_effect=[_plan(cost=100.0), _plan(cost=50.0)])
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(result, OptimizeResult)
    assert result.runtime_evidence_status == "available"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.evidence == "runtime"
    assert candidate.plan_delta is not None
    assert candidate.plan_delta.cost_delta == -50.0
    cache.close()


async def test_original_failure_prevents_candidates(tmp_path: Path) -> None:
    """Original-plan failure → no candidate EXPLAINs, status=failed (§5)."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    adapter.explain = AsyncMock(side_effect=DbAdapterError(
        "db_connection_failed", "boom"
    ))
    lifecycle = _mock_lifecycle(adapter)

    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, OptimizeResult)
    assert result.runtime_evidence_status == "failed"
    # Only the original was attempted.
    assert adapter.explain.await_count == 1


async def test_candidate_failure_marks_partial(tmp_path: Path) -> None:
    """A candidate EXPLAIN failure → runtime_failure on that candidate,
    status=partial, and the static result is still returned (§5)."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    # Original OK; the single candidate fails.
    adapter.explain = AsyncMock(side_effect=[
        _plan(),
        DbAdapterError("explain_timeout", "timeout"),
    ])
    lifecycle = _mock_lifecycle(adapter)

    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, OptimizeResult)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.runtime_failure is not None
    assert candidate.evidence != "runtime"
    assert result.runtime_evidence_status == "partial"


async def test_withheld_candidate_never_explained(tmp_path: Path) -> None:
    """Withheld/unsafe candidates are never explained (§5)."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)

    result = await run_optimize_query_with_runtime(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, OptimizeResult)
    # No candidates for SELECT 1 — only the original was explained.
    assert adapter.explain.await_count == 1


async def test_runtime_failure_never_cached(tmp_path: Path) -> None:
    """Transient DB failure never creates a runtime cache entry (§6)."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    adapter.explain = AsyncMock(side_effect=DbAdapterError(
        "db_connection_failed", "boom"
    ))
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    r1 = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r1, OptimizeResult)
    assert r1.runtime_evidence_status == "failed"

    # Recovery retries immediately even when static result would be a hit.
    adapter.explain = AsyncMock(side_effect=[_plan(cost=100.0), _plan(cost=50.0)])
    r2 = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r2, OptimizeResult)
    assert r2.runtime_evidence_status == "available"
    cache.close()


async def test_runtime_cache_hit(tmp_path: Path) -> None:
    """Second identical call is a runtime-evidence cache hit."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    adapter.explain = AsyncMock(side_effect=[_plan(cost=100.0), _plan(cost=50.0)])
    lifecycle = _mock_lifecycle(adapter)
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    r1 = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r1, OptimizeResult)
    assert r1.runtime_evidence_status == "available"
    explain_count = adapter.explain.await_count

    r2 = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, cache
    )
    assert isinstance(r2, OptimizeResult)
    # No additional EXPLAIN calls — served from runtime cache.
    assert adapter.explain.await_count == explain_count
    assert r2.runtime_evidence_status == "available"
    cache.close()


async def test_cardinality_change_adds_semantic_warning(tmp_path: Path) -> None:
    """A material cardinality difference adds a semantic-safety warning (§5)."""
    _make_repo(tmp_path)
    adapter = _mock_adapter()
    # Original estimates 10 rows; candidate estimates 1000 — material change.
    adapter.explain = AsyncMock(side_effect=[
        _plan(cost=100.0, rows=10), _plan(cost=50.0, rows=1000),
    ])
    lifecycle = _mock_lifecycle(adapter)

    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, OptimizeResult)
    candidate = result.candidates[0]
    assert candidate.plan_delta is not None
    assert candidate.plan_delta.cardinality_changed is True
    assert any("semantic-safety" in p for p in candidate.preconditions)


async def test_db_isolation_of_runtime_evidence(tmp_path: Path) -> None:
    """DB-A and DB-B never share runtime evidence (§6)."""
    _make_repo(tmp_path)
    adapter_a = _mock_adapter()
    adapter_a.identity.fingerprint = "db-a"
    adapter_b = _mock_adapter()
    adapter_b.identity.fingerprint = "db-b"

    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)
    r1 = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI,
        _mock_lifecycle(adapter_a), cache,
    )
    assert isinstance(r1, OptimizeResult)

    r2 = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI,
        _mock_lifecycle(adapter_b), cache,
    )
    assert isinstance(r2, OptimizeResult)
    # Different DB → fresh EXPLAINs, not shared evidence.
    assert adapter_b.explain.await_count >= 1
    cache.close()


async def test_non_postgres_dialect_static_only(tmp_path: Path) -> None:
    """Non-Postgres dialects get static-only results (§8)."""
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)
    config = EzsqlConfig()
    config.default_dialect = "mysql"

    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", config, tmp_path, URI, lifecycle, None,
        dialect="mysql",
    )
    assert isinstance(result, OptimizeResult)
    assert result.runtime_evidence_status == "unavailable"
    assert adapter.explain.await_count == 0


async def test_adapter_limit_static_fallback(tmp_path: Path) -> None:
    """Adapter cap reached → static result with unavailable status (§8)."""
    _make_repo(tmp_path)
    lifecycle = AdapterLifecycle(max_adapters=1)
    # Simulate cap: acquire returns failure.
    failure_result = MagicMock()
    failure_result.adapter = None
    failure_result.failure = DbAdapterError("db_adapter_limit", "cap reached")
    lifecycle.acquire = AsyncMock(return_value=failure_result)  # type: ignore[method-assign]
    lifecycle.release = AsyncMock()  # type: ignore[method-assign]

    result = await run_optimize_query_with_runtime(
        "SELECT * FROM users", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, OptimizeResult)
    assert result.runtime_evidence_status == "unavailable"
    assert result.runtime_evidence_detail is not None


async def test_schema_unavailable_still_explains_original(tmp_path: Path) -> None:
    """No repo schema → static no-schema result + original EXPLAIN still runs."""
    # No migrations dir in tmp_path.
    adapter = _mock_adapter()
    lifecycle = _mock_lifecycle(adapter)

    result = await run_optimize_query_with_runtime(
        "SELECT 1", EzsqlConfig(), tmp_path, URI, lifecycle, None
    )
    assert isinstance(result, OptimizeResult)
    assert adapter.explain.await_count == 1  # original explained
    assert result.runtime_evidence_status == "available"
