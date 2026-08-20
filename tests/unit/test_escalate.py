"""Unit tests for llm/escalate.py (plan_phase4 FR-1).

No real LLM API calls: the transport seam is stubbed (plan §24 —
escalation tested with a stub transport; deterministic fallback paths
are the primary assertions).
"""

import logging

import pytest

from ezsql.config import EzsqlConfig
from ezsql.llm.escalate import LiteLLMTransport, escalate
from ezsql.observability import counters
from ezsql.server.models import EscalationResult


class _StubTransport:
    """Deterministic stub transport (test seam)."""

    def __init__(
        self,
        advisory: str = "advisory text",
        tokens: int = 42,
        error: Exception | None = None,
    ) -> None:
        self.advisory = advisory
        self.tokens = tokens
        self.error = error
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        timeout: int,
    ) -> tuple[str, int]:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.advisory, self.tokens


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no API key in the environment (off-by-default path)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _reset_counters() -> None:
    counters.reset()
    yield
    counters.reset()


class TestOffByDefault:
    def test_no_key_returns_unavailable(self) -> None:
        config = EzsqlConfig()
        result = escalate(["some prompt part"], 1000, config=config)
        assert result == EscalationResult(
            used=False, tokens=0, advisory_text=None, status="unavailable"
        )

    def test_no_key_never_calls_transport(self) -> None:
        config = EzsqlConfig()
        stub = _StubTransport()
        escalate(["part"], 1000, config=config, transport=stub)
        assert stub.calls == []

    def test_empty_key_returns_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "")
        config = EzsqlConfig()
        result = escalate(["part"], 1000, config=config)
        assert result.status == "unavailable"
        assert result.used is False


class TestOkPath:
    def test_ok_path_via_stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport(advisory="use an index", tokens=123)
        result = escalate(["SELECT * FROM t WHERE x = 1"], 1000, config=config, transport=stub)

        assert result.used is True
        assert result.status == "ok"
        assert result.tokens == 123
        assert result.advisory_text == "use an index"

    def test_transport_receives_model_budget_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        config.llm_model = "openai/gpt-4o-mini"
        config.llm_timeout_seconds = 30
        stub = _StubTransport()
        escalate(["part"], 1000, config=config, transport=stub)

        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["model"] == "openai/gpt-4o-mini"
        assert call["timeout"] == 30
        # Budget is min(call budget, config budget)
        assert call["max_tokens"] == 1000

    def test_budget_capped_by_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        config.llm_token_budget = 500
        stub = _StubTransport()
        escalate(["part"], 100_000, config=config, transport=stub)
        assert stub.calls[0]["max_tokens"] == 500

    def test_empty_advisory_becomes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport(advisory="", tokens=5)
        result = escalate(["part"], 1000, config=config, transport=stub)
        assert result.advisory_text is None
        assert result.status == "ok"


class TestFailurePath:
    def test_exception_maps_to_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport(error=RuntimeError("boom with secret http://x"))
        result = escalate(["part"], 1000, config=config, transport=stub)

        assert result.used is False
        assert result.status == "failed"
        assert result.advisory_text is None
        assert result.tokens == 0

    def test_failure_logs_class_name_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport(error=ValueError("message with url http://creds"))
        with caplog.at_level(logging.WARNING, logger="ezsql.llm.escalate"):
            escalate(["part"], 1000, config=config, transport=stub)

        records = [
            r for r in caplog.records if r.name == "ezsql.llm.escalate"
        ]
        assert records, "expected at least one escalation log record"
        record = records[-1]
        assert getattr(record, "exception_class", None) == "ValueError"
        # The exception message (which may embed URLs/credentials) must not
        # be logged — only the class name.
        assert "http://creds" not in record.getMessage()


class TestBudgetExhausted:
    def test_budget_below_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport()
        result = escalate(["part"], 10, config=config, transport=stub)

        assert result.status == "budget_exhausted"
        assert result.used is False
        assert stub.calls == []

    def test_config_budget_below_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        # Direct assignment bypasses the instantiation-time clamp (pydantic v2
        # does not validate on assignment), so 100 stays 100 — below the
        # 256-token floor → budget_exhausted without calling the transport.
        config.llm_token_budget = 100
        stub = _StubTransport()
        result = escalate(["part"], 100_000, config=config, transport=stub)
        assert result.status == "budget_exhausted"
        assert stub.calls == []


class TestAdvisoryBounding:
    def test_advisory_truncated_to_config_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        config.max_advisory_chars = 10
        stub = _StubTransport(advisory="a" * 100)
        result = escalate(["part"], 1000, config=config, transport=stub)
        assert result.advisory_text == "a" * 10


class TestRedaction:
    def test_sql_literals_redacted_before_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport()
        escalate(
            ["SELECT * FROM users WHERE email = 'user@example.com'"],
            1000,
            config=config,
            transport=stub,
        )

        sent = stub.calls[0]["messages"]
        assert isinstance(sent, list)
        user_msg = sent[1]
        assert "'user@example.com'" not in user_msg["content"]
        assert "<string>" in user_msg["content"]


class TestCounters:
    def test_counters_incremented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport(tokens=77)
        escalate(["part"], 1000, config=config, transport=stub)

        assert counters.get("escalation_requests") == 1
        assert counters.get("escalation_successes") == 1
        assert counters.get("escalation_tokens") == 77

    def test_failure_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
        config = EzsqlConfig()
        stub = _StubTransport(error=RuntimeError("x"))
        escalate(["part"], 1000, config=config, transport=stub)
        assert counters.get("escalation_failures") == 1


class TestTransportDefault:
    def test_default_transport_is_litellm(self) -> None:
        assert callable(LiteLLMTransport())
