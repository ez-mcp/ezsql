"""Security rule definitions and predicates (plan §15.6).

Initial rule set:
- SEC-001: f-string interpolation with SQL keywords (python_source, medium)
- SEC-002: String concatenation producing SQL (python_source, medium)
- SEC-003: DROP TABLE (sql, high)
- SEC-004: TRUNCATE TABLE (sql, high)
- SEC-005: DELETE without WHERE (sql, high)
- SEC-006: UPDATE without WHERE (sql, high)
- SEC-007: DROP TABLE in migration context (sql, medium)
- SEC-008: ALTER TABLE DROP COLUMN (sql, medium)
- SEC-009: EXECUTE/EXEC with dynamic SQL (sql, medium, requires_known_dialect)

Host-language injection findings (SEC-001, SEC-002) are:
- ``evidence: static`` (source code structurally contains the pattern)
- ``kind: inference`` (we infer it *may* be unsafe; not modeling input trust)
- ``severity: medium`` (not critical — haven't proven exploit path)
- Message: "Detected potentially unsafe dynamic SQL construction."
"""

from typing import TYPE_CHECKING

from sqlglot import exp

from ezsql.core.schema.model import SourceSpan
from ezsql.core.security.engine import SecurityRule
from ezsql.core.security.hostlang import (
    detect_concat_sql,
    detect_fstring_sql,
)
from ezsql.server.models import Finding

if TYPE_CHECKING:
    from ezsql.core.security.model import AnalysisUnit, InputRole

# Rule IDs
SEC_INJECTION_FSTRING = "SEC-001"
SEC_INJECTION_CONCAT = "SEC-002"
SEC_DROP_TABLE = "SEC-003"
SEC_TRUNCATE = "SEC-004"
SEC_DELETE_NO_WHERE = "SEC-005"
SEC_UPDATE_NO_WHERE = "SEC-006"
SEC_MIGRATION_DROP = "SEC-007"
SEC_MIGRATION_DROP_COLUMN = "SEC-008"
SEC_DYNAMIC_SQL = "SEC-009"

# Security ruleset version (for cache key invalidation).
SECURITY_RULESET_VERSION = "1"


def _make_span(statement_index: int = 0, line: int = 1, col: int = 1) -> SourceSpan:
    """Create a SourceSpan for a finding."""
    return SourceSpan(
        statement_index=statement_index,
        start_line=line,
        start_col=col,
        end_line=line,
        end_col=col,
    )


def _get_meta_span(node: exp.Expr, statement_index: int) -> SourceSpan:
    """Extract source position from a sqlglot AST node."""
    meta = node.meta if hasattr(node, "meta") else {}
    line = meta.get("line", 1)
    col = meta.get("col", 1)
    return _make_span(statement_index, line, col)


# --- SEC-001: f-string interpolation with SQL keywords ---

