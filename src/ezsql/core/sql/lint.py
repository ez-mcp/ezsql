"""SQL linting and heuristic checks with two-dimensional evidence.

Each finding carries both ``evidence`` (source: static/schema/runtime) and
``kind`` (claim type: fact/inference). These are independent dimensions
(plan §8, §16).

Phase 2 heuristics:
- OPT-001: SELECT * present (static, fact)
- OPT-002: Correlated subquery present (static, fact)
- OPT-003: Type mismatch between column and literal (schema, fact)
- OPT-004: No obviously usable index for predicate (schema, inference)

Forbidden in Phase 2: any statement about what a database optimizer will do,
how fast a query will run, or whether an index will be used. These are
runtime claims requiring runtime evidence.
"""

import logging
from typing import TYPE_CHECKING

from sqlglot import exp

from ezsql.core.schema.model import SchemaModel, SourceSpan
from ezsql.core.sql.parse import ParseResult
from ezsql.server.models import Finding

if TYPE_CHECKING:
    pass

logger = logging.getLogger("ezsql.lint")

# Rule IDs for optimization heuristics.
OPT_SELECT_STAR = "OPT-001"
OPT_CORRELATED_SUBQUERY = "OPT-002"
OPT_TYPE_MISMATCH = "OPT-003"
OPT_NO_INDEX = "OPT-004"

# Type class mapping for OPT-003 (plan §16.1.2).
_TYPE_CLASSES: dict[str, str] = {
    "integer": "integer",
    "int": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "serial": "integer",
    "bigserial": "integer",
    "tinyint": "integer",
    "mediumint": "integer",
    "numeric": "numeric",
    "decimal": "numeric",
    "float": "numeric",
    "real": "numeric",
    "double": "numeric",
    "text": "text",
    "varchar": "text",
    "char": "text",
    "string": "text",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "timestamp": "date",
    "timestamptz": "date",
    "time": "date",
    "binary": "binary",
    "bytea": "binary",
    "blob": "binary",
}


def _get_type_class(data_type: str) -> str | None:
    """Map a SQL type string to a type class (plan §16.1.2).

    Returns ``None`` for unknown/unmapped types.
    """
    normalized = data_type.strip().lower().split("(")[0].strip()
    return _TYPE_CLASSES.get(normalized)


def _literal_type_class(literal: exp.Literal) -> str | None:
    """Determine the type class of a sqlglot Literal.

    sqlglot Literal has ``is_string`` (True for string literals) and
    ``is_int`` (True for integer literals).
    """
    if literal.is_string:
        return "text"
    if literal.is_int:
        return "integer"
    # Try to parse as number
    try:
        float(literal.name)
        if "." in literal.name:
            return "numeric"
        return "integer"
    except (ValueError, TypeError):
        return None


def _get_meta_position(node: exp.Expr, statement_index: int) -> SourceSpan:
    """Extract source position from a sqlglot AST node's meta.

    sqlglot stores ``line`` and ``col`` in ``node.meta``. End positions
    may not be available — default to start position (point span).
    """
    meta = node.meta if hasattr(node, "meta") else {}
    line = meta.get("line", 1)
    col = meta.get("col", 1)
    return SourceSpan(
        statement_index=statement_index,
        start_line=line,
        start_col=col,
        end_line=line,
        end_col=col,
    )


def _find_select_star(stmt: exp.Expr, statement_index: int) -> Finding | None:
    """OPT-001: Detect SELECT * (static, fact).

    Fires when a ``Star`` node is found in a SELECT projection.
    """
    if not isinstance(stmt, exp.Select):
        return None
    for expr in stmt.expressions:
        if isinstance(expr, exp.Star):
            return Finding(
                rule_id=OPT_SELECT_STAR,
                title="SELECT * present",
                severity="info",
                message=(
                    "Projection includes all columns (SELECT *). This may "
                    "increase I/O and may prevent covering-index usage when "
                    "the index does not contain all projected columns."
                ),
                location=_get_meta_position(expr, statement_index),
                evidence="static",
                kind="fact",
            )
        # Also check for t.* (qualified star)
        if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            return Finding(
                rule_id=OPT_SELECT_STAR,
                title="SELECT t.* present",
                severity="info",
                message=(
                    "Projection includes all columns of a table (SELECT t.*). "
                    "This may increase I/O and may prevent covering-index usage."
                ),
                location=_get_meta_position(expr, statement_index),
                evidence="static",
                kind="fact",
            )
    return None


