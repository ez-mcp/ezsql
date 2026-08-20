"""Normalized planner plan models (plan_phase3 §2, §4).

Inward-owned contracts: both pipelines and DB adapters consume these.
``db/`` must never import ``server/`` or ``pipelines/`` (dependency rule
``server → pipelines → core + infra``).

"Runtime" evidence means **live database planner evidence**: estimated
costs/cardinality plus planning time. It is NOT observed execution time
(ANALYZE is prohibited; plan_phase3 §0 V3-8).
"""

import json
import logging
import time
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

logger = logging.getLogger("ezsql.plan")

# Plan-model version — bumps when normalization semantics change.
PLAN_MODEL_VERSION = "1"

# EXPLAIN option fingerprint: the exact envelope the adapter is allowed to
# emit. Embedded in cache keys so any change to the envelope invalidates.
EXPLAIN_OPTIONS_FINGERPRINT = "json+costs+summary+settings+pg16"

PlanConditionKind = Literal[
    "filter", "index_cond", "join_filter", "hash_cond",
    "merge_cond", "recheck_cond", "other",
]

# JSON EXPLAIN field name → typed condition kind.
_CONDITION_KINDS: dict[str, PlanConditionKind] = {
    "Filter": "filter",
    "Index Cond": "index_cond",
    "Join Filter": "join_filter",
    "Hash Cond": "hash_cond",
    "Merge Cond": "merge_cond",
    "Recheck Cond": "recheck_cond",
}

# Bounded allowlist of planner-setting names (plan_phase3 §4). SETTINGS
# output is projected through this allowlist rather than returned wholesale.
_ALLOWED_SETTINGS: frozenset[str] = frozenset({
    "enable_seqscan", "enable_indexscan", "enable_indexonlyscan",
    "enable_bitmapscan", "enable_tidscan", "enable_nestloop",
    "enable_hashjoin", "enable_mergejoin", "enable_material",
    "enable_sort", "enable_hashagg", "enable_gathermerge",
    "enable_partition_pruning", "enable_parallel_append",
    "enable_parallel_hash", "enable_partitionwise_join",
    "enable_partitionwise_aggregate", "random_page_cost",
    "seq_page_cost", "cpu_tuple_cost", "cpu_index_tuple_cost",
    "cpu_operator_cost", "effective_cache_size", "work_mem",
    "hash_mem_multiplier", "maintenance_work_mem", "max_parallel_workers",
    "max_parallel_workers_per_gather", "default_statistics_target",
    "plan_cache_mode", "jit", "from_collapse_limit", "join_collapse_limit",
})

# JSON EXPLAIN top-level keys we consume.
_KEY_PLANNING_TIME = "Planning Time"
_KEY_SETTINGS = "Settings"

# Per-node JSON keys.
_NODE_RELATION = "Relation Name"
_NODE_INDEX = "Index Name"
_NODE_STARTUP = "Startup Cost"
_NODE_TOTAL = "Total Cost"
_NODE_ROWS = "Plan Rows"
_NODE_WIDTH = "Plan Width"


class PlanCondition(BaseModel):
    """A single plan condition with typed kind and redacted expression.

    ``expression`` is literal-redacted before it reaches this model, the
    cache, tool output, or logs (plan_phase3 §4). Repeated conditions stay
    repeated list entries — never collapsed into a dict.
    """

    kind: PlanConditionKind
    expression: str


class PlanNode(BaseModel):
    """One node of the normalized plan tree."""

    op: str
    relation: str | None = None
    index: str | None = None
    startup_cost: float | None = None
    total_cost: float | None = None
    estimated_rows: int | None = None
    row_width: int | None = None
    conditions: list[PlanCondition] = Field(default_factory=list)
    children: list["PlanNode"] = Field(default_factory=list)


class ParsedPlan(BaseModel):
    """A fully normalized EXPLAIN plan."""

    root: PlanNode
    format: Literal["json"] = "json"
    planning_time_ms: float | None = None
    settings: dict[str, str] = Field(default_factory=dict)
    captured_at: float = Field(default_factory=lambda: time.time())
    truncated: bool = False
    nodes_suppressed: int = 0


class PlanSummary(BaseModel):
    """Compact summary of a plan for tool output."""

    root_op: str = "Unknown"
    root_relation: str | None = None
    root_total_cost: float | None = None
    root_estimated_rows: int | None = None
    planning_time_ms: float | None = None
    node_count: int = 0
    scan_ops: list[str] = Field(default_factory=list)
    join_ops: list[str] = Field(default_factory=list)
    truncated: bool = False


