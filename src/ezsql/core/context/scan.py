"""Context scanning and repository analysis for SQL files.

Evolved from the original ``sql_search.py``: pruned ``os.walk`` scan with
file classification (migration/query/ORM/config/doc/unknown), DoS protection
(file size limits, binary detection, depth limit), and symlink safety
(``followlinks=False``).
"""

import os
import re
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

# --- Classification types (plan §5.8, §17 Q4 — confirmed sufficient) ---

FileClassification = Literal[
    "migration", "query", "orm", "config", "doc", "unknown"
]

# --- SQL keyword detection (unchanged from original) ---

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
    "Supabase",
)

SQL_KEYWORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:" + "|".join(SQL_STATEMENT_KEYWORDS) + r")\b", re.IGNORECASE
)

# --- Skip directories (pruned before descent) ---

DEFAULT_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        "env", ".git", "__pycache__", "node_modules",
        ".pytest_cache", ".ruff_cache", ".mypy_cache",
        ".venv", "venv", "dist", "build", ".tox",
        ".eggs", ".ezsql",  # never scan our own cache dir
    }
)

# --- Classification heuristics (plan §5.8) ---

_MIGRATION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\d+_.*\.sql$", re.IGNORECASE),  # 001_init.sql
    re.compile(r"^V\d+__.*\.sql$", re.IGNORECASE),  # Flyway V1__init.sql
    re.compile(r".*\.migration\.sql$", re.IGNORECASE),
)

_ORM_MARKERS: Final[frozenset[str]] = frozenset({
    "sqlalchemy", "SQLAlchemy",
    "prisma", "Prisma",
    "activerecord", "ActiveRecord",
    "gorm", "GORM",
    "django.db", "models.Model",
    "sequelize", "Sequelize",
    "typeorm", "TypeORM",
    "sqlmodel", "SQLModel",
})

_CONFIG_EXTS: Final[frozenset[str]] = frozenset({
    ".toml", ".yaml", ".yml", ".env", ".ini", ".cfg", ".json",
})

_DOC_EXTS: Final[frozenset[str]] = frozenset({".md", ".rst"})

_ORM_EXTS: Final[frozenset[str]] = frozenset({
    ".py", ".ts", ".js", ".rb", ".go", ".rs",
})

# Binary detection: files with null bytes in the first 1 KiB are skipped (T2.4).
_BINARY_CHECK_SIZE: Final[int] = 1024


class ScanResult(NamedTuple):
    """Result of a scan: classified files plus DoS-limit metadata.

    ``by_dir`` maps directory path relative to root (``"."`` for root-level)
    to the list of matched files. ``truncated`` is True when a scan limit
    (file count or total bytes) was hit. ``files_skipped`` counts files
    dropped by size/binary/byte-cap guards (plan §23 ScanMetadata).
    """

    by_dir: dict[str, list[Any]]
    truncated: bool
    files_skipped: int


def _is_binary(path: Path) -> bool:
    """Check if a file is binary by looking for null bytes in the first 1 KiB (T2.4)."""
    try:
        with open(path, "rb") as f:
            chunk: bytes = f.read(_BINARY_CHECK_SIZE)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True  # treat unreadable as binary (skip)


def classify_file(name: str, path: Path) -> FileClassification | None:
    """Classify a file by its name and (optionally) content.

    Returns ``None`` if the file doesn't match any SQL-related category
    (caller should skip it). Classification is heuristic + deterministic
    (plan §5.8) — no LLM, no embeddings.

    Args:
        name: The filename.
        path: The full path (used for content-based ORM detection).

    Returns:
        The classification, or None if the file is not SQL-relevant.
    """
    # .sql files are always relevant
    if name.endswith(".sql"):
        for pattern in _MIGRATION_PATTERNS:
            if pattern.match(name):
                return "migration"
        return "query"

    # Config files
    ext = path.suffix.lower()
    if ext in _CONFIG_EXTS:
        # Skip lock files
        if "lock" in name.lower():
            return None
        return "config"

    # Doc files
    if ext in _DOC_EXTS:
        return "doc"

    # ORM files — check content for ORM markers
    if ext in _ORM_EXTS:
        try:
            if _is_binary(path):
                return None
            with open(path, encoding="utf-8") as f:
                text: str = f.read()
        except (UnicodeDecodeError, OSError):
            return None
        for marker in _ORM_MARKERS:
            if marker in text:
                return "orm"
        # Also match if it has SQL keywords
        if SQL_KEYWORD_PATTERN.search(text):
            return "unknown"
        return None

    # Other text files — check for SQL keywords
    try:
        if _is_binary(path):
            return None
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        return None
    if SQL_KEYWORD_PATTERN.search(text):
        return "unknown"
    return None


