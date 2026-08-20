"""Schema design and DDL generation pipeline (plan §21 #6, plan_phase4 FR-2).

Deterministic-first: derives a schema proposal from requirements text
plus the existing repo schema via data-driven heuristics. Escalates to
the LLM **only** when the deterministic derivation is inconclusive
(policy (a), decision D3) and only when an API key is configured — the
escalation refines, never replaces, deterministic findings (plan §9).

Generated DDL passes the repo's own security rules before being
returned; unsafe DDL is withheld and reported with violated rule ids
(plan §16).

Caching: the deterministic skeleton is cached under the ``design``
domain; the escalation advisory is **never cached** (critical review #1
— replaying stale advice and hiding token spend).
"""

import logging
import re
from pathlib import Path
from typing import Literal

from ezsql.cache.keys import design_key
from ezsql.cache.store import CacheStore
from ezsql.config import EzsqlConfig
from ezsql.core.schema.repository import load_repo_schema
from ezsql.observability import counters
from ezsql.server.models import (
    CacheProvenance,
    DesignResult,
    EscalationResult,
    FailureEnvelope,
    ProposedColumn,
    ProposedTable,
)

logger = logging.getLogger("ezsql.pipelines.design")

__all__ = ["run_design_schema"]

# Entity-extraction heuristics (rules-are-data; grow by adding rows).
_ENTITY_HINT_RE = re.compile(
    r"\b(?:table|entity|model|store|record)\s+(?:called\s+|named\s+)?"
    r"[\"'`]?([A-Za-z][A-Za-z0-9_]*)[\"'`]?",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Conservative type mapping for common requirement phrases.
_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("uuid", "UUID"),
    ("email", "TEXT"),
    ("timestamp", "TIMESTAMPTZ"),
    ("date", "DATE"),
    ("boolean", "BOOLEAN"),
    ("json", "JSONB"),
    ("price", "NUMERIC"),
    ("amount", "NUMERIC"),
    ("count", "INTEGER"),
    ("quantity", "INTEGER"),
)


