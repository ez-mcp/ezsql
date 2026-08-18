"""Canonical schema model types with provenance and per-object lossiness.

The schema model is the most important subsystem in Phase 2. It is consumed
by all downstream phases. If it's wrong, everything downstream is wrong.

Key design decisions (plan §13):
- ``foreign_keys: list[ForeignKeyDef]`` is the primary FK representation
  (typed, column-level). ``fk_graph`` is derived from it for convenience.
- ``ParserWarning`` has per-object ``compromised_capabilities`` so consumers
  can decide whether their specific claim is affected.
- ``schema_model_version`` invalidates caches when model interpretation changes.
- ``ColumnDef.raw_default`` stores the raw SQL expression, not canonical semantics.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Schema capabilities that EZSQL claims can depend on (plan §13.5).
SchemaCapability = Literal[
    "column_enumeration",
    "column_type",
    "index_enumeration",
    "index_structure",
    "constraint_enumeration",
    "fk_structure",
    "table_existence",
]


class SourceSpan(BaseModel):
    """Source location for a finding or warning.

    ``statement_index`` is 0-based. When ``file`` is present (sql_sec files
    mode), it is per-file: statement 0 is the first statement in that file.
    """

    statement_index: int = 0
    start_line: int = 1
    start_col: int = 1
    end_line: int = 1
    end_col: int = 1
    file: str | None = None


class ForeignKeyDef(BaseModel):
    """Typed foreign key relationship (plan §13.1)."""

    constraint_name: str | None = None
    source_table: str
    source_columns: list[str]
    target_table: str
    target_columns: list[str]


class ParserWarning(BaseModel):
    """Per-object parser warning with source span and capability info.

    ``affects_schema_completeness`` is True iff ``compromised_capabilities``
    is non-empty. A warning with ``affects_schema_completeness=False`` does
    not compromise any schema capability that Phase 2 claims depend on.
    """

    kind: str
    location: SourceSpan
    object_name: str | None = None
    message: str
    affects_schema_completeness: bool = False
    compromised_capabilities: frozenset[SchemaCapability] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def _validate_completeness(self) -> "ParserWarning":
        """Ensure affects_schema_completeness == bool(compromised_capabilities)."""
        expected = bool(self.compromised_capabilities)
        if self.affects_schema_completeness != expected:
            self.affects_schema_completeness = expected
        return self


class ColumnDef(BaseModel):
    """A column definition with raw type and default (plan §13.1)."""

    name: str
    data_type: str
    nullable: bool = True
    raw_default: str | None = None


class IndexDef(BaseModel):
    """An index definition (plan §13.1).

    Phase 2: partial/expression indexes are detected but not deeply modeled.
    If either field is True, OPT-004 treats the index as "not obviously usable."
    """

    name: str
    columns: list[str]
    unique: bool = False
    is_partial: bool = False
    is_expression: bool = False
    raw_definition: str | None = None


class ConstraintDef(BaseModel):
    """A table constraint (plan §13.1)."""

    name: str | None = None
    type: Literal["primary_key", "foreign_key", "unique", "check", "not_null"]
    columns: list[str]
    references_table: str | None = None
    references_columns: list[str] = Field(default_factory=list)


class TableDef(BaseModel):
    """A table definition with columns, indexes, and constraints."""

    name: str
    columns: dict[str, ColumnDef]
    indexes: dict[str, IndexDef] = Field(default_factory=dict)
    constraints: list[ConstraintDef] = Field(default_factory=list)


class SchemaModel(BaseModel):
    """Canonical schema model with provenance and per-object lossiness.

    The schema model is producer-agnostic. Phase 2 provides the repo-DDL
    producer. Live introspection is a later second producer (Phase 5).
    """

    tables: dict[str, TableDef] = Field(default_factory=dict)
    foreign_keys: list[ForeignKeyDef] = Field(default_factory=list)
    source: Literal["repo-ddl", "introspection"] = "repo-ddl"
    source_files: list[str] = Field(default_factory=list)
    parser_warnings: list[ParserWarning] = Field(default_factory=list)
    schema_model_version: str = "1"
    warnings_truncated: bool = False
    warnings_suppressed: int = 0

    @property
    def fk_graph(self) -> dict[str, list[str]]:
        """Derived FK adjacency graph (convenience, not persisted).

        Maps source_table → [target_table, ...]. Carries less information
        than ``foreign_keys`` — downstream phases need column-level FK info.
        """
        graph: dict[str, list[str]] = {}
        for fk in self.foreign_keys:
            graph.setdefault(fk.source_table, []).append(fk.target_table)
        return graph


__all__ = [
    "ColumnDef",
    "ConstraintDef",
    "ForeignKeyDef",
    "IndexDef",
    "ParserWarning",
    "SchemaCapability",
    "SchemaModel",
    "SourceSpan",
    "TableDef",
]
