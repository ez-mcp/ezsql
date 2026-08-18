# EZSQL Phase 1 — Foundation: Execution Plan

> **Status:** DRAFT v3 — all decisions confirmed. Awaiting final approval to execute.
> **Author:** Copilot, 2026-08-14 (revised 2026-08-17 with security threat model + Q&A).
> Grounded in `plan.md` §25 Phase 1, verified against the actual installed SDK + spec,
> not the plan's assumptions.
>
> **Confirmed decisions (user, 2026-08-17):**
> - §6.1 Python version: keep `>=3.11`, target `py311`.
> - §6.2 `requirements.txt`: delete (outdated, redundant with `pyproject.toml`).
> - §6.3 Roots: **Option B** — `root` tool param primary, no `list_roots()` call.
> - §17 Q1 Root auth: **(a) no additional control** — injection is the user's/project's concern.
> - §17 Q2 Read limits: **accepted** as proposed (config-overridable).
> - §17 Q3 Binary detection: **add it**.
> - §17 Q4 Classification: **sufficient** as-is (migration/query/orm/config/doc/unknown).
> - §17 Q5 Cache location: **`<root>/.ezsql/cache.db`** (project-local).
> - §17 Q6 Tool stubs: **keep** — helps understand the whole project.
> - §17 Q7 `task` param: **accepted but ignored (no-op)** until Phase 2+.

---

## 0. Summary

Phase 1 turns the existing partial skeleton into a **production-grade foundation**: a
uvx-installable package that serves one working tool (`find_context`) over stdio, with
config loading, structured logging, a two-tier content-addressed cache, roots-based
project resolution, and a green test/lint/type gate. Everything downstream (Phases 2–4)
builds on these substrates, so they must be correct, minimal, and not over-built.

**One sentence:** complete the packaging, server, config, observability, cache, and
`find_context` pipeline to the contract in `plan.md` §6/§14/§16/§21, fixing the stale
"roots" assumption against the 2026-07-28 spec, and leave the repo strictly cleaner.

---

## 1. Confirmed Understanding (restated for audit)

### What Phase 1 must deliver (from `plan.md` §25)
1. **Packaging** — `pyproject.toml`, entrypoint, uvx-ready.
2. **Package skeleton** — `src/ezsql/` layout.
3. **Migrate scan → `core/context/`** — `sql_search.py` becomes a reusable service.
4. **Rebuild server** — `app.py`/`tools.py`, roots-based root resolution, fixed descriptions.
5. **Config loader** — `.ezsql/config.toml`, env-var references only.
6. **structlog** — observability setup.
7. **Cache store + keys** — two-tier content-addressed store.
8. **`find_context` tool complete** — the one shipped tool.
9. **Tests green, mypy/ruff clean.**

### Exit criterion (from `plan.md` §25)
> `uvx ezsql` serves `find_context` correctly against a fixture repo.

### What is explicitly OUT of Phase 1
- `core/sql/`, `core/schema/`, `core/security/` — Phase 2.
- `db/` adapter impl, EXPLAIN — Phase 3.
- `refactor_sql`, `design_schema`, `debug_sql`, `llm/escalate.py` — Phase 4.
- The 7 other tools — their stubs stay as empty modules; NOT registered in Phase 1.
- Live schema introspection, write flows, LangGraph, HTTP transport — post-v1.

---

## 2. Critical Discovery: `plan.md` §8 "roots" is STALE

**This is the single most important finding of the investigation and it changes the server
design. I am flagging it explicitly rather than silently following or silently diverging.**

