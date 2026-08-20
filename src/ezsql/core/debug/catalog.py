"""Deterministic database error catalog (plan_phase4 FR-4, decision D4).

Rules-are-data: each entry is a declarative ``(catalog_id, error_pattern,
dbms_scope, diagnosis, fix_guidance, severity)`` record. The catalog grows
by adding rows, not code paths. Verdicts are deterministic — an LLM can
never raise or lower a catalog match (plan §16).

Matching is regex-based against the raw error text (PostgreSQL error
messages embed SQLSTATE codes and stable phrasing). Entries are ranked
by specificity: SQLSTATE-code matches outrank message-text matches.
"""

import re
from dataclasses import dataclass

__all__ = ["DEBUG_CATALOG_VERSION", "CatalogEntry", "CatalogMatch", "match_error"]

DEBUG_CATALOG_VERSION = "1"


@dataclass(frozen=True)
class CatalogEntry:
    """One deterministic error-catalog rule (rules-are-data, plan §10)."""

    catalog_id: str
    error_pattern: "re.Pattern[str]"
    dbms_scope: str  # "postgres", "any", ...
    diagnosis: str
    fix_guidance: str
    severity: str  # "critical" | "high" | "medium" | "low" | "info"
    # SQLSTATE-code matches are more specific than message-text matches.
    specificity: int = 1  # 2 = SQLSTATE code, 1 = message text


@dataclass(frozen=True)
class CatalogMatch:
    """A ranked catalog match for an error text."""

    catalog_id: str
    diagnosis: str
    fix_guidance: str
    severity: str
    specificity: int


def _p(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.IGNORECASE)


