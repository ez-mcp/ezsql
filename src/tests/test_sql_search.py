"""Tests for scripts.sql_search."""

from pathlib import Path

from scripts.sql_search import deepsearchsql, search_sql


def _write(path: Path, content: str) -> Path:
    """Create ``path`` (and its parents) containing ``content``; return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_search_sql_finds_nested_sql_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.sql", "SELECT 1;")
    _write(tmp_path / "sub" / "b.sql", "SELECT 2;")
    _write(tmp_path / "c.txt", "not sql")
    expected = [tmp_path / "a.sql", tmp_path / "sub" / "b.sql"]
    assert sorted(search_sql(tmp_path)) == expected


def test_search_sql_skips_default_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "keep.sql", "SELECT 1;")
    _write(tmp_path / "env" / "ignored.sql", "SELECT 1;")
    assert search_sql(tmp_path) == [tmp_path / "keep.sql"]


def test_deepsearch_matches_embedded_sql_case_insensitively(tmp_path: Path) -> None:
    _write(tmp_path / "a.sql", "SELECT 1;")
    _write(tmp_path / "app.py", "query = 'select * from users'")
    assert deepsearchsql(tmp_path) == [tmp_path / "a.sql", tmp_path / "app.py"]


def test_deepsearch_word_boundaries_do_not_match(tmp_path: Path) -> None:
    _write(tmp_path / "notes.txt", "The selection committee meets today.")
    assert deepsearchsql(tmp_path) == []


def test_deepsearch_skips_binary_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.sql", "SELECT 1;")
    (tmp_path / "img.bin").write_bytes(b"\x00\xff\xfeSELECT")
    assert deepsearchsql(tmp_path) == [tmp_path / "a.sql"]


def test_deepsearch_ignores_env_dir(tmp_path: Path) -> None:
    _write(tmp_path / "a.sql", "SELECT 1;")
    _write(tmp_path / "env" / "pkg.py", "SELECT 1")
    assert deepsearchsql(tmp_path) == [tmp_path / "a.sql"]


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert search_sql(tmp_path) == []
    assert deepsearchsql(tmp_path) == []
