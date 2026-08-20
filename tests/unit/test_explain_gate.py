"""Unit tests for the EXPLAIN statement gate (plan_phase3 §10)."""

import pytest

from ezsql.core.sql.explain_gate import (
    ExplainableQuery,
    GateRejection,
    validate_explainable_query,
)

MAX_BYTES = 262_144


def _accept(sql: str) -> ExplainableQuery:
    result = validate_explainable_query(sql, max_bytes=MAX_BYTES)
    assert isinstance(result, ExplainableQuery), (
        f"expected accept, got rejection: {getattr(result, 'detail', '?')}"
    )
    return result


def _reject(sql: str) -> GateRejection:
    result = validate_explainable_query(sql, max_bytes=MAX_BYTES)
    assert isinstance(result, GateRejection), (
        f"expected rejection, got acceptance: {result.canonical_sql!r}"
    )
    return result


# --- Accept cases ---

def test_accept_simple_select() -> None:
    q = _accept("SELECT 1")
    assert q.canonical_sql == "SELECT 1"
    assert q.has_placeholders is False


def test_accept_select_with_placeholder() -> None:
    q = _accept("SELECT * FROM users WHERE id = $1")
    assert q.has_placeholders is True


def test_accept_repeated_placeholders() -> None:
    q = _accept("SELECT * FROM t WHERE a = $1 OR b = $1")
    assert q.has_placeholders is True


def test_accept_readonly_cte() -> None:
    q = _accept("WITH c AS (SELECT 1) SELECT * FROM c")
    assert "WITH" in q.canonical_sql.upper()


def test_accept_union() -> None:
    _accept("SELECT a FROM t1 UNION SELECT b FROM t2")


def test_accept_intersect_except() -> None:
    _accept("SELECT 1 INTERSECT SELECT 2")
    _accept("SELECT 1 EXCEPT SELECT 2")


def test_accept_trailing_semicolon_and_comment() -> None:
    _accept("SELECT 1; -- trailing comment")


def test_accept_parenthesized_query() -> None:
    _accept("(SELECT 1)")


def test_accept_comments() -> None:
    q = _accept("SELECT 1 /* inline */")
    assert q.canonical_sql


def test_canonical_sql_is_rendered_not_raw() -> None:
    """The adapter receives canonical AST rendering, never the raw string."""
    q = _accept("select   1")
    assert q.canonical_sql == "SELECT 1"


# --- Reject cases ---

def test_reject_empty() -> None:
    r = _reject("")
    assert r.reason == "statement_blocked"


def test_reject_whitespace_only() -> None:
    _reject("   \n\t  ")


def test_reject_multiple_statements() -> None:
    _reject("SELECT 1; SELECT 2")


def test_reject_explicit_explain() -> None:
    r = _reject("EXPLAIN SELECT 1")
    assert r.reason == "statement_blocked"


def test_reject_explain_analyze() -> None:
    _reject("EXPLAIN ANALYZE SELECT 1")


def test_reject_insert() -> None:
    _reject("INSERT INTO t VALUES (1)")


def test_reject_update() -> None:
    _reject("UPDATE t SET x = 1")


def test_reject_delete() -> None:
    _reject("DELETE FROM t")


def test_reject_drop() -> None:
    _reject("DROP TABLE t")


def test_reject_create() -> None:
    _reject("CREATE TABLE t (id INT)")


def test_reject_alter() -> None:
    _reject("ALTER TABLE t ADD COLUMN x INT")


def test_reject_truncate() -> None:
    _reject("TRUNCATE TABLE t")


def test_reject_select_into() -> None:
    _reject("SELECT * INTO new_t FROM t")


def test_reject_locking_for_update() -> None:
    _reject("SELECT * FROM t FOR UPDATE")


def test_reject_locking_for_share() -> None:
    _reject("SELECT * FROM t FOR SHARE")


def test_reject_writable_cte() -> None:
    _reject("WITH c AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM c")


def test_reject_transaction_control() -> None:
    _reject("BEGIN")
    _reject("COMMIT")
    _reject("ROLLBACK")


def test_reject_copy() -> None:
    _reject("COPY t FROM stdin")


def test_reject_vacuum() -> None:
    _reject("VACUUM")


def test_reject_call() -> None:
    _reject("CALL foo()")


def test_reject_grant() -> None:
    _reject("GRANT ALL ON t TO public")


def test_reject_parse_error() -> None:
    _reject("SELECT FROM WHERE")


def test_reject_select_plus_write() -> None:
    _reject("SELECT 1; DELETE FROM t")


def test_reject_oversized() -> None:
    r = validate_explainable_query("SELECT " + "1" * 300_000, max_bytes=1_000)
    assert isinstance(r, GateRejection)
    assert r.reason == "input_too_large"


def test_reject_write_nested_in_subquery() -> None:
    """Deep walk catches forbidden nodes anywhere in the tree."""
    _reject("SELECT * FROM (DELETE FROM t RETURNING *) AS sub")


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT * FROM users WHERE id = $1",
    "WITH c AS (SELECT 1) SELECT * FROM c",
])
def test_gate_deterministic(sql: str) -> None:
    """Same input → same canonical output."""
    r1 = validate_explainable_query(sql, max_bytes=MAX_BYTES)
    r2 = validate_explainable_query(sql, max_bytes=MAX_BYTES)
    assert isinstance(r1, ExplainableQuery)
    assert isinstance(r2, ExplainableQuery)
    assert r1.canonical_sql == r2.canonical_sql