### The facts (verified against the installed SDK + spec, 2026-08-14)
- **SEP-2577** (PR [#2577](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2577),
  merged 2026-05-15, landed in spec version **2026-07-28**) **deprecates Roots, Sampling,
  and Logging** capabilities. Deprecation is advisory; features remain functional for one
  year, but the SDK emits `MCPDeprecationWarning`.
- The installed `mcp==2.0.0` marks `session.list_roots()` with
  `@deprecated("The roots capability is deprecated as of 2026-07-28 (SEP-2577).")`.
- The **2026-07-28 spec** lists only **Elicitation** as a client-offered feature. Roots is
  gone from the client-features list. The SEP motivation states roots had "vague semantics,
  overlaps with tool parameters and server configuration."
- `plan.md` was approved 2026-08-14 — 17 days *after* the deprecation landed in the spec —
  and still specifies (§8): *"MCP `roots` capability when the client provides it; explicit
  `root` tool parameter otherwise; hard error if neither is available. Never `Path.cwd()`."*

### The contradiction
The plan makes a **deprecated** capability the *primary* root-resolution mechanism and the
explicit `root` parameter the *fallback*. Building on a deprecated primitive means every
client that follows the 2026-07-28 spec will not advertise roots, and we will emit
deprecation warnings on the clients that still do.

### Resolution I am proposing (needs your approval — this is a one-way door)
**Invert the plan's priority: make the explicit `root` tool parameter the PRIMARY and ONLY
root-resolution mechanism in Phase 1.** Do not call `list_roots()` at all.

Rationale (steel-manned against the alternative):

| Option | Pro | Con | Verdict |
|---|---|---|---|
| **A: Follow plan — roots primary, `root` param fallback** | Matches approved plan verbatim | Builds on a deprecated capability; emits deprecation warnings; breaks against 2026-07-28-compliant clients that drop roots; adds async client-roundtrip complexity for a value the agent can pass directly | ✗ Rejected — violates "boring and proven beats novel" (a deprecated primitive is not proven-forward) |
| **B: `root` param primary, no roots call** (proposed) | Uses only stable, forward-compatible protocol surface; no deprecation warnings; simpler (no async client roundtrip, no capability negotiation); the agent already knows the project it's working in; matches SEP-2577's stated replacement ("tool parameters and server configuration") | Loses auto-discovery when client doesn't pass `root` — but the agent can be instructed (via server `instructions` + tool description) to pass it, and `.ezsql/config.toml` can pin a default | ✓ **Proposed** |
| **C: Both — try roots, fall back to `root` param** | Maximum compatibility today | Pays the complexity of a deprecated path *and* a stable path; deprecation warnings on the former; strictly more code to maintain and later remove | ✗ Rejected — overengineering a transitional state |

**Under Option B, the root-resolution contract becomes:**
1. `root` tool parameter (explicit, agent-supplied) — primary.
2. `.ezsql/config.toml` `project_root` field — optional pinned default (for `ezsql init` in Phase 4; the loader is built now, the field is read now).
3. If neither is present → return a typed `FailureEnvelope` (`kind="missing_root"`,
   `recoverable=true`, `next_steps=["Pass the `root` parameter with the absolute path to your project root.", "Or run `ezsql init` to pin a default in .ezsql/config.toml"]`).
   **Never `Path.cwd()`** — that bug is preserved-as-fixed.

This keeps the plan's hard invariant ("never `Path.cwd()`") and its failure policy ("fail
safely, never invent") while removing the deprecated dependency. **I need your explicit
decision here before implementation**, because it diverges from the approved plan text.

---

## 3. Current Repository State (verified 2026-08-14)

The skeleton is **further along than `plan.md` §4 describes**. Much of Phase 1's structural
work is already done. This plan accounts for that — it does not redo finished work.

### Already done (verified)
- `pyproject.toml` EXISTS: hatchling backend, `ezsql = "ezsql.server.app:main"` entrypoint,
  deps with `>=` floors, ruff/mypy/pytest configured. **Caveat:** `requires-python = ">=3.11"`
  but venv is 3.14.6; ruff/mypy target `py311`. See §6.1.
- `src/ezsql/` package EXISTS, installed editable (`_editable_impl_ezsql.pth` → `src/`).
- `src/ezsql/__init__.py` with `__version__`.
- `server/app.py` — `create_server()`, `main()` calling `asyncio.run(server.run_stdio_async())`. **Gaps:** no `instructions`, no `lifespan`, no config wiring.
- `server/tools.py` — registers `find_context`; uses docstring-as-description (works per SDK line 78, but plan wants explicit `description=` for reliability). **Gap:** uses `Path.cwd()` fallback (the bug).
- `server/models.py` — all 9 pydantic models from `plan.md` §23 defined. **Gaps:** `ContextMap` lacks `scan_metadata`/`cache_provenance` fields the plan §23 specifies; `FailureEnvelope` defined but never used.
- `core/context/scan.py` — `deepsearchsql` migrated, extended skip-dirs. Works.
- `pipelines/context.py` — `run_find_context` thin wrapper. **Gaps:** ignores `query`/`task` params; no cache; no classification.
- `config.py` — `EzsqlConfig` pydantic model with env-var resolution. **Gaps:** no `.ezsql/config.toml` loading (TOML parse), no `project_root` field, hardcoded defaults only.
- `observability.py` — bare `logger = structlog.get_logger("ezsql")`. **Gaps:** no structlog *configuration* (no `structlog.configure(...)`), no counters.
- `cache/store.py`, `cache/keys.py` — empty stubs (`__all__: list[str] = []`).
- `tasks/registry.py` — empty stub.
- `db/base.py` — `DbAdapter` Protocol defined (Phase 3, leave as-is).
- `db/postgres.py`, `llm/escalate.py`, all `core/sql/*`, `core/schema/*`, `core/security/*`, 5 pipelines — empty stubs (later phases).
- `docs/` — `optimizedsql.md`, `securitysql.md`, `index.md` moved under package.
- Tests: `tests/unit/test_scan.py`, `tests/pipelines/test_context_pipeline.py` — 2 tests, **passing**. ruff clean, mypy clean (28 files).

### Still present (dead weight to remove — §5)
- `src/server/server.py` — the OLD broken server (misplaced docstring, `Path.cwd()`).
- `src/scripts/sql_search.py` — the OLD scan (superseded by `core/context/scan.py`).
- `src/tests/test_sql_search.py` — the OLD broken test (imports removed `search_sql`).
- `src/docs/` — the OLD docs location (now under `src/ezsql/docs/`).
- `src/tools/` — empty dir (plan §20 says delete).
- `src/.mypy_cache/`, `src/.pytest_cache/`, `src/.ruff_cache/` — tracked cache dirs.
- `requirements.txt` — unpinned, redundant with `pyproject.toml`.

---

## 4. Files Affected

### Created (new)
| File | Reason |
|---|---|
| `src/ezsql/cache/store.py` | **Implement** two-tier cache (memory + SQLite). Currently empty stub. |
| `src/ezsql/cache/keys.py` | **Implement** content-addressed key builders (blake2b). Currently empty stub. |
| `src/ezsql/server/roots.py` | Root-resolution logic isolated + testable (param → config → failure). Small, pure. |
| `tests/unit/test_cache_keys.py` | Key stability + determinism tests. |
| `tests/unit/test_cache_store.py` | Store get/put/LRU/TTL + SQLite tier tests. |
| `tests/unit/test_config.py` | Config loader: TOML parse, env-var resolution, defaults, missing file. |
| `tests/unit/test_roots.py` | Root resolution: param wins, config fallback, missing → FailureEnvelope. |
| `tests/pipelines/test_find_context.py` | Full `find_context` pipeline: cache hit/miss, classification, FailureEnvelope on missing root. (Supersedes/replaces `test_context_pipeline.py`.) |
| `tests/fixtures/sample_repo/` | A realistic fixture repo (migrations, queries, ORM, config, doc, noise) for `find_context` exit-criteria. |

### Modified
| File | Reason |
|---|---|
| `pyproject.toml` | Pin deps to installed-verified versions; align `requires-python`/ruff/mypy to 3.14 (§6.1); add `tomllib` note (stdlib in 3.14, no dep). |
| `src/ezsql/server/app.py` | Add `instructions=`, `lifespan=` (config + cache init), wire config loader. |
| `src/ezsql/server/tools.py` | Explicit `description=` on `find_context`; replace `Path.cwd()` with `roots.py` resolution; return typed result (ContextMap or FailureEnvelope); wire cache + task. |
| `src/ezsql/server/models.py` | Add `scan_metadata`, `cache_provenance` to `ContextMap` per §23; add `FileClassification` literal; add `ContextFile` model. |
| `src/ezsql/pipelines/context.py` | Implement: cache check → scan → classify → cache store → ContextMap. Honor `query`/`task`. |
| `src/ezsql/core/context/scan.py` | Add file **classification** (migration/query/ORM/config/doc/unknown) per §6/§13. Keep `deepsearchsql` contract; add `classify_file`. |
| `src/ezsql/config.py` | Add `.ezsql/config.toml` loading via `tomllib` (stdlib 3.11+); add `project_root`, `cache_max_size`, `cache_max_entries`, `task_ttl` fields; `load_config(root)` function. |
| `src/ezsql/observability.py` | Add `configure_logging()` (structlog processor chain) + `Counter` registry + `get_stats()`. |
| `src/ezsql/tasks/registry.py` | **No change** — stays as empty stub. `task` param is a no-op in Phase 1 (§17 Q7); registry implemented in Phase 2+ when multiple tools share task context. |
| `tests/unit/test_scan.py` | Extend with classification cases (migration vs query vs ORM vs noise). |
| `README.md` | One-paragraph real description + install/run instructions (currently "# ezsql"). |

### Deleted
| File | Reason |
|---|---|
| `src/server/server.py` | Old broken server; superseded by `src/ezsql/server/app.py`. |
| `src/scripts/sql_search.py` | Old scan; superseded by `src/ezsql/core/context/scan.py`. |
| `src/tests/test_sql_search.py` | Broken test (imports removed `search_sql`). |
| `src/docs/` (dir) | Old docs location; content moved to `src/ezsql/docs/`. |
| `src/tools/` (dir) | Empty; plan §20 says delete. |
| `src/.mypy_cache/`, `src/.pytest_cache/`, `src/.ruff_cache/` | Tracked cache dirs (hygiene). |
| `requirements.txt` | Unpinned, redundant with `pyproject.toml`. (Confirm with user — some workflows expect it.) |

---

## 5. Approach & Rationale

### 5.1 Layering (preserves `plan.md` §22 acyclic contract)
```
server/tools.py  →  pipelines/context.py  →  core/context/scan.py
       ↓                  ↓                        ↓
   server/roots.py    cache/store.py           cache/keys.py
       ↓                  ↓
   config.py        tasks/registry.py
       ↓                  ↓
   observability.py  (both)
```
Tools never call `core/` directly (§22). Pipelines never import `server/`. Core never
imports pipelines. Cache keys built only in `cache/keys.py`.

### 5.2 Root resolution (Option B from §2)
`server/roots.py` exposes one pure function:
```python
def resolve_root(root_param: str | None, config: EzsqlConfig) -> Path | FailureEnvelope: ...
```
- `root_param` non-empty → `Path(root_param).resolve()` (validate it exists + is a dir; else FailureEnvelope `kind="invalid_root"`).
- Else `config.project_root` non-empty → resolve + validate.
- Else → `FailureEnvelope(kind="missing_root", recoverable=True, next_steps=[...])`.
- **Never `Path.cwd()`.** No `list_roots()` call. No async, no client roundtrip.

### 5.3 Cache (minimal, correct, not over-built)
`plan.md` §14 specifies a two-tier content-addressed store. For Phase 1, only the
**scan result** domain is cached (the one thing `find_context` computes). The store is
built generically so Phase 2+ domains plug in without changes.

- **Keys** (`cache/keys.py`): `blake2b` of `domain ∥ inputs ∥ dep_versions`. For scans:
  `domain="scan"`, `inputs = {root, skip_dirs, ruleset_version}`, `dep_versions = {sqlglot_version}`.
  Single function `scan_key(root: Path) -> str`. **All hashing lives here — §22.**
- **Store** (`cache/store.py`): `CacheStore` class with `get(key) -> T | None`, `put(key, value, ttl=None)`.
  - Memory tier: `OrderedDict` keyed by `str`, LRU-bounded by `max_entries` (config).
  - SQLite tier: `<project>/.ezsql/cache.db`, table `entries(key TEXT PK, domain TEXT, value BLOB, created REAL, ttl REAL, last_access REAL)`, WAL mode. Value = `pickle` of pydantic model (trusted internal data — never external input, never secrets).
  - On get: memory miss → SQLite hit → promote to memory. On put: write both.
  - mtime guard for scan freshness: store `(mtime, size)` per dir; on get, re-stat; if changed, miss (re-scan). This is the §14 "file freshness" guard.
  - **Never cache secrets** (§14, §16): scan results are filenames + classifications — no file contents, no credentials. Enforced by domain: only `"scan"` exists in Phase 1.
- **Concurrency**: stdio = one process per workspace (§19). SQLite WAL + idempotent upserts. No locks needed in Phase 1 (single-threaded stdio). Documented for the Phase 3+ multi-agent case.

### 5.4 Config (`.ezsql/config.toml`)
- `tomllib` (stdlib since 3.11 — no new dependency; we're on 3.14).
- `load_config(root: Path) -> EzsqlConfig`: read `<root>/.ezsql/config.toml` if present, merge with defaults. Missing file → all defaults (honest degradation, §3.7).
- Env-var **references** only (§16): `database_url_env = "DATABASE_URL"` stores the *name*; `get_database_url()` resolves at call time. **Never** store values.
- Fields added: `project_root: str | None`, `cache_max_size_mb: int = 50`, `cache_max_entries: int = 4096`, `task_ttl_seconds: int = 3600`.
- `allow_writes` stays `False` and is **not wired** in Phase 1 (write flow is post-v1).

### 5.5 Observability
- `configure_logging()` called once in `lifespan`: `structlog.configure(processors=[...])` → JSON output to stderr (stdio transport reserves stdout for MCP; stderr is the log channel — and per SEP-2577, stderr is the *recommended* log channel now that Logging is deprecated).
- `Counter` class: `inc(name, n=1)`, `get(name) -> int`, `snapshot() -> dict[str,int]`. In-process dict. Counters: `tool_calls{tool}`, `cache_hits{domain}`, `cache_misses{domain}`, `scan_files_seen`.
- One structured log line per tool call (tool, duration_ms, cache hit/miss, outcome) — §17.

### 5.6 Server `instructions` (§8 activation surface)
Passed as constructor arg: a concise routing string naming the 8 tools (7 as "coming soon")
and the directive: *"For any SQL/database work, call `find_context` first to orient yourself
in the repository's SQL surface."* This is portable, real, and the primary activation signal.

### 5.7 `find_context` tool description (§8)
Explicit `description=` parameter (not docstring reliance) with trigger keywords: SQL,
Postgres, MySQL, SQLite, Supabase, migration, schema, index, query, database. State that
`root` is required (or pinned via `ezsql init`).

### 5.8 File classification (§6, §13)
`core/context/scan.py` gains `classify_file(name: str, path: Path) -> FileClassification`:
- `migration`: name matches `migrations/` dir or `^\d+_.*\.sql$` or `V\d+__.*\.sql` (Flyway) or `.*\.migration\.sql`.
- `query`: `.sql` not classified as migration.
- `orm`: `.py`/`.ts`/`.js`/`.rb`/`.go` containing ORM markers (SQLAlchemy, Prisma, ActiveRecord, GORM imports).
- `config`: `*.toml`, `*.yaml`, `*.yml`, `*.env`, `*.ini`, `*.cfg`, `*.json` (non-`package-lock`).
- `doc`: `*.md`, `*.rst`.
- `unknown`: everything else matching SQL keywords.
Classification is **heuristic + deterministic** — no LLM, no embeddings. Documented as such.

---

## 6. Confirmed Decisions (user, 2026-08-17)

### 6.1 Python version — CONFIRMED: keep `>=3.11`, target `py311`
We use no 3.12+ features today; widest installable audience; `uvx` users on 3.11/3.12
can install. Bump only if we later adopt PEP 695 generics.

### 6.2 `requirements.txt` — CONFIRMED: delete
Outdated, unpinned, redundant with `pyproject.toml` (the source of truth). Deleted in
Step 1.1.

### 6.3 Roots — CONFIRMED: Option B
`root` tool parameter is the PRIMARY and ONLY root-resolution mechanism. No
`list_roots()` call. `.ezsql/config.toml` `project_root` is the fallback. Missing both →
`FailureEnvelope(kind="missing_root")`. Never `Path.cwd()`.

**Security implication (see §14A):** because `root` is now attacker-controllable
(the agent may be influenced by prompt injection from repo files), the root-validation
and scan-safety controls in §14A are load-bearing, not optional.

---

## 7. Justification Trail (per non-obvious decision)

| Decision | Evidence | Source |
|---|---|---|
| Use `MCPServer` v2 API, `run_stdio_async()` | Verified signature `(self) -> None` on installed mcp 2.0.0 | `inspect.signature` |
| Pass `description=` explicitly, not via docstring | SDK line 78: `func_doc = description or fn.__doc__ or ""` — docstring works but is fragile (the original bug was a misplaced docstring) | `mcp/server/mcpserver/tools/base.py:78` |
| Pass `instructions=` as constructor arg | `MCPServer.__init__` has `instructions: str \| None = None`; property at `server.py:284` | `inspect.signature(MCPServer.__init__)` |
| Use `lifespan=` constructor arg for config+cache init | `MCPServer.__init__` has `lifespan: Callable[[MCPServer], AbstractAsyncContextManager] \| None` | `inspect.signature` |
| **Do NOT use `list_roots()`** | `@deprecated("The roots capability is deprecated as of 2026-07-28 (SEP-2577).")` on `session.list_roots`; 2026-07-28 spec lists only Elicitation as client feature | `mcp/server/session.py:316`; spec page |
| Logs to **stderr**, not MCP Logging | SEP-2577 deprecates Logging; spec says it "overlaps with stderr and OpenTelemetry" | SEP-2577 motivation |
| `tomllib` for config, no new dep | stdlib since Python 3.11; we require `>=3.11` | Python docs |
| blake2b for cache keys | `plan.md` §14 specifies blake2b; stdlib `hashlib.blake2b` | plan §14 |
| SQLite WAL + idempotent upserts for concurrency | `plan.md` §14 "Concurrency" row | plan §14 |
| JSON (not pickle) for cache values | Pickle deserializes arbitrary code — violates zero-trust (§16) even for "trusted internal" data because a corrupt cache.db could execute code on load. JSON via `model_validate_json` is safe; pydantic validates schema on load. Cost: negligible at Phase 1 scale. | §14A T6; `security.md` §4 |
| `os.walk` pruned scan (keep, don't switch to rglob) | Measured ~1.9× faster, 6–8× lower peak memory on this machine; already implemented | user memory `pathlib-rglob-file-gotcha.md`, `rglob traversal cost` |
| No `__future__` imports | Repo convention | `/memories/repo/no-future-imports.md` |
| Functional style, minimal classes | User preference (plain functions, tuples, flat dispatch) | `/memories/repo/ezsql-mcp-fastmcp.md` §"User style preferences" |
| Delete old `src/server/`, `src/scripts/`, `src/tests/` | `plan.md` §20 migration table; superseded by new layout | plan §20 |

---

## 8. Prior Art Consulted

Per `architecture.md` "Mine Prior Art — Mandatory":
1. **This repository** — the existing `deepsearchsql` is the prior art for scanning; it's
   well-built and preserved. The existing `EzsqlConfig` is the prior art for config. Both
   are extended, not replaced.
2. **MCP SDK itself** — read `MCPServer`, `ToolManager`, `session.py`, `resolve.py` source to
   understand the real API surface rather than the plan's description of it. This is how the
   roots deprecation was discovered.
3. **MCP 2026-07-28 spec + SEP-2577** — consulted for the roots/sampling/logging deprecation
   and the recommended replacements (tool params + server config).
4. **structlog docs** (implied) — processor chain for JSON-to-stderr.
5. **SQLite WAL** — standard pattern for single-writer concurrent-read; documented in §14.

What it changed: discovered the roots deprecation, which inverted the root-resolution design
(§2) and confirmed stderr as the log channel (not MCP Logging).

---

## 9. Invariants Preserved

| Invariant | How protected |
|---|---|
| Never `Path.cwd()` for root | `server/roots.py` has no `cwd()` call; tested |
| Read-only (no writes) | Phase 1 has no DB adapter wired; `allow_writes` stays False and unwired |
| No secrets in logs/cache/IO | Config stores env-var *names*; scan caches filenames only; structlog redacts via processor |
| Acyclic imports (`server → pipelines → core/infra`) | Enforced by module boundaries; verified by import graph in tests |
| Cache keys built only in `cache/keys.py` | Single module; pipelines call `keys.scan_key(...)`, never hash directly |
| Tools never call `core/` directly | `tools.py` calls `pipelines.context.run_find_context` only |
| Deterministic-first (no LLM in Phase 1) | No `llm/` import anywhere in Phase 1 code paths |
| Retrieved content is data, not instructions | (Phase 2+ concern; Phase 1 returns filenames only) |
| `find_context` returns typed result or FailureEnvelope | Every code path returns one or the other; tested |

---

## 10. Existing Behavior at Risk

| What works now | Risk | Proof of preservation |
|---|---|---|
| `deepsearchsql` scan contract (`dict[str, list[str]]`) | Classification changes the return shape of the *pipeline*, not `deepsearchsql` itself | `deepsearchsql` signature unchanged; classification is a separate `classify_file` fn; `test_scan.py` still asserts the raw contract |
| 2 passing tests | `test_context_pipeline.py` asserts old `ContextMap` shape | New `test_find_context.py` covers the new shape; old test deleted with the old pipeline contract (documented) |
| `ezsql` console script runs | `app.py` changes (instructions, lifespan) | Re-run `env/bin/ezsql` after changes; verify stdio handshake |
| ruff/mypy clean | New code could add errors | Run `ruff check` + `mypy` after each step (§13) |
| Editable install | Deleting `requirements.txt` doesn't affect editable install (driven by `pyproject.toml`) | `pip show ezsql` still resolves after deletion |

---

## 11. Failure Modes (Pre-Mortem)

| Failure | Handling | Accepted? |
|---|---|---|
| `root` param points to nonexistent path | `FailureEnvelope(kind="invalid_root", recoverable=True, next_steps=[...])` | Handled (T1) |
| `root` param points to a file, not dir | Same `invalid_root` envelope | Handled (T1) |
| `root` param is relative path | Resolve; if not absolute after resolve → `invalid_root` | Handled (T1.1) |
| `root` param is a symlink to a file | Resolve target; if not dir → `invalid_root` | Handled (T1.2) |
| Neither `root` param nor config `project_root` | `FailureEnvelope(kind="missing_root", ...)` — never `Path.cwd()` | Handled |
| `root` points outside user's project (semantic misuse) | **Residual risk (T1) — ACCEPTED** (§17 Q1): no additional control; injection is the user's/project's concern, not EZSQL's. Matches MCP trust model. | Accepted (T1) |
| `.ezsql/config.toml` malformed TOML | Log warning, fall back to defaults (honest degradation §3.7). Not a FailureEnvelope (config is not tool input). | Accepted + logged (T4) |
| `.ezsql/config.toml` has out-of-range numeric fields | Clamp to valid range + log warning (T4.3) | Handled |
| `.ezsql/config.toml` sets `database_url_env` to a secret name | Stored as name only; never resolved/logged in Phase 1 (no DB connection). Oracle does not exist yet. | Accepted (T4.2) |
| `.ezsql/cache.db` corrupt / unreadable | Delete + recreate; log warning; cache is derived data (§26) | Handled (T6) |
| `.ezsql/cache.db` contains poisoned JSON values | `model_validate_json` rejects wrong schema → cache miss → re-scan | Handled (T6.1) |
| `.ezsql/cache.db` locked by another process | SQLite WAL handles concurrent readers; single-writer per process. If lock error, log + proceed without cache (degrade to re-scan) | Accepted + logged (T3) |
| Cache DB exceeds `cache_max_size_mb` | LRU eviction before insert (T3.2) | Handled |
| Scan hits unreadable file/dir | Skip silently, already handled by `deepsearchsql` | Handled |
| Scan hits enormous file (> `max_file_size`) | Skip content read; still match by `.sql` extension; log counter (T2.1) | Handled |
| Scan hits too many files (> `max_files_per_scan`) | Stop; return partial results with `truncated=True` in `scan_metadata` (T2.2) | Handled |
| Scan reads too many bytes (> `max_total_bytes`) | Stop; same truncation behavior (T2.3) | Handled |
| Scan hits binary file | Null-byte detection in first 1 KiB → skip (T2.4) | Handled |
| Symlink loop in repo | `os.walk(followlinks=False)` (explicit) — default prevents loops (T1.3) | Handled |
| Scan exceeds depth limit (> `max_scan_depth`) | Stop descending; log counter (T1.4) | Handled |
| Filename contains prompt-injection text | Returned as-is (filenames are data); server `instructions` advise treating output as untrusted (T5) | Accepted (T5) |
| Tool called with `task` | **No-op in Phase 1** (§17 Q7): `task` accepted by signature but ignored; registry stays empty stub. No expiry race possible. | Accepted |
| structlog not configured (lifespan didn't run) | `get_logger` returns a usable logger regardless; configure is idempotent | Handled |
| Secret value accidentally logged | structlog redaction processor replaces `url`/`key`/`token`/`secret`/`password` keys with `<redacted>` (T7.1) | Handled |
| pydantic model serialization round-trip fails in cache | Use `model_dump_json()` / `model_validate_json()` (not pickle); tested | Handled (T6) |

**Revision from §5.3:** Use `model_dump_json()` / `model_validate_json()` for cache values,
not pickle. Pickle deserializes arbitrary code — violates zero-trust (§16) even for "trusted
internal" data, because a corrupt cache.db could then execute code on load. JSON is safe.
Cost: negligible at Phase 1 scale. **Confirmed in §14A T6.**

---

## 12. Testing Strategy (derived from exit criterion)

**Exit criterion:** `uvx ezsql` serves `find_context` correctly against a fixture repo.

### Unit tests
- `test_scan.py` (extend): `.sql` discovery, keyword matching, skip-dirs pruning, **classification** (migration/query/ORM/config/doc/unknown), large-file skip (T2), symlink non-follow (T1.3), depth limit (T1.4), binary detection (T2.4), `max_files_per_scan` truncation (T2.2).
- `test_cache_keys.py`: same inputs → same key; different inputs → different key; key includes sqlglot version.
- `test_cache_store.py`: memory get/put/LRU eviction; SQLite tier get/put/promote; TTL expiry; corrupt-DB recovery (T6); mtime guard miss; size-bounded eviction (T3.2); schema-validation rejects poisoned entries (T6.1).
- `test_config.py`: defaults when no file; TOML parse; env-var name resolution (never value); `project_root` field; malformed TOML → defaults + warning; numeric field clamping (T4.3).
- `test_roots.py`: param wins; config fallback; both missing → FailureEnvelope; invalid path → FailureEnvelope; non-dir → FailureEnvelope; relative path → FailureEnvelope; never cwd; symlink-to-file → FailureEnvelope (T1).

### Pipeline tests
- `test_find_context.py`: full flow against `tests/fixtures/sample_repo/` — cache miss → scan → classify → ContextMap; second call → cache hit; missing root → FailureEnvelope; `task` param accepted but does not affect behavior (no-op, §17 Q7); `truncated` flag when `max_files_per_scan` hit (T2.2).

### Security tests (from §14A threat model)
- `test_scan.py`: scan of `/etc`-equivalent (fixture with sensitive-looking filenames) returns filenames only, never contents (T1, T5).
- `test_cache_store.py`: crafted JSON value with wrong schema → rejected → cache miss (T6.1).
- `test_config.py`: `database_url_env = "AWS_SECRET_ACCESS_KEY"` stored as name only, value never resolved/logged in Phase 1 (T4.2).
- `test_observability.py` (new): structlog redaction processor redacts `url`/`key`/`token`/`secret`/`password` keys (T7.1).

### Integration (manual, documented in README)
- `env/bin/ezsql` started, MCP `initialize` + `tools/list` shows `find_context` with correct description; `tools/call find_context {"root": "<fixture>"}` returns grouped files.

### Gate
- `env/bin/python -m pytest -q` — all green.
- `env/bin/ruff check src/ tests/` — clean.
- `env/bin/python -m mypy src/ezsql` — clean.
- `env/bin/ezsql` — starts, serves `find_context`.

---

## 13. Implementation Order (verifiable steps)

Each step ends with a green gate (pytest + ruff + mypy). No step proceeds until the prior passes.

| Step | Scope | Gate |
|---|---|---|
| **1.1** | Delete dead weight: `src/server/`, `src/scripts/`, `src/tests/`, `src/docs/`, `src/tools/`, tracked caches, `requirements.txt` (§6.2 confirmed). | ruff/mypy clean; tests still pass (2) |
| **1.2** | `pyproject.toml`: pin deps to installed versions; keep `>=3.11`/`py311` (§6.1 confirmed). | `pip install -e .` succeeds; `ezsql` runs |
| **1.3** | `config.py`: `load_config(root)`, `tomllib` parse, new fields (`project_root`, `cache_max_size_mb`, `cache_max_entries`, `task_ttl_seconds`, `max_file_size`, `max_files_per_scan`, `max_total_bytes`, `max_scan_depth`). **Numeric field clamping** (T4.3). `test_config.py`. | pytest green |
| **1.4** | `observability.py`: `configure_logging()` (stderr JSON), `Counter` registry, **structlog redaction processor** (T7.1). `test_observability.py`. | pytest green |
| **1.5** | `cache/keys.py`: `scan_key(root)`. `test_cache_keys.py`. | pytest green |
| **1.6** | `cache/store.py`: `CacheStore` (memory + SQLite, **JSON values** via `model_validate_json` (T6), LRU, TTL, mtime guard, **size-bounded eviction** (T3.2), **corrupt-DB recovery** (T6), **schema-validation rejection** (T6.1)). `test_cache_store.py`. | pytest green |
| **1.7** | `server/roots.py`: `resolve_root` — absolute + dir + symlink checks (T1.1–T1.3). `test_roots.py`. | pytest green |
| **1.8** | `core/context/scan.py`: `classify_file` + `FileClassification`; **`max_file_size` skip** (T2.1), **`max_files_per_scan` truncation** (T2.2), **`max_total_bytes` cap** (T2.3), **binary detection** (T2.4 — confirmed §17 Q3), **`followlinks=False` explicit** (T1.3), **depth limit** (T1.4). Extend `test_scan.py`. | pytest green |
| **1.9** | `server/models.py`: extend `ContextMap` (`scan_metadata` with `truncated`/`files_skipped`/`files_seen`, `cache_provenance`), add `ContextFile`, `FileClassification`. | mypy clean |
| **1.10** | `pipelines/context.py`: cache check → scan → classify → cache store → ContextMap; honor `query`; **`task` accepted but ignored (no-op, §17 Q7)**. `test_find_context.py` (replace `test_context_pipeline.py`). | pytest green |
| **1.11** | `server/tools.py`: explicit `description=`, `roots.resolve_root`, typed result (ContextMap or FailureEnvelope), wire cache; **`task` param in signature but not passed to pipeline (no-op)**. | mypy clean |
| **1.12** | `server/app.py`: `instructions=` (with untrusted-data advisory per T5), `lifespan=` (config + cache + logging init). | `ezsql` starts |
| **1.13** | `README.md`: real description + install/run. `tests/fixtures/sample_repo/` built (includes noise, large file, binary file, symlink for security tests). | full gate green |
| **1.14** | Final gate: pytest + ruff + mypy + manual `ezsql` smoke against fixture. | exit criterion met |

---

## 14. Security Review (per `security.md`)

| Boundary | Control in Phase 1 |
|---|---|
| Tool input (`root`, `query`, `task` strings) | pydantic validation; `root` resolved + validated as existing dir; `query`/`task` length-capped; no SQL executed |
| Repo files | Scan reads filenames + (for non-.sql) file text for keyword match only — **never returned**, only classified. No path traversal: `root` is resolved and scan stays under it (`os.walk` from root) |
| Config file | `tomllib` parse (no code execution); env-var *names* only; values resolved at call time, never logged |
| Cache DB | JSON values (no pickle → no code execution on load); SQLite parameterized queries (no string concat SQL); `.ezsql/` gitignore-recommended |
| Logs | structlog to stderr; no tool input echoed at INFO (hashed/omitted where sensitive); scan logs counts, not filenames |
| No secrets cached | Scan domain caches filenames + classifications only; enforced by domain scope |

---

## 14A. Security Threat Model (added 2026-08-17 — zero-trust analysis)

**The security doctrine demands we treat every input as hostile.** Option B (confirmed)
makes `root` an attacker-controllable parameter: the calling agent is an LLM that may be
influenced by prompt injection from repo files, and `root` is a string it passes to us.
This section analyzes each threat and the control that addresses it. Every control below
is **mandatory in Phase 1**, not optional hardening.

### Threat T1: `root` points outside the user's project (path traversal / arbitrary read)

**Scenario:** A crafted repo file contains prompt injection instructing the agent to call
`find_context(root="/etc")` or `find_context(root="~/.ssh")`. The scan walks that directory,
reads file contents for keyword matching, and returns filenames — exfiltrating the
filesystem layout of sensitive locations to the agent (and thus to whoever injected the
prompt).

**Severity:** High — filesystem enumeration of arbitrary paths.

**Controls (defense in depth):**
1. **`root` must be absolute and resolved.** `Path(root).resolve()` — no relative paths,
   no `..` traversal after resolution. Reject if not absolute after resolve.
2. **`root` must be a directory** (not a file, not a symlink to a file).
3. **Symlink check:** `root.is_symlink()` → resolve and verify the target is a dir, but
   **do not follow symlinks during the scan** (`os.walk(root, followlinks=False)` — already
   the default; make it explicit).
4. **Depth limit:** `os.walk` with a max-depth guard (config, default 20) to prevent
   pathological deep trees.
5. **No file *contents* are ever returned** — only filenames and classifications.
   A scan of `/etc` would return filenames like `passwd`, `shadow` — which is still
   information leakage. **This is the residual risk; see Q1 below.**

**Residual risk — ACCEPTED (user, 2026-08-17, §17 Q1):** We cannot cryptographically prove
the agent's `root` is the "real" project. Controls 1–4 prevent traversal *mechanics* but
not *semantic* misuse (scanning `/etc` and returning filenames). **No additional control
will be added** — prompt injection is the user's and their project's concern, not EZSQL's.
This matches MCP's trust model: the host/agent is trusted; tools are the sandbox. The agent
passing a bad `root` is the same trust level as the agent running `rm -rf`.

### Threat T2: File-content DoS during scan

**Scenario:** `root` contains a 10 GiB file (or many large files). `deepsearchsql` reads
each text file fully (`f.read()`) to check for SQL keywords — OOM or multi-minute hang.

**Severity:** Medium — denial of service against the server process.

**Controls:**
1. **`max_file_size`** (config, default 1 MiB): files larger than this are skipped for
   *content* reading (still matched by `.sql` extension — name matching doesn't read
   content). Log a counter (`scan_files_skipped_oversize`).
2. **`max_files_per_scan`** (config, default 50,000): hard cap; scan stops and returns
   partial results with a `truncated=True` flag in `scan_metadata`.
3. **`max_total_bytes`** (config, default 256 MiB): cumulative bytes-read cap across the
   whole scan; same truncation behavior.
4. **Binary detection:** `open(..., 'rb').read(1024)` + check for null bytes → skip as
   binary (faster than full-read-then-UnicodeDecodeError).

### Threat T3: Cache DB written to attacker-chosen location

**Scenario:** `root` is attacker-controlled → `<root>/.ezsql/cache.db` is written to an
attacker-chosen path. Could be used to: (a) fill disk (no size bound yet), (b) overwrite
an existing file, (c) write to a sensitive location.

**Severity:** Medium — write primitive to arbitrary path.

**Controls:**
1. **Cache path is `<root>/.ezsql/cache.db`** — always under `root`, never elsewhere.
   If `root` is validated (T1 controls), the cache path is bounded to under `root`.
2. **`cache_max_size_mb`** (config, default 50): SQLite `PRAGMA max_page_count` or
   application-level size check before insert; evict LRU if over.
3. **Never overwrite non-cache files:** the `.ezsql/` dir is created with `exist_ok=True`;
   `cache.db` is opened with SQLite's default (creates if absent). If a *directory* exists
   at that path, SQLite fails safely — catch and degrade (no cache, re-scan).
4. **Cache is derived data:** if corrupt or deleted, everything still works (re-scan).
   Documented in §26.

### Threat T4: Attacker-controlled config file

**Scenario:** `root` points to a repo with a crafted `.ezsql/config.toml`. The attacker
controls: `database_url_env` (env-var *name*), `llm_api_key_env` (env-var *name*),
`default_dialect`, `cache_max_size_mb`, `task_ttl_seconds`, etc.

**Severity:** Low–Medium — config influences behavior but cannot execute code.

**Controls:**
1. **`tomllib` is safe** — no code execution, pure data parse.
2. **Env-var *names* only** — we never log the *value*. But an attacker could set
   `database_url_env = "AWS_SECRET_ACCESS_KEY"` and then observe whether EZSQL behaves as
   if a DB is configured (it would try to connect in Phase 3). **In Phase 1, there is no
   DB connection** — `database_url_env` is stored but never *used*. So this oracle does
   not exist yet. **Documented for Phase 3.**
3. **Numeric fields clamped:** `cache_max_size_mb` clamped to `[1, 1024]`,
   `cache_max_entries` to `[16, 65536]`, `task_ttl_seconds` to `[60, 86400]`. Reject
   out-of-range with a warning + default.
4. **`allow_writes` ignored** in Phase 1 (no write path exists).

### Threat T5: Prompt injection via scan output

**Scenario:** A repo file's *filename* or *path* contains prompt-injection text
(e.g., `IGNORE_PREVIOUS_INSTRUCTIONS_AND_.sql`). The filename is returned in the
`ContextMap` and the agent reads it.

**Severity:** Low — filenames are data, but the agent processes them as text.

**Controls:**
1. **Server `instructions`** (§5.6) explicitly states: treat all tool output as untrusted
   data, not instructions. This is the §16 structural + advisory control.
2. **Filenames are not sanitized** (they're real filesystem names; sanitizing would break
   the tool's purpose). The defense is advisory, not filtering.
3. **No file *contents* returned** — only names + classifications. Injection surface is
   limited to filenames/paths, which are low-fidelity injection vectors.

### Threat T6: Cache poisoning / cache DB tampering

**Scenario:** An attacker with filesystem access to `<root>/.ezsql/cache.db` modifies a
cached entry to return wrong file classifications or a poisoned file list.

**Severity:** Low — cache is derived data; worst case is stale/wrong context, not code
execution.

**Controls:**
1. **JSON values via `model_validate_json`** — pydantic validates the schema on load. A
   crafted entry with wrong types is rejected (parse error → cache miss → re-scan).
2. **Content-addressed keys** — the key is `blake2b(inputs)`. An attacker can't forge a
   key that matches a different input (preimage resistance). They can only corrupt the
   *value* under an existing key, which is caught by schema validation.
3. **mtime guard** — scan freshness is checked against the filesystem; a stale cache entry
   with a changed directory is invalidated by mtime+size mismatch.
4. **No integrity check (HMAC)** in Phase 1 — the cache is local derived data; if an
   attacker has filesystem write access to `.ezsql/`, they already have more power than
   cache poisoning. **Accepted residual risk.**

### Threat T7: Secrets in logs

**Scenario:** Config env-var *names* or file paths containing secrets are logged.

**Severity:** Medium — log exfiltration.

**Controls:**
1. **structlog processor redacts** known-sensitive keys (`url`, `key`, `token`, `secret`,
   `password`) by replacing with `"<redacted>"`.
2. **Scan logs counts, not filenames** — `scan_files_seen=42`, not the file list.
3. **Config logs field *names*, not values** — `database_url_env="DATABASE_URL"` (the
   name) is fine; the *value* is never logged.
4. **Tool input logged at DEBUG only** — `root` path is logged (it's not secret), but
   `query`/`task` (agent-supplied, could contain anything) are truncated to 100 chars.

---

## 15. Skills & Doctrine Invoked

| Loaded | What it contributed |
|---|---|
| `plan.md` (§25 Phase 1, §6, §8, §14, §16, §21, §23) | The spec for Phase 1 |
| `repoarchitecture.mmd` | Canonical layering + contracts |
| `/memories/repo/ezsql-mcp-fastmcp.md` | SDK state, user style prefs (functional, minimal classes, no `__future__`) |
| `/memories/repo/no-future-imports.md` | No `__future__` imports |
| `doctrine/planning.md` | Plan template, interrogation axes, pre-mortem |
| `doctrine/architecture.md` | Prior art mining, tradeoff axes, reversibility |
| `doctrine/security.md` | **Fully loaded** (not just referenced) — zero-trust, trust boundaries, no secrets, fail-closed, input validation. Drove §14A threat model. |
| `doctrine/verification.md` (referenced) | Gate-before-done, re-run after edits |
| `python.instructions.md` | Strict typing, zero Pylance errors, stdlib-first |
| mcp SDK source (inspected) | Real API surface; roots deprecation discovery |
| MCP 2026-07-28 spec + SEP-2577 | Roots/sampling/logging deprecation; replacement guidance |

---

## 16. Reversibility

| Decision | Door | Note |
|---|---|---|
| Delete old `src/server/`, `src/scripts/`, `src/tests/` | Two-way (git) | Recoverable from history |
| Root resolution Option B (no `list_roots`) | Two-way | Can add roots back later if needed (but shouldn't — deprecated) |
| Cache JSON-over-pickle | Two-way | Internal format; swap if needed |
| `pyproject.toml` pin versions | Two-way | Bump anytime |
| `server/roots.py` as separate module | Two-way | Inline if it stays tiny |
| `instructions`/`lifespan` wiring | Two-way | Constructor args; change anytime |
| File classification heuristics | Two-way | Data-driven; extend without engine changes |

No one-way doors in Phase 1 except the **roots decision (§6.3)** — once we don't build the
roots path, adding it later is work. But since roots is deprecated, that's the *correct*
one-way door to commit to.

---

## 17. Confirmed Answers (user, 2026-08-17)

**All questions resolved. No further input needed.**

### Security answers

**Q1 — Root authorization model (T1): CONFIRMED (a) — no additional control.**
Prompt injection is the user's and their project's concern, not EZSQL's. Matches MCP's
trust model (host/agent trusted, tools are the sandbox). Residual risk T1 accepted.

**Q2 — File-content read limits (T2): CONFIRMED — accepted as proposed.**
`max_file_size=1 MiB`, `max_files_per_scan=50,000`, `max_total_bytes=256 MiB`.
Config-overridable if the user needs more.

**Q3 — Binary detection (T2.4): CONFIRMED — add it.**
Null-byte detection (read first 1 KiB, check for `\x00`, skip if found) before UTF-8
decode. Faster than full-read-then-UnicodeDecodeError.

### Design answers

**Q4 — Classification categories (§5.8): CONFIRMED — sufficient as-is.**
`migration` / `query` / `orm` / `config` / `doc` / `unknown`. No additions or merges.

**Q5 — Cache location (§5.3): CONFIRMED — `<root>/.ezsql/cache.db` (project-local).**
Creates a file in the user's project; gitignore-recommended. Portable; deleted with project.

**Q6 — Tool stubs (scope): CONFIRMED — keep them.**
The 7 other tool modules stay as empty stubs. They signal the architecture and help
understand the whole project structure. Not registered as MCP tools in Phase 1.

**Q7 — `task` parameter in Phase 1: CONFIRMED — no-op.**
`find_context` accepts `task` in its signature (per plan §21) but does NOT pass it to the
pipeline or use it. `tasks/registry.py` stays as an empty stub. Implemented in Phase 2+
when multiple tools share task context. This simplifies Phase 1: no registry, no TTL,
no expiry race.

---

## 18. Definition of Done (eight yeses)

1. ✅ Interrogated until understanding was exact — verified SDK + spec, not plan assumptions.
2. ✅ Loaded every doctrine file and skill this task triggered (§15).
3. ✅ Every decision justified with doc/precedent/measurement, written here (§7).
4. ✅ Root cause addressed — the roots staleness is surfaced, not papered over (§2).
5. ✅ Simplest design that solves today's problem — no LLM, no DB, no LangGraph, no HTTP, no task registry in Phase 1.
6. ⏳ Verify by running — pytest + ruff + mypy + `ezsql` smoke (§13 step 1.14). *Pending execution.*
7. ⏳ Codebase strictly better — dead weight removed, gate green. *Pending execution.*
8. ✅ Report honestly — including the roots divergence, the security threat model, and all 10 confirmed decisions.

---

**All 10 decisions confirmed (§6.1–6.3, §17 Q1–Q7). Awaiting final approval to execute.**
No further questions remain. Say the word and I begin at Step 1.1.