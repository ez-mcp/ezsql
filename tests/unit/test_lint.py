"""Unit tests for SQL lint service (plan §22.3, §16)."""

from ezsql.core.schema.model import (
    ColumnDef,
    IndexDef,
    SchemaModel,
    TableDef,
)
from ezsql.core.sql.lint import (
    OPT_CORRELATED_SUBQUERY,
    OPT_NO_INDEX,
    OPT_SELECT_STAR,
    OPT_TYPE_MISMATCH,
    lint,
)
from ezsql.core.sql.parse import parse


def _make_schema_with_users() -> SchemaModel:
    """Create a schema model with a users table for testing."""
    return SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={
                    "id": ColumnDef(name="id", data_type="INT", nullable=False),
                    "email": ColumnDef(name="email", data_type="VARCHAR(255)"),
                    "age": ColumnDef(name="age", data_type="INT"),
                },
                indexes={
                    "idx_email": IndexDef(
                        name="idx_email",
                        columns=["email"],
                    ),
                },
            ),
        },
    )


# --- OPT-001: SELECT * ---

def test_select_star_detected() -> None:
    """SELECT * is detected as OPT-001 (static, fact)."""
    result = parse("SELECT * FROM users", dialect="postgres")
    findings = lint(result, dialect="postgres")
    opt001 = [f for f in findings if f.rule_id == OPT_SELECT_STAR]
    assert len(opt001) == 1
    assert opt001[0].evidence == "static"
    assert opt001[0].kind == "fact"
    assert opt001[0].severity == "info"


def test_select_star_not_triggered_for_explicit_cols() -> None:
    """SELECT id, name does not trigger OPT-001."""
    result = parse("SELECT id, name FROM users", dialect="postgres")
    findings = lint(result, dialect="postgres")
    opt001 = [f for f in findings if f.rule_id == OPT_SELECT_STAR]
    assert len(opt001) == 0


def test_select_star_qualified() -> None:
    """SELECT t.* is detected as OPT-001."""
    result = parse("SELECT u.* FROM users u", dialect="postgres")
    findings = lint(result, dialect="postgres")
    opt001 = [f for f in findings if f.rule_id == OPT_SELECT_STAR]
    assert len(opt001) == 1


# --- OPT-002: Correlated subquery ---

def test_correlated_subquery_detected() -> None:
    """Correlated subquery is detected as OPT-002 (static, fact)."""
    sql = "SELECT * FROM t1 WHERE x > (SELECT AVG(y) FROM t2 WHERE t2.id = t1.id)"
    result = parse(sql, dialect="postgres")
    findings = lint(result, dialect="postgres")
    opt002 = [f for f in findings if f.rule_id == OPT_CORRELATED_SUBQUERY]
    assert len(opt002) == 1
    assert opt002[0].evidence == "static"
    assert opt002[0].kind == "fact"


def test_non_correlated_subquery_not_detected() -> None:
    """Non-correlated subquery does not trigger OPT-002."""
    sql = "SELECT * FROM t1 WHERE x > (SELECT AVG(y) FROM t2)"
    result = parse(sql, dialect="postgres")
    findings = lint(result, dialect="postgres")
    opt002 = [f for f in findings if f.rule_id == OPT_CORRELATED_SUBQUERY]
    assert len(opt002) == 0


# --- OPT-003: Type mismatch ---

