"""Pipeline-level tests for find_context."""

from pathlib import Path

from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.pipelines.context import run_find_context
from ezsql.server.models import ContextMap


def test_run_find_context(tmp_path: Path) -> None:
    """Verify run_find_context produces a valid ContextMap with classifications."""
    (tmp_path / "001_init.sql").write_text(
        "CREATE TABLE items (id SERIAL PRIMARY KEY);", encoding="utf-8"
    )
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column\nclass Item: pass", encoding="utf-8"
    )
    (tmp_path / "plain.txt").write_text("no sql here", encoding="utf-8")

    config = EzsqlConfig()
    result = run_find_context(tmp_path, config)

    assert isinstance(result, ContextMap)
    assert "." in result.files_by_dir
    files = {f.name: f.classification for f in result.files_by_dir["."]}
    assert files["001_init.sql"] == "migration"
    assert files["models.py"] == "orm"
    assert "plain.txt" not in files
    assert result.scan_metadata.files_seen == 2
    assert result.scan_metadata.scan_root == str(tmp_path)
    # Gap 3: cache_provenance populated (miss on first call)
    assert result.cache_provenance.cache_hit is False
    assert result.cache_provenance.cache_key != ""
    # Gap 2: files_manifest populated for freshness guard
    assert len(result.scan_metadata.files_manifest) > 0


def test_find_context_cache_hit(tmp_path: Path) -> None:
    """Second call with cache returns cached result (freshness passes)."""
    (tmp_path / "schema.sql").write_text("SELECT 1;", encoding="utf-8")

    config = EzsqlConfig()
    cache = CacheStore(tmp_path, max_entries=4, max_size_mb=1)

    # First call — cache miss
    result1 = run_find_context(tmp_path, config, cache=cache)
    assert not result1.cache_provenance.cache_hit

    # Second call — cache hit (files unchanged → freshness passes)
    result2 = run_find_context(tmp_path, config, cache=cache)
    assert result2.files_by_dir == result1.files_by_dir
    # Gap 3: cache_hit flag propagated on hit
    assert result2.cache_provenance.cache_hit is True
    assert result2.cache_provenance.cache_key != ""

    cache.close()


def test_find_context_cache_freshness_invalidates(tmp_path: Path) -> None:
    """File change after caching → cache miss (Gap 2 freshness guard)."""
    import os

    (tmp_path / "schema.sql").write_text("SELECT 1;", encoding="utf-8")
    config = EzsqlConfig()
    cache = CacheStore(tmp_path, max_entries=4, max_size_mb=1)

    # First call — populates cache
    result1 = run_find_context(tmp_path, config, cache=cache)
    assert not result1.cache_provenance.cache_hit

    # Modify the file and bump mtime so the manifest differs
    f = tmp_path / "schema.sql"
    f.write_text("SELECT 2;", encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    # Second call — freshness mismatch → re-scan (miss)
    result2 = run_find_context(tmp_path, config, cache=cache)
    assert not result2.cache_provenance.cache_hit

    cache.close()


def test_find_context_no_cache(tmp_path: Path) -> None:
    """Works without cache (cache=None)."""
    (tmp_path / "schema.sql").write_text("SELECT 1;", encoding="utf-8")
    config = EzsqlConfig()
    result = run_find_context(tmp_path, config, cache=None)
    assert isinstance(result, ContextMap)
    assert "." in result.files_by_dir


def test_find_context_task_noop(tmp_path: Path) -> None:
    """task parameter is accepted but does not affect behavior (§17 Q7)."""
    (tmp_path / "schema.sql").write_text("SELECT 1;", encoding="utf-8")
    config = EzsqlConfig()

    result_with_task = run_find_context(tmp_path, config, task="my-task")
    result_without_task = run_find_context(tmp_path, config, task=None)

    # Same result regardless of task
    assert result_with_task.files_by_dir == result_without_task.files_by_dir


def test_find_context_truncation(tmp_path: Path) -> None:
    """truncated flag set when max_files_per_scan is hit (T2.2).

    Note: max_files_per_scan is clamped to [100, 500000] (T4.3), so we
    create 101 files and set the limit to 100.
    """
    for i in range(101):
        (tmp_path / f"f{i:03d}.sql").write_text("SELECT 1;", encoding="utf-8")

    config = EzsqlConfig(max_files_per_scan=100)
    result = run_find_context(tmp_path, config)
    # Scan stops at 100 files
    total = sum(len(v) for v in result.files_by_dir.values())
    assert total <= 100
    # Gap 3: truncated flag propagated to ScanMetadata
    assert result.scan_metadata.truncated is True


def test_find_context_empty_repo(tmp_path: Path) -> None:
    """Empty repo returns empty ContextMap."""
    config = EzsqlConfig()
    result = run_find_context(tmp_path, config)
    assert isinstance(result, ContextMap)
    assert len(result.files_by_dir) == 0
    assert result.scan_metadata.files_seen == 0


def test_find_context_files_skipped_propagated(tmp_path: Path) -> None:
    """files_skipped propagates to ScanMetadata (Gap 3)."""
    (tmp_path / "ok.sql").write_text("SELECT 1;", encoding="utf-8")
    # Oversized non-.sql file with SQL keyword — skipped by size cap
    big_content = "SELECT " + "x" * 2048
    (tmp_path / "big.txt").write_text(big_content, encoding="utf-8")
    config = EzsqlConfig(max_file_size=512)
    result = run_find_context(tmp_path, config)
    assert result.scan_metadata.files_skipped >= 1


def test_find_context_load_config_applied(tmp_path: Path) -> None:
    """Per-call load_config applies scan limits from .ezsql/config.toml (Gap 1)."""
    from ezsql.config import load_config

    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    # Set a low max_files_per_scan to trigger truncation
    (config_dir / "config.toml").write_text(
        "[ezsql]\nmax_files_per_scan = 100\n", encoding="utf-8"
    )
    for i in range(101):
        (tmp_path / f"f{i:03d}.sql").write_text("SELECT 1;", encoding="utf-8")

    # load_config is what tools.py calls per-call; verify it applies
    call_config = load_config(tmp_path)
    assert call_config.max_files_per_scan == 100
    result = run_find_context(tmp_path, call_config)
    assert result.scan_metadata.truncated is True