def _find_correlated_subquery(stmt: exp.Expr, statement_index: int) -> Finding | None:
    """OPT-002: Detect correlated subquery (static, fact).

    A subquery is correlated if it references a table/alias from the outer
    query. We detect this by checking if columns in the subquery are
    qualified with table names that don't belong to the subquery's own tables.
    """
    if not isinstance(stmt, exp.Select):
        return None

    # Get all tables in the outer query (including aliases)
    outer_table_names: set[str] = set()
    for tbl in stmt.find_all(exp.Table):
        outer_table_names.add(tbl.name)
        if tbl.alias:
            outer_table_names.add(tbl.alias)

    # Find subqueries
    for subq in stmt.find_all(exp.Subquery):
        # Get tables defined inside the subquery
        sub_tables: set[str] = set()
        for tbl in subq.find_all(exp.Table):
            sub_tables.add(tbl.name)
            if tbl.alias:
                sub_tables.add(tbl.alias)

        # Check if any column in the subquery references an outer table
        for col in subq.find_all(exp.Column):
            col_table = col.table
            if col_table and col_table not in sub_tables and col_table in outer_table_names:
                # Correlated! The subquery references an outer table.
                return Finding(
                    rule_id=OPT_CORRELATED_SUBQUERY,
                    title="Correlated subquery present",
                    severity="info",
                    message=(
                        "Correlated subquery references outer relation. The "
                        "eventual execution strategy is unknown without EXPLAIN; "
                        "modern optimizers may decorrelate this."
                    ),
                    location=_get_meta_position(subq, statement_index),
                    evidence="static",
                    kind="fact",
                )
    return None


def _find_type_mismatch(
    stmt: exp.Expr,
    statement_index: int,
    schema: SchemaModel | None,
) -> Finding | None:
    """OPT-003: Type mismatch between column and literal (schema, fact).

    Fires when a predicate compares a column to a literal of a different
    type class. Requires schema to know column type. Requires known dialect.
    """
    if schema is None or not isinstance(stmt, exp.Select):
        return None

    where = stmt.args.get("where")
    if where is None:
        return None

    # Find comparison conditions
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE,
                        exp.Between, exp.In, exp.Like)

    for cond in where.find_all(*comparison_types):
        if isinstance(cond, (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE)):
            left = cond.this
            right = cond.expression
            col_node = None
            literal_node = None

            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                col_node = left
                literal_node = right
            elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
                col_node = right
                literal_node = left

            if col_node is not None and literal_node is not None:
                finding = _check_type_mismatch(
                    col_node, literal_node, statement_index, schema
                )
                if finding is not None:
                    return finding
    return None


def _check_type_mismatch(
    col_node: exp.Column,
    literal_node: exp.Literal,
    statement_index: int,
    schema: SchemaModel,
) -> Finding | None:
    """Check if a column-literal comparison has a type class mismatch."""
    col_name = col_node.name
    # Find the column in the schema. We need to check all tables.
    for _table_name, table_def in schema.tables.items():
        if col_name in table_def.columns:
            col_type = table_def.columns[col_name].data_type
            col_class = _get_type_class(col_type)
            lit_class = _literal_type_class(literal_node)
            if col_class is not None and lit_class is not None and col_class != lit_class:
                return Finding(
                    rule_id=OPT_TYPE_MISMATCH,
                    title="Type mismatch between column and literal",
                    severity="low",
                    message=(
                        f"Schema model indicates column {col_name} is {col_type} "
                        f"(class {col_class}); predicate compares to {lit_class} "
                        f"literal. Implicit conversion behavior depends on the "
                        f"database."
                    ),
                    location=_get_meta_position(col_node, statement_index),
                    evidence="schema",
                    kind="fact",
                    schema_source="repo-ddl",
                )
    return None


def _find_no_usable_index(
    stmt: exp.Expr,
    statement_index: int,
    schema: SchemaModel | None,
) -> Finding | None:
    """OPT-004: No obviously usable index for predicate (schema, inference).

    Fires when a predicate on a single column has no "obviously usable" index
    in the schema model. See plan §16.1.1 for the exact contract.

    Withheld (not fired) when:
    - Schema has no indexes at all for the table (may mean model is incomplete)
    - Schema has a ParserWarning with affects_schema_completeness=True for
      the target table
    """
    if schema is None or not isinstance(stmt, exp.Select):
        return None

    where = stmt.args.get("where")
    if where is None:
        return None

    # Find equality/range comparisons on single columns
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE,
                        exp.Between, exp.In)

    for cond in where.find_all(*comparison_types):
        if isinstance(cond, (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE)):
            left = cond.this
            right = cond.expression
            col_node = None
            if isinstance(left, exp.Column) and not isinstance(left.this, exp.Star):
                col_node = left
            elif isinstance(right, exp.Column) and not isinstance(right.this, exp.Star):
                col_node = right

            if col_node is not None:
                finding = _check_no_usable_index(
                    col_node, statement_index, schema
                )
                if finding is not None:
                    return finding
    return None