def test_type_mismatch_detected() -> None:
    """INT column compared to string literal → OPT-003 (schema, fact)."""
    schema = _make_schema_with_users()
    result = parse("SELECT * FROM users WHERE id = 'abc'", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt003 = [f for f in findings if f.rule_id == OPT_TYPE_MISMATCH]
    assert len(opt003) == 1
    assert opt003[0].evidence == "schema"
    assert opt003[0].kind == "fact"
    assert opt003[0].schema_source == "repo-ddl"


def test_type_match_not_detected() -> None:
    """INT column compared to integer literal → no OPT-003."""
    schema = _make_schema_with_users()
    result = parse("SELECT * FROM users WHERE id = 5", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt003 = [f for f in findings if f.rule_id == OPT_TYPE_MISMATCH]
    assert len(opt003) == 0


def test_type_mismatch_withheld_without_schema() -> None:
    """OPT-003 is withheld when schema is unavailable."""
    result = parse("SELECT * FROM users WHERE id = 'abc'", dialect="postgres")
    findings = lint(result, schema=None, dialect="postgres")
    opt003 = [f for f in findings if f.rule_id == OPT_TYPE_MISMATCH]
    assert len(opt003) == 0


def test_type_mismatch_withheld_unknown_dialect() -> None:
    """OPT-003 is withheld when dialect is unknown."""
    schema = _make_schema_with_users()
    result = parse("SELECT * FROM users WHERE id = 'abc'", dialect="postgres")
    findings = lint(result, schema=schema, dialect="unknown")
    opt003 = [f for f in findings if f.rule_id == OPT_TYPE_MISMATCH]
    assert len(opt003) == 0


# --- OPT-004: No usable index ---

def test_no_usable_index_detected() -> None:
    """Predicate on unindexed column → OPT-004 (schema, inference)."""
    schema = _make_schema_with_users()
    # 'age' has no index
    result = parse("SELECT * FROM users WHERE age = 30", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 1
    assert opt004[0].evidence == "schema"
    assert opt004[0].kind == "inference"


def test_usable_index_not_detected() -> None:
    """Predicate on indexed column → no OPT-004."""
    schema = _make_schema_with_users()
    # 'email' has an index
    result = parse("SELECT * FROM users WHERE email = 'x@y.com'", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 0


def test_no_index_withheld_without_schema() -> None:
    """OPT-004 is withheld when schema is unavailable."""
    result = parse("SELECT * FROM users WHERE age = 30", dialect="postgres")
    findings = lint(result, schema=None, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 0


def test_no_index_withheld_when_no_indexes_in_model() -> None:
    """OPT-004 is withheld when the table has no indexes in the model."""
    schema = SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={"age": ColumnDef(name="age", data_type="INT")},
                indexes={},  # no indexes
            ),
        },
    )
    result = parse("SELECT * FROM users WHERE age = 30", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 0  # withheld — model may be incomplete


def test_composite_index_not_usable_for_non_first_column() -> None:
    """Composite index is not usable for non-first column (§16.1.1 condition 2)."""
    schema = SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={
                    "a": ColumnDef(name="a", data_type="INT"),
                    "b": ColumnDef(name="b", data_type="INT"),
                },
                indexes={
                    "idx_ab": IndexDef(name="idx_ab", columns=["a", "b"]),
                },
            ),
        },
    )
    # Predicate on 'b' (not first column of composite index)
    result = parse("SELECT * FROM users WHERE b = 1", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 1  # fires — b is not the first column


def test_partial_index_not_usable() -> None:
    """Partial index is not "obviously usable" (§16.1.1 condition 3)."""
    schema = SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={"email": ColumnDef(name="email", data_type="VARCHAR(255)")},
                indexes={
                    "idx_partial": IndexDef(
                        name="idx_partial",
                        columns=["email"],
                        is_partial=True,
                    ),
                },
            ),
        },
    )
    result = parse("SELECT * FROM users WHERE email = 'x'", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 1  # fires — partial index not "obviously usable"


def test_expression_index_not_usable() -> None:
    """Expression index is not "obviously usable" (§16.1.1 condition 4)."""
    schema = SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={"email": ColumnDef(name="email", data_type="VARCHAR(255)")},
                indexes={
                    "idx_expr": IndexDef(
                        name="idx_expr",
                        columns=["email"],
                        is_expression=True,
                    ),
                },
            ),
        },
    )
    result = parse("SELECT * FROM users WHERE email = 'x'", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    opt004 = [f for f in findings if f.rule_id == OPT_NO_INDEX]
    assert len(opt004) == 1  # fires — expression index not "obviously usable"


# --- Finding ordering ---

def test_findings_ordered_by_source_location() -> None:
    """Findings are ordered by (statement_index, line, col, rule_id)."""
    result = parse("SELECT * FROM users", dialect="postgres")
    findings = lint(result, dialect="postgres")
    # Verify ordering
    for i in range(len(findings) - 1):
        a, b = findings[i], findings[i + 1]
        key_a = (a.location.statement_index, a.location.start_line, a.location.start_col, a.rule_id)
        key_b = (b.location.statement_index, b.location.start_line, b.location.start_col, b.rule_id)
        assert key_a <= key_b


# --- Empty / no findings ---

def test_no_findings_for_clean_query() -> None:
    """A clean query with no issues produces no findings."""
    schema = _make_schema_with_users()
    result = parse("SELECT id, email FROM users WHERE email = 'x@y.com'", dialect="postgres")
    findings = lint(result, schema=schema, dialect="postgres")
    assert len(findings) == 0
