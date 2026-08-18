"""MCP tool registrations for EZSQL.

Phase 1 registers one tool: ``find_context``. The other 7 tools are
stub modules (kept for architectural visibility, §17 Q6) but NOT registered
until their phases.

Per-call config loading (Gap 1 fix): the config passed to ``register_tools``
is used only for root resolution's ``project_root`` fallback. Once a root is
resolved, ``load_config(root)`` is called to apply scan limits + cache
sizing from ``<root>/.ezsql/config.toml``. This means the
``config.project_root`` fallback is only reachable against the
register-time config (defaults) — a documented limitation of loading config
from the resolved root (plan §6.3 Option B).
"""

from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from ezsql.config import EzsqlConfig, load_config
from ezsql.observability import counters, logger
from ezsql.pipelines.context import run_find_context
from ezsql.server.cache_lifecycle import get_cache
from ezsql.server.models import ContextMap, FailureEnvelope
from ezsql.server.roots import resolve_root

# Explicit tool description with trigger keywords (plan §8 — primary
# activation signal). States that root is required.
_FIND_CONTEXT_DESCRIPTION = (
    "Find SQL-bearing files, migrations, ORM models, queries, and configs "
    "in a repository. Returns a grouped map of relevant files classified by "
    "type (migration, query, orm, config, doc, unknown). Use this FIRST for "
    "any SQL, Postgres, MySQL, SQLite, Supabase, migration, schema, index, "
    "query, or database work to orient yourself in the repository's SQL "
    "surface. The 'root' parameter is required (absolute path to the project "
    "root) unless pinned via .ezsql/config.toml."
)


def register_tools(
    mcp: MCPServer,
    config: EzsqlConfig,
    cache: Any | None = None,  # noqa: ARG001 — kept for API compat; cache wired per-call
) -> None:
    """Register all Phase 1 tools on the given MCPServer instance.

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
        """Find SQL-bearing files in the repository.

        Returns a grouped dictionary where keys are directory paths relative
        to root and values are lists of classified files. Each file has a
        name and classification (migration/query/orm/config/doc/unknown).
        """
        counters.inc("tool_calls", 1)
        logger.info("tool_call", tool="find_context", root=root, query=query)

        # Resolve root (plan §6.3 — Option B: root param primary)
        root_result = resolve_root(root, config)
        if isinstance(root_result, FailureEnvelope):
            logger.warning(
                "find_context_failed",
                kind=root_result.kind,
                detail=root_result.detail,
            )
            return root_result.model_dump()

        root_path: Path = root_result

        # Load per-call config from <root>/.ezsql/config.toml (Gap 1 fix).
        # Falls back to defaults if the file is missing/malformed.
        call_config = load_config(root_path)

        # Wire cache per-root (Gap 2 fix — D3: module-level dict, reused).
        cache = get_cache(root_path, call_config)

        # Run the context pipeline
        result: ContextMap = run_find_context(
            root_path,
            call_config,
            cache=cache,
            query=query,
        )

        return result.model_dump()


__all__ = ["register_tools"]
