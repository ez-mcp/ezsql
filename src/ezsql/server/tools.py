"""MCP tool registrations for EZSQL.

Phase 2 registers 4 tools: ``find_context``, ``analyze_sql``, ``sql_sec``,
``optimize_query``. The remaining 4 tools are stub modules (kept for
architectural visibility) but NOT registered until their phases.

Per-call config loading (Gap 1 fix): the config passed to ``register_tools``
is used only for root resolution's ``project_root`` fallback. Once a root is
resolved, ``load_config(root)`` is called to apply scan limits + cache
sizing from ``<root>/.ezsql/config.toml``.
"""

from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from ezsql.config import EzsqlConfig, load_config
from ezsql.observability import counters, logger
from ezsql.pipelines.analyze import run_analyze_sql
from ezsql.pipelines.context import run_find_context
from ezsql.pipelines.optimize import run_optimize_query
from ezsql.pipelines.security import run_sql_sec
from ezsql.server.cache_lifecycle import get_cache
from ezsql.server.models import FailureEnvelope
from ezsql.server.roots import resolve_root

# Tool descriptions with trigger keywords (plan §8 — primary activation signal).
_FIND_CONTEXT_DESCRIPTION = (
    "Find SQL-bearing files, migrations, ORM models, queries, and configs "
    "in a repository. Returns a grouped map of relevant files classified by "
    "type (migration, query, orm, config, doc, unknown). Use this FIRST for "
    "any SQL, Postgres, MySQL, SQLite, Supabase, migration, schema, index, "
    "query, or database work to orient yourself in the repository's SQL "
    "surface. The 'root' parameter is required (absolute path to the project "
    "root) unless pinned via .ezsql/config.toml."
)

_ANALYZE_SQL_DESCRIPTION = (
    "Analyze SQL statements: parse, extract AST facts (tables, columns, "
    "joins, predicates), and run lint heuristics. Returns structured "
    "findings with two-dimensional evidence (source × claim kind). Use for "
    "any SQL, Postgres, MySQL, SQLite, query, SELECT, JOIN, WHERE, or "
    "database analysis work. Pass 'sql' with the SQL string and optionally "
    "'dialect' (postgres, mysql, sqlite, etc.)."
)

_SQL_SEC_DESCRIPTION = (
    "Security analysis of SQL or source files. Detects dangerous statements "
    "(DROP TABLE, DELETE without WHERE, TRUNCATE), migration safety issues "
    "(DROP COLUMN), and host-language injection patterns (f-string SQL, "
    "string concatenation). Returns findings with rule IDs, severities, and "
    "coverage tracking. [] findings ≠ secure — check coverage. Use for SQL "
    "security, injection, migration safety, DROP, DELETE, UPDATE, or "
    "database safety work. Pass 'sql' for SQL string mode or 'files' for "
    "file-based analysis."
)

_OPTIMIZE_QUERY_DESCRIPTION = (
    "Optimize SQL queries with static analysis. Detects SELECT *, correlated "
    "subqueries, type mismatches, and missing indexes. Generates rewrite "
    "candidates (SELECT * expansion). All findings carry two-dimensional "
    "evidence (static/schema × fact/inference). No runtime claims without "
    "EXPLAIN. Use for query optimization, performance, index, SELECT *, "
    "subquery, or database performance work. Pass 'sql' with the query and "
    "optionally 'dialect'."
)


def register_tools(
    mcp: MCPServer,
    config: EzsqlConfig,
    cache: Any | None = None,  # noqa: ARG001 — kept for API compat; cache wired per-call
) -> None:
    """Register all Phase 2 tools on the given MCPServer instance.

    Args:
        mcp: The MCPServer instance.
        config: The register-time EZSQL config (used for root resolution's
            ``project_root`` fallback only). Per-call config is loaded from
            ``<root>/.ezsql/config.toml`` inside the tool.
        cache: Unused (kept for API compatibility). Cache is wired per-call
            via ``get_cache`` so it is sized from the per-call config.
    """

    @mcp.tool(name="find_context", description=_FIND_CONTEXT_DESCRIPTION)
    def find_context(
        root: str | None = None,
        query: str | None = None,
        task: str | None = None,  # noqa: ARG001 — no-op in Phase 1 (§17 Q7)
    ) -> dict[str, Any]:
        """Find SQL-bearing files in the repository."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="find_context", root=root, query=query)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            logger.warning("find_context_failed", kind=root_result.kind, detail=root_result.detail)
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_find_context(root_path, call_config, cache=cache, query=query)
        return result.model_dump()

    @mcp.tool(name="analyze_sql", description=_ANALYZE_SQL_DESCRIPTION)
    def analyze_sql(
        sql: str,
        root: str | None = None,
        dialect: str | None = None,
        task: str | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        """Analyze SQL: parse, extract AST facts, run lint heuristics."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="analyze_sql", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_analyze_sql(
            sql, call_config, cache=cache, dialect=dialect, task=task,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()

    @mcp.tool(name="sql_sec", description=_SQL_SEC_DESCRIPTION)
    def sql_sec(
        root: str,
        sql: str | None = None,
        files: list[str] | None = None,
        dialect: str | None = None,
        task: str | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        """Security analysis of SQL or source files."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="sql_sec", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_sql_sec(
            call_config, root_path, cache=cache,
            sql=sql, files=files, dialect=dialect, task=task,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()

    @mcp.tool(name="optimize_query", description=_OPTIMIZE_QUERY_DESCRIPTION)
    def optimize_query(
        sql: str,
        root: str | None = None,
        dialect: str | None = None,
        task: str | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        """Optimize SQL queries with static analysis."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="optimize_query", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_optimize_query(
            sql, call_config, cache=cache, dialect=dialect, task=task,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()


__all__ = ["register_tools"]
