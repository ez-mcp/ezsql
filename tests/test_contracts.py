"""Model contract tests (plan §22.2).

These tests verify that the outward-facing models have the required fields
and properties for Phase 2 and forward compatibility with Phase 3+.
"""

from pydantic import BaseModel

from ezsql.server.models import (
    Finding,
    OptimizeResult,
    RewriteCandidate,
    RuleCoverage,
    SchemaModel,
    SecurityScanResult,
    SourceSpan,
    SqlAnalysis,
)


def test_finding_has_evidence_and_kind() -> None:
    """Finding has two-dimensional evidence model (plan §22.2.1)."""
    f = Finding(
        rule_id="TEST",
        severity="info",
        message="test",
        evidence="static",
        kind="fact",
    )
    assert f.evidence == "static"
    assert f.kind == "fact"


def test_finding_schema_source_exists() -> None:
    """Finding.schema_source is first-class (plan §22.2.1)."""
    f = Finding(
        rule_id="TEST",
        severity="info",
        message="test",
        schema_source="repo-ddl",
    )
    assert f.schema_source == "repo-ddl"
    assert "schema_source" in Finding.model_fields


def test_source_span_has_file_field() -> None:
    """SourceSpan has file field for source scanning (plan §22.2.1)."""
    span = SourceSpan(file="test.sql")
    assert span.file == "test.sql"
    assert "file" in SourceSpan.model_fields


def test_rewrite_candidate_plan_delta_defaults_none() -> None:
    """RewriteCandidate.plan_delta defaults to None (plan §22.2.1)."""
    c = RewriteCandidate(
        original_hash="abc",
        rewritten_sql="SELECT 1",
    )
    assert c.plan_delta is None


def test_rewrite_candidate_validation_status_exists() -> None:
    """RewriteCandidate.validation_status exists (plan §22.2.1)."""
    c = RewriteCandidate(
        original_hash="abc",
        rewritten_sql="SELECT 1",
        validation_status="validated",
    )
    assert c.validation_status == "validated"


def test_schema_model_foreign_keys_typed() -> None:
    """SchemaModel.foreign_keys is list[ForeignKeyDef] (plan §22.2.1)."""
    from ezsql.core.schema.model import ForeignKeyDef
    sm = SchemaModel()
    sm.foreign_keys.append(ForeignKeyDef(
        source_table="a",
        source_columns=["id"],
        target_table="b",
        target_columns=["id"],
    ))
    assert len(sm.foreign_keys) == 1
    assert isinstance(sm.foreign_keys[0], ForeignKeyDef)


def test_schema_model_parser_warnings_have_completeness() -> None:
    """SchemaModel.parser_warnings items have affects_schema_completeness (plan §22.2.1)."""
    from ezsql.core.schema.model import ParserWarning
    w = ParserWarning(
        kind="test",
        location=SourceSpan(),
        message="test",
    )
    assert hasattr(w, "affects_schema_completeness")
    assert w.affects_schema_completeness is False  # default


def test_schema_model_version_exists() -> None:
    """SchemaModel.schema_model_version exists (plan §22.2.1)."""
    sm = SchemaModel()
    assert hasattr(sm, "schema_model_version")
    assert sm.schema_model_version != ""


def test_security_scan_result_has_coverage() -> None:
    """SecurityScanResult has coverage with status enum (plan §22.2.1)."""
    r = SecurityScanResult(
        findings=[],
        coverage=[
            RuleCoverage(rule_id="SEC-001", unit_id="sql:0", status="evaluated"),
        ],
    )
    assert len(r.coverage) == 1
    assert r.coverage[0].status == "evaluated"


def test_security_scan_result_has_truncated_and_suppressed() -> None:
    """SecurityScanResult has truncated and suppressed_count (plan §22.2.1)."""
    r = SecurityScanResult(truncated=True, suppressed_count=5)
    assert r.truncated is True
    assert r.suppressed_count == 5


def test_models_serialize_deserialize_roundtrip() -> None:
    """All models serialize/deserialize round-trip (plan §22.2.1 — cache safety)."""
    models = [
        Finding(rule_id="TEST", severity="info", message="test"),
        SecurityScanResult(),
        OptimizeResult(),
        SqlAnalysis(),
        RewriteCandidate(original_hash="abc", rewritten_sql="SELECT 1"),
    ]
    for model in models:
        assert isinstance(model, BaseModel)
        json = model.model_dump_json()
        cls = type(model)
        restored = cls.model_validate_json(json)
        assert type(restored) is cls


# --- Upgradeability tests (plan §22.5) ---

def test_rewrite_candidate_evidence_accepts_runtime() -> None:
    """RewriteCandidate.evidence accepts 'runtime' for Phase 3 EXPLAIN upgrade."""
    c = RewriteCandidate(
        original_hash="abc",
        rewritten_sql="SELECT 1",
        evidence="runtime",
    )
    assert c.evidence == "runtime"


def test_rewrite_candidate_plan_delta_is_typed() -> None:
    """RewriteCandidate.plan_delta is a typed PlanDelta (Phase 3, plan_phase3 §9).

    The old forward-compatibility assertion (arbitrary dict) is removed:
    plan_phase3 §9 explicitly replaces it with typed round trips.
    """
    from ezsql.core.sql.plan import PlanDelta

    c = RewriteCandidate(
        original_hash="abc",
        rewritten_sql="SELECT 1",
        plan_delta=PlanDelta(
            original_total_cost=100.0,
            candidate_total_cost=50.0,
            cost_delta=-50.0,
            cost_delta_pct=-50.0,
        ),
    )
    assert c.plan_delta is not None
    assert c.plan_delta.original_total_cost == 100.0
    assert c.plan_delta.cost_delta == -50.0


def test_schema_model_source_accepts_introspection() -> None:
    """SchemaModel.source field accepts 'introspection' for Phase 5."""
    sm = SchemaModel(source="introspection")
    assert sm.source == "introspection"


def test_finding_evidence_accepts_runtime() -> None:
    """Finding.evidence accepts 'runtime' for Phase 3 EXPLAIN upgrade."""
    f = Finding(
        rule_id="TEST",
        severity="info",
        message="test",
        evidence="runtime",
    )
    assert f.evidence == "runtime"
