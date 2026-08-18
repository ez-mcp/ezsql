"""DDL and migration parser for schema extraction (plan §13, §14).

Pure function: ``parse_migrations`` accepts ``list[tuple[str, str]]``
(path, content) and produces a ``SchemaModel``. The pipeline owns file
reading; this function is pure and testable.

Migration ordering (plan §14.2):
1. Detect naming convention per migration directory.
2. If all candidates resolve to the same convention: sort by that convention.
3. If mixed conventions: return ``FailureEnvelope(kind="ambiguous_migration_conventions")``.
4. Multiple migration roots: return ``FailureEnvelope(kind="ambiguous_migration_roots")``.

The parser accumulates schema state across ordered migrations:
- CREATE TABLE → add table
- ALTER TABLE ADD COLUMN → add column to existing table
- ALTER TABLE DROP COLUMN → remove column
- ALTER TABLE RENAME COLUMN → rename
- CREATE INDEX → add index
- DROP INDEX → remove index
- DROP TABLE → remove table
- Unsupported DDL → ParserWarning (not silently dropped)
"""

import logging
import re

from sqlglot import exp

from ezsql.core.schema.model import (
    ColumnDef,
    ConstraintDef,
    ForeignKeyDef,
    IndexDef,
    ParserWarning,
    SchemaCapability,
    SchemaModel,
    SourceSpan,
    TableDef,
)
from ezsql.core.sql.parse import InternalFailure, parse
from ezsql.server.models import FailureEnvelope

logger = logging.getLogger("ezsql.schema.ddl")

# Schema model version (bumps when model interpretation changes).
SCHEMA_MODEL_VERSION = "1"

# Migration naming patterns (plan §14.2).
_NUMERIC_PATTERN = re.compile(r"^(\d+)_.*\.sql$", re.IGNORECASE)
_FLYWAY_PATTERN = re.compile(r"^V(\d+)__.*\.sql$", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"^(\d{8})_.*\.sql$", re.IGNORECASE)
_TIMESTAMP_PATTERN = re.compile(r"^(\d{10})_.*\.sql$", re.IGNORECASE)


def _detect_convention(filename: str) -> str | None:
    """Detect the migration naming convention of a file.

    Returns ``"numeric"``, ``"flyway"``, ``"date"``, ``"timestamp"``, or
    ``None`` if the file doesn't match any migration pattern.
    """
    # Check most specific patterns first (date/timestamp before numeric)
    if _DATE_PATTERN.match(filename):
        return "date"
    if _TIMESTAMP_PATTERN.match(filename):
        return "timestamp"
    if _FLYWAY_PATTERN.match(filename):
        return "flyway"
    if _NUMERIC_PATTERN.match(filename):
        return "numeric"
    return None


def _extract_sort_key(filename: str, convention: str) -> str:
    """Extract the sort key from a migration filename."""
    if convention == "numeric":
        m = _NUMERIC_PATTERN.match(filename)
        return m.group(1).lstrip("0") or "0" if m else "0"
    if convention == "flyway":
        m = _FLYWAY_PATTERN.match(filename)
        return m.group(1).lstrip("0") or "0" if m else "0"
    if convention == "date":
        m = _DATE_PATTERN.match(filename)
        return m.group(1) if m else "0"
    if convention == "timestamp":
        m = _TIMESTAMP_PATTERN.match(filename)
        return m.group(1) if m else "0"
    return "0"


def _order_migrations(
    files: list[tuple[str, str]],
) -> list[tuple[str, str]] | FailureEnvelope:
    """Order migration files by their naming convention.

    Returns ordered ``[(path, content), ...]`` or ``FailureEnvelope`` if
    the migration set is ambiguous (mixed conventions or multiple roots).
    """
    # Detect convention for each file
    candidates: list[tuple[str, str, str | None]] = []
    for path, content in files:
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        convention = _detect_convention(filename)
        if convention is not None:
            candidates.append((path, content, convention))

    if not candidates:
        return []

    # Check for mixed conventions
    conventions = {c for _, _, c in candidates if c is not None}
    if len(conventions) > 1:
        return FailureEnvelope(
            kind="ambiguous_migration_conventions",
            detail=f"Migration files use mixed naming conventions: {sorted(conventions)}. "
                   f"Cannot determine ordering across different conventions.",
            recoverable=True,
            next_steps=["Use a single migration naming convention."],
        )

    convention = conventions.pop()

    # Check for duplicate versions
    sort_keys: dict[str, str] = {}
    for path, _, _ in candidates:
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        key = _extract_sort_key(filename, convention)
        if key in sort_keys:
            return FailureEnvelope(
                kind="duplicate_migration_version",
                detail=f"Duplicate migration version '{key}': "
                       f"{sort_keys[key]} and {path}",
                recoverable=True,
                next_steps=["Rename one of the conflicting migration files."],
            )
        sort_keys[key] = path

    # Sort by sort key
    def sort_func(item: tuple[str, str, str | None]) -> str:
        path = item[0]
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        return _extract_sort_key(filename, convention)

    candidates.sort(key=sort_func)
    return [(path, content) for path, content, _ in candidates]


