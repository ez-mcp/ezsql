"""Unit tests for SQL parse service (plan §22.3, §12)."""


from ezsql.core.sql.parse import (
    InternalFailure,
    ParseError,
    ParseResult,
    parse,
    parse_one,
)

# --- Basic parsing ---

def test_parse_empty() -> None:
    """Empty input → empty ParseResult."""
    result = parse("")
    assert isinstance(result, ParseResult)
    assert result.statements == []
    assert result.errors == []
    assert result.dialect == "unknown"


def test_parse_whitespace_only() -> None:
    """Whitespace-only input → empty ParseResult."""
    result = parse("   \n\t  ")
    assert isinstance(result, ParseResult)
    assert result.statements == []


def test_parse_single_statement() -> None:
    """Single SELECT statement parses successfully."""
    result = parse("SELECT 1", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1
    assert result.errors == []
    assert result.dialect == "postgres"


def test_parse_multi_statement() -> None:
    """Multiple statements parse into separate ASTs."""
    result = parse("SELECT 1; SELECT 2;", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 2
    assert result.errors == []


def test_parse_dialect_resolution_explicit() -> None:
    """Explicit dialect takes priority."""
    result = parse("SELECT 1", dialect="mysql", configured_dialect="postgres")
    assert result.dialect == "mysql"


def test_parse_dialect_resolution_configured() -> None:
    """Configured dialect used when no explicit."""
    result = parse("SELECT 1", configured_dialect="postgres")
    assert result.dialect == "postgres"


def test_parse_dialect_resolution_unknown() -> None:
    """No dialect → 'unknown'."""
    result = parse("SELECT 1")
    assert result.dialect == "unknown"


def test_parse_invalid_dialect_falls_back() -> None:
    """Invalid dialect name → 'unknown' (fail safely)."""
    result = parse("SELECT 1", dialect="not_a_real_dialect")
    assert result.dialect == "unknown"


# --- Per-statement recovery ---

def test_parse_per_statement_recovery() -> None:
    """One bad statement in multi-statement → partial results + error."""
    result = parse("SELECT 1; SELECT FROM WHERE; SELECT 3;", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 2  # stmt 0 and 2 parsed
    assert len(result.errors) == 1
    assert result.errors[0].kind == "parse_error"
    assert result.errors[0].statement_index == 1


def test_parse_bad_first_statement() -> None:
    """Bad first statement → remaining statements still parsed."""
    result = parse("SELECT FROM WHERE; SELECT 1;", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1
    assert len(result.errors) == 1
    assert result.errors[0].statement_index == 0


def test_parse_error_has_position() -> None:
    """Parse errors include line/col when available."""
    result = parse("SELECT FROM WHERE", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.line is not None
    assert err.col is not None


def test_parse_truly_invalid_sql() -> None:
    """Truly invalid SQL → parse error, not crash."""
    result = parse("SELECT FROM WHERE", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert result.statements == []
    assert len(result.errors) == 1


# --- parse_one ---

def test_parse_one_single() -> None:
    """Single statement → one AST."""
    result = parse_one("SELECT 1", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1
    assert result.errors == []


def test_parse_one_rejects_multi() -> None:
    """Multi-statement → rejected, never silently truncates."""
    result = parse_one("SELECT 1; DROP TABLE t;", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert result.statements == []
    assert len(result.errors) == 1
    assert result.errors[0].kind == "expected_one_statement"


def test_parse_one_empty() -> None:
    """Empty input → empty result."""
    result = parse_one("", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert result.statements == []
    assert result.errors == []


def test_parse_one_parse_error() -> None:
    """Invalid SQL → parse error."""
    result = parse_one("SELECT FROM WHERE", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert result.statements == []
    assert len(result.errors) == 1
    assert result.errors[0].kind == "parse_error"


# --- Statement budget ---

def test_parse_statement_budget() -> None:
    """Exceeding max_statements → truncated + budget error."""
    sql = "; ".join(f"SELECT {i}" for i in range(5))
    result = parse(sql, dialect="postgres", max_statements=3)
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 3
    assert result.truncated is True
    assert result.statements_suppressed == 2
    assert any(e.kind == "statement_budget_exceeded" for e in result.errors)


def test_statement_budget_error_no_position() -> None:
    """Budget-exceeded error has line=None, col=None (exit criterion §23.34)."""
    sql = "; ".join(f"SELECT {i}" for i in range(3))
    result = parse(sql, dialect="postgres", max_statements=1)
    assert isinstance(result, ParseResult)
    budget_errors = [e for e in result.errors if e.kind == "statement_budget_exceeded"]
    assert len(budget_errors) == 1
    assert budget_errors[0].line is None
    assert budget_errors[0].col is None


# --- Edge cases ---

def test_parse_semicolon_in_string() -> None:
    """Semicolons inside string literals don't split statements."""
    result = parse("SELECT 'hello; world'", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1


def test_parse_trailing_semicolons() -> None:
    """Trailing semicolons don't create empty statements."""
    result = parse("SELECT 1;;;", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1


def test_parse_bom() -> None:
    """BOM is handled (stripped by sqlglot)."""
    result = parse("\ufeffSELECT 1", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1


def test_parse_crlf() -> None:
    """CRLF line endings are handled."""
    result = parse("SELECT 1\r\nFROM users", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1


def test_parse_ddl() -> None:
    """DDL statements parse correctly."""
    result = parse("CREATE TABLE users (id INT PRIMARY KEY)", dialect="postgres")
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 1


def test_parse_multiple_ddl() -> None:
    """Multiple DDL statements parse correctly."""
    result = parse(
        "CREATE TABLE users (id INT); CREATE TABLE orders (id INT);",
        dialect="postgres",
    )
    assert isinstance(result, ParseResult)
    assert len(result.statements) == 2


# --- Internal failure ---

def test_internal_failure_is_distinct_type() -> None:
    """InternalFailure is a distinct type from ParseError (exit criterion §23.18)."""
    # We can't easily trigger a real internal failure, but we can verify
    # the type exists and is distinct.
    assert InternalFailure is not ParseError
    assert InternalFailure is not ParseResult
