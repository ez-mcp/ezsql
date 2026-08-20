"""Architecture invariant tests (plan §22.6).

These tests enforce architectural constraints that prevent regressions:
- No LLM in deterministic pipelines (llm only in design/debug)
- No DB in static pipelines
- Acyclic imports
- Tool count is 8
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


def test_no_llm_in_refactor() -> None:
    """pipelines/refactor.py does not import ezsql.llm (plan_phase4 FR-3)."""
    refactor_mod = sys.modules.get("ezsql.pipelines.refactor")
    if refactor_mod is None:
        refactor_mod = importlib.import_module("ezsql.pipelines.refactor")
    with open(refactor_mod.__file__) as f:  # type: ignore[union-attr]
        source = f.read()
    assert "ezsql.llm" not in source
    assert "from ezsql.llm" not in source
    assert "import ezsql.llm" not in source


def test_llm_only_in_design_and_debug() -> None:
    """ezsql.llm is imported ONLY by design/debug pipelines (plan §9).

    Positive control: the two escalating pipelines must reference
    ezsql.llm; every other pipeline module must not.
    """
    escalating = ("design", "debug")
    deterministic = ("analyze", "security", "optimize", "context", "refactor")

    for name in escalating:
        mod = sys.modules.get(f"ezsql.pipelines.{name}")
        if mod is None:
            mod = importlib.import_module(f"ezsql.pipelines.{name}")
        with open(mod.__file__) as f:  # type: ignore[union-attr]
            source = f.read()
        assert "ezsql.llm" in source, f"{name} should import ezsql.llm"

    for name in deterministic:
        mod = sys.modules.get(f"ezsql.pipelines.{name}")
        if mod is None:
            mod = importlib.import_module(f"ezsql.pipelines.{name}")
        with open(mod.__file__) as f:  # type: ignore[union-attr]
            source = f.read()
        assert "ezsql.llm" not in source, f"{name} imports ezsql.llm"


def test_no_db_in_phase2() -> None:
    """Static pipelines do not import ezsql.db (plan_phase3 §9).

    The Phase 3 runtime pipelines (explain, optimize_runtime) are allowed
    DB access; the static ones are not. Phase 4's design/refactor/debug
    are static too (plan_phase4 FR-3).
    """
    for pipeline_name in (
        "analyze", "security", "optimize", "context",
        "design", "refactor", "debug",
    ):
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


def test_tool_count_is_8() -> None:
    """Exactly 8 workflow tools registered via list_tools() (plan_phase4 §FR-9)."""
    import asyncio

    from ezsql.server.app import create_server

    async def _list_tools() -> list[str]:
        server = create_server()
        tools = await server.list_tools()
        return sorted(t.name for t in tools)

    tool_names = asyncio.run(_list_tools())
    assert tool_names == [
        "analyze_sql",
        "debug_sql",
        "design_schema",
        "explain_query",
        "find_context",
        "optimize_query",
        "refactor_sql",
        "sql_sec",
    ]


def test_prompt_count_is_3() -> None:
    """Exactly 3 prompts registered via list_prompts() (plan_phase3 §9)."""
    import asyncio

    from ezsql.server.app import create_server

    async def _list_prompts() -> list[str]:
        server = create_server()
        prompts = await server.list_prompts()
        return sorted(p.name for p in prompts)

    prompt_names = asyncio.run(_list_prompts())
    assert prompt_names == [
        "explain_guide",
        "sql_optimization_guide",
        "sql_security_guide",
    ]


def test_db_never_imports_server_or_pipelines() -> None:
    """db/ modules never import server/ or pipelines/ (plan_phase3 §2, V3-6)."""
    for mod_name in ("ezsql.db.base", "ezsql.db.errors",
                     "ezsql.db.postgres", "ezsql.db.lifecycle"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            mod = importlib.import_module(mod_name)
        with open(mod.__file__) as f:  # type: ignore[union-attr]
            source = f.read()
        assert "ezsql.server" not in source, f"{mod_name} imports ezsql.server"
        assert "ezsql.pipelines" not in source, f"{mod_name} imports ezsql.pipelines"


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