def _make_span(
    statement_index: int,
    line: int = 1,
    col: int = 1,
    file: str | None = None,
) -> SourceSpan:
    """Create a SourceSpan."""
    return SourceSpan(
        statement_index=statement_index,
        start_line=line,
        start_col=col,
        end_line=line,
        end_col=col,
        file=file,
    )


def _get_meta_span(node: exp.Expr, statement_index: int, file: str | None = None) -> SourceSpan:
    """Extract source position from a sqlglot AST node."""
    meta = node.meta if hasattr(node, "meta") else {}
    line = meta.get("line", 1)
    col = meta.get("col", 1)
    return _make_span(statement_index, line, col, file)


def _parse_create_table(
    stmt: exp.Create,
    schema: SchemaModel,
    statement_index: int,
    file: str | None,
) -> list[ParserWarning]:
    """Parse a CREATE TABLE statement into the schema model."""
    warnings: list[ParserWarning] = []

    # The table is in stmt.this (a Schema with Table + column expressions)
    schema_node = stmt.this
    if not isinstance(schema_node, exp.Schema):
        # Could be CREATE TABLE LIKE x or other variant
        warnings.append(ParserWarning(
            kind="unsupported_create_variant",
            location=_make_span(statement_index, file=file),
            message=f"Unsupported CREATE variant: {stmt.sql()[:50]}",
            affects_schema_completeness=False,
        ))
        return warnings

    table_node = schema_node.this
    if not isinstance(table_node, exp.Table):
        return warnings

    table_name = table_node.name
    columns: dict[str, ColumnDef] = {}
    constraints: list[ConstraintDef] = []
    foreign_keys: list[ForeignKeyDef] = []

    for col_expr in schema_node.expressions:
        if isinstance(col_expr, exp.ColumnDef):
            col_def = _parse_column_def(col_expr)
            columns[col_def.name] = col_def
            # Check for column-level FK
            col_constraints = col_expr.args.get("constraints") or []
            for c in col_constraints:
                kind = c.args.get("kind")
                if isinstance(kind, exp.Reference):
                    fk = _parse_column_fk(table_name, col_def.name, kind)
                    if fk is not None:
                        foreign_keys.append(fk)
                        constraints.append(ConstraintDef(
                            name=None,
                            type="foreign_key",
                            columns=[col_def.name],
                            references_table=fk.target_table,
                            references_columns=fk.target_columns,
                        ))
                if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                    constraints.append(ConstraintDef(
                        name=None,
                        type="primary_key",
                        columns=[col_def.name],
                    ))
                if isinstance(kind, exp.NotNullColumnConstraint):
                    col_def.nullable = False
                if isinstance(kind, exp.UniqueColumnConstraint):
                    constraints.append(ConstraintDef(
                        name=None,
                        type="unique",
                        columns=[col_def.name],
                    ))
        elif isinstance(col_expr, exp.ForeignKey):
            # Table-level FK
            fk = _parse_table_fk(table_name, col_expr)
            if fk is not None:
                foreign_keys.append(fk)
                constraints.append(ConstraintDef(
                    name=None,
                    type="foreign_key",
                    columns=fk.source_columns,
                    references_table=fk.target_table,
                    references_columns=fk.target_columns,
                ))
        elif isinstance(col_expr, exp.PrimaryKey):
            pk_cols = [c.name for c in col_expr.find_all(exp.Column)]
            constraints.append(ConstraintDef(
                name=None,
                type="primary_key",
                columns=pk_cols,
            ))
        elif type(col_expr).__name__ == "Unique":
            uq_cols = [c.name for c in col_expr.find_all(exp.Column)]
            constraints.append(ConstraintDef(
                name=None,
                type="unique",
                columns=uq_cols,
            ))
        else:
            # Unsupported constraint or expression
            warnings.append(ParserWarning(
                kind="unsupported_table_expression",
                location=_make_span(statement_index, file=file),
                object_name=table_name,
                message=f"Unsupported table expression: {type(col_expr).__name__}",
                affects_schema_completeness=False,
            ))

    table_def = TableDef(
        name=table_name,
        columns=columns,
        constraints=constraints,
    )
    schema.tables[table_name] = table_def
    schema.foreign_keys.extend(foreign_keys)

    return warnings


