# ezsql

An AI-native SQL engineering MCP server. EZSQL sits between an LLM coding
agent and SQL/database infrastructure: it scans repositories for SQL files,
classifies them (migration/query/orm/config/doc), provides deterministic
SQL analysis, optimization, and security tooling, and — when a PostgreSQL
database is configured — returns **live planner evidence** (EXPLAIN plans
and cost deltas) without ever executing a query.

## Install

```bash
uvx ezsql
```

Or from source:

```bash
pip install -e .
```

## Run

EZSQL runs as a stdio MCP server:

```bash
ezsql
```

Configure it in your MCP client (VS Code, Claude, Cursor) as a stdio server.

## Tools (Phase 3 — five workflow tools)

### `find_context`

Scans a repository for SQL-bearing files and returns a grouped, classified
map. Call this FIRST for any SQL/database work to orient yourself.

**Parameters:**
- `root` (required): Absolute path to the project root.
- `query` (optional): Filter query.
- `task` (optional): Task ID (accepted, currently a no-op).

### `analyze_sql`

Parses SQL, extracts AST facts (tables, columns, joins, predicates), and
runs lint heuristics with two-dimensional evidence.

### `sql_sec`

Security analysis of SQL or source files: dangerous statements, migration
safety, host-language injection patterns, with rule coverage tracking.

### `optimize_query`

Static query optimization with rewrite candidates. When a PostgreSQL
database is configured, eligible candidates gain **live planner evidence**:
typed `plan_delta` values (estimated cost/cardinality deltas from the live
planner). Live evidence annotates candidates — it never decides semantic
correctness, and candidates are never reranked or promoted because of it.

### `explain_query`

The PostgreSQL planner's plan for one query: normalized plan tree with
estimated costs, row counts, scan/join operations, and planning time.

**Input contract:** exactly one **unprefixed PostgreSQL SELECT query**
(read-only CTEs and set operations allowed). `EXPLAIN` prefixes, writes,
commands, locking clauses (`FOR UPDATE`), writable CTEs, `SELECT INTO`,
and multi-statement input are all rejected. EZSQL owns the EXPLAIN
envelope — `ANALYZE` is never used, so queries are never executed.

**Evidence semantics:** costs and row counts are **planner estimates**,
not measured execution time. Planning time is not execution time. Generic
plans (for `$n` parameterized queries) may differ from value-specific
plans. Every result carries these limitations explicitly.

## Configuration

Optional `.ezsql/config.toml` in the project root (see
`.ezsql/config.example.toml` for a Phase 3 excerpt):

```toml
[ezsql]
project_root = "/path/to/project"
default_dialect = "postgres"
database_url_env = "DATABASE_URL"   # env-var NAME, never the value
max_explain_sql_bytes = 262144
explain_ttl_seconds = 3600
```

Missing config file → all defaults. Numeric fields are clamped to valid
ranges. **Unknown keys are rejected** (the config falls back to defaults
and the misspelled key names are logged) — never silently ignored.

## Database access (Phase 3)

When the env var named by `database_url_env` holds a PostgreSQL URI,
`explain_query` returns live plans and `optimize_query` enriches candidates
with planner deltas. Without a database, everything degrades honestly:
`optimize_query` returns static results and `explain_query` fails with
`db_unavailable`.

**Connection requirements:**
- PostgreSQL 16+ (JSON EXPLAIN is the adapter contract).
- URI scheme `postgres://` or `postgresql://` with **explicit** host,
  database, and role (no `PGHOST`/`PGDATABASE`/`PGUSER` inheritance).
- TCP requires `sslmode=require`, `verify-ca`, or `verify-full`.
  Unix-domain sockets are allowed as local IPC.
- Unknown URI query parameters are **rejected**, not forwarded.

**Read-only safety model (defense in depth):**
1. **Statement gate** — exactly one unprefixed SELECT; writes, commands,
   locks, writable CTEs, and explicit EXPLAIN are rejected before any
   network I/O. The adapter only ever receives canonical SQL rendered
   from the validated AST.
2. **Explicit readonly transaction** — every EXPLAIN runs inside
   `transaction(readonly=True)` with transaction-local statement and lock
   timeouts (pool-level `RESET ALL` cannot remove these).
3. **No write API** — the adapter exposes only `connect`, `explain`,
   `close`. There is no execute or fetch method to misuse.
4. **Role preflight** — superusers, `BYPASSRLS`, `CREATEROLE`/`CREATEDB`
   holders, TEMP/CREATE grantees, and roles with any write privilege on
   non-system relations are rejected at connect time.
5. **Encrypted transport** — insecure TCP SSL modes are refused.

**Residual limit (stated honestly):** PostgreSQL read-only transactions
are a high-level control, not a perfect sandbox — they can permit writes
to temporary objects, and planner support functions depend on DB
administrator trust. A dedicated least-privileged role (CONNECT, USAGE,
SELECT only; no TEMP, no CREATE, no write privileges) is a **deployment
requirement**, documented in `.ezsql/config.example.toml`. EZSQL never
runs `EXPLAIN ANALYZE`; queries are never executed.

**Caching:** static analyses are cached content-addressed (Phase 2
semantics unchanged). Live plans and runtime evidence live in separate
TTL-bound cache domains keyed by a **non-secret DB identity fingerprint**
(host, port, database, role, SSL mode — passwords and certificate paths
are excluded). Failures are never cached; a recovered database is retried
immediately. Plan condition expressions are literal-redacted before they
reach models, cache, output, or logs.

## Development

```bash
# Run tests
env/bin/python -m pytest -q

# Lint
env/bin/ruff check src/ tests/

# Type check
env/bin/python -m mypy src/ezsql
```

Integration tests against a real PostgreSQL 16 run only when
`EZSQL_TEST_DATABASE_URL` is set and the database name matches the
test-only pattern (`ezsql_test`); the fixture refuses any other database.
CI (`.github/workflows/ci.yml`) provisions an isolated database, a
least-privileged role, and TLS, plus a separate no-DB job proving honest
degradation.
