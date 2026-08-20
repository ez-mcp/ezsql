"""Budgeted LLM escalation via LiteLLM (plan §9, §16, §22).

Escalation is a budgeted exception inside the ``design_schema`` and
``debug_sql`` pipelines — never an architectural layer. It is **off by
default**: unless the env var named by ``config.llm_api_key_env`` is set,
``escalate()`` returns ``status="unavailable"`` without touching LiteLLM.

Security contract (plan §16):

- Only schema shapes, SQL text, and deterministic findings enter prompts —
  never credentials, connection strings, env-var values, or full file dumps.
- Prompt parts are redacted (SQL literals → placeholders) before send.
- The advisory text is untrusted LLM output: length-bounded, advisory-only,
  and never able to alter a deterministic verdict.
- Failures map to ``status="failed"`` with the exception class name only
  logged (messages may embed URLs or keys).
"""

import logging
from collections.abc import Sequence
from typing import Protocol

from ezsql.config import EzsqlConfig
from ezsql.core.sql.plan import redact_literals
from ezsql.observability import counters
from ezsql.server.models import EscalationResult

logger = logging.getLogger("ezsql.llm.escalate")

__all__ = ["EscalationTransport", "LiteLLMTransport", "escalate"]

# Floor for a minimal useful escalation call (prompt + short advisory).
_MIN_CALL_TOKENS = 256


class EscalationTransport(Protocol):
    """Transport seam for escalation calls (test stub injects here)."""

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        timeout: int,
    ) -> tuple[str, int]:
        """Return ``(advisory_text, total_tokens)``.

        Raises on any failure; the caller maps exceptions to
        ``status="failed"``.
        """
        ...


class LiteLLMTransport:
    """Default transport: LiteLLM ``completion()`` (lazy import).

    LiteLLM is imported only when a call is actually attempted, so the
    off-by-default path (no API key) never loads the library.
    """

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        timeout: int,
    ) -> tuple[str, int]:
        from litellm import completion  # lazy: only on the escalation path

        response = completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        text = ""
        choices = getattr(response, "choices", None)
        if choices:
            content = getattr(choices[0], "message", None)
            if content is not None:
                text = getattr(content, "content", None) or ""
        usage = getattr(response, "usage", None)
        tokens = 0
        if usage is not None:
            total = getattr(usage, "total_tokens", None)
            tokens = int(total) if total is not None else 0
        return text, tokens


def _redact_part(part: str, max_chars: int) -> str:
    """Redact one prompt part (SQL literals → placeholders, fail closed)."""
    redacted = redact_literals(part, max_chars)
    return redacted[:max_chars]


def escalate(
    prompt_parts: Sequence[str],
    budget: int,
    *,
    config: EzsqlConfig,
    transport: EscalationTransport | None = None,
) -> EscalationResult:
    """Run one budgeted LLM escalation (plan §22 contract).

    Args:
        prompt_parts: Whitelisted content fragments (schema shapes, SQL
            text, deterministic findings). Redacted before send.
        budget: Token budget for this call (from the calling pipeline).
        config: Loaded config (model, timeout, key env-var name).
        transport: Optional transport override (test seam). Defaults to
            LiteLLM.

    Returns:
        ``EscalationResult`` — advisory-only; never a verdict.
    """
    counters.inc("escalation_requests", 1)

    # Off by default (plan §9): no key → unavailable, zero LiteLLM activity.
    if not config.get_llm_api_key():
        logger.info("escalation_unavailable", extra={"reason": "no_api_key"})
        return EscalationResult(
            used=False, tokens=0, advisory_text=None, status="unavailable"
        )

    effective_budget = min(budget, config.llm_token_budget)
    if effective_budget < _MIN_CALL_TOKENS:
        counters.inc("escalation_budget_exhausted", 1)
        logger.info("escalation_budget_exhausted", extra={"budget": effective_budget})
        return EscalationResult(
            used=False, tokens=0, advisory_text=None, status="budget_exhausted"
        )

    # Redact every part before send (plan §16 — no literals leave the host).
    max_part_chars = max(1, config.max_advisory_chars)
    redacted_parts = [_redact_part(p, max_part_chars) for p in prompt_parts]
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are a SQL engineering advisor. Your output is advisory "
                "only and will be shown to a coding agent as untrusted data. "
                "Be concise and concrete."
            ),
        },
        {"role": "user", "content": "\n\n".join(redacted_parts)},
    ]

    active_transport = transport if transport is not None else LiteLLMTransport()
    try:
        advisory, tokens = active_transport(
            model=config.llm_model,
            messages=messages,
            max_tokens=effective_budget,
            timeout=config.llm_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — boundary maps all failures
        counters.inc("escalation_failures", 1)
        # Class name only: messages may embed URLs or credentials.
        logger.warning(
            "escalation_failed", extra={"exception_class": type(exc).__name__}
        )
        return EscalationResult(
            used=False, tokens=0, advisory_text=None, status="failed"
        )

    counters.inc("escalation_successes", 1)
    counters.inc("escalation_tokens", tokens)
    bounded_advisory = advisory[: config.max_advisory_chars]
    return EscalationResult(
        used=True,
        tokens=tokens,
        advisory_text=bounded_advisory if bounded_advisory else None,
        status="ok",
    )
