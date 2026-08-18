"""Pipeline tests for optimize_query (plan §22.3)."""

from pathlib import Path

from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.schema.model import (
    ColumnDef,
    SchemaModel,
    TableDef,
)
from ezsql.pipelines.optimize import run_optimize_query
from ezsql.server.models import FailureEnvelope, OptimizeResult


def _make_schema() -> SchemaModel:
    """Create a schema with a users table."""
    return SchemaModel(
        tables={
            "users": TableDef(
                name="users",
                columns={
                    "id": ColumnDef(name="id", data_type="INT", nullable=False),
                    "email": ColumnDef(name="email", data_type="VARCHAR(255)"),
                    "name": ColumnDef(name="name", data_type="TEXT"),
                },
            ),
        },
    )


def test_optimize_query_select_star() -> None:
    """SELECT * → OPT-001 finding + rewrite candidate."""
    schema = _make_schema()
    config = EzsqlConfig()
    result = run_optimize_query(
        "SELECT * FROM users", config, dialect="postgres", schema=schema
    )
    assert isinstance(result, OptimizeResult)
    assert any(f.rule_id == "OPT-001" for f in result.findings)
    assert len(result.candidates) == 1
    assert result.candidates[0].validation_status == "validated"
    assert result.candidates[0].plan_delta is None  # always None in Phase 2


def test_optimize_query_no_schema_no_rewrite() -> None:
    """Without schema, SELECT * finding fires but no rewrite candidate."""
    config = EzsqlConfig()
    result = run_optimize_query("SELECT * FROM users", config, dialect="postgres")
    assert isinstance(result, OptimizeResult)
    assert any(f.rule_id == "OPT-001" for f in result.findings)
    assert len(result.candidates) == 0


def test_optimize_query_parse_error() -> None:
    """Invalid SQL → FailureEnvelope."""
    config = EzsqlConfig()
    result = run_optimize_query("SELECT FROM WHERE", config, dialect="postgres")
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "parse_error"


def test_optimize_query_input_too_large() -> None:
    """Oversized input → FailureEnvelope."""
    config = EzsqlConfig()
    config.max_sql_input_bytes = 10
    result = run_optimize_query("SELECT * FROM users", config, dialect="postgres")
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "input_too_large"


def test_optimize_query_cache_hit(tmp_path: Path) -> None:
    """Second call with cache returns cached result."""
    config = EzsqlConfig()
    cache = CacheStore(tmp_path, max_entries=10, max_size_mb=1)

    result1 = run_optimize_query("SELECT 1", config, cache=cache, dialect="postgres")
    assert isinstance(result1, OptimizeResult)
    assert not result1.cache_provenance.cache_hit

    result2 = run_optimize_query("SELECT 1", config, cache=cache, dialect="postgres")
    assert isinstance(result2, OptimizeResult)
    assert result2.cache_provenance.cache_hit

    cache.close()


def test_optimize_query_plan_delta_always_none() -> None:
    """plan_delta is always None in Phase 2 (exit criterion §23.14)."""
    schema = _make_schema()
    config = EzsqlConfig()
    result = run_optimize_query(
        "SELECT * FROM users", config, dialect="postgres", schema=schema
    )
    assert isinstance(result, OptimizeResult)
    for candidate in result.candidates:
        assert candidate.plan_delta is None


def test_optimize_query_correlated_subquery() -> None:
    """Correlated subquery → OPT-002 finding."""
    config = EzsqlConfig()
    sql = "SELECT * FROM t1 WHERE x > (SELECT AVG(y) FROM t2 WHERE t2.id = t1.id)"
    result = run_optimize_query(sql, config, dialect="postgres")
    assert isinstance(result, OptimizeResult)
    assert any(f.rule_id == "OPT-002" for f in result.findings)
