"""Unit tests for SQL rewrite service (plan §22.3, §17)."""

import sqlglot
from sqlglot import exp

from ezsql.core.schema.model import (
    ColumnDef,
    ParserWarning,
    SchemaModel,
    SourceSpan,
    TableDef,
)
from ezsql.core.sql.rewrite import rewrite


def _make_schema() -> SchemaModel:
    """Create a schema with a users table."""
    return SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={
                    "id": ColumnDef(name="id", data_type="INT", nullable=False),
                    "email": ColumnDef(name="email", data_type="VARCHAR(255)"),
                    "name": ColumnDef(name="name", data_type="TEXT"),
                },
            ),
        },
    )


def _parse_select(sql: str) -> exp.Select:
    """Parse a SELECT statement."""
    ast = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(ast, exp.Select)
    return ast


# --- Successful rewrite ---

def test_select_star_expansion() -> None:
    """SELECT * is expanded to explicit columns."""
    schema = _make_schema()
    stmt = _parse_select("SELECT * FROM users")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.validation_status == "validated"
    assert "id" in candidate.rewritten_sql
    assert "email" in candidate.rewritten_sql
    assert "name" in candidate.rewritten_sql
    assert "*" not in candidate.rewritten_sql
    assert candidate.evidence == "schema"
    assert candidate.plan_delta is None  # always None in Phase 2


def test_select_star_column_order() -> None:
    """Columns follow schema model declaration order (plan §17.1.1)."""
    schema = _make_schema()
    stmt = _parse_select("SELECT * FROM users")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 1
    # Order should be id, email, name (declaration order)
    sql = candidates[0].rewritten_sql
    id_pos = sql.index("id")
    email_pos = sql.index("email")
    name_pos = sql.index("name")
    assert id_pos < email_pos < name_pos


def test_select_star_qualified() -> None:
    """SELECT u.* is expanded to explicit columns."""
    schema = _make_schema()
    stmt = _parse_select("SELECT u.* FROM users u")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 1
    assert candidates[0].validation_status == "validated"


# --- Withheld rewrites ---

def test_rewrite_withheld_without_schema() -> None:
    """Rewrite is withheld when schema is unavailable."""
    stmt = _parse_select("SELECT * FROM users")
    candidates = rewrite(stmt, None, dialect="postgres")
    assert len(candidates) == 0


def test_rewrite_withheld_with_join() -> None:
    """Rewrite is withheld for multi-table (join)."""
    schema = _make_schema()
    stmt = _parse_select("SELECT * FROM users JOIN orders ON users.id = orders.user_id")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


def test_rewrite_withheld_with_cte() -> None:
    """Rewrite is withheld when CTE obscures column set."""
    schema = _make_schema()
    stmt = _parse_select("WITH cte AS (SELECT * FROM users) SELECT * FROM cte")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


def test_rewrite_withheld_with_completeness_warning() -> None:
    """Rewrite is withheld when table has completeness warning (§17.1 precondition 4)."""
    schema = SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={"id": ColumnDef(name="id", data_type="INT")},
            ),
        },
        parser_warnings=[
            ParserWarning(
                kind="unsupported_column_type",
                location=SourceSpan(),
                object_name="users",
                message="Unsupported column type",
                affects_schema_completeness=True,
                compromised_capabilities=frozenset({"column_type"}),
            ),
        ],
    )
    stmt = _parse_select("SELECT * FROM users")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


def test_rewrite_withheld_non_repo_ddl_source() -> None:
    """Rewrite is withheld when schema source is not repo-ddl (§17.1 precondition 2)."""
    schema = SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={"id": ColumnDef(name="id", data_type="INT")},
            ),
        },
        source="introspection",
    )
    stmt = _parse_select("SELECT * FROM users")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


def test_rewrite_withheld_table_not_in_schema() -> None:
    """Rewrite is withheld when table is not in schema model."""
    schema = _make_schema()
    stmt = _parse_select("SELECT * FROM nonexistent_table")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


def test_rewrite_withheld_with_subquery_in_from() -> None:
    """Rewrite is withheld when FROM has a subquery."""
    schema = _make_schema()
    stmt = _parse_select("SELECT * FROM (SELECT * FROM users) AS sub")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


# --- No SELECT * ---

def test_no_rewrite_for_explicit_columns() -> None:
    """No rewrite when SELECT already has explicit columns."""
    schema = _make_schema()
    stmt = _parse_select("SELECT id, email FROM users")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 0


# --- plan_delta always None ---

def test_plan_delta_always_none() -> None:
    """RewriteCandidate.plan_delta is always None in Phase 2 (exit criterion §23.14)."""
    schema = _make_schema()
    stmt = _parse_select("SELECT * FROM users")
    candidates = rewrite(stmt, schema, dialect="postgres")
    assert len(candidates) == 1
    assert candidates[0].plan_delta is None
