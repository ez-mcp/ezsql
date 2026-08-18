"""Unit tests for security engine (plan §22.3, §15)."""

from ezsql.core.security.engine import evaluate
from ezsql.core.security.model import AnalysisUnit
from ezsql.core.security.rules import (
    SEC_DELETE_NO_WHERE,
    SEC_DROP_TABLE,
    SEC_MIGRATION_DROP,
    get_rules,
)


def _make_sql_unit(sql: str, role: str = "query") -> AnalysisUnit:
    """Create a SQL analysis unit."""
    return AnalysisUnit(
        unit_id="sql:0",
        content=sql,
        input_kind="sql",
        input_role=role,  # type: ignore[arg-type]
    )


def _make_py_unit(source: str) -> AnalysisUnit:
    """Create a Python source analysis unit."""
    return AnalysisUnit(
        unit_id="test.py",
        file="test.py",
        content=source,
        input_kind="python_source",
        input_role="script",
    )


# --- Coverage model ---

def test_evaluated_status() -> None:
    """A rule that runs on matching input → evaluated."""
    unit = _make_sql_unit("DROP TABLE users", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_DROP_TABLE]
    result = evaluate(rules, [unit], dialect="postgres")
    assert len(result.coverage) == 1
    assert result.coverage[0].status == "evaluated"
    assert len(result.findings) == 1


def test_not_applicable_input_kind_mismatch() -> None:
    """SQL rule on python_source → not_applicable (input_kind_mismatch)."""
    unit = _make_py_unit("x = 1")
    rules = [r for r in get_rules() if r.rule_id == SEC_DROP_TABLE]
    result = evaluate(rules, [unit], dialect="postgres")
    assert result.coverage[0].status == "not_applicable"
    assert result.coverage[0].reason == "input_kind_mismatch"


def test_not_applicable_role_mismatch() -> None:
    """Migration-only rule on query input → not_applicable (input_role_mismatch)."""
    unit = _make_sql_unit("DROP TABLE users", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_MIGRATION_DROP]
    result = evaluate(rules, [unit], dialect="postgres")
    assert result.coverage[0].status == "not_applicable"
    assert result.coverage[0].reason == "input_role_mismatch"


def test_skipped_dialect_mismatch() -> None:
    """Dialect-dependent rule with unknown dialect → skipped."""
    unit = _make_sql_unit("EXECUTE somesp()", role="query")
    rules = [r for r in get_rules() if r.rule_id == "SEC-009"]
    result = evaluate(rules, [unit], dialect="unknown")
    assert result.coverage[0].status == "skipped"
    assert result.coverage[0].reason == "dialect_mismatch"


# --- Findings ---

def test_drop_table_finding() -> None:
    """DROP TABLE → SEC-003 finding (high, static, fact)."""
    unit = _make_sql_unit("DROP TABLE users", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_DROP_TABLE]
    result = evaluate(rules, [unit], dialect="postgres")
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.rule_id == SEC_DROP_TABLE
    assert f.severity == "high"
    assert f.evidence == "static"
    assert f.kind == "fact"


def test_drop_table_in_migration_triggers_both_rules() -> None:
    """DROP TABLE in migration → SEC-003 + SEC-007 (plan §15.9)."""
    unit = _make_sql_unit("DROP TABLE users", role="migration")
    rules = [r for r in get_rules() if r.rule_id in (SEC_DROP_TABLE, SEC_MIGRATION_DROP)]
    result = evaluate(rules, [unit], dialect="postgres")
    rule_ids = {f.rule_id for f in result.findings}
    assert SEC_DROP_TABLE in rule_ids
    assert SEC_MIGRATION_DROP in rule_ids


def test_delete_without_where() -> None:
    """DELETE without WHERE → SEC-005 finding."""
    unit = _make_sql_unit("DELETE FROM users", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_DELETE_NO_WHERE]
    result = evaluate(rules, [unit], dialect="postgres")
    assert len(result.findings) == 1
    assert result.findings[0].rule_id == SEC_DELETE_NO_WHERE


def test_delete_with_where_no_finding() -> None:
    """DELETE with WHERE → no SEC-005 finding."""
    unit = _make_sql_unit("DELETE FROM users WHERE id = 1", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_DELETE_NO_WHERE]
    result = evaluate(rules, [unit], dialect="postgres")
    assert len(result.findings) == 0


def test_empty_findings_with_coverage() -> None:
    """[] findings with evaluated coverage ≠ secure (plan §15.10)."""
    unit = _make_sql_unit("SELECT 1", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_DROP_TABLE]
    result = evaluate(rules, [unit], dialect="postgres")
    assert len(result.findings) == 0
    assert len(result.coverage) == 1
    assert result.coverage[0].status == "evaluated"


# --- Finding ordering ---

def test_findings_ordered_by_source_location() -> None:
    """Findings are output in source-location order."""
    unit = _make_sql_unit("DROP TABLE a; DROP TABLE b;", role="query")
    rules = [r for r in get_rules() if r.rule_id == SEC_DROP_TABLE]
    result = evaluate(rules, [unit], dialect="postgres")
    # Two findings, ordered by statement_index
    assert len(result.findings) == 2
    assert (
        result.findings[0].location.statement_index
        <= result.findings[1].location.statement_index
    )
