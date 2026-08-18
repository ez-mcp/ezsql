"""SQL AST rewrites with round-trip verification.

Phase 2 ships exactly one rewrite: ``SELECT *`` → explicit columns,
scoped to exactly one known base table (plan §17).

Preconditions (all must be met, plan §17.1):
1. Schema is available.
2. ``schema.source == "repo-ddl"``.
3. Migration set is unambiguous (checked by pipeline, not here).
4. No ``ParserWarning`` with ``affects_schema_completeness=True`` for target table.
5. The ``SELECT *`` resolves to exactly one base table (no joins, CTEs, subqueries).
6. Round-trip validation passes (syntactic stability).

Round-trip validation checks that sqlglot can parse its own output and
produce an equivalent serialized form. This is **syntactic stability**,
not semantic equivalence (plan §17.3).
"""

import hashlib
import logging

import sqlglot
from sqlglot import exp

from ezsql.core.schema.model import SchemaModel, SourceSpan
from ezsql.server.models import RewriteCandidate

logger = logging.getLogger("ezsql.rewrite")


def _check_preconditions(
    stmt: exp.Select,
    schema: SchemaModel | None,
) -> tuple[bool, str, str | None]:
    """Check rewrite preconditions (plan §17.1).

    Returns ``(ok, reason_if_withheld, table_name)``.
    """
    if schema is None:
        return False, "schema unavailable", None

    # Precondition 2: schema source must be repo-ddl
    if schema.source != "repo-ddl":
        return False, "schema source not authoritative for Phase 2 rewrite", None

    # Find the SELECT * and the base table
    has_star = False
    for expr in stmt.expressions:
        if isinstance(expr, exp.Star):
            has_star = True
            break
        if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            has_star = True
            break

    if not has_star:
        return False, "no SELECT * found", None

    # Precondition 5: exactly one base table (no joins, CTEs, subqueries)
    # Check for CTEs
    with_ctes = stmt.args.get("with")
    if with_ctes is not None and with_ctes.expressions:
        return False, "CTE obscures column set", None

    # Check the FROM clause — only immediate tables, not nested subqueries
    from_clause = stmt.args.get("from") or stmt.args.get("from_")
    if from_clause is None:
        return False, "no FROM clause", None

    from_source = from_clause.this
    # If the FROM source is a subquery/derived table, withhold
    if isinstance(from_source, exp.Subquery):
        return False, "subquery in FROM not supported in Phase 2", None

    # Count immediate table references (not inside subqueries)
    immediate_tables: list[exp.Table] = []
    if isinstance(from_source, exp.Table):
        immediate_tables.append(from_source)

    # Check for joins — each join adds another table
    joins = stmt.args.get("joins")
    if joins:
        for join in joins:
            join_this = join.this if hasattr(join, "this") else None
            if isinstance(join_this, exp.Table):
                immediate_tables.append(join_this)
            elif isinstance(join_this, exp.Subquery):
                return False, "subquery in JOIN not supported in Phase 2", None

    if len(immediate_tables) != 1:
        return False, "multi-table expansion not supported in Phase 2", None

    table_node = immediate_tables[0]
    table_name = table_node.name

    # Precondition 4: no ParserWarning with affects_schema_completeness=True
    for warning in schema.parser_warnings:
        if warning.affects_schema_completeness and warning.object_name == table_name:
            return False, "schema model has a completeness warning for this table", None

    # Check table exists in schema
    if table_name not in schema.tables:
        return False, f"table '{table_name}' not found in schema model", None

    return True, "", table_name


def _expand_select_star(
    stmt: exp.Select,
    table_name: str,
    schema: SchemaModel,
) -> exp.Select | None:
    """Expand SELECT * to explicit columns from the schema model.

    Columns follow the schema model's column declaration order (plan §17.1.1).
    Returns a new Select AST with the star replaced by explicit columns,
    or None if expansion failed.
    """
    table_def = schema.tables[table_name]
    column_names = list(table_def.columns.keys())

    if not column_names:
        return None

    # Build new expressions: replace Star with explicit columns
    new_expressions: list[exp.Expr] = []
    for expr in stmt.expressions:
        if isinstance(expr, exp.Star):
            # Replace with all columns
            for col_name in column_names:
                new_expressions.append(exp.column(col_name, table=table_name))
        elif isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            # t.* — replace with all columns of that table
            for col_name in column_names:
                new_expressions.append(exp.column(col_name, table=table_name))
        else:
            new_expressions.append(expr)

    # Create a copy with new expressions
    new_stmt = stmt.copy()
    new_stmt.set("expressions", new_expressions)
    return new_stmt


