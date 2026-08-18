"""Pipeline tests for sql_sec (plan §22.3)."""

from pathlib import Path

from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.pipelines.security import run_sql_sec
from ezsql.server.models import FailureEnvelope, SecurityScanResult


def test_sql_sec_sql_mode_drop_table() -> None:
    """sql= mode: DROP TABLE → SEC-003 finding."""
    config = EzsqlConfig()
    result = run_sql_sec(config, Path("/tmp"), sql="DROP TABLE users", dialect="postgres")
    assert isinstance(result, SecurityScanResult)
    assert any(f.rule_id == "SEC-003" for f in result.findings)
    assert result.input_mode == "sql"


def test_sql_sec_sql_mode_delete_no_where() -> None:
    """sql= mode: DELETE without WHERE → SEC-005 finding."""
    config = EzsqlConfig()
    result = run_sql_sec(config, Path("/tmp"), sql="DELETE FROM users", dialect="postgres")
    assert isinstance(result, SecurityScanResult)
    assert any(f.rule_id == "SEC-005" for f in result.findings)


def test_sql_sec_empty_findings_with_coverage() -> None:
    """[] findings with evaluated coverage ≠ secure (plan §15.10)."""
    config = EzsqlConfig()
    result = run_sql_sec(config, Path("/tmp"), sql="SELECT 1", dialect="postgres")
    assert isinstance(result, SecurityScanResult)
    assert len(result.findings) == 0
    assert len(result.coverage) > 0
    # At least some rules should be evaluated
    evaluated = [c for c in result.coverage if c.status == "evaluated"]
    assert len(evaluated) > 0


def test_sql_sec_no_input() -> None:
    """No sql or files → FailureEnvelope."""
    config = EzsqlConfig()
    result = run_sql_sec(config, Path("/tmp"))
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "no_input"


def test_sql_sec_input_too_large() -> None:
    """Oversized SQL → FailureEnvelope."""
    config = EzsqlConfig()
    config.max_sql_input_bytes = 10
    result = run_sql_sec(config, Path("/tmp"), sql="SELECT * FROM users", dialect="postgres")
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "input_too_large"


def test_sql_sec_cache_hit(tmp_path: Path) -> None:
    """Second call with cache returns cached result."""
    config = EzsqlConfig()
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    result1 = run_sql_sec(config, tmp_path, cache=cache, sql="DROP TABLE t", dialect="postgres")
    assert isinstance(result1, SecurityScanResult)
    assert not result1.cache_provenance.cache_hit

    result2 = run_sql_sec(config, tmp_path, cache=cache, sql="DROP TABLE t", dialect="postgres")
    assert isinstance(result2, SecurityScanResult)
    assert result2.cache_provenance.cache_hit

    cache.close()


def test_sql_sec_files_mode(tmp_path: Path) -> None:
    """files= mode: reads files and analyzes them."""
    # Create a test SQL file
    sql_file = tmp_path / "001_init.sql"
    sql_file.write_text("DROP TABLE users", encoding="utf-8")

    config = EzsqlConfig()
    result = run_sql_sec(config, tmp_path, files=[str(sql_file)], dialect="postgres")
    assert isinstance(result, SecurityScanResult)
    assert result.input_mode == "files"
    # Migration file → SEC-007 should fire (DROP TABLE in migration)
    assert any(f.rule_id == "SEC-007" for f in result.findings)


def test_sql_sec_path_traversal_rejected(tmp_path: Path) -> None:
    """Path outside root → FailureEnvelope (plan §21.2)."""
    config = EzsqlConfig()
    result = run_sql_sec(config, tmp_path, files=["/etc/passwd"], dialect="postgres")
    assert isinstance(result, FailureEnvelope)
    assert result.kind in ("path_outside_root", "not_a_file", "file_unreadable")
