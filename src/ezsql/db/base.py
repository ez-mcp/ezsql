"""Database adapter base protocol."""


from typing import Any, Protocol


class DbAdapter(Protocol):
    """Protocol for read-only database adapters."""

    async def connect(self) -> None:
        """Establish connection."""
        ...

    async def explain(self, sql: str, analyze: bool = False) -> dict[str, Any]:
        """Explain a SQL query without executing mutations."""
        ...

    async def close(self) -> None:
        """Close connection."""
        ...


__all__ = ["DbAdapter"]
