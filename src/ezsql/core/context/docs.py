"""Keyword + frontmatter document retrieval (plan §13, plan_phase4 FR-5).

No vector database: the corpus is small and query patterns are keyword-
shaped. Retrieval is deterministic, explainable, and dependency-free.

Corpus (decision D5 — bounded exfil surface):

- Bundled docs shipped inside the package (``ezsql/docs/*.md``), loaded
  via ``importlib.resources``.
- User-project docs from ``<root>/docs/**/*.md`` and
  ``<root>/.ezsql/docs/*.md`` ONLY. ``.ezsql`` is in the scanner's
  default skip-dirs, so the ``.ezsql/docs`` subtree is walked explicitly.

Retrieved content is **data, never instructions** (plan §16): sections
are returned as bounded text blocks for the calling pipeline to embed
inside untrusted-data delimiters.
"""

import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ezsql.config import EzsqlConfig

logger = logging.getLogger("ezsql.core.context.docs")

__all__ = ["DocSection", "retrieve_docs"]

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n", re.DOTALL
)
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):\s*(?P<value>.*)$",
                     re.MULTILINE)
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+)$", re.MULTILINE)

# Word-ish tokens for keyword scoring (lowercased).
_TOKEN_RE = re.compile(r"[a-z_][a-z0-9_]*")

# Bundled doc filenames shipped in the package (single source of truth:
# app.py prompts load the same files).
_BUNDLED_DOC_NAMES: tuple[str, ...] = (
    "optimizedsql.md",
    "securitysql.md",
    "explainsql.md",
)

# User-doc directories (decision D5).
_USER_DOC_DIRS: tuple[str, ...] = ("docs", ".ezsql/docs")


@dataclass(frozen=True)
class DocSection:
    """One bounded, scored section from a doc in the corpus."""

    source: str  # "bundled:<name>" or "user:<relative-path>"
    title: str
    text: str
    score: float


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Split YAML-ish frontmatter (flat key: value pairs) from body."""
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return {}, content
    fm_text = match.group("fm")
    body = content[match.end():]
    meta: dict[str, str] = {}
    for kv in _KEY_RE.finditer(fm_text):
        meta[kv.group("key").lower()] = kv.group("value").strip().strip("'\"")
    return meta, body


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split a markdown body into (title, text) sections by heading.

    Content before the first heading becomes a section with an empty
    title.
    """
    sections: list[tuple[str, str]] = []
    boundaries: list[tuple[int, int, str]] = []  # (start, level_len, title)
    for m in _HEADING_RE.finditer(body):
        boundaries.append((m.start(), len(m.group("level")), m.group("title")))
    if not boundaries:
        return [("", body.strip())]
    if boundaries[0][0] > 0:
        sections.append(("", body[: boundaries[0][0]].strip()))
    for i, (start, _lvl, title) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(body)
        sections.append((title, body[start:end].strip()))
    return [(t, txt) for t, txt in sections if txt]


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _score_section(
    query_tokens: list[str],
    title: str,
    text: str,
    keywords_meta: str,
) -> float:
    """Deterministic keyword + frontmatter scoring.

    Title hits and frontmatter-keyword hits outweigh body hits.
    """
    title_tokens = set(_tokens(title))
    keyword_tokens = set(_tokens(keywords_meta))
    body_tokens = set(_tokens(text))
    score = 0.0
    for qt in query_tokens:
        if qt in title_tokens:
            score += 3.0
        if qt in keyword_tokens:
            score += 2.0
        if qt in body_tokens:
            score += 1.0
    return score


def _bounded(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _load_bundled() -> list[tuple[str, str, str]]:
    """Load bundled docs: (source_label, frontmatter_keywords, content)."""
    loaded: list[tuple[str, str, str]] = []
    for name in _BUNDLED_DOC_NAMES:
        try:
            content = (
                resources.files("ezsql") / "docs" / name
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            logger.warning("bundled_doc_unreadable", extra={"name": name})
            continue
        loaded.append((f"bundled:{name}", "", content))
    return loaded


def _load_user_docs(root: Path, config: EzsqlConfig) -> list[tuple[str, str, str]]:
    """Load user-project docs from the two allowed directories (D5)."""
    loaded: list[tuple[str, str, str]] = []
    for dir_rel in _USER_DOC_DIRS:
        base = root / dir_rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            try:
                if path.stat().st_size > config.max_file_size:
                    continue
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(root)
            loaded.append((f"user:{rel.as_posix()}", "", content))
    return loaded


def retrieve_docs(
    query: str,
    root: Path,
    *,
    config: EzsqlConfig,
) -> list[DocSection]:
    """Retrieve bounded doc sections relevant to ``query`` (plan §13).

    Args:
        query: The information need (keywords).
        root: Project root (for user docs).
        config: Loaded config (bounds).

    Returns:
        Scored, bounded sections — highest score first, at most
        ``config.max_doc_sections``. Sections with score 0 are excluded.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return []

    corpus = _load_bundled() + _load_user_docs(root, config)

    scored: list[DocSection] = []
    for source, _unused, content in corpus:
        meta, body = _parse_frontmatter(content)
        keywords_meta = meta.get("keywords", "")
        for title, text in _split_sections(body):
            score = _score_section(query_tokens, title, text, keywords_meta)
            if score <= 0:
                continue
            bounded_text, _trunc = _bounded(text, config.max_doc_section_chars)
            scored.append(
                DocSection(
                    source=source,
                    title=title,
                    text=bounded_text,
                    score=score,
                )
            )

    scored.sort(key=lambda s: (-s.score, s.source, s.title))
    return scored[: config.max_doc_sections]
