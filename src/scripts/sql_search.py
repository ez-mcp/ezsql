"""Find SQL files and non SQL files that have embedded SQL queries."""

import os
import re
from pathlib import Path
from typing import Final

SQL_STATEMENT_KEYWORDS: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "JOIN",
)

SQL_KEYWORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:" + "|".join(SQL_STATEMENT_KEYWORDS) + r")\b", re.IGNORECASE
)

DEFAULT_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {"env", ".git", "__pycache__", "node_modules"}
)



def search_sql(root: Path) -> list[Path]:
    """Return every .sql file under root, recursively."""
    return [
        file
        for file in root.rglob("*.sql")
        if file.is_file()
        and not any(part in DEFAULT_SKIP_DIRS for part in file.relative_to(root).parts)
    ]



def deepsearchsql(root: Path) -> dict[str, list[str]]:
    """Return SQL-related files grouped by directory.

    Matches every ``.sql`` file (matched by name, never read) plus any
    readable text file containing a SQL statement keyword. Directories
    named env/, .git/, __pycache__/ or node_modules/ are pruned before
    descent, and files bearing those names are skipped.

    Returns a dict mapping each directory's path relative to ``root``
    (``"."`` for files directly under ``root``) to the sorted list of
    matching filenames in it. Keys are sorted and use "/" separators for
    deterministic output; directories with no matches are absent.
    Reconstruct a file path with ``root / key / name``.
    Unreadable files and directories are skipped silently, as before.
    """
    by_dir: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped subtrees before descending: never scan env/ etc.
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIRS]
        rel: str = os.path.relpath(dirpath, root).replace(os.sep, "/")
        for name in filenames:
            if name in DEFAULT_SKIP_DIRS:
                continue
            if name.endswith(".sql"):
                by_dir.setdefault(rel, []).append(name)
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    text: str = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            if SQL_KEYWORD_PATTERN.search(text):
                by_dir.setdefault(rel, []).append(name)
    return {key: sorted(names) for key, names in sorted(by_dir.items())}
















        
            
    
    
    
    
    
    


