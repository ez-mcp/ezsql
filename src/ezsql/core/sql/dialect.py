"""SQL dialect resolution and advisory inference.

Dialect resolution chain (plan §10.1):
1. Explicit dialect parameter (agent-supplied)
2. Project-configured dialect (``.ezsql/config.toml``)
3. ``"unknown"``

When ``"unknown"``, sqlglot parses with its generic/broad dialect.
Dialect-dependent findings are withheld (not downgraded).

Advisory inference (plan §10.3): ``infer_dialect()`` is NEVER called
automatically by the parse pipeline. It returns a ``rank_score`` (heuristic
ranking score, NOT a calibrated probability) for ordering candidates.
"""

import re
from dataclasses import dataclass, field

import sqlglot

# Known dialects (from sqlglot.dialects.DIALECTS, which is a list of names).
_KNOWN_DIALECTS: frozenset[str] = frozenset(
    name.lower() for name in sqlglot.dialects.DIALECTS
)


def list_dialects() -> list[str]:
    """Return the list of dialects recognized by sqlglot."""
    return sorted(_KNOWN_DIALECTS)


def resolve_dialect(
    dialect: str | None,
    configured_dialect: str | None,
) -> str:
    """Resolve dialect: explicit → configured → ``"unknown"``.

    Validates against sqlglot's known dialects. An invalid dialect name
    falls back to ``"unknown"`` (fail safely, never invent).
    """
    if dialect is not None and dialect.strip():
        resolved = dialect.strip().lower()
        if resolved in _KNOWN_DIALECTS:
            return resolved
        return "unknown"
    if configured_dialect is not None and configured_dialect.strip():
        resolved = configured_dialect.strip().lower()
        if resolved in _KNOWN_DIALECTS:
            return resolved
        return "unknown"
    return "unknown"


def is_known_dialect(dialect: str) -> bool:
    """Check if a dialect name is recognized by sqlglot."""
    return dialect.strip().lower() in _KNOWN_DIALECTS


# --- Advisory inference (NOT auto-detection) ---

# Syntax markers for dialect inference. Each marker contributes evidence
# for a specific dialect. These are heuristic patterns, not definitive.
_DIALECT_MARKERS: dict[str, list[tuple[str, str]]] = {
    "postgres": [
        (r"::", "double-colon cast operator"),
        (r"\bSERIAL\b", "SERIAL type"),
        (r"\bBIGSERIAL\b", "BIGSERIAL type"),
        (r"\bTIMESTAMPTZ\b", "TIMESTAMPTZ type"),
        (r"\bRETURNING\b", "RETURNING clause"),
        (r"\bILIKE\b", "ILIKE operator"),
        (r"\b~\b", "regex match operator"),
        (r"\bARRAY\[", "ARRAY constructor"),
        (r"\bjsonb\b", "jsonb type"),
        (r"\bgen_random_uuid\b", "gen_random_uuid function"),
    ],
    "mysql": [
        (r"\bAUTO_INCREMENT\b", "AUTO_INCREMENT"),
        (r"\bENGINE\s*=", "ENGINE clause"),
        (r"\bCHARSET\b", "CHARSET clause"),
        (r"`\w+`", "backtick identifiers"),
        (r"\bLIMIT\s+\d+\s*,\s*\d+", "comma-separated LIMIT"),
        (r"\bMEDIUMINT\b", "MEDIUMINT type"),
        (r"\bTINYINT\b", "TINYINT type"),
    ],
    "sqlite": [
        (r"\bAUTOINCREMENT\b", "AUTOINCREMENT"),
        (r"\bINTEGER\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY (rowid alias)"),
        (r"\bPRAGMA\b", "PRAGMA statement"),
        (r"\bGLOB\b", "GLOB operator"),
    ],
    "tsql": [
        (r"\bTOP\s+\d+", "TOP clause"),
        (r"\b@@\w+", "global variable (@@var)"),
        (r"\bNVARCHAR\b", "NVARCHAR type"),
        (r"\bGETDATE\(\)", "GETDATE function"),
        (r"\bISNULL\b", "ISNULL function"),
        (r"\[\w+\]", "bracket identifiers"),
    ],
    "oracle": [
        (r"\bROWNUM\b", "ROWNUM"),
        (r"\bDUAL\b", "DUAL table"),
        (r"\bSYSDATE\b", "SYSDATE"),
        (r"\bNVARCHAR2\b", "NVARCHAR2 type"),
        (r"\bNUMBER\b", "NUMBER type"),
    ],
    "bigquery": [
        (r"\bSTRUCT\b", "STRUCT type"),
        (r"\bARRAY\b", "ARRAY type"),
        (r"\b_EXTRACT_DATE\b", "BigQuery pseudo-column"),
    ],
}


@dataclass(frozen=True)
class DialectInference:
    """Advisory dialect inference. Never called automatically.

    ``rank_score`` is a heuristic ranking score, NOT a calibrated probability.
    It has no statistical meaning. It exists only to order candidates.
    """

    candidates: list[str] = field(default_factory=list)
    rank_score: float = 0.0
    evidence: list[str] = field(default_factory=list)


def infer_dialect(sql: str) -> DialectInference:
    """Infer likely dialect from syntax markers. Advisory only.

    ``rank_score`` is a heuristic ranking score, not a calibrated probability.
    It has no validation dataset, no statistical meaning. It orders candidates;
    it does not predict. Never called automatically by the parse pipeline.
    """
    if not sql or not sql.strip():
        return DialectInference()

    scores: dict[str, list[str]] = {}
    for dialect_name, markers in _DIALECT_MARKERS.items():
        evidence: list[str] = []
        for pattern, description in markers:
            if re.search(pattern, sql, re.IGNORECASE):
                evidence.append(description)
        if evidence:
            scores[dialect_name] = evidence

    if not scores:
        return DialectInference()

    # Rank by number of markers matched (more markers = higher rank)
    ranked = sorted(scores.items(), key=lambda x: len(x[1]), reverse=True)
    best_dialect, best_evidence = ranked[0]
    best_score = len(best_evidence) / 10.0  # normalize to 0-1ish range

    candidates = [d for d, _ in ranked]
    all_evidence = [f"{d}: {', '.join(e)}" for d, e in ranked]

    return DialectInference(
        candidates=candidates,
        rank_score=min(best_score, 1.0),
        evidence=all_evidence,
    )


__all__ = [
    "DialectInference",
    "infer_dialect",
    "is_known_dialect",
    "list_dialects",
    "resolve_dialect",
]
