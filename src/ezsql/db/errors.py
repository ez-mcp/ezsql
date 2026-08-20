"""Safe typed adapter exceptions (plan_phase3 §2, §8).

``db/`` never imports ``server/`` or ``pipelines/``. Expected DB failures
are adapter exceptions carrying a safe category and exception class name
only — original driver messages can contain DSNs, SQL, identifiers, or
literal values and are never surfaced (plan_phase3 §0 V3-12).
"""

from typing import Literal

DbErrorCategory = Literal[
    "invalid_database_config",
    "database_version_unsupported",
    "unsafe_database_role",
    "db_connection_failed",
    "db_acquire_timeout",
    "db_adapter_limit",
    "explain_timeout",
    "explain_total_timeout",
    "statement_blocked",
    "parameter_types_required",
    "explain_parse_failed",
    "plan_too_large",
    "db_internal_error",
]


class DbAdapterError(Exception):
    """A typed, safe adapter failure.

    ``category`` maps to a public failure kind (plan_phase3 §8).
    ``driver_exception`` is the exception *class name* only — safe to log.
    The original driver message is deliberately dropped.
    """

    def __init__(self, category: DbErrorCategory, detail: str, *,
                 driver_exception: str | None = None) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail
        self.driver_exception = driver_exception


__all__ = ["DbAdapterError", "DbErrorCategory"]
