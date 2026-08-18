"""Unit tests for dialect service (plan §22.3)."""

from ezsql.core.sql.dialect import (
    DialectInference,
    infer_dialect,
    is_known_dialect,
    list_dialects,
    resolve_dialect,
)

# --- Resolution chain ---

def test_resolve_explicit_takes_priority() -> None:
    """Explicit dialect takes priority over configured."""
    assert resolve_dialect("mysql", "postgres") == "mysql"


def test_resolve_configured_when_no_explicit() -> None:
    """Configured dialect used when no explicit."""
    assert resolve_dialect(None, "postgres") == "postgres"


def test_resolve_unknown_when_neither() -> None:
    """No dialect → 'unknown'."""
    assert resolve_dialect(None, None) == "unknown"


def test_resolve_invalid_falls_back() -> None:
    """Invalid dialect name → 'unknown'."""
    assert resolve_dialect("not_real", None) == "unknown"


def test_resolve_case_insensitive() -> None:
    """Dialect names are case-insensitive."""
    assert resolve_dialect("Postgres", None) == "postgres"
    assert resolve_dialect("MYSQL", None) == "mysql"


def test_resolve_whitespace_stripped() -> None:
    """Whitespace is stripped from dialect names."""
    assert resolve_dialect("  postgres  ", None) == "postgres"


# --- list_dialects ---

def test_list_dialects_non_empty() -> None:
    """list_dialects returns known dialects."""
    dialects = list_dialects()
    assert len(dialects) > 0
    assert "postgres" in dialects
    assert "mysql" in dialects


def test_list_dialects_sorted() -> None:
    """list_dialects returns sorted list."""
    dialects = list_dialects()
    assert dialects == sorted(dialects)


# --- is_known_dialect ---

def test_is_known_dialect() -> None:
    """is_known_dialect recognizes valid dialects."""
    assert is_known_dialect("postgres")
    assert is_known_dialect("mysql")
    assert not is_known_dialect("not_real")


# --- infer_dialect (advisory, never auto-called) ---

def test_infer_dialect_postgres_markers() -> None:
    """Postgres syntax markers detected."""
    result = infer_dialect("SELECT id::text FROM users WHERE email ILIKE '%@%'")
    assert isinstance(result, DialectInference)
    assert "postgres" in result.candidates
    assert result.rank_score > 0


def test_infer_dialect_mysql_markers() -> None:
    """MySQL syntax markers detected."""
    result = infer_dialect("SELECT * FROM users LIMIT 0, 10")
    assert "mysql" in result.candidates


def test_infer_dialect_tsql_markers() -> None:
    """TSQL syntax markers detected."""
    result = infer_dialect("SELECT TOP 10 * FROM users")
    assert "tsql" in result.candidates


def test_infer_dialect_no_markers() -> None:
    """No markers → empty candidates."""
    result = infer_dialect("SELECT 1")
    assert result.candidates == []
    assert result.rank_score == 0.0


def test_infer_dialect_empty_input() -> None:
    """Empty input → empty inference."""
    result = infer_dialect("")
    assert result.candidates == []


def test_infer_dialect_rank_score_not_probability() -> None:
    """rank_score is a heuristic score, not a probability (plan §10.3)."""
    result = infer_dialect("SELECT id::text FROM users")
    # Score should be in a reasonable range, not necessarily 0-1
    assert isinstance(result.rank_score, float)
    assert result.rank_score >= 0


def test_infer_dialect_evidence_strings() -> None:
    """Evidence strings are human-readable."""
    result = infer_dialect("SELECT id::text FROM users")
    assert len(result.evidence) > 0
    assert isinstance(result.evidence[0], str)