def deepsearchsql(
    root: Path,
    *,
    max_file_size: int = 1024 * 1024,
    max_files_per_scan: int = 50_000,
    max_total_bytes: int = 256 * 1024 * 1024,
    max_scan_depth: int = 20,
) -> ScanResult:
    """Return SQL-related files grouped by directory.

    Matches every ``.sql`` file (matched by name, never read for content
    unless classifying) plus any readable text file containing a SQL
    statement keyword or ORM marker. Directories in ``DEFAULT_SKIP_DIRS``
    are pruned before descent. Files larger than ``max_file_size`` are
    skipped for content reading (T2.1). Binary files are skipped (T2.4).
    Symlinks are not followed (T1.3 — ``followlinks=False``).

    Returns a ``ScanResult`` whose ``by_dir`` maps each directory's path
    relative to ``root`` (``"."`` for files directly under ``root``) to
    the sorted list of matching filenames. Keys are sorted and use "/"
    separators for deterministic output; directories with no matches are
    absent. ``truncated`` and ``files_skipped`` report DoS-limit hits.
    """
    by_dir: dict[str, list[str]] = {}
    files_seen: int = 0
    files_skipped: int = 0
    total_bytes_read: int = 0
    truncated: bool = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune skipped subtrees before descending (T1.3)
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIRS]

        # Depth limit (T1.4)
        rel_depth = os.path.relpath(dirpath, root).count(os.sep)
        if rel_depth >= max_scan_depth:
            dirnames.clear()  # stop descending
            continue

        rel: str = os.path.relpath(dirpath, root).replace(os.sep, "/")

        for name in filenames:
            # File count cap (T2.2)
            if files_seen >= max_files_per_scan:
                truncated = True
                break

            if name in DEFAULT_SKIP_DIRS:
                continue

            file_path = Path(dirpath) / name

            # Size check for content reading (T2.1)
            try:
                file_size = file_path.stat().st_size
            except OSError:
                continue

            # For .sql files, we match by name — no content read needed
            if name.endswith(".sql"):
                by_dir.setdefault(rel, []).append(name)
                files_seen += 1
                continue

            # For non-.sql files, check size before reading
            if file_size > max_file_size:
                files_skipped += 1  # oversized — counted as skipped (T2.1)
                continue

            # Total bytes cap (T2.3)
            if total_bytes_read + file_size > max_total_bytes:
                truncated = True
                break

            # Binary detection (T2.4)
            if _is_binary(file_path):
                files_skipped += 1
                continue

            try:
                with open(file_path, encoding="utf-8") as f:
                    text: str = f.read()
            except (UnicodeDecodeError, OSError):
                files_skipped += 1
                continue

            total_bytes_read += len(text.encode("utf-8"))

            if SQL_KEYWORD_PATTERN.search(text):
                by_dir.setdefault(rel, []).append(name)
                files_seen += 1
            else:
                files_skipped += 1  # read but no SQL keyword — not a match

        if truncated:
            break

    sorted_by_dir = {key: sorted(names) for key, names in sorted(by_dir.items())}
    return ScanResult(by_dir=sorted_by_dir, truncated=truncated, files_skipped=files_skipped)


