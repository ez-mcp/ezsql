"""Observability setup with structlog and internal counters.

Structlog is configured to emit JSON to stderr (plan §5.5, §14A T7).
stderr is the log channel because stdio transport reserves stdout for MCP,
and SEP-2577 deprecates the MCP Logging capability in favor of stderr/OTLP.

A structlog redaction processor replaces known-sensitive keys with
``"<redacted>"`` before output (T7.1).
"""

import logging
import sys
from collections import defaultdict
from collections.abc import MutableMapping
from threading import Lock
from typing import Any

import structlog

# Keys whose values are redacted in log output (T7.1).
_REDACT_KEYS: frozenset[str] = frozenset({
    "url", "key", "token", "secret", "password", "credential",
    "api_key", "database_url", "connection_string",
})

_configured: bool = False


def _redact_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Redact known-sensitive keys in log events (T7.1).

    Replaces values of sensitive keys with ``"<redacted>"``. Does not
    redact the key *name* (e.g. ``database_url_env`` is fine — it's a name,
    not a value).
    """
    for key in list(event_dict):
        if key in _REDACT_KEYS:
            event_dict[key] = "<redacted>"
    return event_dict


def configure_logging(*, debug: bool = False) -> None:
    """Configure structlog for JSON output to stderr.

    Idempotent — safe to call multiple times (e.g. in tests). Called once
    in the server lifespan (plan §5.5).
    """
    global _configured
    if _configured:
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if debug else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "ezsql") -> Any:
    """Get a bound structlog logger.

    Returns a usable logger even if ``configure_logging`` hasn't been called
    yet (structlog has safe defaults).
    """
    return structlog.get_logger(name)


class CounterRegistry:
    """In-process counter registry (plan §17).

    Thread-safe via a single lock. Counters are simple integers keyed by
    name. Exposed via ``snapshot()`` for the stats diagnostic surface.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def inc(self, name: str, n: int = 1) -> None:
        """Increment a counter by ``n`` (default 1)."""
        with self._lock:
            self._counts[name] += n

    def get(self, name: str) -> int:
        """Get the current value of a counter."""
        with self._lock:
            return self._counts.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        """Get a snapshot of all counters."""
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        with self._lock:
            self._counts.clear()


# Module-level singleton (plan §17 — one registry per process).
counters = CounterRegistry()

logger = get_logger()

__all__ = ["configure_logging", "get_logger", "logger", "counters", "CounterRegistry"]
