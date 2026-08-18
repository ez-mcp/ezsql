"""Internal security-domain model types.

These are internal analysis primitives, not MCP-facing I/O models. They live
here (not in ``server/models.py``) because they are analysis-unit concepts
used by the security engine and pipeline, not by the agent directly.

Defined here (plan §15.1):
- ``AnalysisUnit``: a single unit of security analysis (file or SQL string)
- ``RuleCoverage``: re-exported from server/models.py for convenience
- ``SchemaRequirement``: a single object-capability dependency
- ``RuleDependency``: what a rule's findings depend on
"""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from ezsql.core.schema.model import SchemaCapability
from ezsql.server.models import RuleCoverage  # noqa: F401 — re-export

InputKind = Literal["sql", "python_source"]
InputRole = Literal["query", "migration", "script"]


class AnalysisUnit(BaseModel):
    """A single unit of security analysis (plan §15.1).

    For files mode: ``unit_id`` is the relative file path.
    For sql= mode: ``unit_id`` is ``"sql:0"``.
    """

    unit_id: str
    file: str | None = None
    content: str
    input_kind: InputKind
    input_role: InputRole  # resolved, never "auto"


class SchemaRequirement(BaseModel):
    """A single object-capability dependency (plan §13.5)."""

    object_name: str
    capabilities: frozenset[SchemaCapability] = Field(default_factory=frozenset)


# Type alias for the rule dependency callable.
# A rule declares its schema dependencies as a function that takes a Finding
# and returns the list of SchemaRequirements it depends on.
RuleDependencyFn = Callable[..., list[SchemaRequirement]]


__all__ = [
    "AnalysisUnit",
    "InputKind",
    "InputRole",
    "RuleCoverage",
    "RuleDependencyFn",
    "SchemaRequirement",
]