def scan_with_classification(
    root: Path,
    *,
    max_file_size: int = 1024 * 1024,
    max_files_per_scan: int = 50_000,
    max_total_bytes: int = 256 * 1024 * 1024,
    max_scan_depth: int = 20,
) -> ScanResult:
    """Scan and classify files (plan §5.8).

    Returns a ``ScanResult`` whose ``by_dir`` maps directory → list of
    (filename, classification). Uses ``classify_file`` for each matched
    file. ``truncated`` and ``files_skipped`` report DoS-limit hits.
    """
    by_dir: dict[str, list[tuple[str, FileClassification]]] = {}
    files_seen: int = 0
    files_skipped: int = 0
    total_bytes_read: int = 0
    truncated: bool = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIRS]

        rel_depth = os.path.relpath(dirpath, root).count(os.sep)
        if rel_depth >= max_scan_depth:
            dirnames.clear()
            continue

        rel: str = os.path.relpath(dirpath, root).replace(os.sep, "/")

        for name in filenames:
            if files_seen >= max_files_per_scan:
                truncated = True
                break

            if name in DEFAULT_SKIP_DIRS:
                continue

            file_path = Path(dirpath) / name

            try:
                file_size = file_path.stat().st_size
            except OSError:
                files_skipped += 1
                continue

            # .sql files are matched by name — no content read, no byte cap.
            if name.endswith(".sql"):
                classification = classify_file(name, file_path)
                if classification is not None:
                    by_dir.setdefault(rel, []).append((name, classification))
                    files_seen += 1
                continue

            # Non-.sql: enforce size cap (T2.1) before reading content.
            if file_size > max_file_size:
                files_skipped += 1
                continue

            # Total bytes cap (T2.3) — bounds content reads.
            if total_bytes_read + file_size > max_total_bytes:
                truncated = True
                break

            classification = classify_file(name, file_path)
            if classification is not None:
                by_dir.setdefault(rel, []).append((name, classification))
                files_seen += 1
                total_bytes_read += file_size
            else:
                files_skipped += 1  # read but not SQL-relevant

        if truncated:
            break

    sorted_by_dir = {key: sorted(vals) for key, vals in sorted(by_dir.items())}
    return ScanResult(by_dir=sorted_by_dir, truncated=truncated, files_skipped=files_skipped)


def build_file_manifest(root: Path) -> dict[str, tuple[float, int]]:
    """Build a cheap freshness manifest of SQL-relevant files under ``root``.

    Returns ``{relative_path: (mtime_ns, size)}`` for every ``.sql`` file
    and every readable text file containing a SQL keyword or ORM marker.
    This is the "mtime+size fast guard" from plan §14: recomputing it via
    ``os.walk`` + ``stat`` (no content reads for ``.sql``; one read for
    text files) lets the pipeline detect staleness without a full re-scan.

    Only the files that *would* appear in a scan result are included, so a
    manifest comparison is a sufficient freshness signal: if every matched
    file has the same mtime+size, the cached classification is still valid.
    """
    manifest: dict[str, tuple[float, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        for name in filenames:
            if name in DEFAULT_SKIP_DIRS:
                continue
            file_path = Path(dirpath) / name
            try:
                st = file_path.stat()
            except OSError:
                continue
            rel_path = f"{rel_dir}/{name}" if rel_dir != "." else name
            # .sql files are always relevant (matched by name).
            if name.endswith(".sql"):
                manifest[rel_path] = (st.st_mtime_ns, st.st_size)
                continue
            # Non-.sql: only include if it has a SQL keyword (cheap content read).
            if st.st_size > 1024 * 1024:  # skip oversized for manifest too
                continue
            if _is_binary(file_path):
                continue
            try:
                with open(file_path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            if SQL_KEYWORD_PATTERN.search(text) or any(
                marker in text for marker in _ORM_MARKERS
            ):
                manifest[rel_path] = (st.st_mtime_ns, st.st_size)
    return manifest


__all__ = [
    "SQL_KEYWORD_PATTERN",
    "SQL_STATEMENT_KEYWORDS",
    "DEFAULT_SKIP_DIRS",
    "FileClassification",
    "ScanResult",
    "build_file_manifest",
    "classify_file",
    "deepsearchsql",
    "scan_with_classification",
]
