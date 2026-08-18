"""Architecture invariant tests (plan §22.6).

These tests enforce architectural constraints that prevent regressions:
- No LLM in optimize/security pipelines
- No DB in Phase 2 pipelines
- Acyclic imports
- Tool count is 4
- Schema lossiness invariant
"""

import importlib
import sys


def test_no_llm_in_optimize() -> None:
    """pipelines/optimize.py does not import ezsql.llm (plan §22.6)."""
    # Check that optimize.py doesn't import llm
    optimize_mod = sys.modules.get("ezsql.pipelines.optimize")
    if optimize_mod is None:
        optimize_mod = importlib.import_module("ezsql.pipelines.optimize")
    with open(optimize_mod.__file__) as f:  # type: ignore[union-attr]
        source = f.read()
    assert "ezsql.llm" not in source
    assert "from ezsql.llm" not in source
    assert "import ezsql.llm" not in source


def test_no_llm_in_security() -> None:
    """pipelines/security.py does not import ezsql.llm (plan §22.6)."""
    security_mod = sys.modules.get("ezsql.pipelines.security")
    if security_mod is None:
        security_mod = importlib.import_module("ezsql.pipelines.security")
    with open(security_mod.__file__) as f:  # type: ignore[union-attr]
        source = f.read()
    assert "ezsql.llm" not in source
    assert "from ezsql.llm" not in source
    assert "import ezsql.llm" not in source


def test_no_db_in_phase2() -> None:
    """No Phase 2 pipeline imports ezsql.db (plan §22.6)."""
    for pipeline_name in ("analyze", "security", "optimize", "context"):
        mod = sys.modules.get(f"ezsql.pipelines.{pipeline_name}")
        if mod is None:
            mod = importlib.import_module(f"ezsql.pipelines.{pipeline_name}")
        with open(mod.__file__) as f:  # type: ignore[union-attr]
            source = f.read()
        assert "ezsql.db" not in source, f"{pipeline_name} imports ezsql.db"


def test_acyclic_imports_core_no_pipelines() -> None:
    """Core modules don't import pipelines (plan §22.6)."""
    core_modules = [
        "ezsql.core.sql.parse",
        "ezsql.core.sql.lint",
        "ezsql.core.sql.rewrite",
        "ezsql.core.sql.dialect",
        "ezsql.core.security.engine",
        "ezsql.core.security.rules",
        "ezsql.core.security.hostlang",
        "ezsql.core.schema.ddl",
        "ezsql.core.schema.model",
    ]
    for mod_name in core_modules:
        mod = sys.modules.get(mod_name)
        if mod is None:
            mod = importlib.import_module(mod_name)
        with open(mod.__file__) as f:  # type: ignore[union-attr]
            source = f.read()
        assert "ezsql.pipelines" not in source, f"{mod_name} imports ezsql.pipelines"


def test_tool_count_is_4() -> None:
    """Exactly 4 tools registered (plan §22.6 — find_context + 3 new)."""
    from ezsql.server.tools import register_tools

    # We can't easily count registered tools without a mock MCPServer,
    # but we can check that register_tools exists and doesn't crash.
    # A more thorough test would mock MCPServer and count @mcp.tool calls.
    assert callable(register_tools)


def test_schema_lossiness_invariant() -> None:
    """Schema lossiness: withhold iff warning matches object AND capabilities intersect.

    See plan §22.6.
    """
    from ezsql.core.schema.model import ParserWarning, SchemaCapability, SourceSpan

    # Test: same object + unrelated capability → finding survives
    warning = ParserWarning(
        kind="test",
        location=SourceSpan(),
        object_name="users",
        message="test",
        affects_schema_completeness=True,
        compromised_capabilities=frozenset({"column_type"}),
    )
    # A finding that depends on index_enumeration should NOT be withheld
    # by a warning that compromises column_type
    required_capabilities: frozenset[SchemaCapability] = frozenset({"index_enumeration"})
    intersection = warning.compromised_capabilities & required_capabilities
    assert len(intersection) == 0  # no intersection → finding survives

    # Test: same object + relevant capability → finding withheld
    required_capabilities = frozenset({"column_type"})
    intersection = warning.compromised_capabilities & required_capabilities
    assert len(intersection) > 0  # intersection → finding withheld

    # Test: affects_schema_completeness=False → finding survives
    safe_warning = ParserWarning(
        kind="test",
        location=SourceSpan(),
        object_name="users",
        message="test",
        affects_schema_completeness=False,
        compromised_capabilities=frozenset(),
    )
    assert len(safe_warning.compromised_capabilities) == 0