def _parse_column_def(col_expr: exp.ColumnDef) -> ColumnDef:
    """Parse a ColumnDef into a ColumnDef model."""
    name = col_expr.name
    kind = col_expr.args.get("kind")
    data_type = kind.sql() if kind is not None else "UNKNOWN"
    nullable = True
    raw_default = None

    constraints = col_expr.args.get("constraints") or []
    for c in constraints:
        kind_obj = c.args.get("kind")
        if isinstance(kind_obj, exp.NotNullColumnConstraint):
            nullable = False
        if isinstance(kind_obj, exp.DefaultColumnConstraint):
            raw_default = kind_obj.sql()

    return ColumnDef(
        name=name,
        data_type=data_type,
        nullable=nullable,
        raw_default=raw_default,
    )


def _parse_column_fk(
    table_name: str,
    col_name: str,
    ref: exp.Reference,
) -> ForeignKeyDef | None:
    """Parse a column-level FK reference."""
    ref_schema = ref.this
    if not isinstance(ref_schema, exp.Schema):
        return None
    target_table_node = ref_schema.this
    if not isinstance(target_table_node, exp.Table):
        return None
    target_table = target_table_node.name
    # Use ref_schema.expressions for target columns (not find_all which picks up table name)
    target_cols = [c.name for c in ref_schema.expressions if isinstance(c, exp.Identifier)]
    return ForeignKeyDef(
        constraint_name=None,
        source_table=table_name,
        source_columns=[col_name],
        target_table=target_table,
        target_columns=target_cols,
    )


def _parse_table_fk(
    table_name: str,
    fk_expr: exp.ForeignKey,
) -> ForeignKeyDef | None:
    """Parse a table-level FOREIGN KEY constraint."""
    source_cols = [c.name for c in fk_expr.expressions if isinstance(c, exp.Column)]
    # Or they might be Identifiers
    if not source_cols:
        source_cols = [c.name for c in fk_expr.expressions if isinstance(c, exp.Identifier)]

    ref = fk_expr.args.get("reference")
    if ref is None or not isinstance(ref, exp.Reference):
        return None

    ref_schema = ref.this
    if not isinstance(ref_schema, exp.Schema):
        return None

    target_table_node = ref_schema.this
    if not isinstance(target_table_node, exp.Table):
        return None

    target_table = target_table_node.name
    target_cols = [c.name for c in ref_schema.expressions if isinstance(c, exp.Identifier)]

    return ForeignKeyDef(
        constraint_name=None,
        source_table=table_name,
        source_columns=source_cols,
        target_table=target_table,
        target_columns=target_cols,
    )


def _parse_alter_table(
    stmt: exp.Alter,
    schema: SchemaModel,
    statement_index: int,
    file: str | None,
) -> list[ParserWarning]:
    """Parse an ALTER TABLE statement."""
    warnings: list[ParserWarning] = []

    table_node = stmt.this
    if isinstance(table_node, exp.Table):
        table_name = table_node.name
    elif isinstance(table_node, exp.Schema):
        table_node = table_node.this
        table_name = table_node.name if isinstance(table_node, exp.Table) else ""
    else:
        table_name = ""

    if not table_name or table_name not in schema.tables:
        warnings.append(ParserWarning(
            kind="alter_unknown_table",
            location=_make_span(statement_index, file=file),
            object_name=table_name,
            message=f"ALTER TABLE on unknown table: {table_name}",
            affects_schema_completeness=False,
        ))
        return warnings

    table_def = schema.tables[table_name]
    actions = stmt.args.get("actions") or []

    for action in actions:
        if isinstance(action, exp.ColumnDef):
            # ADD COLUMN
            col_def = _parse_column_def(action)
            table_def.columns[col_def.name] = col_def
        elif isinstance(action, exp.Drop):
            action_kind = action.args.get("kind")
            if action_kind and str(action_kind).upper() == "COLUMN":
                col_name = action.this.name if action.this else ""
                if col_name and col_name in table_def.columns:
                    del table_def.columns[col_name]
        elif type(action).__name__ == "Rename":
            # RENAME COLUMN
            old_name = action.this.name if action.this else ""
            new_name = action.args.get("to")
            if old_name and new_name and old_name in table_def.columns:
                col_def = table_def.columns.pop(old_name)
                col_def.name = new_name.name if hasattr(new_name, "name") else str(new_name)
                table_def.columns[col_def.name] = col_def
        else:
            warnings.append(ParserWarning(
                kind="unsupported_alter_action",
                location=_make_span(statement_index, file=file),
                object_name=table_name,
                message=f"Unsupported ALTER action: {type(action).__name__}",
                affects_schema_completeness=False,
            ))

    return warnings


