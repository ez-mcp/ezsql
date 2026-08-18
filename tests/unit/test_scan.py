"""Unit tests for context scanning and classification."""

from pathlib import Path

from ezsql.core.context.scan import (
    ScanResult,
    build_file_manifest,
    classify_file,
    deepsearchsql,
    scan_with_classification,
)


def test_deepsearchsql_basic(tmp_path: Path) -> None:
    """Verify deepsearchsql discovers .sql files and files with SQL keywords."""
    sql_file = tmp_path / "schema.sql"
    sql_file.write_text("CREATE TABLE users (id INT);", encoding="utf-8")

    py_file = tmp_path / "queries.py"
    py_file.write_text("query = 'SELECT * FROM users'", encoding="utf-8")

    ignored_file = tmp_path / "plain.txt"
    ignored_file.write_text("Hello world without sql", encoding="utf-8")

    result = deepsearchsql(tmp_path)
    assert isinstance(result, ScanResult)
    assert "." in result.by_dir
    assert "schema.sql" in result.by_dir["."]
    assert "queries.py" in result.by_dir["."]
    assert "plain.txt" not in result.by_dir["."]


def test_classify_migration(tmp_path: Path) -> None:
    """Migration files are classified correctly."""
    f = tmp_path / "001_init.sql"
    f.write_text("CREATE TABLE users (id INT);", encoding="utf-8")
    assert classify_file(f.name, f) == "migration"

    f2 = tmp_path / "V1__create_users.sql"
    f2.write_text("CREATE TABLE users (id INT);", encoding="utf-8")
    assert classify_file(f2.name, f2) == "migration"


def test_classify_query(tmp_path: Path) -> None:
    """Non-migration .sql files are classified as 'query'."""
    f = tmp_path / "get_users.sql"
    f.write_text("SELECT * FROM users;", encoding="utf-8")
    assert classify_file(f.name, f) == "query"


def test_classify_orm(tmp_path: Path) -> None:
    """Python files with ORM markers are classified as 'orm'."""
    f = tmp_path / "models.py"
    f.write_text(
        "from sqlalchemy import Column, Integer\n"
        "class User: id = Column(Integer, primary_key=True)",
        encoding="utf-8",
    )
    assert classify_file(f.name, f) == "orm"


def test_classify_config(tmp_path: Path) -> None:
    """Config files are classified correctly."""
    f = tmp_path / "config.toml"
    f.write_text("[db]\nurl = 'postgres://localhost'", encoding="utf-8")
    assert classify_file(f.name, f) == "config"

    f2 = tmp_path / "app.yaml"
    f2.write_text("db: postgres://localhost", encoding="utf-8")
    assert classify_file(f2.name, f2) == "config"


def test_classify_doc(tmp_path: Path) -> None:
    """Doc files are classified correctly."""
    f = tmp_path / "README.md"
    f.write_text("# Project\nSome docs about SELECT statements", encoding="utf-8")
    assert classify_file(f.name, f) == "doc"


def test_classify_unknown_sql_keyword(tmp_path: Path) -> None:
    """Non-ORM text files with SQL keywords are 'unknown'."""
    f = tmp_path / "notes.txt"
    f.write_text("Remember to SELECT the right columns", encoding="utf-8")
    assert classify_file(f.name, f) == "unknown"


def test_classify_none_for_plain(tmp_path: Path) -> None:
    """Plain text files without SQL keywords return None."""
    f = tmp_path / "plain.txt"
    f.write_text("Hello world without sql", encoding="utf-8")
    assert classify_file(f.name, f) is None


def test_classify_lock_file_skipped(tmp_path: Path) -> None:
    """Lock files are not classified (return None)."""
    f = tmp_path / "package-lock.json"
    f.write_text('{"name": "app"}', encoding="utf-8")
    assert classify_file(f.name, f) is None


def test_scan_with_classification(tmp_path: Path) -> None:
    """scan_with_classification returns classified file lists."""
    (tmp_path / "001_init.sql").write_text("CREATE TABLE x (id INT);", encoding="utf-8")
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column\nclass X: pass", encoding="utf-8"
    )
    (tmp_path / "config.toml").write_text("[db]", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("no sql here", encoding="utf-8")

    result = scan_with_classification(tmp_path)
    assert isinstance(result, ScanResult)
    assert "." in result.by_dir
    names_classes = dict(result.by_dir["."])
    assert names_classes["001_init.sql"] == "migration"
    assert names_classes["models.py"] == "orm"
    assert names_classes["config.toml"] == "config"
    assert "plain.txt" not in names_classes


def test_skip_dirs_pruned(tmp_path: Path) -> None:
    """Skip directories are pruned before descent."""
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "mod.sql").write_text(
        "SELECT 1;", encoding="utf-8"
    )
    (tmp_path / "real.sql").write_text("SELECT 1;", encoding="utf-8")
    result = deepsearchsql(tmp_path)
    assert "real.sql" in result.by_dir.get(".", [])
    assert "node_modules" not in result.by_dir