# Initial PostgreSQL error classes (plan_phase4 FR-4). Ordered by SQLSTATE.
_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        catalog_id="PG-42601",
        error_pattern=_p(r"\b42601\b|syntax error at or near"),
        dbms_scope="postgres",
        diagnosis=(
            "Syntax error: the statement violates PostgreSQL grammar at the "
            "reported position."
        ),
        fix_guidance=(
            "Check the token at the reported position for typos, missing "
            "commas, unbalanced parentheses, or a reserved word used as an "
            "identifier (quote it if intended)."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-42703",
        error_pattern=_p(r"\b42703\b|column .* does not exist"),
        dbms_scope="postgres",
        diagnosis="Undefined column: the referenced column is not in any table in scope.",
        fix_guidance=(
            "Verify the column name spelling and the table alias; check that "
            "the column exists in the schema (analyze_sql or the repo DDL)."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-42P01",
        error_pattern=_p(r"\b42P01\b|relation .* does not exist"),
        dbms_scope="postgres",
        diagnosis="Undefined table: the referenced relation is not in the search path.",
        fix_guidance=(
            "Check the table name and schema qualification; confirm the "
            "migration creating it has been applied (repo DDL cross-check)."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-42883",
        error_pattern=_p(r"\b42883\b|operator does not exist"),
        dbms_scope="postgres",
        diagnosis="No operator matches the operand types (implicit cast missing).",
        fix_guidance=(
            "Add an explicit cast for the compared values (e.g. "
            "col::text) or align the operand types; watch for "
            "quoted numeric literals compared to non-numeric columns."
        ),
        severity="medium",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-42804",
        error_pattern=_p(r"\b42804\b|datatype mismatch"),
        dbms_scope="postgres",
        diagnosis="Datatype mismatch between expected and provided values.",
        fix_guidance=(
            "Align the datatypes on both sides of the assignment/comparison; "
            "use explicit casts rather than relying on implicit conversion."
        ),
        severity="medium",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-22P02",
        error_pattern=_p(r"\b22P02\b|invalid input syntax for type"),
        dbms_scope="postgres",
        diagnosis="A literal or parameter value cannot be parsed as the target type.",
        fix_guidance=(
            "Validate/normalize the input value before it reaches the query; "
            "check locale formats for numbers and dates."
        ),
        severity="medium",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-23505",
        error_pattern=_p(r"\b23505\b|duplicate key value violates unique constraint"),
        dbms_scope="postgres",
        diagnosis="A unique or primary-key constraint was violated by the inserted/updated row.",
        fix_guidance=(
            "Check for an existing row with the same key (upsert with "
            "ON CONFLICT if the semantics allow), or generate keys "
            "sequentially/uuids instead of client-side guesses."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-23503",
        error_pattern=_p(r"\b23503\b|violates foreign key constraint"),
        dbms_scope="postgres",
        diagnosis="Foreign-key constraint violation: referenced row does not exist.",
        fix_guidance=(
            "Insert or reference an existing parent row first; verify the FK "
            "column values against the parent table's keys."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-42P07",
        error_pattern=_p(r"\b42P07\b|relation .* already exists"),
        dbms_scope="postgres",
        diagnosis="A relation with the same name already exists (duplicate DDL).",
        fix_guidance=(
            "Use CREATE TABLE IF NOT EXISTS, or check migration ordering / "
            "partial application before re-running DDL."
        ),
        severity="medium",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-42501",
        error_pattern=_p(r"\b42501\b|permission denied"),
        dbms_scope="postgres",
        diagnosis="The current role lacks the required privilege for the operation.",
        fix_guidance=(
            "Grant the needed privilege to the role (least-privilege scope), "
            "or run the operation with a role that holds it."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-40P01",
        error_pattern=_p(r"\b40P01\b|deadlock detected"),
        dbms_scope="postgres",
        diagnosis="Deadlock: two transactions each hold locks the other needs.",
        fix_guidance=(
            "Acquire locks in a consistent order across transactions; keep "
            "transactions short; retry on 40P01 with backoff."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-57014",
        error_pattern=_p(r"\b57014\b|statement timeout|canceling statement due to"),
        dbms_scope="postgres",
        diagnosis="The statement exceeded the configured timeout and was canceled.",
        fix_guidance=(
            "Optimize the query (see optimize_query findings), add "
            "supporting indexes, or raise the statement timeout if the "
            "workload legitimately needs longer."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-53300",
        error_pattern=_p(r"\b53300\b|too many connections"),
        dbms_scope="postgres",
        diagnosis="Server connection limit reached.",
        fix_guidance=(
            "Use pooling (pgbouncer or pool in app), reduce pool sizes, or "
            "raise max_connections on the server."
        ),
        severity="high",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="PG-55006",
        error_pattern=_p(r"\b55006\b|cannot drop .* because other objects depend on it"),
        dbms_scope="postgres",
        diagnosis="Dependent objects prevent the drop.",
        fix_guidance=(
            "Drop dependents first or use CASCADE deliberately; review the "
            "dependency list before cascading."
        ),
        severity="medium",
        specificity=2,
    ),
    CatalogEntry(
        catalog_id="GEN-TIMEOUT",
        error_pattern=_p(r"\btimeout expired\b|timed out\b|connection timed out"),
        dbms_scope="any",
        diagnosis="An operation or connection timed out.",
        fix_guidance=(
            "Check network reachability and load; add explicit timeouts and "
            "bounded retries with backoff."
        ),
        severity="medium",
        specificity=1,
    ),
    CatalogEntry(
        catalog_id="GEN-CONNREFUSED",
        error_pattern=_p(r"connection refused|could not connect"),
        dbms_scope="any",
        diagnosis="The database server refused or could not be reached.",
        fix_guidance=(
            "Verify host/port, that the server is running, and firewall/"
            "network policy; check the connection string env var name."
        ),
        severity="high",
        specificity=1,
    ),
)


def match_error(error_text: str, dialect: str) -> list[CatalogMatch]:
    """Match an error text against the catalog, ranked by specificity.

    Args:
        error_text: Raw error text (untrusted — treated as data).
        dialect: Resolved dialect; entries scoped to other DBMS are
            skipped. ``"any"``-scoped entries always apply.

    Returns:
        Ranked matches (highest specificity first, then catalog order).
        Empty list is a valid honest answer ("no match").
    """
    matches: list[CatalogMatch] = []
    for entry in _CATALOG:
        if entry.dbms_scope not in ("any", dialect):
            continue
        if entry.error_pattern.search(error_text):
            matches.append(
                CatalogMatch(
                    catalog_id=entry.catalog_id,
                    diagnosis=entry.diagnosis,
                    fix_guidance=entry.fix_guidance,
                    severity=entry.severity,
                    specificity=entry.specificity,
                )
            )
    matches.sort(key=lambda m: -m.specificity)
    return matches
