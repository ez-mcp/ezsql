"""Find SQL files and non SQL files that have embedded SQL queries."""

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



def deepsearchsql(root: Path) -> list[Path]:
    """Return .sql files plus non-.sql files containing SQL keywords.
    
    Directories named env/, .git/, __pycache__/ or node_modules/ are skipped.
    """
    matches: list[Path] = search_sql(root)
    seen: set[Path] = set(matches)

    for file in root.rglob("*"):
        if file in seen or not file.is_file():
            continue
        if any(part in DEFAULT_SKIP_DIRS for part in file.relative_to(root).parts):
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue 
        if SQL_KEYWORD_PATTERN.search(text):
            seen.add(file)
            matches.append(file)
    return matches
















        
            
    
    
    
    
    
    


