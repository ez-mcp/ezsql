"""Unit tests for observability: structlog redaction and counters."""

import io

from ezsql.observability import CounterRegistry, configure_logging, counters, get_logger


def test_counter_inc_and_get() -> None:
    """Counter increments and reads correctly."""
    reg = CounterRegistry()
    reg.inc("tool_calls")
    reg.inc("tool_calls")
    reg.inc("cache_hits", 5)
    assert reg.get("tool_calls") == 2
    assert reg.get("cache_hits") == 5
    assert reg.get("nonexistent") == 0


def test_counter_snapshot() -> None:
    """Snapshot returns all counters."""
    reg = CounterRegistry()
    reg.inc("a")
    reg.inc("b", 3)
    snap = reg.snapshot()
    assert snap == {"a": 1, "b": 3}


def test_counter_reset() -> None:
    """Reset clears all counters."""
    reg = CounterRegistry()
    reg.inc("x")
    reg.reset()
    assert reg.get("x") == 0
    assert reg.snapshot() == {}


def test_module_counters_singleton() -> None:
    """Module-level counters singleton is usable."""
    counters.reset()
    counters.inc("test_counter")
    assert counters.get("test_counter") == 1
    counters.reset()


def test_redaction_processor() -> None:
    """Structlog redaction processor redacts sensitive keys (T7.1)."""
    import structlog

    configure_logging(debug=True)
    # Capture stderr output
    captured: list[str] = []

    class _CaptureStream(io.StringIO):
        def write(self, s: str) -> int:
            captured.append(s)
            return len(s)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Import the redaction processor directly
            __import__(
                "ezsql.observability", fromlist=["_redact_processor"]
            )._redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=_CaptureStream()),
        cache_logger_on_first_use=False,
    )

    log = structlog.get_logger("test")
    log.info("test_event", url="postgres://secret@host/db", key="abc123",
             token="xyz", safe_field="visible")

    output = "".join(captured)
    assert "<redacted>" in output
    assert "postgres://secret@host/db" not in output
    assert "abc123" not in output
    assert "xyz" not in output
    assert "visible" in output


def test_configure_logging_idempotent() -> None:
    """configure_logging is idempotent — safe to call multiple times."""
    configure_logging()
    configure_logging()
    configure_logging(debug=True)
    # No error means success
    log = get_logger("test")
    assert log is not None


# --- Phase 3 tests (plan_phase3 §9, §11) ---


def test_domain_counter_ownership() -> None:
    """Wrappers own tool_calls; pipelines own domain counters (§11).

    The static optimize pipeline must NOT increment tool_calls — the
    duplicate increment was removed in Phase 3 (V3-5).
    """
    counters.reset()
    from ezsql.config import EzsqlConfig
    from ezsql.pipelines.optimize import run_optimize_query

    run_optimize_query("SELECT 1", EzsqlConfig(), dialect="postgres")
    assert counters.get("tool_calls") == 0  # pipeline never increments it
    counters.reset()


def test_phase3_domain_counters_exist() -> None:
    """Phase 3 domain counters are usable in the registry (§11)."""
    counters.reset()
    for name in ("explain_requests", "explain_successes", "explain_failures",
                 "explain_cache_hits", "explain_cache_misses",
                 "runtime_candidates_attempted", "runtime_candidates_verified",
                 "statement_gate_blocks", "db_connection_failures",
                 "db_acquire_timeouts", "explain_timeouts",
                 "plan_parse_failures"):
        counters.inc(name, 1)
    snap = counters.snapshot()
    for name in snap:
        assert snap[name] == 1
    counters.reset()


def test_counter_zero_default_projection() -> None:
    """Unset counters project to zero (§11)."""
    reg = CounterRegistry()
    assert reg.get("explain_requests") == 0
    assert reg.snapshot() == {}
