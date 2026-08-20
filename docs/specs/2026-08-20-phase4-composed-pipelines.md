# EZSQL Phase 4 — Composed & Escalating Pipelines: Specification

> **Version:** v1 · **Status:** Pending approval · **Date:** 2026-08-20
> **Canonical inputs:** `plan.md` (approved 2026-08-14, §25 Phase 4), `repoarchitecture.mmd` (Phase 3 amended 2026-08-19)
> **Evidence legend:** [USR] user-stated requirement · [REPO] repo-derived fact · [EXT] externally verified fact · [PREF] user preference · [REC] agent recommendation · [DEC] user-approved decision · [INF] inference · [ASM] assumption

---

## Objective

Implement Phase 4 of the approved EZSQL architecture: the full 8-tool MCP surface (`refactor_sql`, `design_schema`, `debug_sql` added to the existing 5), budgeted optional LLM escalation via LiteLLM (design/debug pipelines only, off by default), user-project documentation retrieval, task-registry wiring, and the `ezsql init` CLI — to strict, professional-grade standards (full test coverage, ruff + mypy strict clean, security review pass).

## Context

- Phases 1–3 are complete: 387 tests green, ruff + mypy strict clean, version 0.2.0, 5 tools live. [REPO]
- Phase 4 target files exist as **empty stubs**: `pipelines/design.py`, `pipelines/refactor.py`, `pipelines/debug.py`, `llm/escalate.py`, `core/context/docs.py`. [REPO]
- `EscalationResult` model already exists in `server/models.py` with the exact plan §23 shape. [REPO]
- `litellm>=1.96.0` is already a pinned dependency, imported nowhere. [REPO]
- Config already carries `llm_api_key_env` (env-var *name*), `llm_token_budget` (default 4000), `allow_writes=False`. [REPO]
- LiteLLM `completion()` supports `timeout` (default 600s — must be overridden), `max_tokens`, and returns usage data. [EXT]

## Phase 0 — Hard Precondition (before any implementation)

**P0.1 — Fixture pollution cleanup, committed and pushed to GitHub first.** [DEC]
- Delete `tests/fixtures/sample_repo/symlink_to_tmp/` (accidental snapshot of hundreds of system tmp files). [REPO]
- Verify via `git log --oneline -- tests/fixtures/sample_repo/symlink_to_tmp` that nothing legitimate lived there before deleting.
- Commit and push **before** any Phase 4 implementation work begins.

## In Scope

1. **Phase 0** cleanup (above).
2. `llm/escalate.py` — budgeted LiteLLM escalation with deterministic fallback.
3. `pipelines/design.py` + `design_schema` tool + `DesignResult` model.
4. `pipelines/refactor.py` + `refactor_sql` tool + `RefactorResult` model.
5. `pipelines/debug.py` + `debug_sql` tool + `DebugResult` model + deterministic error catalog (`core/debug/catalog.py`, rules-are-data).
6. `core/context/docs.py` — keyword + frontmatter retrieval over bundled docs and user-project docs (`<root>/docs/**/*.md` and `<root>/.ezsql/docs/*.md`). [DEC]
7. Docs single-source-of-truth fix: `app.py` prompts load from `docs/` files; frontmatter standardized across all bundled docs. [DEC]
8. `ezsql init` CLI — hand-rolled `sys.argv` dispatch (no new dependency). [DEC]
9. Task-registry wiring across all 8 tools. [DEC — scope expansion, user-approved]
10. Config additions: `llm_model`, `llm_timeout_seconds`, clamp range for `llm_token_budget`.
11. Fix `optimize.py` `schema_hash` TODO (static optimize cache key ignores schema). [REC — in blast radius]
12. Counter-ownership fix: new pipelines follow the wrappers-own-`tool_calls` convention; remove the double-count in `run_sql_sec`. [REC]
13. Update `_INSTRUCTIONS`, tool descriptions, `ArtifactType` Literal, tool-count invariant (5 → 8).
14. Full test suite for all of the above + security review pass. [DEC]

## Out of Scope