def _validate_roundtrip(original: str, rewritten: str, dialect: str) -> bool:
    """Check round-trip validity (syntactic stability, plan §17.3).

    Verifies that sqlglot can parse the rewritten SQL and produce an
    equivalent serialized form. This is NOT semantic equivalence.
    """
    try:
        sg_dialect = dialect if dialect != "unknown" else None
        ast = sqlglot.parse_one(rewritten, dialect=sg_dialect)
        if ast is None:
            return False
        # Re-render and check it's stable
        re_rendered = ast.sql(dialect=sg_dialect)
        # Parse again — should produce the same SQL
        ast2 = sqlglot.parse_one(re_rendered, dialect=sg_dialect)
        if ast2 is None:
            return False
        return ast.sql(dialect=sg_dialect) == ast2.sql(dialect=sg_dialect)
    except Exception:  # noqa: BLE001
        return False


def rewrite_select_star(
    stmt: exp.Select,
    schema: SchemaModel | None,
    dialect: str = "unknown",
    statement_index: int = 0,
) -> RewriteCandidate | None:
    """Rewrite SELECT * to explicit columns (plan §17).

    Returns a ``RewriteCandidate`` if all preconditions are met and
    round-trip validation passes. Returns ``None`` if the rewrite is
    not applicable (no SELECT *, or preconditions fail without a
    candidate). Returns a candidate with ``validation_status="withheld"``
    if preconditions fail but a candidate was attempted.

    ``plan_delta`` is always ``None`` in Phase 2.
    """
    ok, reason, table_name = _check_preconditions(stmt, schema)
    if not ok:
        logger.debug("rewrite_withheld: %s", reason)
        return None

    assert table_name is not None  # guaranteed by ok=True
    assert schema is not None

    # Expand the star
    new_stmt = _expand_select_star(stmt, table_name, schema)
    if new_stmt is None:
        return None

    sg_dialect = dialect if dialect != "unknown" else None
    original_sql = stmt.sql(dialect=sg_dialect)
    rewritten_sql = new_stmt.sql(dialect=sg_dialect)

    # Round-trip validation
    if not _validate_roundtrip(original_sql, rewritten_sql, dialect):
        return RewriteCandidate(
            original_hash=hashlib.blake2b(
                original_sql.encode("utf-8"), digest_size=16
            ).hexdigest(),
            rewritten_sql=rewritten_sql,
            transformations=["select_star_expansion"],
            evidence="schema",
            source_span=SourceSpan(statement_index=statement_index),
            preconditions=[
                "schema available",
                "schema source is repo-ddl",
                "single base table",
                "no completeness warnings",
            ],
            schema_dependency=table_name,
            dialect=dialect,
            validation_status="withheld",
        )

    return RewriteCandidate(
        original_hash=hashlib.blake2b(
            original_sql.encode("utf-8"), digest_size=16
        ).hexdigest(),
        rewritten_sql=rewritten_sql,
        transformations=["select_star_expansion"],
        evidence="schema",
        source_span=SourceSpan(statement_index=statement_index),
        preconditions=[
            "schema available",
            "schema source is repo-ddl",
            "single base table",
            "no completeness warnings",
        ],
        schema_dependency=table_name,
        dialect=dialect,
        validation_status="validated",
    )


def rewrite(
    stmt: exp.Expr,
    schema: SchemaModel | None,
    dialect: str = "unknown",
    statement_index: int = 0,
) -> list[RewriteCandidate]:
    """Run all applicable rewrites on a statement.

    Phase 2: only SELECT * expansion. Returns a list of candidates
    (empty if no rewrites apply).
    """
    candidates: list[RewriteCandidate] = []

    if isinstance(stmt, exp.Select):
        candidate = rewrite_select_star(stmt, schema, dialect, statement_index)
        if candidate is not None:
            candidates.append(candidate)

    return candidates


__all__ = [
    "rewrite",
    "rewrite_select_star",
]
