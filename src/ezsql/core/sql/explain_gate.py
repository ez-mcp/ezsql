"""Strict single-query gate for live EXPLAIN (plan_phase3 §3 Gate 1).

Accepts exactly one **unprefixed PostgreSQL query** (SELECT, read-only
CTEs, set operations). All ``Command`` nodes, explicit EXPLAIN, DDL/DML,
transaction control, COPY, CALL/DO, locking clauses, ``SELECT INTO``, and
writable CTEs are rejected. The adapter never receives the caller's raw
string — only canonical SQL rendered from the validated AST, eliminating
the v2 TOCTOU/double-EXPLAIN paths (V3-2).
"""

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as SqlglotParseError

logger = logging.getLogger("ezsql.explain_gate")

# Statement root types that represent a read-only query.
_QUERY_ROOTS: frozenset[type] = frozenset({
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,  # parenthesized query
})

# Node types forbidden anywhere in the tree (deep walk). Built from names
# that exist in this sqlglot version — the set is filtered below.
_FORBIDDEN_NODE_NAMES: tuple[str, ...] = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "TruncateTable", "Command", "Into", "Copy", "Transaction", "Commit",
    "Rollback", "Savepoint", "Grant", "Revoke", "Analyze", "Vacuum",
    "Refresh", "Call", "LoadData", "Load", "Set", "SetItem",
)

_FORBIDDEN: frozenset[type] = frozenset(
    t for name in _FORBIDDEN_NODE_NAMES
    if isinstance(t := getattr(exp, name, None), type)
)

# Locking actions (FOR UPDATE / FOR SHARE / FOR NO KEY UPDATE / ...).
_LOCK_ACTIONS: frozenset[str] = frozenset({
    "UPDATE", "SHARE", "NO KEY UPDATE", "KEY SHARE",
})


@dataclass(frozen=True)
class ExplainableQuery:
    """A validated, canonical query ready for the EXPLAIN envelope.

    ``canonical_sql`` is rendered from the validated AST — the adapter
    never sees the caller's original raw string. ``has_placeholders`` is
    True when positional ``$n`` placeholders exist (adapter uses
    GENERIC_PLAN).
    """

    canonical_sql: str
    has_placeholders: bool


@dataclass(frozen=True)
class GateRejection:
    """Why the gate rejected a query. ``reason`` is a stable machine kind."""

    reason: str
    detail: str


def _find_locking(stmt: exp.Expr) -> str | None:
    """Find a locking clause (FOR UPDATE / FOR SHARE / ...) on a Select."""
    for node in stmt.walk():
        if isinstance(node, exp.Lock):
            action = node.args.get("actions")
            if action is not None:
                rendered = action.sql().upper() if hasattr(action, "sql") else str(action)
                for lock in _LOCK_ACTIONS:
                    if lock in rendered:
                        return f"locking clause FOR {lock}"
            return "locking clause"
    return None


def _has_writable_cte(stmt: exp.Expr) -> bool:
    """Detect data-modifying CTEs (WITH ... AS INSERT/UPDATE/DELETE)."""
    for node in stmt.walk():
        if isinstance(node, exp.CTE) and isinstance(
            node.this, (exp.Insert, exp.Update, exp.Delete)
        ):
            return True
    return False


def validate_explainable_query(sql: str, *, max_bytes: int) -> ExplainableQuery | GateRejection:
    """Validate exactly one unprefixed read-only PostgreSQL query.

    Steps (plan_phase3 §3 Gate 1):
    1. Reject empty input and input over ``max_bytes``.
    2. Parse as PostgreSQL with ZERO parse errors, exactly one statement.
    3. Reject Command, explicit EXPLAIN, DDL/DML, transaction control,
       COPY, CALL/DO, locking clauses, unknown statement roots.
    4. Require a query root (SELECT / read-only CTE / set operation).
    5. Deep-walk every descendant; reject write/command/lock nodes.
    6. Render canonical PostgreSQL SQL from the validated AST.
    7. Record placeholder presence for GENERIC_PLAN.
    """
    if not sql or not sql.strip():
        return GateRejection(reason="statement_blocked", detail="empty input")

    if len(sql.encode("utf-8")) > max_bytes:
        return GateRejection(
            reason="input_too_large",
            detail=f"SQL exceeds max_explain_sql_bytes ({max_bytes})",
        )

    # Parse with zero-error requirement. sqlglot.parse returns a list;
    # a multi-statement string yields multiple entries. Partial recovery
    # is NOT acceptable at this gate.
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except SqlglotParseError as exc:
        return GateRejection(
            reason="statement_blocked",
            detail=f"parse error: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — internal parse failure
        logger.error("explain gate internal parse failure: %s", type(exc).__name__)
        return GateRejection(
            reason="statement_blocked",
            detail="query could not be parsed as PostgreSQL",
        )

    # Filter Nones and comment-only Semicolon nodes (sqlglot yields a
    # Semicolon node holding only trailing comments after the last ';').
    stmts = [
        s for s in statements
        if s is not None and not isinstance(s, exp.Semicolon)
    ]
    if len(stmts) != 1:
        return GateRejection(
            reason="statement_blocked",
            detail=f"expected exactly one statement, found {len(stmts)}",
        )
    stmt = stmts[0]

    # Explicit EXPLAIN prefix — sqlglot models its tail as opaque Command
    # text, but check the node name too for defense in depth.
    if type(stmt).__name__ == "Explain":
        return GateRejection(
            reason="statement_blocked",
            detail="explicit EXPLAIN is not accepted; pass the unprefixed query",
        )

    # Unknown/fallback statement root (Command = sqlglot gave up).
    if isinstance(stmt, exp.Command):
        return GateRejection(
            reason="statement_blocked",
            detail=f"unsupported statement type: {type(stmt).__name__}",
        )

    # Require a query root.
    if not isinstance(stmt, tuple(_QUERY_ROOTS)):
        return GateRejection(
            reason="statement_blocked",
            detail=f"not a read-only query: {type(stmt).__name__}",
        )

    # Locking clauses.
    lock = _find_locking(stmt)
    if lock is not None:
        return GateRejection(reason="statement_blocked", detail=lock)

    # Writable CTEs.
    if _has_writable_cte(stmt):
        return GateRejection(
            reason="statement_blocked",
            detail="data-modifying CTE (WITH ... AS INSERT/UPDATE/DELETE)",
        )

    # Deep walk: reject forbidden nodes anywhere in the tree.
    for node in stmt.walk():
        if isinstance(node, tuple(_FORBIDDEN)):
            return GateRejection(
                reason="statement_blocked",
                detail=f"forbidden node in query tree: {type(node).__name__}",
            )

    # SELECT INTO detection: sqlglot models INTO via the Into node, which
    # is in _FORBIDDEN, but also check the Select's "into" arg explicitly.
    if isinstance(stmt, exp.Select) and stmt.args.get("into") is not None:
        return GateRejection(reason="statement_blocked", detail="SELECT INTO")

    # Placeholders: sqlglot models PostgreSQL $n as exp.Parameter.
    has_placeholders = any(isinstance(n, exp.Parameter) for n in stmt.walk())

    # Canonical rendering from the validated AST — the adapter never
    # receives the original raw string.
    canonical_sql = stmt.sql(dialect="postgres")

    return ExplainableQuery(canonical_sql=canonical_sql, has_placeholders=has_placeholders)


__all__ = ["ExplainableQuery", "GateRejection", "validate_explainable_query"]