class PlanDelta(BaseModel):
    """Typed cost/cardinality delta between two plans (plan_phase3 §5).

    Differences are ``None`` when either operand is absent.
    ``cost_delta_pct`` is ``None`` when original cost is absent or ``<= 0``.
    Planning time is reported, never used to rank candidates.
    """

    original_total_cost: float | None = None
    candidate_total_cost: float | None = None
    cost_delta: float | None = None
    cost_delta_pct: float | None = None
    original_estimated_rows: int | None = None
    candidate_estimated_rows: int | None = None
    rows_delta: int | None = None
    original_planning_time_ms: float | None = None
    candidate_planning_time_ms: float | None = None
    cardinality_changed: bool = False


class PlanParseError(Exception):
    """Raised when EXPLAIN JSON cannot be normalized (fail closed)."""


def summarize_plan(plan: ParsedPlan) -> PlanSummary:
    """Build a compact summary from a normalized plan."""
    scan_ops: list[str] = []
    join_ops: list[str] = []

    def walk(node: PlanNode) -> int:
        count = 1
        op = node.op
        if "Scan" in op:
            scan_ops.append(op)
        if "Join" in op or op == "Nested Loop":
            join_ops.append(op)
        for child in node.children:
            count += walk(child)
        return count

    node_count = walk(plan.root)
    return PlanSummary(
        root_op=plan.root.op,
        root_relation=plan.root.relation,
        root_total_cost=plan.root.total_cost,
        root_estimated_rows=plan.root.estimated_rows,
        planning_time_ms=plan.planning_time_ms,
        node_count=node_count,
        scan_ops=scan_ops,
        join_ops=join_ops,
        truncated=plan.truncated,
    )


def compute_plan_delta(original: ParsedPlan, candidate: ParsedPlan) -> PlanDelta:
    """Compute a typed delta between two plans (plan_phase3 §5).

    A material root-cardinality difference sets ``cardinality_changed`` —
    the caller must add a semantic-safety warning, never a speed claim.
    """
    o_cost = original.root.total_cost
    c_cost = candidate.root.total_cost
    o_rows = original.root.estimated_rows
    c_rows = candidate.root.estimated_rows

    cost_delta = None
    cost_delta_pct = None
    if o_cost is not None and c_cost is not None:
        cost_delta = c_cost - o_cost
        if o_cost > 0:
            cost_delta_pct = (c_cost - o_cost) / o_cost * 100.0

    rows_delta = None
    if o_rows is not None and c_rows is not None:
        rows_delta = c_rows - o_rows

    # Cardinality change: both present, both > 0, and a material difference.
    # Small estimate jitter (±10%) is not material.
    cardinality_changed = False
    if o_rows is not None and c_rows is not None and o_rows > 0 and c_rows > 0:
        ratio = abs(c_rows - o_rows) / o_rows
        cardinality_changed = ratio > 0.10

    return PlanDelta(
        original_total_cost=o_cost,
        candidate_total_cost=c_cost,
        cost_delta=cost_delta,
        cost_delta_pct=cost_delta_pct,
        original_estimated_rows=o_rows,
        candidate_estimated_rows=c_rows,
        rows_delta=rows_delta,
        original_planning_time_ms=original.planning_time_ms,
        candidate_planning_time_ms=candidate.planning_time_ms,
        cardinality_changed=cardinality_changed,
    )


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def redact_literals(expression: str, max_chars: int) -> str:
    """Replace PostgreSQL literals in a plan condition with typed placeholders.

    Parses the expression as PostgreSQL and transforms literal AST nodes
    into ``<string>``, ``<number>``, ``<bytes>`` placeholders. If parsing
    fails, emits only ``<redacted-unparsed-expression>`` (fail closed) —
    never the original text (plan_phase3 §4).
    """
    try:
        ast = sqlglot.parse_one(expression, dialect="postgres")
    except Exception:  # noqa: BLE001 — any parse failure fails closed
        return "<redacted-unparsed-expression>"

    for node in ast.walk():
        if isinstance(node, exp.Literal):
            if node.is_string:
                node.replace(exp.Identifier(this="<string>", quoted=True))
            elif node.is_int or _is_number(node.name):
                node.replace(exp.Identifier(this="<number>", quoted=True))
            else:
                node.replace(exp.Identifier(this="<bytes>", quoted=True))

    rendered = ast.sql(dialect="postgres")
    if len(rendered) > max_chars:
        return rendered[:max_chars]
    return rendered


