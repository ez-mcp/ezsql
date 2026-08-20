"""MCP tool registrations for EZSQL.

Phase 4 registers all 8 tools: ``find_context``, ``analyze_sql``,
``sql_sec``, ``optimize_query``, ``explain_query``, ``design_schema``,
``refactor_sql``, ``debug_sql``.

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
from ezsql.pipelines.debug import run_debug_sql
from ezsql.pipelines.design import run_design_schema
from ezsql.pipelines.explain import run_explain_query
from ezsql.pipelines.optimize_runtime import run_optimize_query_with_runtime
from ezsql.pipelines.refactor import run_refactor_sql
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
    "evidence (static/schema × fact/inference). When a PostgreSQL database "
    "is configured, eligible candidates gain live planner evidence (typed "
    "plan deltas with estimated costs — planner estimates, not measured "
    "execution). Use for query optimization, performance, index, SELECT *, "
    "subquery, or database performance work. Pass 'sql' with the query and "
    "optionally 'dialect'."
)

_EXPLAIN_QUERY_DESCRIPTION = (
    "Get the PostgreSQL planner's plan for one query: normalized plan tree "
    "with estimated costs, row counts, scan/join operations, and planning "
    "time. Requires a configured database (db URL env var in "
    ".ezsql/config.toml). Input must be exactly one unprefixed PostgreSQL "
    "SELECT query (read-only CTEs and set operations allowed; EXPLAIN "
    "prefix, writes, commands, and locking clauses are rejected). Costs "
    "are planner estimates, not measured execution time. Use for plan, "
    "cost, scan, join, index usage, or query performance questions."
)

_DESIGN_SCHEMA_DESCRIPTION = (
    "Design a database schema from natural-language requirements: proposed "
    "tables, columns, types, primary/foreign keys, generated CREATE TABLE "
    "DDL, migration strategy, risks, and a Mermaid ERD. Deterministic "
    "derivation first; when it is inconclusive AND an LLM API key is "
    "configured, an advisory is attached (advisory-only, never a verdict). "
    "Generated DDL is validated against the security rule engine before "
    "being returned. Use for schema design, new tables, data modeling, "
    "entity relationships, or CREATE TABLE work. Pass 'requirements' with "
    "a description of the entities and relationships you need."
)

_REFACTOR_SQL_DESCRIPTION = (
    "Comprehensive refactoring report for a SQL target in one pass: "
    "security findings, optimization findings and rewrite candidates, and "
    "schema impact (missing tables/columns vs the repo schema), plus "
    "proposed changes the agent applies. Composes the sql_sec and "
    "optimize_query analyses internally — deterministic only. Use for "
    "query refactoring, code review of SQL, holistic improvement, or "
    "before applying changes to a query or .sql file. Pass 'sql' with the "
    "SQL text or 'files' with paths relative to the project root."
)

_DEBUG_SQL_DESCRIPTION = (
    "Diagnose a database error: deterministic error-catalog matching "
    "(PostgreSQL SQLSTATE codes and message patterns), schema/AST "
    "cross-check of the failing SQL against the repo schema, and ranked "
    "hypotheses with next diagnostics. When no catalog match is "
    "conclusive AND an LLM API key is configured, an advisory is attached "
    "(advisory-only, never a verdict). Use for database errors, failing "
    "queries, SQLSTATE codes, deadlocks, timeouts, 'relation does not "
    "exist', 'column does not exist', or connection issues. Pass 'error' "
    "with the error message and optionally 'sql' with the failing query."
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
    async def optimize_query(
        sql: str,
        root: str | None = None,
        dialect: str | None = None,
        task: str | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        """Optimize SQL queries with static analysis + optional live evidence."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="optimize_query", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        # Runtime enrichment only when a DB URL is configured (§5).
        db_uri = call_config.get_database_url()
        if db_uri is not None:
            from ezsql.db.lifecycle import get_adapter_lifecycle
            lifecycle = await get_adapter_lifecycle(call_config)
            result = await run_optimize_query_with_runtime(
                sql, call_config, root_path, db_uri, lifecycle, cache,
                dialect=dialect, task=task,
            )
        else:
            result = await run_optimize_query_with_runtime(
                sql, call_config, root_path, None, None, cache,
                dialect=dialect, task=task,
            )

        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()

    @mcp.tool(name="explain_query", description=_EXPLAIN_QUERY_DESCRIPTION)
    async def explain_query(
        sql: str,
        root: str | None = None,
        dialect: str | None = None,
        task: str | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        """Get the PostgreSQL planner's plan for one query."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="explain_query", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        db_uri = call_config.get_database_url()
        if db_uri is None:
            return FailureEnvelope(
                kind="db_unavailable",
                detail="No database URL configured. Set the env var named by "
                       "database_url_env (default DATABASE_URL).",
                recoverable=True,
                next_steps=[
                    "Configure a PostgreSQL 16+ read-only role URL in the "
                    "environment and retry.",
                ],
            ).model_dump()

        from ezsql.db.lifecycle import get_adapter_lifecycle
        lifecycle = await get_adapter_lifecycle(call_config)

        result = await run_explain_query(
            sql, call_config, root_path, db_uri, lifecycle, cache,
            dialect=dialect,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()

    @mcp.tool(name="design_schema", description=_DESIGN_SCHEMA_DESCRIPTION)
    def design_schema(
        requirements: str,
        root: str | None = None,
        dialect: str | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        """Design a schema from requirements: proposal, DDL, risks, ERD."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="design_schema", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_design_schema(
            call_config, root_path, cache=cache,
            requirements=requirements, dialect=dialect, task=task,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()

    @mcp.tool(name="refactor_sql", description=_REFACTOR_SQL_DESCRIPTION)
    def refactor_sql(
        root: str,
        sql: str | None = None,
        files: list[str] | None = None,
        dialect: str | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        """Composed refactoring report: security + optimization + schema impact."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="refactor_sql", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_refactor_sql(
            call_config, root_path, cache=cache,
            sql=sql, files=files, dialect=dialect, task=task,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()

    @mcp.tool(name="debug_sql", description=_DEBUG_SQL_DESCRIPTION)
    def debug_sql(
        error: str,
        root: str | None = None,
        sql: str | None = None,
        context: str | None = None,
        dialect: str | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        """Diagnose a database error: catalog match + cross-check + hypotheses."""
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="debug_sql", dialect=dialect)

        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            return root_result.model_dump()

        root_path: Path = root_result
        call_config = load_config(root_path)
        cache = get_cache(root_path, call_config)

        result = run_debug_sql(
            call_config, root_path, cache=cache,
            error=error, sql=sql, context=context, dialect=dialect, task=task,
        )
        if isinstance(result, FailureEnvelope):
            return result.model_dump()
        return result.model_dump()


__all__ = ["register_tools"]