def test_large_file_skipped(tmp_path: Path) -> None:
    """Files over max_file_size are skipped for content reading (T2.1)."""
    # .sql files are matched by name, so they still appear
    (tmp_path / "big.sql").write_text("SELECT 1;", encoding="utf-8")
    # Large non-.sql file with SQL keyword — should be skipped
    big_content = "SELECT " + "x" * 2048
    (tmp_path / "big.txt").write_text(big_content, encoding="utf-8")
    result = deepsearchsql(tmp_path, max_file_size=512)
    assert "big.sql" in result.by_dir.get(".", [])
    assert "big.txt" not in result.by_dir.get(".", [])
    # Oversized file is counted as skipped (Gap 3 fix)
    assert result.files_skipped >= 1


def test_binary_file_skipped(tmp_path: Path) -> None:
    """Binary files are skipped (T2.4)."""
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02SELECT\x00\x03")
    (tmp_path / "text.sql").write_text("SELECT 1;", encoding="utf-8")
    result = deepsearchsql(tmp_path)
    assert "text.sql" in result.by_dir.get(".", [])
    assert "binary.bin" not in result.by_dir.get(".", [])
    # Binary file is counted as skipped (Gap 3 fix)
    assert result.files_skipped >= 1


def test_max_files_truncation(tmp_path: Path) -> None:
    """Scan stops at max_files_per_scan (T2.2)."""
    for i in range(10):
        (tmp_path / f"f{i}.sql").write_text("SELECT 1;", encoding="utf-8")
    result = deepsearchsql(tmp_path, max_files_per_scan=3)
    total_files = sum(len(v) for v in result.by_dir.values())
    assert total_files <= 3
    # Truncation flag is set (Gap 3 fix)
    assert result.truncated is True


def test_symlink_not_followed(tmp_path: Path) -> None:
    """Symlinks are not followed during scan (T1.3)."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "linked.sql").write_text("SELECT 1;", encoding="utf-8")
    link = tmp_path / "linkdir"
    link.symlink_to(target)
    result = deepsearchsql(tmp_path)
    # The symlinked dir should not be traversed
    all_dirs = list(result.by_dir.keys())
    assert "linkdir" not in all_dirs

def test_scan_result_no_truncation_when_under_limits(tmp_path: Path) -> None:
    """truncated is False and files_skipped is 0 when no limits are hit."""
    (tmp_path / "a.sql").write_text("SELECT 1;", encoding="utf-8")
    result = scan_with_classification(tmp_path)
    assert result.truncated is False
    assert result.files_skipped == 0


def test_scan_files_skipped_counts_oversized(tmp_path: Path) -> None:
    """files_skipped increments for oversized non-.sql files (Gap 3)."""
    (tmp_path / "ok.sql").write_text("SELECT 1;", encoding="utf-8")
    big_content = "SELECT " + "x" * 2048
    (tmp_path / "big.txt").write_text(big_content, encoding="utf-8")
    result = scan_with_classification(tmp_path, max_file_size=512)
    assert result.files_skipped >= 1
    assert result.truncated is False


def test_build_file_manifest_basic(tmp_path: Path) -> None:
    """build_file_manifest returns mtime+size for SQL-relevant files (Gap 2)."""
    (tmp_path / "schema.sql").write_text("CREATE TABLE x (id INT);", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("no sql here", encoding="utf-8")
    manifest = build_file_manifest(tmp_path)
    assert "schema.sql" in manifest
    assert "plain.txt" not in manifest
    mtime_ns, size = manifest["schema.sql"]
    assert size > 0
    assert mtime_ns > 0


def test_build_file_manifest_detects_change(tmp_path: Path) -> None:
    """Manifest changes when a file's mtime/size changes (Gap 2 freshness)."""
    import os
    f = tmp_path / "schema.sql"
    f.write_text("CREATE TABLE x (id INT);", encoding="utf-8")
    m1 = build_file_manifest(tmp_path)
    # Modify the file and bump mtime
    f.write_text("CREATE TABLE y (id INT, name TEXT);", encoding="utf-8")
    # Ensure mtime advances (filesystem granularity can be coarse)
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    m2 = build_file_manifest(tmp_path)
    assert m1["schema.sql"] != m2["schema.sql"]