def _parse_create_index(
    stmt: exp.Create,
    schema: SchemaModel,
    statement_index: int,
    file: str | None,
) -> list[ParserWarning]:
    """Parse a CREATE INDEX statement."""
    warnings: list[ParserWarning] = []

    idx = stmt.this
    if not isinstance(idx, exp.Index):
        warnings.append(ParserWarning(
            kind="unsupported_index_variant",
            location=_make_span(statement_index, file=file),
            message=f"Unsupported index variant: {stmt.sql()[:50]}",
            affects_schema_completeness=False,
        ))
        return warnings

    idx_name = idx.name
    table_ref = idx.args.get("table")
    if not isinstance(table_ref, exp.Table):
        return warnings

    table_name = table_ref.name
    if table_name not in schema.tables:
        # Index on unknown table — record but don't fail
        return warnings

    # Extract columns from IndexParameters
    params = idx.args.get("params")
    columns: list[str] = []
    is_partial = False
    is_expression = False

    if params is not None:
        cols = params.args.get("columns")
        if cols:
            for c in cols:
                # c is an Ordered expression; the actual column is in c.this
                inner = c.this if hasattr(c, "this") else c
                if isinstance(inner, exp.Column):
                    columns.append(inner.name)
                elif isinstance(inner, exp.Func):
                    is_expression = True
                    columns.append(inner.sql())
                else:
                    # Try to get the name
                    if hasattr(inner, "name") and inner.name:
                        columns.append(inner.name)
                    else:
                        is_expression = True
                        columns.append(inner.sql() if hasattr(inner, "sql") else str(inner))

        # Check for WHERE clause (partial index)
        where = params.args.get("where")
        if where is not None:
            is_partial = True

    is_unique = bool(stmt.args.get("unique"))

    index_def = IndexDef(
        name=idx_name,
        columns=columns,
        unique=is_unique,
        is_partial=is_partial,
        is_expression=is_expression,
    )
    schema.tables[table_name].indexes[idx_name] = index_def

    return warnings


def _parse_drop_table(
    stmt: exp.Drop,
    schema: SchemaModel,
) -> None:
    """Parse a DROP TABLE statement — remove table from schema."""
    kind = stmt.args.get("kind")
    if kind and str(kind).upper() == "TABLE":
        table_node = stmt.this
        if isinstance(table_node, exp.Table):
            table_name = table_node.name
            schema.tables.pop(table_name, None)
            # Remove FKs that reference this table
            schema.foreign_keys = [
                fk for fk in schema.foreign_keys
                if fk.source_table != table_name and fk.target_table != table_name
            ]


def _parse_drop_index(
    stmt: exp.Drop,
    schema: SchemaModel,
) -> None:
    """Parse a DROP INDEX statement — remove index from schema."""
    kind = stmt.args.get("kind")
    if kind and str(kind).upper() == "INDEX":
        idx_node = stmt.this
        idx_name = idx_node.name if hasattr(idx_node, "name") else ""
        if idx_name:
            for table_def in schema.tables.values():
                if idx_name in table_def.indexes:
                    del table_def.indexes[idx_name]
                    break


