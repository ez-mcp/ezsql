"""SQL parsing via sqlglot with per-statement recovery.

Provides ``parse()`` for multi-statement input and ``parse_one()`` for
single-statement input. Both use sqlglot's parser under the hood.

Per-statement recovery (plan §12.1.1): sqlglot's ``parse()`` aborts on the
first real parse error. We implement recovery by tokenizing the input,
splitting on top-level semicolons (respecting string/comment boundaries),
and parsing each statement individually. A failed statement produces a
``ParseError``; successfully parsed statements are returned. This limits
blast radius — one bad statement in 100 doesn't stop the whole pipeline.

Dialect resolution (plan §10.1): explicit → configured → ``"unknown"``.
When ``"unknown"``, sqlglot parses with its generic/broad dialect.
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as SqlglotParseError

logger = logging.getLogger("ezsql.parse")

ParseErrorKind = Literal[
    "parse_error",
    "expected_one_statement",
    "statement_budget_exceeded",
]


@dataclass(frozen=True)
class ParseError:
    """Structured parse failure for a single statement.

    ``line``/``col`` are the sqlglot-reported position when available.
    When ``kind="statement_budget_exceeded"`` or
    ``kind="expected_one_statement"``, ``line``/``col`` are ``None``
    (the error is not tied to a specific source position).
    """

    message: str
    line: int | None
    col: int | None
    dialect: str
    kind: ParseErrorKind = "parse_error"
    statement_index: int = 0


@dataclass(frozen=True)
class InternalFailure:
    """Internal EZSQL error, not a user SQL error.

    Distinct from ``ParseError`` so EZSQL bugs are not misreported as
    bad user SQL (plan §12.3, exit criterion §23.18).
    """

    kind: Literal["internal_error"]
    detail: str


@dataclass(frozen=True)
class ParseResult:
    """Result of parsing: successfully parsed statements + per-statement errors.

    ``statements`` contains successfully parsed ASTs (up to ``max_statements``).
    ``errors`` contains per-statement parse errors (empty if all succeeded).
    ``dialect`` is the resolved dialect string.
    """

    statements: list[exp.Expr] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    dialect: str = "unknown"
    truncated: bool = False
    statements_suppressed: int = 0


def _resolve_dialect(
    dialect: str | None,
    configured_dialect: str | None,
) -> str:
    """Resolve dialect: explicit → configured → ``"unknown"`` (plan §10.1).

    Validates against sqlglot's known dialects. An invalid dialect name
    falls back to ``"unknown"`` (fail safely, never invent).
    """
    if dialect is not None and dialect.strip():
        resolved = dialect.strip().lower()
        if _is_valid_dialect(resolved):
            return resolved
        logger.warning("invalid dialect '%s', falling back to unknown", dialect)
        return "unknown"
    if configured_dialect is not None and configured_dialect.strip():
        resolved = configured_dialect.strip().lower()
        if _is_valid_dialect(resolved):
            return resolved
        logger.warning(
            "invalid configured dialect '%s', falling back to unknown",
            configured_dialect,
        )
        return "unknown"
    return "unknown"


def _is_valid_dialect(dialect: str) -> bool:
    """Check if a dialect name is recognized by sqlglot."""
    try:
        d = sqlglot.Dialect.get_or_raise(dialect)
        return d is not None
    except (ValueError, TypeError):
        return False


def _get_sqlglot_dialect(dialect: str) -> str | None:
    """Convert our dialect string to sqlglot's expected format.

    Returns ``None`` for ``"unknown"`` (sqlglot uses its default/generic
    parser when dialect is ``None``).
    """
    if dialect == "unknown":
        return None
    return dialect


def _extract_error_position(err: SqlglotParseError) -> tuple[int | None, int | None]:
    """Extract line/col from a sqlglot ParseError.

    sqlglot stores error details in ``err.errors`` as a list of dicts
    with ``line`` and ``col`` keys.
    """
    errors = err.errors
    if errors:
        first = errors[0]
        if isinstance(first, dict):
            line = first.get("line")
            col = first.get("col")
            return (
                int(line) if line is not None else None,
                int(col) if col is not None else None,
            )
    return None, None


def _split_statements(sql: str, dialect: str | None) -> list[tuple[int, str]]:
    """Split SQL into individual statements using sqlglot's tokenizer.

    Returns a list of ``(statement_index, statement_text)`` tuples.
    Uses the tokenizer to find semicolon positions, respecting string and
    comment boundaries (naive ``str.split(';')`` breaks on semicolons
    inside string literals or comments).

    The statement_index is 0-based and reflects the original position in
    the input, even if some statements are empty (trailing semicolons).
    """
    try:
        if dialect:
            d = sqlglot.Dialect.get_or_raise(dialect)
        else:
            d = sqlglot.Dialect.get_or_raise("postgres")
        if isinstance(d, type):
            d = d()
        tokenizer = (
            d.tokenizer()
            if callable(d.tokenizer) and not hasattr(d.tokenizer, "tokenize")
            else d.tokenizer
        )
        tokens = tokenizer.tokenize(sql)
    except Exception:  # noqa: BLE001 — tokenizer failure is internal
        # Fallback: naive split (better than nothing)
        fallback_parts = [p.strip() for p in sql.split(";") if p.strip()]
        return [(i, p) for i, p in enumerate(fallback_parts)]

    # Find top-level semicolon positions (tokenizer already handles
    # string/comment boundaries — semicolons inside strings are different
    # token types).
    semicolon_positions: list[int] = []
    for tok in tokens:
        if tok.token_type.name == "SEMICOLON":
            semicolon_positions.append(tok.start)

    if not semicolon_positions:
        # No semicolons — single statement
        stripped = sql.strip()
        if stripped:
            return [(0, stripped)]
        return []

    # Split at semicolon positions
    parts: list[tuple[int, str]] = []
    prev = 0
    idx = 0
    for pos in semicolon_positions:
        segment = sql[prev:pos].strip()
        if segment:
            parts.append((idx, segment))
            idx += 1
        prev = pos + 1  # skip the semicolon itself
    # Trailing content after last semicolon
    trailing = sql[prev:].strip()
    if trailing:
        parts.append((idx, trailing))

    return parts


def parse(
    sql: str,
    dialect: str | None = None,
    configured_dialect: str | None = None,
    *,
    max_statements: int = 10_000,
) -> ParseResult | InternalFailure:
    """Parse SQL into a list of ASTs. Multi-statement safe.

    Dialect resolution: explicit → configured → ``"unknown"``.

    Per-statement recovery: if one statement fails to parse, the parser
    returns the successfully parsed statements plus a ``ParseError`` for
    the failed statement. The pipeline decides whether to proceed with
    partial results.

    Parser budget: ``max_statements`` is a parser/computation limit. The
    parser stops after parsing ``max_statements`` statements; remaining
    statements are not parsed. If the input contains more statements than
    the budget, ``ParseResult.errors`` includes a ``ParseError`` with
    ``kind="statement_budget_exceeded"`` and ``truncated=True``.

    Returns ``ParseResult`` on success and known parse errors.
    Returns ``InternalFailure`` on unexpected internal errors (not user
    SQL errors).
    """
    resolved = _resolve_dialect(dialect, configured_dialect)
    sg_dialect = _get_sqlglot_dialect(resolved)

    # Strip BOM (sqlglot doesn't handle it natively)
    if sql.startswith("\ufeff"):
        sql = sql[1:]

    # Handle empty/whitespace-only input
    if not sql or not sql.strip():
        return ParseResult(statements=[], errors=[], dialect=resolved)

    # Split into individual statements
    try:
        statement_parts = _split_statements(sql, sg_dialect)
    except Exception as exc:  # noqa: BLE001 — internal tokenizer failure
        logger.error("internal split failure: %s", type(exc).__name__)
        return InternalFailure(
            kind="internal_error",
            detail="An internal error occurred during statement splitting.",
        )

    if not statement_parts:
        return ParseResult(statements=[], errors=[], dialect=resolved)

    statements: list[exp.Expr] = []
    errors: list[ParseError] = []
    truncated = False
    suppressed = 0

    for stmt_idx, stmt_text in statement_parts:
        # Check budget
        if len(statements) >= max_statements:
            truncated = True
            suppressed = len(statement_parts) - stmt_idx
            errors.append(ParseError(
                message=f"Statement budget exceeded ({max_statements}). "
                        f"{suppressed} additional statement(s) not parsed.",
                line=None,
                col=None,
                dialect=resolved,
                kind="statement_budget_exceeded",
                statement_index=stmt_idx,
            ))
            break

        try:
            ast = sqlglot.parse_one(stmt_text, dialect=sg_dialect)
            if ast is not None:
                # Detect Block (multi-statement parsed as one) — shouldn't
                # happen after splitting, but guard against it.
                if isinstance(ast, exp.Block):
                    # A Block means the statement itself contained semicolons
                    # that the tokenizer missed (e.g., inside a function body).
                    # Extract sub-statements from the Block.
                    for sub in ast.expressions:
                        statements.append(sub)
                else:
                    statements.append(ast)
        except SqlglotParseError as exc:
            line, col = _extract_error_position(exc)
            errors.append(ParseError(
                message=str(exc),
                line=line,
                col=col,
                dialect=resolved,
                kind="parse_error",
                statement_index=stmt_idx,
            ))
        except Exception as exc:  # noqa: BLE001 — internal crash, not user SQL
            logger.error(
                "internal parse failure: %s: %s",
                type(exc).__name__,
                str(exc),
            )
            return InternalFailure(
                kind="internal_error",
                detail="An internal error occurred during parsing.",
            )

    return ParseResult(
        statements=statements,
        errors=errors,
        dialect=resolved,
        truncated=truncated,
        statements_suppressed=suppressed,
    )


def parse_one(
    sql: str,
    dialect: str | None = None,
    configured_dialect: str | None = None,
) -> ParseResult | InternalFailure:
    """Parse exactly one statement. Rejects multi-statement input.

    - 0 statements → ``ParseResult(statements=[], errors=[])``
    - 1 statement → ``ParseResult(statements=[ast], errors=[])``
    - >1 statements → ``ParseResult(statements=[], errors=[ParseError(
        kind="expected_one_statement")])``

    Never silently truncates. Never returns the first statement and
    discards the rest (plan §12.2, exit criterion §23.16).
    """
    resolved = _resolve_dialect(dialect, configured_dialect)
    sg_dialect = _get_sqlglot_dialect(resolved)

    # Strip BOM (sqlglot doesn't handle it natively)
    if sql.startswith("\ufeff"):
        sql = sql[1:]

    if not sql or not sql.strip():
        return ParseResult(statements=[], errors=[], dialect=resolved)

    # Split to detect multi-statement input
    try:
        statement_parts = _split_statements(sql, sg_dialect)
    except Exception as exc:  # noqa: BLE001
        logger.error("internal split failure: %s", type(exc).__name__)
        return InternalFailure(
            kind="internal_error",
            detail="An internal error occurred during statement splitting.",
        )

    if len(statement_parts) > 1:
        return ParseResult(
            statements=[],
            errors=[ParseError(
                message="Input contains multiple statements; use parse() instead",
                line=None,
                col=None,
                dialect=resolved,
                kind="expected_one_statement",
            )],
            dialect=resolved,
        )

    if not statement_parts:
        return ParseResult(statements=[], errors=[], dialect=resolved)

    _, stmt_text = statement_parts[0]
    try:
        ast = sqlglot.parse_one(stmt_text, dialect=sg_dialect)
        if ast is None:
            return ParseResult(statements=[], errors=[], dialect=resolved)
        # A Block means the statement contained semicolons (shouldn't happen
        # after splitting, but guard).
        if isinstance(ast, exp.Block):
            return ParseResult(
                statements=[],
                errors=[ParseError(
                    message="Input contains multiple statements; use parse() instead",
                    line=None,
                    col=None,
                    dialect=resolved,
                    kind="expected_one_statement",
                )],
                dialect=resolved,
            )
        return ParseResult(statements=[ast], errors=[], dialect=resolved)
    except SqlglotParseError as exc:
        line, col = _extract_error_position(exc)
        return ParseResult(
            statements=[],
            errors=[ParseError(
                message=str(exc),
                line=line,
                col=col,
                dialect=resolved,
                kind="parse_error",
            )],
            dialect=resolved,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "internal parse failure: %s: %s",
            type(exc).__name__,
            str(exc),
        )
        return InternalFailure(
            kind="internal_error",
            detail="An internal error occurred during parsing.",
        )


__all__ = [
    "InternalFailure",
    "ParseError",
    "ParseErrorKind",
    "ParseResult",
    "parse",
    "parse_one",
]
