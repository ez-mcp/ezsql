"""Pydantic input and output models for EZSQL MCP server."""

from typing import Any, Literal

from pydantic import BaseModel, Field

# File classification type (plan §5.8 — confirmed §17 Q4).
FileClassification = Literal[
    "migration", "query", "orm", "config", "doc", "unknown"
]


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


class SqlAnalysis(BaseModel):
    """AST facts and lint findings for SQL statements."""

    dialect: str = "postgres"
    statements: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    lint_findings: list[dict[str, Any]] = Field(default_factory=list)


class Finding(BaseModel):
    """Evidence-tiered rule finding."""

    rule_id: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    message: str
    location: str | None = None
    fix_suggestion: str | None = None
    evidence_tier: Literal["explain-verified", "heuristic", "unverified"] = "heuristic"


class RewriteCandidate(BaseModel):
    """Optimization rewrite candidate."""

    original_hash: str
    rewritten_sql: str
    transformations: list[str] = Field(default_factory=list)
    evidence_tier: Literal["explain-verified", "heuristic", "unverified"] = "heuristic"
    plan_delta: dict[str, Any] | None = None


class SchemaModel(BaseModel):
    """Canonical schema model representation."""

    tables: dict[str, Any] = Field(default_factory=dict)
    fk_graph: dict[str, list[str]] = Field(default_factory=dict)
    source: Literal["repo-ddl", "introspection"] = "repo-ddl"
    migration_set_hash: str = ""


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


__all__ = [
    "CacheProvenance",
    "ContextFile",
    "ContextMap",
    "EscalationResult",
    "FailureEnvelope",
    "FileClassification",
    "Finding",
    "Plan",
    "RewriteCandidate",
    "ScanMetadata",
    "SchemaModel",
    "SqlAnalysis",
    "TaskState",
]
