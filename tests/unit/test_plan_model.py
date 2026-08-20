"""Unit tests for plan normalization, redaction, and deltas (plan_phase3 §10)."""

import json

import pytest

from ezsql.core.sql.plan import (
    ParsedPlan,
    PlanNode,
    PlanParseError,
    compute_plan_delta,
    normalize_explain_json,
    redact_literals,
    summarize_plan,
)


def _plan_json(total_cost: float = 10.0, rows: int = 5, **extra: object) -> str:
    node: dict[str, object] = {
        "Node Type": "Seq Scan",
        "Relation Name": "users",
        "Startup Cost": 0.0,
        "Total Cost": total_cost,
        "Plan Rows": rows,
        "Plan Width": 212,
    }
    node.update(extra)
    return json.dumps([{"Plan": node, "Planning Time": 0.1}])


# --- Normalization ---

def test_normalize_seq_scan() -> None:
    plan = normalize_explain_json(_plan_json())
    assert plan.root.op == "Seq Scan"
    assert plan.root.relation == "users"
    assert plan.root.total_cost == 10.0
    assert plan.root.estimated_rows == 5
    assert plan.planning_time_ms == 0.1
    assert plan.truncated is False


def test_normalize_index_scan_with_conditions() -> None:
    raw = json.dumps([{
        "Plan": {
            "Node Type": "Index Scan",
            "Index Name": "users_pkey",
            "Relation Name": "users",
            "Total Cost": 8.3,
            "Index Cond": "(users.id = 42)",
            "Filter": "(users.name = 'bob')",
        },
    }])
    plan = normalize_explain_json(raw)
    kinds = {c.kind for c in plan.root.conditions}
    assert kinds == {"index_cond", "filter"}
    # Literals are redacted.
    for cond in plan.root.conditions:
        assert "42" not in cond.expression
        assert "bob" not in cond.expression


def test_normalize_nested_children() -> None:
    raw = json.dumps([{
        "Plan": {
            "Node Type": "Nested Loop",
            "Total Cost": 100.0,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "a", "Total Cost": 10.0},
                {"Node Type": "Index Scan", "Relation Name": "b", "Total Cost": 20.0,
                 "Plans": [{"Node Type": "Sort", "Total Cost": 5.0}]},
            ],
        },
    }])
    plan = normalize_explain_json(raw)
    assert plan.root.op == "Nested Loop"
    assert len(plan.root.children) == 2
    assert plan.root.children[1].children[0].op == "Sort"


def test_normalize_repeated_conditions_stay_repeated() -> None:
    raw = json.dumps([{
        "Plan": {
            "Node Type": "Seq Scan",
            "Filter": "(a = 1)",
            "Plans": [{"Node Type": "Seq Scan", "Filter": "(a = 1)"}],
        },
    }])
    plan = normalize_explain_json(raw)
    assert len(plan.root.conditions) == 1
    assert len(plan.root.children[0].conditions) == 1


def test_normalize_settings_allowlisted() -> None:
    raw = json.dumps([{
        "Plan": {"Node Type": "Seq Scan"},
        "Settings": {"work_mem": "4MB", "custom_secret_guc": "leak"},
    }])
    plan = normalize_explain_json(raw)
    assert plan.settings == {"work_mem": "4MB"}


def test_normalize_missing_planning_time() -> None:
    raw = json.dumps([{"Plan": {"Node Type": "Seq Scan"}}])
    plan = normalize_explain_json(raw)
    assert plan.planning_time_ms is None


# --- Bounds ---

def test_node_count_bound() -> None:
    # Build a chain deeper than max_nodes.
    node: dict[str, object] = {"Node Type": "Seq Scan"}
    for _ in range(10):
        node = {"Node Type": "Nested Loop", "Plans": [node]}
    raw = json.dumps([{"Plan": node}])
    plan = normalize_explain_json(raw, max_plan_nodes=5)
    assert plan.truncated is True
    assert plan.nodes_suppressed > 0


