"""Unit tests for host-language injection detection (plan §22.3, §15.7)."""

from ezsql.core.security.hostlang import (
    detect_concat_sql,
    detect_fstring_sql,
    detect_unsafe_execute,
    is_safe_parameterized,
)

# --- f-string detection ---

def test_fstring_sql_detected() -> None:
    """f-string with SQL keywords and interpolation → detected."""
    assert detect_fstring_sql('query = f"SELECT * FROM users WHERE id = {user_id}"')


def test_fstring_no_sql_keywords_not_detected() -> None:
    """f-string without SQL keywords → not detected."""
    assert not detect_fstring_sql('msg = f"Hello {name}"')


def test_fstring_no_interpolation_not_detected() -> None:
    """f-string with SQL keywords but no interpolation → not detected."""
    assert not detect_fstring_sql('query = f"SELECT * FROM users"')


def test_non_fstring_not_detected() -> None:
    """Regular string with SQL keywords → not detected as f-string."""
    assert not detect_fstring_sql('query = "SELECT * FROM users WHERE id = 5"')


# --- concat detection ---

def test_concat_sql_detected() -> None:
    """String concatenation with SQL keywords → detected."""
    assert detect_concat_sql('query = "SELECT * FROM " + table_name')
    assert detect_concat_sql('query = base + " WHERE id = 5"')


def test_concat_no_sql_not_detected() -> None:
    """String concatenation without SQL keywords → not detected."""
    assert not detect_concat_sql('msg = "Hello " + name')


def test_plain_string_not_detected() -> None:
    """Plain string with SQL keywords → not detected as concat."""
    assert not detect_concat_sql('query = "SELECT * FROM users"')


# --- execute() detection ---

def test_unsafe_execute_detected() -> None:
    """execute() with variable argument → detected."""
    source = """
import sqlite3
conn = sqlite3.connect(":memory:")
cursor = conn.cursor()
cursor.execute(query)
"""
    findings = detect_unsafe_execute(source)
    assert len(findings) == 1
    assert findings[0][1] == "execute"


def test_safe_execute_not_detected() -> None:
    """execute() with string literal → not flagged as unsafe."""
    source = '''
import sqlite3
cursor = sqlite3.connect(":memory:").cursor()
cursor.execute("SELECT * FROM users WHERE id = ?", (1,))
'''
    findings = detect_unsafe_execute(source)
    assert len(findings) == 0


def test_parameterized_query_is_safe() -> None:
    """Parameterized execute → is_safe_parameterized returns True."""
    source = '''
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
'''
    assert is_safe_parameterized(source)


def test_dynamic_sql_not_safe() -> None:
    """Dynamic SQL execute → is_safe_parameterized returns False."""
    source = '''
query = f"SELECT * FROM users WHERE id = {user_id}"
cursor.execute(query)
'''
    assert not is_safe_parameterized(source)


def test_syntax_error_returns_empty() -> None:
    """Syntax error in source → empty findings."""
    source = "def broken(:"
    assert detect_unsafe_execute(source) == []
    assert is_safe_parameterized(source) is False
