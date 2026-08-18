"""Host-language source inspection for injection detection (plan §15.7).

Operates on Python source files (``python_source`` input kind), not on
runtime SQL strings. Uses the ``ast`` module for structural detection.

Findings from hostlang are:
- ``evidence: static`` (source code structurally contains the pattern)
- ``kind: inference`` (we infer it *may* be unsafe; not modeling input trust)
- ``severity: medium`` (not critical — haven't proven exploit path)
- Message: "Detected potentially unsafe dynamic SQL construction."

Phase 2 does NOT implement taint analysis. The finding is an inference
that the agent should investigate further.
"""

import ast
import re

# SQL keywords for heuristic detection (used for line-level pre-filtering).
_SQL_KEYWORDS_PATTERN = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|FROM|WHERE|JOIN)\b",
    re.IGNORECASE,
)


def detect_fstring_sql(line: str) -> bool:
    """Detect f-string interpolation containing SQL keywords.

    This is a heuristic line-level check. It looks for f-strings (``f"..."``
    or ``f'...'``) that contain SQL keywords AND interpolation braces (``{``).

    This is NOT a full AST analysis — it's a fast pre-filter. False positives
    are possible (e.g., a string that happens to contain "SELECTED USERS" and
    a brace). The finding is ``kind: inference``, not ``fact``.
    """
    if not _SQL_KEYWORDS_PATTERN.search(line):
        return False
    # Check for f-string prefix
    if not re.search(r"\bf['\"]", line):
        return False
    # Check for interpolation braces
    return "{" in line


def detect_concat_sql(line: str) -> bool:
    """Detect string concatenation producing SQL.

    Looks for ``+`` concatenation involving strings with SQL keywords.
    This is a heuristic — false positives are possible. The finding is
    ``kind: inference``.
    """
    if not _SQL_KEYWORDS_PATTERN.search(line):
        return False
    # Check for string concatenation patterns
    # Pattern: "..." + variable or variable + "..."
    if re.search(r"['\"][^'\"]*['\"]\s*\+", line):
        return True
    return bool(re.search(r"\+\s*['\"][^'\"]*['\"]", line))


def detect_unsafe_execute(source: str) -> list[tuple[int, str]]:
    """Detect ``execute()`` calls that might use dynamic SQL.

    Uses the ``ast`` module for structural detection. Returns a list of
    ``(line_number, call_text)`` tuples for suspicious execute calls.

    This is more precise than regex — it finds actual method calls named
    ``execute`` or ``executemany``.
    """
    findings: list[tuple[int, str]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return findings

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                method_name = func.attr
                if method_name in ("execute", "executemany") and node.args:
                    arg = node.args[0]
                    if not isinstance(arg, ast.Constant):
                        line = node.lineno if hasattr(node, "lineno") else 0
                        findings.append((line, method_name))
    return findings


def is_safe_parameterized(source: str) -> bool:
    """Check if execute() calls use parameterized queries.

    A parameterized query passes the SQL and parameters separately:
    ``cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))``

    Returns True if ALL execute calls appear to be parameterized.
    Returns False if any execute call uses dynamic SQL construction.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    has_execute = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany"):
                has_execute = True
                if node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        continue
                    if len(node.args) >= 2:
                        continue
                    return False
    return has_execute


__all__ = [
    "detect_concat_sql",
    "detect_fstring_sql",
    "detect_unsafe_execute",
    "is_safe_parameterized",
]
