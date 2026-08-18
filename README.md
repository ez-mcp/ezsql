# ezsql

An AI-native SQL engineering MCP server. EZSQL sits between an LLM coding
agent and SQL/database infrastructure: it scans repositories for SQL files,
classifies them (migration/query/orm/config/doc), and provides deterministic
SQL analysis, optimization, and security tooling.

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

## Tools (Phase 1)

### `find_context`

Scans a repository for SQL-bearing files and returns a grouped, classified
map. Call this FIRST for any SQL/database work to orient yourself.

**Parameters:**
- `root` (required): Absolute path to the project root.
- `query` (optional): Filter query (Phase 2+).
- `task` (optional): Task ID for continuity (Phase 2+).

**Output:** A grouped dictionary of classified files by directory.

## Configuration

Optional `.ezsql/config.toml` in the project root:

```toml
[ezsql]
project_root = "/path/to/project"
default_dialect = "postgres"
max_file_size = 1048576  # 1 MiB
max_files_per_scan = 50000
max_total_bytes = 268435456  # 256 MiB
max_scan_depth = 20
```

Missing config file → all defaults. Numeric fields are clamped to valid ranges.

## Development

```bash
# Run tests
env/bin/python -m pytest -q

# Lint
env/bin/ruff check src/ tests/

# Type check
env/bin/python -m mypy src/ezsql
```