def _parse_unsupported_ddl(
    stmt: exp.Expr,
    statement_index: int,
    file: str | None,
) -> ParserWarning:
    """Create a ParserWarning for unsupported DDL."""
    kind_name = type(stmt).__name__
    # Determine if this affects schema completeness
    # Triggers, stored procedures, etc. don't affect table/column/index info
    affects = False
    compromised: frozenset[SchemaCapability] = frozenset()

    if isinstance(stmt, (exp.Create,)):
        create_kind = stmt.args.get("kind")
        if create_kind and str(create_kind).upper() in ("TRIGGER", "PROCEDURE", "FUNCTION", "VIEW"):
            affects = False
        elif create_kind and str(create_kind).upper() == "TYPE":
            # Custom types might affect column type interpretation
            affects = True
            compromised = frozenset({"column_type"})

    return ParserWarning(
        kind=f"unsupported_{kind_name.lower()}",
        location=_make_span(statement_index, file=file),
        message=f"Unsupported DDL: {kind_name} ({stmt.sql()[:50]})",
        affects_schema_completeness=affects,
        compromised_capabilities=compromised,
    )


def parse_migrations(
    files: list[tuple[str, str]],
    *,
    max_parser_warnings: int = 100,
) -> SchemaModel | FailureEnvelope:
    """Parse migration files into a SchemaModel.

    Pure function: accepts ``list[tuple[str, str]]`` (path, content).
    The pipeline owns file reading.

    Args:
        files: List of ``(relative_path, content)`` tuples.
        max_parser_warnings: Maximum parser warnings before truncation.

    Returns:
        ``SchemaModel`` on success, or ``FailureEnvelope`` if migration
        discovery is ambiguous (mixed conventions, multiple roots, duplicate
        versions).
    """
    if not files:
        return SchemaModel(
            source="repo-ddl",
            source_files=[],
            schema_model_version=SCHEMA_MODEL_VERSION,
        )

    # Order migrations
    ordered = _order_migrations(files)
    if isinstance(ordered, FailureEnvelope):
        return ordered

    schema = SchemaModel(
        source="repo-ddl",
        source_files=[path for path, _ in ordered],
        schema_model_version=SCHEMA_MODEL_VERSION,
    )

    all_warnings: list[ParserWarning] = []

    for path, content in ordered:
        # Parse the migration file
        result = parse(content, dialect="postgres")  # TODO: configurable dialect
        if isinstance(result, InternalFailure):
            # InternalFailure — skip this file
            all_warnings.append(ParserWarning(
                kind="parse_failure",
                location=_make_span(0, file=path),
                object_name=path,
                message="Failed to parse migration file",
                affects_schema_completeness=True,
                compromised_capabilities=frozenset({
                    "column_enumeration", "column_type", "index_enumeration",
                    "index_structure", "constraint_enumeration", "fk_structure",
                    "table_existence",
                }),
            ))
            continue

        for i, stmt in enumerate(result.statements):
            stmt_warnings: list[ParserWarning] = []

            if isinstance(stmt, exp.Create):
                create_kind = stmt.args.get("kind")
                if create_kind and str(create_kind).upper() == "TABLE":
                    stmt_warnings = _parse_create_table(stmt, schema, i, path)
                elif create_kind and str(create_kind).upper() == "INDEX":
                    stmt_warnings = _parse_create_index(stmt, schema, i, path)
                else:
                    # Other CREATE (trigger, function, view, type, etc.)
                    w = _parse_unsupported_ddl(stmt, i, path)
                    stmt_warnings = [w]
            elif isinstance(stmt, exp.Alter):
                stmt_warnings = _parse_alter_table(stmt, schema, i, path)
            elif isinstance(stmt, exp.Drop):
                drop_kind = stmt.args.get("kind")
                if drop_kind and str(drop_kind).upper() == "TABLE":
                    _parse_drop_table(stmt, schema)
                elif drop_kind and str(drop_kind).upper() == "INDEX":
                    _parse_drop_index(stmt, schema)
                # DROP COLUMN is handled in ALTER
            else:
                # Non-DDL statement (SELECT, INSERT, etc.) in a migration file
                # — not an error, just not schema-relevant
                # But check for Command type (sqlglot fallback for unsupported syntax)
                if type(stmt).__name__ == "Command":
                    w = _parse_unsupported_ddl(stmt, i, path)
                    all_warnings.append(w)

            all_warnings.extend(stmt_warnings)

    # Truncate warnings if needed
    if len(all_warnings) > max_parser_warnings:
        schema.warnings_truncated = True
        schema.warnings_suppressed = len(all_warnings) - max_parser_warnings
        schema.parser_warnings = all_warnings[:max_parser_warnings]
    else:
        schema.parser_warnings = all_warnings

    return schema


__all__ = [
    "SCHEMA_MODEL_VERSION",
    "parse_migrations",
]
