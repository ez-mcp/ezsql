"""Pipeline tests for analyze_sql (plan §22.3)."""

from pathlib import Path

from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.pipelines.analyze import run_analyze_sql
from ezsql.server.models import FailureEnvelope, SqlAnalysis


def test_analyze_sql_basic() -> None:
    """Basic analyze_sql returns SqlAnalysis with AST facts."""
    config = EzsqlConfig()
    result = run_analyze_sql("SELECT id, name FROM users WHERE id = 1", config, dialect="postgres")
    assert isinstance(result, SqlAnalysis)
    assert "users" in result.tables
    assert "id" in result.columns
    assert "name" in result.columns
    assert result.dialect == "postgres"


def test_analyze_sql_select_star() -> None:
    """SELECT * produces OPT-001 finding."""
    config = EzsqlConfig()
    result = run_analyze_sql("SELECT * FROM users", config, dialect="postgres")
    assert isinstance(result, SqlAnalysis)
    opt001 = [f for f in result.lint_findings if f.rule_id == "OPT-001"]
    assert len(opt001) == 1


def test_analyze_sql_parse_error() -> None:
    """Invalid SQL returns FailureEnvelope with parse_error."""
    config = EzsqlConfig()
    result = run_analyze_sql("SELECT FROM WHERE", config, dialect="postgres")
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "parse_error"


def test_analyze_sql_input_too_large() -> None:
    """Oversized input returns FailureEnvelope."""
    config = EzsqlConfig()
    config.max_sql_input_bytes = 10
    result = run_analyze_sql("SELECT * FROM users", config, dialect="postgres")
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "input_too_large"


def test_analyze_sql_cache_hit(tmp_path: Path) -> None:
    """Second call with cache returns cached result."""
    config = EzsqlConfig()
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    result1 = run_analyze_sql("SELECT 1", config, cache=cache, dialect="postgres")
    assert isinstance(result1, SqlAnalysis)
    assert not result1.cache_provenance.cache_hit

    result2 = run_analyze_sql("SELECT 1", config, cache=cache, dialect="postgres")
    assert isinstance(result2, SqlAnalysis)
    assert result2.cache_provenance.cache_hit

    cache.close()


def test_analyze_sql_multi_statement() -> None:
    """Multi-statement SQL is parsed correctly."""
    config = EzsqlConfig()
    result = run_analyze_sql("SELECT 1; SELECT 2;", config, dialect="postgres")
    assert isinstance(result, SqlAnalysis)
    assert len(result.statements) == 2


def test_analyze_sql_empty() -> None:
    """Empty SQL returns empty SqlAnalysis."""
    config = EzsqlConfig()
    result = run_analyze_sql("", config, dialect="postgres")
    assert isinstance(result, SqlAnalysis)
    assert len(result.statements) == 0
