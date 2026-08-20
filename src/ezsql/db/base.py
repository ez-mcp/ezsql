"""Database adapter base protocol (plan_phase3 §2).

The protocol exposes only ``connect``, ``explain``, and ``close`` — no
generic execute, no fetch, no write API (Gate 3). Adapters return the
inward-owned ``ParsedPlan`` and raise typed ``DbAdapterError`` values;
they never import ``server/`` or ``pipelines/``.
"""

from typing import Protocol

from ezsql.core.sql.explain_gate import ExplainableQuery
from ezsql.core.sql.plan import ParsedPlan


class DbAdapter(Protocol):
    """Protocol for read-only EXPLAIN database adapters."""

    async def connect(self) -> None:
        """Establish the connection pool and run safety preflights."""
        ...

    async def explain(self, query: ExplainableQuery) -> ParsedPlan:
        """Explain one validated query without executing it."""
        ...

    async def close(self) -> None:
        """Close the pool."""
        ...


__all__ = ["DbAdapter"]
