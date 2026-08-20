"""Unit tests for the bounded repository schema loader (plan_phase3 §10)."""

from pathlib import Path

from ezsql.config import EzsqlConfig
from ezsql.core.schema.repository import load_repo_schema


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_from_migrations_dir(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/001_init.sql",
           "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);")
    _write(tmp_path, "migrations/002_add_orders.sql",
           "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT);")

    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is not None
    assert "users" in result.schema.tables
    assert "orders" in result.schema.tables
    assert result.fingerprint
    assert result.unavailable_reason is None


def test_no_migrations_dir(tmp_path: Path) -> None:
    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is None
    assert result.unavailable_reason is not None
    assert "no migration directory" in result.unavailable_reason


def test_ambiguous_roots(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/001_a.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path, "db/migrations/001_b.sql", "CREATE TABLE b (id INT);")
    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is None
    assert "ambiguous" in (result.unavailable_reason or "")


def test_empty_migrations_dir(tmp_path: Path) -> None:
    (tmp_path / "migrations").mkdir()
    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is None
    assert result.unavailable_reason is not None


def test_symlinked_migration_skipped(tmp_path: Path) -> None:
    """Symlinks are never followed (§6)."""
    real = tmp_path / "real.sql"
    real.write_text("CREATE TABLE a (id INT);")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    link = migrations / "001_link.sql"
    link.symlink_to(real)

    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is None  # only the symlink existed → no files


def test_file_count_bound(tmp_path: Path) -> None:
    config = EzsqlConfig()
    config.max_schema_files = 3
    for i in range(5):
        _write(tmp_path, f"migrations/{i:03d}_m.sql", "CREATE TABLE t (id INT);")
    result = load_repo_schema(tmp_path, config)
    assert result.schema is None
    assert "max_schema_files" in (result.unavailable_reason or "")


def test_per_file_byte_bound(tmp_path: Path) -> None:
    config = EzsqlConfig()
    config.max_schema_file_bytes = 10
    _write(tmp_path, "migrations/001_big.sql", "CREATE TABLE t (id INT); -- " + "x" * 100)
    result = load_repo_schema(tmp_path, config)
    assert result.schema is None
    assert "max_schema_file_bytes" in (result.unavailable_reason or "")


def test_total_byte_bound(tmp_path: Path) -> None:
    config = EzsqlConfig()
    config.max_schema_total_bytes = 30
    for i in range(5):
        _write(tmp_path, f"migrations/{i:03d}_m.sql",
               "CREATE TABLE t (id INT); -- padding padding")
    result = load_repo_schema(tmp_path, config)
    assert result.schema is None
    assert "max_schema_total_bytes" in (result.unavailable_reason or "")


def test_skip_dirs_pruned(tmp_path: Path) -> None:
    """Skip dirs (env, node_modules, ...) are pruned before descent."""
    _write(tmp_path, "migrations/001_ok.sql", "CREATE TABLE ok (id INT);")
    _write(tmp_path, "migrations/node_modules/002_bad.sql",
           "CREATE TABLE bad (id INT);")
    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is not None
    assert "ok" in result.schema.tables
    assert "bad" not in result.schema.tables


def test_fingerprint_changes_on_content_change(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/001_a.sql", "CREATE TABLE a (id INT);")
    r1 = load_repo_schema(tmp_path, EzsqlConfig())
    assert r1.schema is not None

    _write(tmp_path, "migrations/001_a.sql", "CREATE TABLE a (id INT, x INT);")
    r2 = load_repo_schema(tmp_path, EzsqlConfig())
    assert r2.schema is not None
    assert r1.fingerprint != r2.fingerprint


def test_fingerprint_changes_on_file_addition(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/001_a.sql", "CREATE TABLE a (id INT);")
    r1 = load_repo_schema(tmp_path, EzsqlConfig())

    _write(tmp_path, "migrations/002_b.sql", "CREATE TABLE b (id INT);")
    r2 = load_repo_schema(tmp_path, EzsqlConfig())
    assert r1.fingerprint != r2.fingerprint


def test_cache_roundtrip(tmp_path: Path) -> None:
    from ezsql.cache.store import CacheStore

    _write(tmp_path, "migrations/001_a.sql", "CREATE TABLE a (id INT);")
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    r1 = load_repo_schema(tmp_path, EzsqlConfig(), cache)
    assert r1.cache_hit is False
    assert r1.schema is not None

    r2 = load_repo_schema(tmp_path, EzsqlConfig(), cache)
    assert r2.cache_hit is True
    assert r2.schema is not None
    assert "a" in r2.schema.tables

    cache.close()


def test_ambiguous_conventions_reported(tmp_path: Path) -> None:
    """Mixed migration naming conventions → explicit unavailable reason."""
    _write(tmp_path, "migrations/001_a.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path, "migrations/V1__b.sql", "CREATE TABLE b (id INT);")
    result = load_repo_schema(tmp_path, EzsqlConfig())
    assert result.schema is None
    assert result.unavailable_reason is not None