def _predicate_sec001(
    content: str,
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect f-string interpolation with SQL keywords."""
    findings: list[Finding] = []
    for line_no, line in enumerate(content.splitlines(), 1):
        if detect_fstring_sql(line):
            findings.append(Finding(
                rule_id=SEC_INJECTION_FSTRING,
                title="f-string SQL construction",
                severity="medium",
                message="Detected potentially unsafe dynamic SQL construction "
                        "(f-string interpolation with SQL keywords).",
                location=_make_span(0, line_no, 1),
                evidence="static",
                kind="inference",
            ))
    return findings


# --- SEC-002: String concatenation producing SQL ---

def _predicate_sec002(
    content: str,
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect string concatenation producing SQL."""
    findings: list[Finding] = []
    for line_no, line in enumerate(content.splitlines(), 1):
        if detect_concat_sql(line):
            findings.append(Finding(
                rule_id=SEC_INJECTION_CONCAT,
                title="String concatenation SQL construction",
                severity="medium",
                message="Detected potentially unsafe dynamic SQL construction "
                        "(string concatenation with SQL keywords).",
                location=_make_span(0, line_no, 1),
                evidence="static",
                kind="inference",
            ))
    return findings


# --- SEC-003: DROP TABLE ---

def _predicate_sec003(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect DROP TABLE (destructive schema operation)."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        if isinstance(stmt, exp.Drop):
            kind = stmt.args.get("kind")
            if kind and str(kind).upper() == "TABLE":
                has_if_exists = bool(stmt.args.get("exists"))
                message = "DROP TABLE (destructive schema operation)"
                if has_if_exists:
                    message += (
                        " — IF EXISTS present (suppresses error, does not prevent destruction)"
                    )
                findings.append(Finding(
                    rule_id=SEC_DROP_TABLE,
                    title="DROP TABLE",
                    severity="high",
                    message=message,
                    location=_get_meta_span(stmt, i),
                    evidence="static",
                    kind="fact",
                    fix_suggestion="Review whether the table drop is intentional. "
                                   "Consider renaming or archiving instead.",
                ))
    return findings


# --- SEC-004: TRUNCATE TABLE ---

def _predicate_sec004(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect TRUNCATE TABLE (destructive data operation)."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        if isinstance(stmt, exp.TruncateTable):
            findings.append(Finding(
                rule_id=SEC_TRUNCATE,
                title="TRUNCATE TABLE",
                severity="high",
                message="TRUNCATE TABLE (destructive data operation — removes all rows).",
                location=_get_meta_span(stmt, i),
                evidence="static",
                kind="fact",
            ))
    return findings


# --- SEC-005: DELETE without WHERE ---

def _predicate_sec005(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect DELETE without WHERE (unbounded deletion)."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        if isinstance(stmt, exp.Delete):
            where = stmt.args.get("where")
            if where is None:
                findings.append(Finding(
                    rule_id=SEC_DELETE_NO_WHERE,
                    title="DELETE without WHERE",
                    severity="high",
                    message="DELETE without WHERE clause (unbounded deletion).",
                    location=_get_meta_span(stmt, i),
                    evidence="static",
                    kind="fact",
                    fix_suggestion="Add a WHERE clause to limit the scope of deletion.",
                ))
    return findings


# --- SEC-006: UPDATE without WHERE ---

def _predicate_sec006(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect UPDATE without WHERE (unbounded update)."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        if isinstance(stmt, exp.Update):
            where = stmt.args.get("where")
            if where is None:
                findings.append(Finding(
                    rule_id=SEC_UPDATE_NO_WHERE,
                    title="UPDATE without WHERE",
                    severity="high",
                    message="UPDATE without WHERE clause (unbounded update).",
                    location=_get_meta_span(stmt, i),
                    evidence="static",
                    kind="fact",
                    fix_suggestion="Add a WHERE clause to limit the scope of the update.",
                ))
    return findings


# --- SEC-007: DROP TABLE in migration context ---

def _predicate_sec007(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect DROP TABLE in migration context (irreversible)."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        if isinstance(stmt, exp.Drop):
            kind = stmt.args.get("kind")
            if kind and str(kind).upper() == "TABLE":
                findings.append(Finding(
                    rule_id=SEC_MIGRATION_DROP,
                    title="DROP TABLE in migration",
                    severity="medium",
                    message="DROP TABLE in migration context (irreversible — "
                            "data loss cannot be rolled back after migration applies).",
                    location=_get_meta_span(stmt, i),
                    evidence="static",
                    kind="fact",
                ))
    return findings


# --- SEC-008: ALTER TABLE DROP COLUMN ---

def _predicate_sec008(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect ALTER TABLE DROP COLUMN (data loss)."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        if isinstance(stmt, exp.Alter):
            actions = stmt.args.get("actions")
            if actions:
                for action in actions:
                    if isinstance(action, exp.Drop):
                        # Check if it's a column drop
                        action_kind = action.args.get("kind")
                        if action_kind and str(action_kind).upper() == "COLUMN":
                            col_name = action.this.name if action.this else "unknown"
                            findings.append(Finding(
                                rule_id=SEC_MIGRATION_DROP_COLUMN,
                                title="ALTER TABLE DROP COLUMN",
                                severity="medium",
                                message=f"ALTER TABLE DROP COLUMN {col_name} "
                                        f"(data loss — column data is destroyed).",
                                location=_get_meta_span(stmt, i),
                                evidence="static",
                                kind="fact",
                            ))
    return findings


# --- SEC-009: EXECUTE/EXEC with dynamic SQL ---

def _predicate_sec009(
    statements: list[exp.Expr],
    unit: "AnalysisUnit",
    dialect: str,
    **kwargs: object,
) -> list[Finding]:
    """Detect EXECUTE/EXEC with dynamically constructed SQL."""
    findings: list[Finding] = []
    for i, stmt in enumerate(statements):
        # Check for Execute statement (sqlglot exp.Execute)
        if isinstance(stmt, exp.Execute):
            findings.append(Finding(
                rule_id=SEC_DYNAMIC_SQL,
                title="EXECUTE statement",
                severity="medium",
                message="EXECUTE statement detected. Verify the SQL is not "
                        "dynamically constructed from untrusted input.",
                location=_get_meta_span(stmt, i),
                evidence="static",
                kind="inference",
            ))
    return findings


# --- Rule definitions ---

ALL_ROLES: frozenset[InputRole] = frozenset({"query", "migration", "script"})
MIGRATION_ONLY: frozenset[InputRole] = frozenset({"migration"})

RULES: list[SecurityRule] = [
    SecurityRule(
        rule_id=SEC_INJECTION_FSTRING,
        title="f-string SQL construction",
        severity="medium",
        category="injection",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"python_source"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec001,
        description="Detects f-string interpolation containing SQL keywords.",
    ),
    SecurityRule(
        rule_id=SEC_INJECTION_CONCAT,
        title="String concatenation SQL construction",
        severity="medium",
        category="injection",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"python_source"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec002,
        description="Detects string concatenation producing SQL.",
    ),
    SecurityRule(
        rule_id=SEC_DROP_TABLE,
        title="DROP TABLE",
        severity="high",
        category="dangerous_statement",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"sql"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec003,
        description="Detects DROP TABLE (destructive schema operation).",
    ),
    SecurityRule(
        rule_id=SEC_TRUNCATE,
        title="TRUNCATE TABLE",
        severity="high",
        category="dangerous_statement",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"sql"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec004,
        description="Detects TRUNCATE TABLE (destructive data operation).",
    ),
    SecurityRule(
        rule_id=SEC_DELETE_NO_WHERE,
        title="DELETE without WHERE",
        severity="high",
        category="dangerous_statement",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"sql"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec005,
        description="Detects DELETE without WHERE (unbounded deletion).",
    ),
    SecurityRule(
        rule_id=SEC_UPDATE_NO_WHERE,
        title="UPDATE without WHERE",
        severity="high",
        category="dangerous_statement",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"sql"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec006,
        description="Detects UPDATE without WHERE (unbounded update).",
    ),
    SecurityRule(
        rule_id=SEC_MIGRATION_DROP,
        title="DROP TABLE in migration",
        severity="medium",
        category="migration_safety",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"sql"}),
        applicable_roles=MIGRATION_ONLY,
        predicate=_predicate_sec007,
        description="Detects DROP TABLE in migration context (irreversible).",
    ),
    SecurityRule(
        rule_id=SEC_MIGRATION_DROP_COLUMN,
        title="ALTER TABLE DROP COLUMN",
        severity="medium",
        category="migration_safety",
        dialect_scope=None,
        requires_known_dialect=False,
        input_kinds=frozenset({"sql"}),
        applicable_roles=MIGRATION_ONLY,
        predicate=_predicate_sec008,
        description="Detects ALTER TABLE DROP COLUMN (data loss).",
    ),
    SecurityRule(
        rule_id=SEC_DYNAMIC_SQL,
        title="EXECUTE/EXEC with dynamic SQL",
        severity="medium",
        category="dynamic_sql",
        dialect_scope=None,
        requires_known_dialect=True,
        input_kinds=frozenset({"sql"}),
        applicable_roles=ALL_ROLES,
        predicate=_predicate_sec009,
        description="Detects EXECUTE/EXEC statements (potential dynamic SQL).",
    ),
]


def get_rules() -> list[SecurityRule]:
    """Return the current security rule set."""
    return RULES


__all__ = [
    "ALL_ROLES",
    "MIGRATION_ONLY",
    "SECURITY_RULESET_VERSION",
    "SEC_DELETE_NO_WHERE",
    "SEC_DROP_TABLE",
    "SEC_DYNAMIC_SQL",
    "SEC_INJECTION_CONCAT",
    "SEC_INJECTION_FSTRING",
    "SEC_MIGRATION_DROP",
    "SEC_MIGRATION_DROP_COLUMN",
    "SEC_TRUNCATE",
    "SEC_UPDATE_NO_WHERE",
    "RULES",
    "get_rules",
]
