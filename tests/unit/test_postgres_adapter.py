"""Unit tests for the PostgreSQL adapter (plan_phase3 §10, mocked).

Real-database behavior is covered by tests/pipelines/test_postgres_integration.py
when EZSQL_TEST_DATABASE_URL is set. These tests verify the safety model
with mocked asyncpg objects.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ezsql.core.sql.explain_gate import validate_explainable_query
from ezsql.core.sql.plan import ParsedPlan
from ezsql.db.errors import DbAdapterError
from ezsql.db.postgres import PostgresAdapter, parse_db_uri

SECURE_URI = "postgres://role:pw@db.example.com:5432/mydb?sslmode=require"


def _gate(sql: str = "SELECT 1"):
    result = validate_explainable_query(sql, max_bytes=262_144)
    assert not hasattr(result, "reason")
    return result


# --- URI parsing / identity ---

def test_parse_uri_valid() -> None:
    identity, kwargs = parse_db_uri(SECURE_URI)
    assert identity.host == "db.example.com"
    assert identity.port == 5432
    assert identity.database == "mydb"
    assert identity.role == "role"
    assert identity.ssl_mode == "require"
    assert identity.transport == "tcp"
    assert kwargs["password"] == "pw"


def test_parse_uri_rejects_non_postgres_scheme() -> None:
    with pytest.raises(DbAdapterError) as exc_info:
        parse_db_uri("mysql://user@host/db")
    assert exc_info.value.category == "invalid_database_config"


def test_parse_uri_rejects_missing_host() -> None:
    with pytest.raises(DbAdapterError):
        parse_db_uri("postgres:///mydb")


def test_parse_uri_rejects_missing_database() -> None:
    with pytest.raises(DbAdapterError):
        parse_db_uri("postgres://user@host")


def test_parse_uri_rejects_missing_role() -> None:
    with pytest.raises(DbAdapterError):
        parse_db_uri("postgres://host/db?sslmode=require")


def test_parse_uri_rejects_insecure_sslmode() -> None:
    for mode in ("disable", "allow", "prefer", ""):
        uri = f"postgres://role@host/db?sslmode={mode}" if mode else "postgres://role@host/db"
        with pytest.raises(DbAdapterError) as exc_info:
            parse_db_uri(uri)
        assert exc_info.value.category == "invalid_database_config"


def test_parse_uri_rejects_unknown_param() -> None:
    with pytest.raises(DbAdapterError) as exc_info:
        parse_db_uri("postgres://role@host/db?sslmode=require&evil_option=1")
    assert "evil_option" in exc_info.value.detail


def test_parse_uri_unix_socket_allowed() -> None:
    identity, _ = parse_db_uri("postgres://role@/var/run/postgresql/mydb")
    assert identity.transport == "unix"


def test_identity_fingerprint_excludes_password() -> None:
    """Password rotation does not change the persistent identity (§6)."""
    id1, _ = parse_db_uri("postgres://role:old@host/db?sslmode=require")
    id2, _ = parse_db_uri("postgres://role:new@host/db?sslmode=require")
    assert id1.fingerprint == id2.fingerprint


def test_identity_fingerprint_changes_on_identity_fields() -> None:
    base = "postgres://role@host/db?sslmode=require"
    id1, _ = parse_db_uri(base)
    id2, _ = parse_db_uri("postgres://role@host/otherdb?sslmode=require")
    id3, _ = parse_db_uri("postgres://other@host/db?sslmode=require")
    id4, _ = parse_db_uri("postgres://role@otherhost/db?sslmode=require")
    assert id1.fingerprint != id2.fingerprint
    assert id1.fingerprint != id3.fingerprint
    assert id1.fingerprint != id4.fingerprint


def test_identity_fingerprint_deterministic() -> None:
    id1, _ = parse_db_uri(SECURE_URI)
    id2, _ = parse_db_uri(SECURE_URI)
    assert id1.fingerprint == id2.fingerprint


# --- Adapter explain (mocked pool) ---

def _mock_pool(plan_json: str | None = None) -> Any:
    """Build a mocked asyncpg pool whose connections return plan JSON."""
    conn = MagicMock()
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.execute = AsyncMock(return_value="")
    conn.fetchval = AsyncMock(return_value=plan_json)

    pool = MagicMock()
    pool.close = AsyncMock()
    pool.terminate = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _plan_json() -> str:
    return json.dumps([{
        "Plan": {"Node Type": "Seq Scan", "Relation Name": "t",
                 "Total Cost": 10.0, "Plan Rows": 1},
        "Planning Time": 0.1,
    }])


def test_explain_builds_trusted_envelope() -> None:
    """The adapter emits exactly one EXPLAIN prefix with constant options."""
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(_plan_json())
    adapter._pool = pool

    captured: dict[str, Any] = {}

    async def fetchval(sql: str, *args: object) -> str:
        captured["sql"] = sql
        return _plan_json()

    pool.acquire.return_value.__aenter__.return_value.fetchval = fetchval

    plan = asyncio.run(adapter.explain(_gate()))
    assert isinstance(plan, ParsedPlan)
    sql = captured["sql"]
    # Exactly one EXPLAIN prefix, constant options, no ANALYZE.
    assert sql.count("EXPLAIN") == 1
    assert sql.startswith("EXPLAIN (FORMAT JSON, COSTS TRUE, SUMMARY TRUE, SETTINGS TRUE)")
    assert "ANALYZE" not in sql
    assert "BUFFERS" not in sql
    assert "TIMING" not in sql
    # Canonical SQL, not the raw input.
    assert sql.endswith("SELECT 1")


def test_explain_generic_plan_for_placeholders() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(_plan_json())
    adapter._pool = pool

    captured: dict[str, Any] = {}

    async def fetchval(sql: str, *args: object) -> str:
        captured["sql"] = sql
        return _plan_json()

    pool.acquire.return_value.__aenter__.return_value.fetchval = fetchval

    gate = _gate("SELECT * FROM t WHERE id = $1")
    asyncio.run(adapter.explain(gate))
    assert "GENERIC_PLAN TRUE" in captured["sql"]


def test_explain_no_generic_plan_without_placeholders() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(_plan_json())
    adapter._pool = pool

    captured: dict[str, Any] = {}

    async def fetchval(sql: str, *args: object) -> str:
        captured["sql"] = sql
        return _plan_json()

    pool.acquire.return_value.__aenter__.return_value.fetchval = fetchval

    asyncio.run(adapter.explain(_gate()))
    assert "GENERIC_PLAN" not in captured["sql"]


def test_explain_uses_readonly_transaction() -> None:
    """Every EXPLAIN runs inside transaction(readonly=True) (V3-1)."""
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(_plan_json())
    adapter._pool = pool
    conn = pool.acquire.return_value.__aenter__.return_value

    asyncio.run(adapter.explain(_gate()))
    conn.transaction.assert_called_once_with(readonly=True)


def test_explain_sets_transaction_local_timeouts() -> None:
    """Statement and lock timeouts are set inside the transaction."""
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(_plan_json())
    adapter._pool = pool
    conn = pool.acquire.return_value.__aenter__.return_value

    asyncio.run(adapter.explain(_gate()))
    executed = [c.args[0] for c in conn.execute.call_args_list]
    assert any("statement_timeout" in s for s in executed)
    assert any("lock_timeout" in s for s in executed)
    # set_config SQL is a constant; values are parameters.
    for call in conn.execute.call_args_list:
        assert call.args[0].startswith("SELECT set_config(")


def test_explain_empty_response_fails() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(None)
    adapter._pool = pool

    with pytest.raises(DbAdapterError) as exc_info:
        asyncio.run(adapter.explain(_gate()))
    assert exc_info.value.category == "explain_parse_failed"


def test_explain_oversized_response_rejected() -> None:
    adapter = PostgresAdapter(SECURE_URI, max_plan_response_bytes=100)
    big = json.dumps([{"Plan": {"Node Type": "Seq", "Pad": "x" * 500}}])
    pool = _mock_pool(big)
    adapter._pool = pool

    with pytest.raises(DbAdapterError) as exc_info:
        asyncio.run(adapter.explain(_gate()))
    assert exc_info.value.category == "plan_too_large"


def test_explain_not_connected_fails() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    with pytest.raises(DbAdapterError) as exc_info:
        asyncio.run(adapter.explain(_gate()))
    assert exc_info.value.category == "db_connection_failed"


def test_explain_invalid_json_fails_closed() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool("not json")
    adapter._pool = pool

    with pytest.raises(DbAdapterError) as exc_info:
        asyncio.run(adapter.explain(_gate()))
    assert exc_info.value.category == "explain_parse_failed"


def test_explain_driver_error_is_sanitized() -> None:
    """Driver exception messages never surface — only class names."""
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool(_plan_json())
    adapter._pool = pool
    conn = pool.acquire.return_value.__aenter__.return_value

    async def boom(sql: str, *args: object) -> str:
        raise RuntimeError("postgres://secret@host/db?password=hunter2 failed")

    conn.fetchval = boom

    with pytest.raises(DbAdapterError) as exc_info:
        asyncio.run(adapter.explain(_gate()))
    # The DSN in the driver message must not leak.
    assert "hunter2" not in exc_info.value.detail
    assert "postgres://" not in exc_info.value.detail


def test_adapter_has_no_write_api() -> None:
    """Gate 3: the adapter surface exposes no execute/fetch/write method."""
    adapter = PostgresAdapter(SECURE_URI)
    public = [n for n in dir(adapter) if not n.startswith("_")]
    for forbidden in ("execute", "fetch", "fetchrow", "fetchval", "query"):
        assert forbidden not in public, f"adapter exposes {forbidden!r}"
    assert set(public) <= {"connect", "explain", "close", "identity",
                           "server_major_version"}


# --- Connect-time checks (mocked) ---

def test_connect_rejects_old_server_version() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool()
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchval = AsyncMock(return_value="15.4")

    with patch("ezsql.db.postgres.asyncpg.create_pool", return_value=_async_pool(pool)):
        with pytest.raises(DbAdapterError) as exc_info:
            asyncio.run(adapter.connect())
        assert exc_info.value.category == "database_version_unsupported"


def test_connect_rejects_superuser_role() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool()
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchval = AsyncMock(side_effect=["16.4", False, False, False, False,
                                           False, False])
    conn.fetchrow = AsyncMock(return_value={
        "rolsuper": True, "rolbypassrls": False,
        "rolcreaterole": False, "rolcreatedb": False,
    })

    with patch("ezsql.db.postgres.asyncpg.create_pool", return_value=_async_pool(pool)):
        with pytest.raises(DbAdapterError) as exc_info:
            asyncio.run(adapter.connect())
        assert exc_info.value.category == "unsafe_database_role"
        assert exc_info.value.detail == "role is superuser"


def test_connect_rejects_write_privileges() -> None:
    adapter = PostgresAdapter(SECURE_URI)
    pool = _mock_pool()
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchval = AsyncMock(side_effect=["16.4", False, False, False, False, False])
    conn.fetchrow = AsyncMock(return_value={
        "rolsuper": False, "rolbypassrls": False,
        "rolcreaterole": False, "rolcreatedb": False,
    })
    conn.fetch = AsyncMock(side_effect=[
        [],  # schemas
        [],  # memberships
        [{"relname": "orders", "nspname": "public"}],  # write privileges
    ])

    with patch("ezsql.db.postgres.asyncpg.create_pool", return_value=_async_pool(pool)):
        with pytest.raises(DbAdapterError) as exc_info:
            asyncio.run(adapter.connect())
        assert exc_info.value.category == "unsafe_database_role"
        assert "orders" in exc_info.value.detail


def _async_pool(pool: Any) -> Any:
    """Make ``await create_pool(...)`` return the mocked pool."""
    async def _factory(*args: object, **kwargs: object) -> Any:
        return pool
    return _factory()