def _check_no_usable_index(
    col_node: exp.Column,
    statement_index: int,
    schema: SchemaModel,
) -> Finding | None:
    """Check if a column predicate has no obviously usable index (§16.1.1)."""
    col_name = col_node.name
    # Check if the column has a functional transformation
    # (e.g., WHERE LOWER(email) = 'x') — if so, skip
    parent = col_node.parent
    if parent is not None and isinstance(parent, exp.Func):
        return None  # Functional transformation — not obviously usable, but
        # we don't fire because the predicate isn't on a plain column

    # Find the table that has this column
    for table_name, table_def in schema.tables.items():
        if col_name not in table_def.columns:
            continue

        # Check for ParserWarning with affects_schema_completeness=True
        for warning in schema.parser_warnings:
            if (warning.affects_schema_completeness
                    and warning.object_name == table_name):
                # Schema completeness warning for this table — withhold
                return None

        indexes = table_def.indexes
        if not indexes:
            # No indexes in model for this table — withhold (may be incomplete)
            return None

        # Check if any index is "obviously usable" for this column
        has_usable = False
        for idx_def in indexes.values():
            if _is_index_obviously_usable(idx_def, col_name):
                has_usable = True
                break

        if not has_usable:
            return Finding(
                rule_id=OPT_NO_INDEX,
                title="No obviously usable index for predicate",
                severity="low",
                message=(
                    f"No obviously usable index found in schema model for "
                    f"predicate on {col_name}. This is a static inference; "
                    f"the optimizer may use a different plan."
                ),
                location=_get_meta_position(col_node, statement_index),
                evidence="schema",
                kind="inference",
                schema_source="repo-ddl",
            )
    return None


def _is_index_obviously_usable(idx_def: object, col_name: str) -> bool:
    """Check if an index is "obviously usable" for a single-column predicate.

    Implements the five conditions from plan §16.1.1:
    1. Predicate is equality/range on a single column (checked by caller).
    2. Column is the first column of the index.
    3. Index is not partial.
    4. Index is not an expression index.
    5. No functional transformation (checked by caller).
    """
    from ezsql.core.schema.model import IndexDef
    if not isinstance(idx_def, IndexDef):
        return False
    # Condition 3: not partial
    if idx_def.is_partial:
        return False
    # Condition 4: not expression index
    if idx_def.is_expression:
        return False
    # Condition 2: column is first column of index
    if not idx_def.columns:
        return False
    return idx_def.columns[0].lower() == col_name.lower()


def lint(
    parse_result: ParseResult,
    schema: SchemaModel | None = None,
    *,
    dialect: str = "unknown",
) -> list[Finding]:
    """Run lint heuristics on parsed SQL statements.

    Returns findings ordered by ``(statement_index, start_line, start_col,
    rule_id)``. Each finding carries two-dimensional evidence.

    Dialect-dependent rules (OPT-003, OPT-004) are skipped when
    ``dialect == "unknown"`` (plan §10.2).
    """
    findings: list[Finding] = []

    for i, stmt in enumerate(parse_result.statements):
        # OPT-001: SELECT * (dialect-independent)
        f = _find_select_star(stmt, i)
        if f is not None:
            findings.append(f)

        # OPT-002: Correlated subquery (dialect-independent)
        f = _find_correlated_subquery(stmt, i)
        if f is not None:
            findings.append(f)

        # OPT-003: Type mismatch (dialect-dependent, requires schema)
        if dialect != "unknown" and schema is not None:
            f = _find_type_mismatch(stmt, i, schema)
            if f is not None:
                findings.append(f)

        # OPT-004: No usable index (dialect-dependent, requires schema)
        if dialect != "unknown" and schema is not None:
            f = _find_no_usable_index(stmt, i, schema)
            if f is not None:
                findings.append(f)

    # Sort by source location (plan §9.6 — semantically meaningful order)
    findings.sort(key=lambda f: (
        f.location.statement_index,
        f.location.start_line,
        f.location.start_col,
        f.rule_id,
    ))

    return findings


__all__ = [
    "lint",
    "OPT_CORRELATED_SUBQUERY",
    "OPT_NO_INDEX",
    "OPT_SELECT_STAR",
    "OPT_TYPE_MISMATCH",
]