def _snake_case(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _derive_tables(requirements: str) -> list[ProposedTable]:
    """Deterministic entity extraction from requirements text."""
    tables: list[ProposedTable] = []
    seen: set[str] = set()
    for match in _ENTITY_HINT_RE.finditer(requirements):
        raw_name = match.group(1)
        name = _snake_case(raw_name)
        if not _ID_RE.match(name) or name in seen:
            continue
        seen.add(name)
        columns = [
            ProposedColumn(
                name="id",
                data_type="UUID",
                nullable=False,
                rationale="Default surrogate primary key.",
            )
        ]
        # Column hints from phrases mentioning the entity.
        for col_match in re.finditer(
            rf"\b{re.escape(raw_name)}\s+(?:has|with|contains)\s+([^.]+)",
            requirements,
            re.IGNORECASE,
        ):
            fragment = col_match.group(1)
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", fragment):
                col_name = _snake_case(token)
                if not _ID_RE.match(col_name) or col_name == "id":
                    continue
                data_type = "TEXT"
                lowered = col_name.lower()
                for hint, mapped in _TYPE_HINTS:
                    if hint in lowered:
                        data_type = mapped
                        break
                columns.append(
                    ProposedColumn(
                        name=col_name, data_type=data_type, nullable=True
                    )
                )
        tables.append(
            ProposedTable(
                name=name,
                columns=columns,
                primary_key=["id"],
                rationale=f"Extracted from requirements mention of '{raw_name}'.",
            )
        )
    return tables


def _render_ddl(tables: list[ProposedTable], dialect: str) -> list[str]:
    """Render CREATE TABLE statements for the proposal."""
    statements: list[str] = []
    for table in tables:
        lines = [f"CREATE TABLE {table.name} ("]
        col_defs = []
        for col in table.columns:
            col_def = f"  {col.name} {col.data_type}"
            if not col.nullable:
                col_def += " NOT NULL"
            col_defs.append(col_def)
        if table.primary_key:
            col_defs.append(f"  PRIMARY KEY ({', '.join(table.primary_key)})")
        for fk in table.foreign_keys:
            col_defs.append(
                f"  FOREIGN KEY ({', '.join(fk.source_columns)}) "
                f"REFERENCES {fk.target_table} ({', '.join(fk.target_columns)})"
            )
        lines.append(",\n".join(col_defs))
        lines.append(");")
        statements.append("\n".join(lines))
    return statements


def _render_mermaid_erd(tables: list[ProposedTable]) -> str | None:
    """Render a Mermaid ERD for the proposal (plan §21 #6)."""
    if not tables:
        return None
    lines = ["erDiagram"]
    for table in tables:
        if not table.columns:
            continue
        cols = " ".join(
            f"{col.data_type} {col.name}" for col in table.columns
        )
        lines.append(f'  {table.name} {{ {cols} }}')
    for table in tables:
        for fk in table.foreign_keys:
            lines.append(
                f"  {table.name} }}o--|| {fk.target_table} : {fk.source_columns[0]}"
            )
    return "\n".join(lines) if len(lines) > 1 else None


def _validate_ddl(
    ddl_statements: list[str],
    config: EzsqlConfig,
    root: Path,
    dialect: str,
) -> tuple[list[str], list[str]]:
    """Run generated DDL through the security rule engine (plan §16).

    Returns ``(safe_ddl, withheld_rule_ids)``.
    """
    from ezsql.pipelines.security import run_sql_sec

    safe: list[str] = []
    withheld: list[str] = []
    for stmt in ddl_statements:
        result = run_sql_sec(config, root, cache=None, sql=stmt, dialect=dialect)
        if isinstance(result, FailureEnvelope):
            # Validation infrastructure failure → withhold (fail closed).
            withheld.append("validation_unavailable")
            continue
        violated = [
            f.rule_id for f in result.findings if f.severity in ("critical", "high")
        ]
        if violated:
            withheld.extend(violated)
        else:
            safe.append(stmt)
    return safe, withheld


def run_design_schema(
    config: EzsqlConfig,
    root: Path,
    cache: CacheStore | None = None,
    *,
    requirements: str,
    dialect: str | None = None,
    task: str | None = None,
) -> DesignResult | FailureEnvelope:
    """Run the design_schema pipeline (plan_phase4 FR-2).

    Args:
        config: The loaded EZSQL config.
        root: The resolved project root.
        cache: Optional cache store.
        requirements: Natural-language schema requirements.
        dialect: Optional explicit dialect.
        task: Optional task ID (registry wiring, FR-8).

    Returns:
        ``DesignResult`` on success, or ``FailureEnvelope`` on failure.
    """
    # Note: tool invocation counters are owned by the server wrappers
    # (plan_phase3 §11); the pipeline owns domain events only.
    counters.inc("design_requests", 1)

    if len(requirements.encode("utf-8")) > config.max_requirements_bytes:
        return FailureEnvelope(
            kind="input_too_large",
            detail=(
                f"Requirements exceed max_requirements_bytes "
                f"({config.max_requirements_bytes})"
            ),
            recoverable=True,
            next_steps=["Reduce the requirements text size."],
        )
    if not requirements.strip():
        return FailureEnvelope(
            kind="no_input",
            detail="Empty requirements.",
            recoverable=True,
            next_steps=["Describe the entities and relationships you need."],
        )

    resolved_dialect = dialect or config.default_dialect

    schema_load = load_repo_schema(root, config, cache)
    fingerprint = (
        schema_load.fingerprint if schema_load.schema is not None else None
    )

    key = design_key(requirements, resolved_dialect, fingerprint)
    escalation: EscalationResult | None = None
    if cache is not None:
        cached = cache.get(key, DesignResult)
        if cached is not None:
            counters.inc("cache_hits", 1)
            logger.info("design_cache_hit")
            cached.cache_provenance = CacheProvenance(cache_hit=True, cache_key=key)
            # Advisory is never cached: re-derive per call if inconclusive.
            if cached.derivation_status == "inconclusive":
                escalation = _maybe_escalate(requirements, cached, config)
                if escalation is not None:
                    cached.escalation = escalation
            _record_task_ref(task, key)
            return cached

    counters.inc("cache_misses", 1)

    tables = _derive_tables(requirements)
    tables_truncated = False
    tables_suppressed = 0
    if len(tables) > config.max_design_tables:
        tables_suppressed = len(tables) - config.max_design_tables
        tables = tables[: config.max_design_tables]
        tables_truncated = True

    if tables:
        derivation_status: Literal["derived", "inconclusive"] = "derived"
    else:
        derivation_status = "inconclusive"

    ddl_statements: list[str] = []
    withheld_rule_ids: list[str] = []
    if tables:
        raw_ddl = _render_ddl(tables, resolved_dialect)
        ddl_statements, withheld_rule_ids = _validate_ddl(
            raw_ddl, config, root, resolved_dialect
        )

    risks: list[str] = []
    if schema_load.schema is not None:
        for table in tables:
            if table.name in schema_load.schema.tables:
                risks.append(
                    f"Proposed table '{table.name}' already exists in the repo "
                    f"schema (source: repo-ddl)."
                )
    if derivation_status == "inconclusive":
        risks.append(
            "Deterministic derivation was inconclusive; the proposal is "
            "empty. See escalation advisory if available."
        )

    migration_strategy = [
        "Apply CREATE TABLE statements in dependency order (referenced tables first).",
        "Add foreign keys after both tables exist.",
        "Prefer additive migrations; EZSQL never executes DDL itself.",
    ] if tables else []

    result = DesignResult(
        dialect=resolved_dialect,
        tables=tables,
        generated_ddl=ddl_statements,
        migration_strategy=migration_strategy,
        risks=risks,
        mermaid_erd=_render_mermaid_erd(tables),
        ddl_withheld_rule_ids=withheld_rule_ids,
        derivation_status=derivation_status,
        schema_source="repo-ddl" if schema_load.schema is not None else "none",
        tables_truncated=tables_truncated,
        tables_suppressed=tables_suppressed,
        cache_provenance=CacheProvenance(cache_hit=False, cache_key=key),
    )

    # Escalation trigger — policy (a): only when inconclusive (D3).
    if derivation_status == "inconclusive":
        escalation = _maybe_escalate(requirements, result, config)
        if escalation is not None:
            result.escalation = escalation

    if cache is not None:
        # Advisory is never cached: strip before storing (critical review #1).
        cached_copy = result.model_copy(deep=True)
        cached_copy.escalation = EscalationResult()
        cache.put(key, "design", cached_copy)
    logger.info(
        "design_complete",
        extra={
            "tables": len(tables),
            "derivation_status": derivation_status,
            "withheld": len(withheld_rule_ids),
        },
    )
    _record_task_ref(task, key)
    return result


def _maybe_escalate(
    requirements: str,
    result: DesignResult,
    config: EzsqlConfig,
) -> EscalationResult | None:
    """Escalate when deterministic derivation is inconclusive (policy a).

    Returns ``None`` when escalation should not run at all (e.g. the
    deterministic result is conclusive — the caller guards on
    ``derivation_status``).
    """
    from ezsql.llm.escalate import escalate

    prompt_parts = [
        "Propose a database schema (tables, columns, types, foreign keys) "
        "for these requirements. Output concise DDL-ready guidance only.",
        requirements[: config.max_requirements_bytes],
    ]
    if result.tables:
        prompt_parts.append(
            "Existing deterministic proposal (refine, do not replace): "
            + ", ".join(t.name for t in result.tables)
        )
    return escalate(prompt_parts, config.llm_token_budget, config=config)


def _record_task_ref(task: str | None, cache_key: str) -> None:
    """Attach the result to the task registry (plan_phase4 FR-8)."""
    if task is None:
        return
    from ezsql.tasks.registry import get_registry

    registry = get_registry()
    registry.get_or_create(task)
    registry.add_ref(task, cache_key, "design")