def _normalize_condition(kind_str: str, expression: Any, max_chars: int) -> PlanCondition:
    """Normalize one condition entry with literal redaction."""
    kind = _CONDITION_KINDS.get(kind_str, "other")
    if not isinstance(expression, str):
        expression = str(expression)
    return PlanCondition(
        kind=kind,
        expression=redact_literals(expression, max_chars),
    )


def _normalize_node(
    raw: dict[str, Any],
    limits: dict[str, int],
    depth: int,
    counter: list[int],
) -> PlanNode | None:
    """Recursively normalize one JSON plan node.

    Returns ``None`` when suppressed by node-count or depth bounds; the
    caller accumulates ``counter[1]`` as the suppression count.
    """
    counter[0] += 1
    if counter[0] > limits["max_nodes"] or depth > limits["max_depth"]:
        counter[1] += 1
        return None

    node_type = raw.get("Node Type", "Unknown")
    relation = raw.get(_NODE_RELATION)
    index = raw.get(_NODE_INDEX)

    def _opt_float(key: str) -> float | None:
        v = raw.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def _opt_int(key: str) -> int | None:
        v = raw.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    conditions: list[PlanCondition] = []
    for key, value in raw.items():
        if key in _CONDITION_KINDS:
            if isinstance(value, list):
                for item in value:
                    conditions.append(
                        _normalize_condition(key, item, limits["max_condition_chars"])
                    )
            elif isinstance(value, str):
                conditions.append(
                    _normalize_condition(key, value, limits["max_condition_chars"])
                )

    children: list[PlanNode] = []
    plans = raw.get("Plans", [])
    if isinstance(plans, list):
        for child in plans:
            if isinstance(child, dict):
                normalized = _normalize_node(child, limits, depth + 1, counter)
                if normalized is not None:
                    children.append(normalized)

    return PlanNode(
        op=node_type,
        relation=relation if isinstance(relation, str) else None,
        index=index if isinstance(index, str) else None,
        startup_cost=_opt_float(_NODE_STARTUP),
        total_cost=_opt_float(_NODE_TOTAL),
        estimated_rows=_opt_int(_NODE_ROWS),
        row_width=_opt_int(_NODE_WIDTH),
        conditions=conditions,
        children=children,
    )


def normalize_explain_json(
    raw_json: str,
    *,
    max_plan_nodes: int = 500,
    max_plan_depth: int = 64,
    max_plan_condition_chars: int = 1_024,
) -> ParsedPlan:
    """Normalize a PostgreSQL JSON EXPLAIN response into a ``ParsedPlan``.

    Bounds are enforced during traversal; suppressed nodes are counted in
    ``nodes_suppressed`` and ``truncated`` is set when any suppression
    occurred. Raises ``PlanParseError`` on malformed input (fail closed).
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PlanParseError(f"invalid JSON: {type(exc).__name__}") from exc

    # EXPLAIN (FORMAT JSON) returns a list with exactly one element.
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise PlanParseError("unexpected EXPLAIN JSON structure")

    top = data[0]
    plan_raw = top.get("Plan")
    if not isinstance(plan_raw, dict):
        raise PlanParseError("missing Plan object")

    limits = {
        "max_nodes": max_plan_nodes,
        "max_depth": max_plan_depth,
        "max_condition_chars": max_plan_condition_chars,
    }
    counter = [0, 0]  # [visited, suppressed]
    root = _normalize_node(plan_raw, limits, depth=1, counter=counter)
    if root is None:
        raise PlanParseError("plan root suppressed by bounds")

    planning_time = top.get(_KEY_PLANNING_TIME)
    planning_time_ms = (
        float(planning_time) if isinstance(planning_time, (int, float)) else None
    )

    settings_raw = top.get(_KEY_SETTINGS, {})
    settings: dict[str, str] = {}
    if isinstance(settings_raw, dict):
        for key, value in settings_raw.items():
            if key in _ALLOWED_SETTINGS:
                settings[key] = str(value)

    return ParsedPlan(
        root=root,
        planning_time_ms=planning_time_ms,
        settings=settings,
        truncated=counter[1] > 0,
        nodes_suppressed=counter[1],
    )


__all__ = [
    "EXPLAIN_OPTIONS_FINGERPRINT",
    "PLAN_MODEL_VERSION",
    "ParsedPlan",
    "PlanCondition",
    "PlanConditionKind",
    "PlanDelta",
    "PlanNode",
    "PlanParseError",
    "PlanSummary",
    "compute_plan_delta",
    "normalize_explain_json",
    "redact_literals",
    "summarize_plan",
]
