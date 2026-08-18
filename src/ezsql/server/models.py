"""Pydantic input and output models for EZSQL MCP server.

Phase 2 updates (plan §9):
- ``Finding`` gains two-dimensional evidence model (``evidence`` × ``kind``)
  and ``schema_source`` (first-class schema dependency).
- ``SourceSpan`` replaces string locations with structured source positions.
- ``SecurityScanResult`` with ``RuleCoverage`` — ``[]`` findings ≠ secure.
- ``RewriteCandidate`` gains ``validation_status``, ``security_status``,
  ``preconditions``, ``schema_dependency``.
- ``SqlAnalysis`` gains truncation fields for structural collections.
- ``SchemaModel`` is now imported from ``core/schema/model.py`` (canonical).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from ezsql.core.schema.model import (
    ColumnDef,
    ConstraintDef,
    ForeignKeyDef,
    IndexDef,
    ParserWarning,
    SchemaCapability,
    SchemaModel,
    TableDef,
)
from ezsql.core.schema.model import (
    SourceSpan as SchemaSourceSpan,
)

# File classification type (plan §5.8 — confirmed §17 Q4).
FileClassification = Literal[
    "migration", "query", "orm", "config", "doc", "unknown"
]

# Re-export SourceSpan from schema model for convenience (it's the canonical
# definition used by both schema warnings and findings).
SourceSpan = SchemaSourceSpan


class FailureEnvelope(BaseModel):
    """Uniform typed failure envelope (plan §18)."""

    ok: Literal[False] = False
    kind: str
    detail: str
    recoverable: bool = True
    next_steps: list[str] = Field(default_factory=list)


class ContextFile(BaseModel):
    """A single file in the context map with its classification."""

    name: str
    classification: FileClassification


class ScanMetadata(BaseModel):
    """Metadata about a scan operation (plan §23 ContextMap).

    ``files_manifest`` is a freshness guard (plan §14 — "mtime+size fast
    guard"): ``{relative_path: [mtime_ns, size]}`` for every matched file.
    On a cache hit, the pipeline recomputes the manifest and compares; a
    mismatch invalidates the cached entry. Stored as a list-of-lists (not
    tuples) for JSON round-trip safety via pydantic.
    """

    files_seen: int = 0
    files_skipped: int = 0
    truncated: bool = False
    scan_root: str = ""
    files_manifest: dict[str, list[int]] = Field(default_factory=dict)


class CacheProvenance(BaseModel):
    """Cache hit/miss provenance for a tool result."""

    cache_hit: bool = False
    cache_key: str = ""


class Finding(BaseModel):
    """A single rule finding with two-dimensional evidence (plan §9.2).

    ``evidence`` describes where the evidence came from (static/schema/runtime).
    ``kind`` describes what kind of claim is being made (fact/inference).
    These are independent dimensions.

    ``schema_source`` is ``none`` for ``static`` findings. For ``schema``
    findings, it identifies where the schema came from.
    """

    rule_id: str
    title: str = ""
    severity: Literal["critical", "high", "medium", "low", "info"]
    message: str
    location: SourceSpan = Field(default_factory=SourceSpan)
    evidence: Literal["static", "schema", "runtime"] = "static"
    kind: Literal["fact", "inference"] = "fact"
    schema_source: Literal["repo-ddl", "introspection", "none"] = "none"
    unit_id: str | None = None
    input_role: Literal["query", "migration", "script"] | None = None
    fix_suggestion: str | None = None
    dialect: str = "unknown"


class RuleCoverage(BaseModel):
    """Coverage tracking for a single rule on a single analysis unit (plan §9.3)."""

    rule_id: str
    unit_id: str
    status: Literal["evaluated", "skipped", "not_applicable"]
    reason: str | None = None


class SecurityScanResult(BaseModel):
    """Security scan result with explicit coverage (plan §9.3).

    Named ``SecurityScanResult`` (not ``SecurityAssessment``) — boring
    terminology that doesn't imply broader assessment than was performed.
    ``[]`` findings with ``coverage`` showing ``evaluated`` rules means
    "checks ran and found nothing," not "secure."
    """

    findings: list[Finding] = Field(default_factory=list)
    coverage: list[RuleCoverage] = Field(default_factory=list)
    ruleset_version: str = "1"
    input_mode: Literal["sql", "files", "mixed"] = "sql"
    input_role_resolved: Literal["query", "migration", "script", "mixed"] = "query"
    truncated: bool = False
    suppressed_count: int = 0
    cache_provenance: CacheProvenance = Field(default_factory=CacheProvenance)


class RewriteCandidate(BaseModel):
    """A rewrite candidate with forward-compatible metadata (plan §9.4).

    ``plan_delta`` is **always ``None``** in Phase 2. No estimated cost
    changes. No "approximate" performance claims.
    """

    original_hash: str
    rewritten_sql: str
    transformations: list[str] = Field(default_factory=list)
    evidence: Literal["static", "schema", "runtime"] = "static"
    plan_delta: dict[str, Any] | None = None
    source_span: SourceSpan | None = None
    preconditions: list[str] = Field(default_factory=list)
    schema_dependency: str | None = None
    dialect: str = "unknown"
    validation_status: Literal["validated", "withheld", "failed"] = "validated"
    security_status: Literal["unchecked", "passed", "withheld"] = "unchecked"


class OptimizeResult(BaseModel):
    """Optimization analysis result (plan §9, §16).

    Findings are ordered by source location (statement_index, line, col).
    ``plan_delta`` on candidates is always ``None`` in Phase 2.
    """

    dialect: str = "unknown"
    findings: list[Finding] = Field(default_factory=list)
    candidates: list[RewriteCandidate] = Field(default_factory=list)
    schema_source: Literal["repo-ddl", "introspection", "none"] = "none"
    truncated: bool = False
    suppressed_count: int = 0
    candidates_truncated: bool = False
    candidates_suppressed: int = 0
    cache_provenance: CacheProvenance = Field(default_factory=CacheProvenance)


class SqlAnalysis(BaseModel):
    """AST facts and lint findings for SQL statements (plan §9.6).

    Extraction semantics:
    - ``tables``: all ``exp.Table`` nodes, by name (not alias).
    - ``columns``: all ``exp.Column`` nodes, by name (not qualified).
      ``SELECT *`` does not add a column entry.
    - ``joins``: all ``exp.Join`` nodes, rendered as SQL strings.
    - ``predicates``: all conditions in WHERE and HAVING, rendered as SQL.
    - ``statements``: each parsed statement rendered back to SQL.

    Finding order: findings within ``lint_findings`` are ordered by
    ``(statement_index, start_line, start_col, rule_id)``.
    """

    dialect: str = "unknown"
    statements: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    lint_findings: list[Finding] = Field(default_factory=list)
    schema_source: Literal["repo-ddl", "introspection", "none"] = "none"
    # Truncation fields for structural collections (§11.4.1):
    tables_truncated: bool = False
    tables_suppressed: int = 0
    columns_truncated: bool = False
    columns_suppressed: int = 0
    joins_truncated: bool = False
    joins_suppressed: int = 0
    predicates_truncated: bool = False
    predicates_suppressed: int = 0
    statements_truncated: bool = False
    statements_suppressed: int = 0
    cache_provenance: CacheProvenance = Field(default_factory=CacheProvenance)


class Plan(BaseModel):
    """Normalized query execution plan."""

    dialect: str = "postgres"
    root: dict[str, Any] = Field(default_factory=dict)


class ContextMap(BaseModel):
    """Grouped context map of repository files (plan §23).

    ``files_by_dir`` maps directory path (relative to root, ``"."`` for
    root-level) to a list of classified files.
    """

    files_by_dir: dict[str, list[ContextFile]] = Field(default_factory=dict)
    scan_metadata: ScanMetadata = Field(default_factory=ScanMetadata)
    cache_provenance: CacheProvenance = Field(default_factory=CacheProvenance)


class TaskState(BaseModel):
    """Task continuity state."""

    task_id: str
    created_at: float
    ttl: float
    context_refs: list[str] = Field(default_factory=list)


class EscalationResult(BaseModel):
    """LLM escalation output."""

    used: bool = False
    tokens: int = 0
    advisory_text: str | None = None
    status: Literal["ok", "unavailable", "failed", "budget_exhausted"] = "unavailable"


# Re-export schema model types for convenience (they're canonical in
# core/schema/model.py but consumers often import from server/models.py).
__all__ = [
    "CacheProvenance",
    "ColumnDef",
    "ConstraintDef",
    "ContextFile",
    "ContextMap",
    "EscalationResult",
    "FailureEnvelope",
    "FileClassification",
    "Finding",
    "ForeignKeyDef",
    "IndexDef",
    "OptimizeResult",
    "ParserWarning",
    "Plan",
    "RewriteCandidate",
    "RuleCoverage",
    "ScanMetadata",
    "SchemaCapability",
    "SchemaModel",
    "SecurityScanResult",
    "SourceSpan",
    "SqlAnalysis",
    "TableDef",
    "TaskState",
]