- Write-grant flow / elicitation (Phase 5). [USR via plan]
- MySQL/SQLite/SQL Server EXPLAIN adapters, live schema introspection (Phase 5).
- LangGraph iterate-loops (Phase 5).
- HTTP transport (Phase 5).
- MCP sampling as escalation transport (future; `escalate.py` is the seam).
- Any change to Phase 1–3 tool behavior or result contracts beyond the explicit fixes listed in scope (items 10–12).

## Deferred

- OTLP metrics export (bolt-on seam already exists in counter registry).
- Retrieval quality evaluation (revisit only if measured recall is poor — plan §27).

## Functional Requirements

### FR-1 — `llm/escalate.py`
- Contract (plan §22): `escalate(prompt_parts, budget) -> EscalationResult` (model exists in `server/models.py`). [REPO]
- **Off by default**: if the env var named by `config.llm_api_key_env` is unset/empty, return `EscalationResult(used=False, tokens=0, advisory_text=None, status="unavailable")` without importing/calling LiteLLM eagerly. [USR via plan §9]
- LiteLLM call: `completion(model=config.llm_model, messages=..., max_tokens=<bounded by remaining budget>, timeout=config.llm_timeout_seconds)`. Timeout must be set explicitly (LiteLLM default is 600s). [EXT]
- Budget: `max_tokens` derived from `config.llm_token_budget`; actual usage read from response usage; `status="budget_exhausted"` when the budget cannot accommodate a minimal call.
- Failure policy: any exception (network, auth, provider, parse) → `EscalationResult(used=False, status="failed")` with the exception **class name only** logged (never the message, which may embed URLs/keys). Deterministic fallback is the caller's pre-existing result. [REC, security doctrine §5]
- **Content whitelist** (plan §16): only schema shapes, SQL text, and deterministic findings enter prompts. Never credentials, connection strings, env-var values, or full file dumps. [USR via plan]
- **Redaction before send**: prompt parts pass through literal/secret redaction (reuse `plan.py` literal-redaction utilities where applicable). [REC]
- Import restriction (test-enforced): only `pipelines/design.py` and `pipelines/debug.py` may import `ezsql.llm`. `optimize`/`security`/`analyze`/`refactor` never can. [USR via plan §9; refactor is deterministic-only by composition]
- Advisory output is length-bounded (config limit) and treated as untrusted data (see Security).