def test_depth_bound() -> None:
    node: dict[str, object] = {"Node Type": "Seq Scan"}
    for _ in range(20):
        node = {"Node Type": "Nested Loop", "Plans": [node]}
    raw = json.dumps([{"Plan": node}])
    plan = normalize_explain_json(raw, max_plan_depth=5)
    assert plan.truncated is True


def test_condition_char_bound() -> None:
    raw = json.dumps([{
        "Plan": {"Node Type": "Seq Scan", "Filter": "(a = 1 AND b = 2)"},
    }])
    plan = normalize_explain_json(raw, max_plan_condition_chars=10)
    for cond in plan.root.conditions:
        assert len(cond.expression) <= 10


def test_root_suppressed_raises() -> None:
    raw = json.dumps([{"Plan": {"Node Type": "Seq Scan"}}])
    with pytest.raises(PlanParseError):
        normalize_explain_json(raw, max_plan_nodes=0)


# --- Malformed input (fail closed) ---

@pytest.mark.parametrize("bad", [
    "not json",
    "{}",
    "[]",
    "[{}]",
    '[{"Plan": "not an object"}]',
    '[{"Other": 1}]',
])
def test_malformed_json_raises(bad: str) -> None:
    with pytest.raises(PlanParseError):
        normalize_explain_json(bad)


# --- Redaction ---

def test_redact_string_literal() -> None:
    out = redact_literals("(users.name = 'secret')", 1_024)
    assert "secret" not in out
    assert "<string>" in out


def test_redact_number_literal() -> None:
    out = redact_literals("(users.id = 42)", 1_024)
    assert "42" not in out
    assert "<number>" in out


def test_redact_unparseable_fails_closed() -> None:
    out = redact_literals("totally ((( unparseable", 1_024)
    assert out == "<redacted-unparsed-expression>"


def test_redact_mixed_literals() -> None:
    out = redact_literals("(a = 1 AND b = 'x' AND c = 3.14)", 1_024)
    assert "1" not in out.replace("<number>", "")
    assert "'x'" not in out
    assert "3.14" not in out


# --- Summary and delta ---

def test_summary_counts_nodes_and_scans() -> None:
    plan = ParsedPlan(
        root=PlanNode(
            op="Nested Loop",
            children=[
                PlanNode(op="Seq Scan"),
                PlanNode(op="Index Scan"),
            ],
        )
    )
    s = summarize_plan(plan)
    assert s.node_count == 3
    assert set(s.scan_ops) == {"Seq Scan", "Index Scan"}
    assert s.join_ops == ["Nested Loop"]


def test_delta_basic() -> None:
    p1 = normalize_explain_json(_plan_json(total_cost=100.0, rows=10))
    p2 = normalize_explain_json(_plan_json(total_cost=50.0, rows=10))
    d = compute_plan_delta(p1, p2)
    assert d.cost_delta == -50.0
    assert d.cost_delta_pct == -50.0
    assert d.cardinality_changed is False


def test_delta_cardinality_change_flagged() -> None:
    p1 = normalize_explain_json(_plan_json(rows=10))
    p2 = normalize_explain_json(_plan_json(rows=100))
    d = compute_plan_delta(p1, p2)
    assert d.cardinality_changed is True
    assert d.rows_delta == 90


def test_delta_small_jitter_not_material() -> None:
    p1 = normalize_explain_json(_plan_json(rows=100))
    p2 = normalize_explain_json(_plan_json(rows=105))
    d = compute_plan_delta(p1, p2)
    assert d.cardinality_changed is False


def test_delta_absent_costs_are_none() -> None:
    p1 = normalize_explain_json(json.dumps([{"Plan": {"Node Type": "Seq"}}]))
    p2 = normalize_explain_json(_plan_json())
    d = compute_plan_delta(p1, p2)
    assert d.cost_delta is None
    assert d.cost_delta_pct is None
    assert d.original_total_cost is None


def test_delta_zero_original_cost_no_pct_division() -> None:
    p1 = normalize_explain_json(_plan_json(total_cost=0.0))
    p2 = normalize_explain_json(_plan_json(total_cost=5.0))
    d = compute_plan_delta(p1, p2)
    assert d.cost_delta == 5.0
    assert d.cost_delta_pct is None  # no division by zero
