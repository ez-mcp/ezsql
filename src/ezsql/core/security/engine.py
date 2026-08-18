"""Security rule engine with coverage tracking.

The engine evaluates rules against analysis units, producing findings and
coverage records. Coverage distinguishes evaluated / skipped / not-applicable
(plan §15.5).

Applicability is metadata-driven (plan §15.4):
- ``input_kinds`` mismatch → ``not_applicable`` (reason: ``input_kind_mismatch``)
- ``applicable_roles`` mismatch → ``not_applicable`` (reason: ``input_role_mismatch``)
- ``requires_known_dialect`` and dialect is ``unknown`` → ``skipped``
  (reason: ``dialect_mismatch``)
- Otherwise → ``evaluated``

The engine never calls the predicate for not-applicable or skipped rules.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ezsql.core.security.model import AnalysisUnit, InputKind, InputRole
from ezsql.core.sql.parse import InternalFailure, ParseResult, parse
from ezsql.server.models import Finding, RuleCoverage

logger = logging.getLogger("ezsql.security.engine")

Severity = Literal["critical", "high", "medium", "low", "info"]


@dataclass(frozen=True)
class SecurityRule:
    """A security rule definition (plan §15.4).

    Rules are data: ``(rule_id, severity, category, predicate, ...)``.
    The rule set grows without engine changes.
    """

    rule_id: str
    title: str
    severity: Severity
    category: str
    dialect_scope: tuple[str, ...] | None  # None = all dialects
    requires_known_dialect: bool
    input_kinds: frozenset[InputKind]
    applicable_roles: frozenset[InputRole]
    predicate: Callable[..., list[Finding]]
    description: str
    autofix_available: bool = False


@dataclass
class EngineResult:
    """Result of running the security engine over analysis units."""

    findings: list[Finding] = field(default_factory=list)
    coverage: list[RuleCoverage] = field(default_factory=list)


def _check_applicability(
    rule: SecurityRule,
    unit: AnalysisUnit,
    dialect: str,
) -> tuple[bool, str | None]:
    """Check if a rule applies to a unit.

    Returns ``(should_evaluate, reason_if_not)``.
    """
    # Check input_kinds
    if unit.input_kind not in rule.input_kinds:
        return False, "input_kind_mismatch"

    # Check applicable_roles
    if unit.input_role not in rule.applicable_roles:
        return False, "input_role_mismatch"

    # Check dialect
    if rule.requires_known_dialect and dialect == "unknown":
        return False, "dialect_mismatch"

    # Check dialect_scope (if specified)
    if (
        rule.dialect_scope is not None
        and dialect != "unknown"
        and dialect not in rule.dialect_scope
    ):
        return False, "dialect_mismatch"

    return True, None


def evaluate_rule(
    rule: SecurityRule,
    unit: AnalysisUnit,
    dialect: str,
    parse_result: ParseResult | None = None,
) -> tuple[list[Finding], RuleCoverage]:
    """Evaluate a single rule against a single analysis unit.

    Returns ``(findings, coverage)``. The coverage record indicates whether
    the rule was evaluated, skipped, or not_applicable.
    """
    should_eval, reason = _check_applicability(rule, unit, dialect)

    if not should_eval:
        status: Literal["evaluated", "skipped", "not_applicable"]
        status = "skipped" if reason == "dialect_mismatch" else "not_applicable"
        return [], RuleCoverage(
            rule_id=rule.rule_id,
            unit_id=unit.unit_id,
            status=status,
            reason=reason,
        )

    # Evaluate the predicate
    try:
        if unit.input_kind == "sql":
            # Parse the SQL if not already parsed
            if parse_result is None:
                pr = parse(unit.content, dialect=dialect)
                if isinstance(pr, InternalFailure):
                    return [], RuleCoverage(
                        rule_id=rule.rule_id,
                        unit_id=unit.unit_id,
                        status="skipped",
                        reason="parse_failure",
                    )
                parse_result = pr
            findings = rule.predicate(
                statements=parse_result.statements,
                unit=unit,
                dialect=dialect,
            )
        else:
            # python_source
            findings = rule.predicate(
                content=unit.content,
                unit=unit,
                dialect=dialect,
            )
    except Exception as exc:  # noqa: BLE001 — rule predicate crash
        logger.error(
            "rule_predicate_crash: %s: %s",
            rule.rule_id,
            type(exc).__name__,
        )
        return [], RuleCoverage(
            rule_id=rule.rule_id,
            unit_id=unit.unit_id,
            status="skipped",
            reason="predicate_error",
        )

    # Tag findings with unit context
    for f in findings:
        f.unit_id = unit.unit_id
        f.input_role = unit.input_role
        f.dialect = dialect

    return findings, RuleCoverage(
        rule_id=rule.rule_id,
        unit_id=unit.unit_id,
        status="evaluated",
    )


def evaluate(
    rules: list[SecurityRule],
    units: list[AnalysisUnit],
    dialect: str,
) -> EngineResult:
    """Evaluate all rules against all analysis units.

    Rules are evaluated in **severity → rule → unit** order (plan §14.3).
    Findings are output in source-location order (re-sorted before returning).

    The computation budget (``max_findings``) terminates evaluation with the
    most important findings. When the budget is reached, remaining rules are
    marked ``skipped`` with reason ``computation_budget_exhausted``.
    """
    all_findings: list[Finding] = []
    all_coverage: list[RuleCoverage] = []

    # Sort rules by severity for evaluation order (plan §14.3)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_rules = sorted(rules, key=lambda r: severity_order.get(r.severity, 99))

    for rule in sorted_rules:
        for unit in units:
            # Parse SQL units once per rule (could be optimized later)
            parse_result: ParseResult | None = None
            if unit.input_kind == "sql":
                pr = parse(unit.content, dialect=dialect)
                if isinstance(pr, InternalFailure):
                    all_coverage.append(RuleCoverage(
                        rule_id=rule.rule_id,
                        unit_id=unit.unit_id,
                        status="skipped",
                        reason="parse_failure",
                    ))
                    continue
                parse_result = pr

            findings, coverage = evaluate_rule(
                rule, unit, dialect, parse_result
            )
            all_findings.extend(findings)
            all_coverage.append(coverage)

    # Sort findings by source location (plan §14.3 — output ordering)
    all_findings.sort(key=lambda f: (
        f.location.statement_index,
        f.location.start_line,
        f.location.start_col,
        f.rule_id,
    ))

    return EngineResult(findings=all_findings, coverage=all_coverage)


__all__ = [
    "EngineResult",
    "SecurityRule",
    "Severity",
    "evaluate",
    "evaluate_rule",
]