### FR-2 — `pipelines/design.py` (`design_schema`)
- Input: `requirements: str`, `task?`, `root?`, `dialect?`. Output: `DesignResult` — schema proposal (tables/columns/types/constraints/FKs), generated DDL, relationships, migration strategy, risks, optional Mermaid ERD, `escalation: EscalationResult`, `cache_provenance`, truncation pairs. [USR via plan §21]
- Deterministic path: derive proposal from requirements text + existing repo `SchemaModel` (naming/typing heuristics as data-driven rules; reuse `core/schema` model types for the proposal shape). [REC]
- **Escalation trigger — policy (a)**: pipeline code decides via explicit inconclusive conditions (e.g., deterministic derivation yields no confident tables). The agent cannot force escalation via a parameter. [DEC]
- Escalation refines, never replaces: deterministic findings always present; advisory merged as advisory. [USR via plan §9]
- Generated DDL must pass the repo's own `sql_sec` rules before being returned; unsafe DDL withheld and reported with violated rule ids. [USR via plan §16]
- Cache: deterministic skeleton cached under new `design` domain (content-addressed key incl. requirements hash + schema hash + ruleset version); **escalation advisory is never cached**. [DEC — critical review #1]

### FR-3 — `pipelines/refactor.py` (`refactor_sql`)
- Input: `target` (sql text or files), `task?`, `root?`, `dialect?`. Output: `RefactorResult` — composed report: security findings + optimization findings/candidates + schema impact + proposed changes; agent applies. [USR via plan §21]
- **Internal composition only**: calls `run_sql_sec`, `run_optimize_query` (or their core services) as Python functions — never as MCP-call chaining. [USR via plan §5.1]
- Deterministic only — never imports `ezsql.llm`. [USR via plan §9]
- Schema impact: diff target's table/column references against repo `SchemaModel` (missing tables/columns flagged with `schema_source: repo-ddl` caveat). [REC]
- Cache: `refactor` domain key over target content + schema hash + composed ruleset versions.

### FR-4 — `pipelines/debug.py` (`debug_sql`) + error catalog
- Input: `error: str`, `sql?`, `context?`, `task?`, `root?`, `dialect?`. Output: `DebugResult` — deterministic error-catalog matches, schema/AST cross-check, ranked hypotheses, next diagnostics, `escalation: EscalationResult`. [USR via plan §21]
- **Error catalog is a data-driven core service** at `core/debug/catalog.py`: `(catalog_id, error_pattern, dbms_scope, diagnosis, fix_guidance, severity)` entries — rules-are-data, grows without engine changes, unit-testable. Initial catalog: PostgreSQL error classes (syntax errors, undefined table/column, datatype mismatch, duplicate key, deadlock, timeout, permission denied, relation does not exist, etc.). [DEC]
- Escalation trigger — policy (a): no catalog match above threshold. [DEC]
- Cache: `debug` domain; advisory never cached (same rule as FR-2).

### FR-5 — `core/context/docs.py` (retrieval)
- Keyword + frontmatter-metadata scoring; no vector DB. [USR via plan §13]
- Corpus: bundled docs (via `importlib.resources`) **and** user-project docs from `<root>/docs/**/*.md` + `<root>/.ezsql/docs/*.md` only — bounded, predictable, no arbitrary repo markdown flows into LLM prompts. [DEC]
- API: retrieval returns relevant *sections* only, never bulk-loads docs. [USR via plan §13]
- Standardize frontmatter (`name`, `description`, optional `keywords`) across all bundled docs; `index.md` becomes generated or frontmatter-driven. [DEC]

### FR-6 — Docs single-source-of-truth fix
- `app.py` prompt registrations load content from `docs/*.md` files (as `explainsql.md` already does); delete the hardcoded Python-string duplicates. [DEC]

### FR-7 — `ezsql init` CLI
- Hand-rolled `sys.argv` dispatch in `server/app.py`: no args → server (current behavior preserved); `ezsql init [--force]` → init. No new dependency. [DEC]
- Emits: `.ezsql/config.toml` (env-var *names* only, commented defaults), `.github/instructions/ezsql.instructions.md` (YAML frontmatter with `applyTo`), `CLAUDE.md` section, `.cursor/rules/ezsql.mdc`. [USR via plan §8; EXT-verified formats]
- Creates `.ezsql/` directory; appends `.ezsql/` to `.gitignore` (creates file if absent; idempotent append). [DEC]
- **Non-destructive**: refuses to overwrite any existing file unless `--force`; prints exactly what was written/skipped. [DEC]
- All writes confined to the resolved project root (path-traversal safe). [REC, security]

### FR-8 — Task registry wiring
- Pipelines call `registry.get_or_create(task)` on entry and `registry.add_ref(...)` on successful results; `resolve_context` feeds prior `context_map`/`schema_model` into pipelines that can use them. [DEC]
- `ArtifactType` extended with `"design"`, `"refactor"`, `"debug"` (and existing types used by the 5 live tools). [REPO gap]

### FR-9 — Server surface updates
- Register 3 new tools in `tools.py` following the exact existing wiring pattern (resolve_root → load_config → get_cache → pipeline → FailureEnvelope check → model_dump). [REPO convention]
- New keyword-rich tool descriptions (module-level constants, existing pattern). [REPO convention]
- `_INSTRUCTIONS` updated to 8 tools + routing guidance. [REPO gap]
- `test_tool_count_is_5` → 8. [REPO gap]

### FR-10 — Config additions
- `llm_model: str` (default: a conservative current model; exact default is a Plan-agent decision — must be documented), `llm_timeout_seconds: int` (clamped, e.g. 5–120, default ~30). [REC]
- `llm_token_budget` added to `_CLAMP_RANGES`. [REPO gap]

## Non-Functional Requirements

- **NFR-1**: mypy strict + ruff clean; zero new errors (C5). [DEC]
- **NFR-2**: All new pipelines sync where possible (matching static-pipeline convention); escalation runs inside the sync call with its own timeout. [REC]
- **NFR-3**: Every new collection in results carries `truncated`/`suppressed_count` pairs per config limits. [REPO convention]
- **NFR-4**: One structured log line per tool call; domain counters for escalations (`escalation_requests`, `escalation_successes`, `escalation_failures`, `escalation_tokens`). [REPO convention]
- **NFR-5**: No real LLM API calls in tests — stub transport only. [USR via plan §24]

## Constraints

- Layering (test-enforced): `server → pipelines → core/infra`; core never imports pipelines; static pipelines must not import `ezsql.db`; `optimize`/`security`/`refactor` must never import `ezsql.llm`. [REPO]
- Cache keys only from `cache/keys.py`; failures never cached; TTL only for explain/runtime domains. [REPO]
- Secrets as env-var names only; never in tool I/O, logs, cache, prompts, or error messages. [USR via plan §16]
- Python ≥3.11; existing pinned dependency set (litellm already present — no new runtime deps). [REPO]
- Result models carry `schema_version`-style versioning; cache keys embed ruleset versions. [REPO]

## Explicit User Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Hand-rolled CLI dispatch, no typer/click | One subcommand; stdlib-first |
| D2 | `init` is non-destructive + gitignore append | Safety |
| D3 | Escalation trigger = pipeline-decided inconclusiveness (policy a) | Escalation stays a budgeted exception; agent can't force LLM calls |
| D4 | Error catalog = data-driven Python module | Deterministic, testable verdicts; docs stay advisory |
| D5 | User docs = `<root>/docs/**` + `.ezsql/docs/**` only | Bounded exfil surface |
| D6 | Fix docs duplication now | Phase 4 touches docs anyway |
| D7 | Wire task registry now | Registry complete; refactor/debug are the payoff workflows |
| D8 | Fixture cleanup + push BEFORE implementation | Test hygiene in blast radius |
| D9 | Verification bar incl. security review pass | "Professional grade" |

## Assumptions

| Assumption | Impact if wrong | Confirm? |
|---|---|---|
| A1: `symlink_to_tmp` contains nothing legitimate (verified via git log before deletion) | Lost fixture history | No — verified in P0.1 |
| A2: Default `llm_model` value is a Plan-agent detail (documented choice) | Config default churn | No — non-material |
| A3: MCP SDK executes sync tools without blocking the event loop (existing sync tools already work this way) | Would need `asyncio.to_thread` wrapper | No — existing precedent [REPO] |
| A4: Claude/Cursor instruction formats as per current public conventions | Init emits slightly stale formats | No — low cost to update later |

## Security Considerations

- **Threat model additions**: LLM provider (network, external), LLM output (untrusted input), generated instruction files (written into user repo), user-project docs (untrusted content flowing toward prompts).
- **Escalation prompt hygiene**: content whitelist (FR-1); redaction before send; no credentials ever in prompt parts; token + timeout bounds prevent cost/DoS amplification. [USR via plan §16]
- **LLM output handling**: `advisory_text` is untrusted data — length-bounded, returned inside the established untrusted-data delimiting convention, never parsed as instructions, never able to alter a deterministic verdict (advisory-only by type construction). [security doctrine §10]
- **`ezsql init` path safety**: all output paths resolved under the project root; refuse absolute/escaping paths; no shell execution. [security doctrine §4]
- **Generated config contains env-var names only** — never values. [USR via plan §16]
- **Prompt-injection tests required**: fixtures with injection payloads in user docs / error text / requirements must not change verdicts or escape data delimiting. [DEC — D9]
- **Security review pass** (user-added acceptance item): pre-ship checklist from security doctrine run against the full diff; findings block completion. [DEC]

## Performance Considerations

- Escalation timeout default ~30s (vs LiteLLM's 600s default) keeps tool calls responsive. [EXT]
- Retrieval is keyword-based over a bounded corpus — no index infrastructure needed at this scale. [USR via plan §13]
- Cache-first everywhere; escalation is the only network path and is off by default.

## Reliability Considerations

- Escalation failure never fails the tool: deterministic result + `status` field. [USR via plan §18]
- `init` is idempotent and non-destructive; partial-write safety (write-then-rename not required for fresh files; refuse-overwrite covers the rest). [REC]
- Failures never cached (existing invariant extended to new domains).

## Operational Considerations

- New counters exposed via existing stats surface; escalation token usage logged per call (counts only, no content). [REPO convention]
- `ezsql init` output is human-readable stdout (no logging framework dependency for CLI mode).

## Dependencies & Integrations

- `litellm>=1.96.0` (already pinned) — first actual use. [REPO]
- No new runtime dependencies. [DEC D1]

## Risks

| Risk | Mitigation |
|---|---|
| LiteLLM exception taxonomy drift | Broad `except Exception` → typed status mapping at boundary; class-name-only logging |
| Deterministic design derivation is weak (requirements → schema is genuinely hard) | Escalation exists precisely for this; deterministic path degrades honestly to "inconclusive" |
| Error catalog coverage gaps | Catalog is data-driven; initial PostgreSQL set is bounded and testable; "no match" is a valid honest answer |
| Task wiring changes behavior of existing 5 tools | Wiring is additive (registry calls only); existing tests must stay green unchanged |
| Instruction-file format drift across clients | Formats verified against current docs; cheap to regenerate via `ezsql init --force` |

## Tradeoffs

| Decision | Accepted cost |
|---|---|
| Advisory never cached | Repeat escalations re-spend tokens; bounded by trigger policy (a) rarity |
| Pipeline-decided escalation | Agent can't request escalation when it would help; mitigated by honest "inconclusive" reporting |
| Hand-rolled CLI | No help text/arg parsing sophistication; acceptable for one subcommand |
| Data-driven error catalog over LLM diagnosis | Catalog coverage bounded; deterministic guarantee preserved |

## Rejected Alternatives

- **Typer/click for CLI** — dependency for one subcommand; rejected (D1). [DEC]
- **Agent-controlled `escalate: bool` param** — violates budgeted-exception principle; rejected (D3). [DEC]
- **Error catalog as retrieved markdown docs** — verdicts must be deterministic and testable; docs advisory only; rejected (D4). [DEC]
- **Caching escalation advisories** — staleness + hidden token spend; rejected (critical review #1). [REC → accepted by construction]
- **Always-escalate-when-keyed** — token cost + violates plan §9; rejected. [DEC]

## External Evidence

- LiteLLM `completion()` input params (timeout default 600s, max_tokens, usage on response): docs.litellm.ai/docs/completion/input, fetched 2026-08-20. [EXT]
- VS Code instruction files (`.github/instructions/*.instructions.md`, frontmatter, applyTo): code.visualstudio.com/docs/copilot/copilot-customization, fetched 2026-08-20. [EXT]

## Unresolved Non-Blocking Unknowns

- Exact default `llm_model` string (Plan-agent decision, documented). [ASM A2]
- LiteLLM exception-handling doc page did not fetch cleanly; broad-exception boundary makes exact taxonomy non-blocking. [INF]

## Acceptance Criteria

1. **Phase 0**: fixture cleanup committed and pushed before any other change. [D8]
2. All 8 tools registered and functional; tool-count invariant updated to 8 and passing.
3. Escalation off-by-default verified: with no key env var set, design/debug return deterministic results with `escalation.status="unavailable"` and zero LiteLLM network activity (test-proven via stub).
4. Escalation on-path verified via stub transport: budget enforcement, timeout setting, failure → `status="failed"` with deterministic fallback intact, token accounting.
5. Layering invariants extended and passing: `ezsql.llm` importable only from design/debug pipelines; refactor never imports it.
6. `refactor_sql` composes services internally (no MCP-call chaining — verified by code inspection + tests calling pipeline directly).
7. Generated DDL from `design_schema` passes `sql_sec` rules; unsafe DDL withheld with rule ids.
8. Docs retrieval returns bounded sections from bundled + user docs; `app.py` contains no hardcoded guide strings.
9. `ezsql init` emits all files, is non-destructive without `--force`, appends gitignore, confines writes to root (traversal test).
10. Task registry accumulates refs across calls; existing 5 tools' tests unchanged and green.
11. Prompt-injection fixtures (user docs, error text, requirements) cannot alter verdicts or escape data delimiting.
12. Full suite green (existing 387+ plus new unit/pipeline/invariant tests); ruff + mypy strict clean.
13. **Security review pass completed** against the pre-ship checklist; all findings resolved or explicitly accepted. [D9]
14. `uvx ezsql` smoke check: server starts, 8 tools listed; `ezsql init` works from a clean directory.
