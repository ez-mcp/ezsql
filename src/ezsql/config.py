"""Configuration loading and secret resolution for EZSQL.

Config is loaded from ``<root>/.ezsql/config.toml`` if present. Missing file →
all defaults (honest degradation, plan §3.7). Secrets are stored as env-var
*names* only (plan §16); values are resolved in-process at call time, never
in tool I/O, logs, or cache.
"""

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger("ezsql.config")

_CONFIG_DIR = ".ezsql"
_CONFIG_FILE = "config.toml"

# Conservative identifier pattern for env-var names (plan_phase3 §7).
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Clamping ranges for numeric config fields (T4.3 — attacker-controlled config).
_CLAMP_RANGES: dict[str, tuple[int, int]] = {
    "cache_max_size_mb": (1, 1024),
    "cache_max_entries": (16, 65536),
    "task_ttl_seconds": (60, 86400),
    "max_file_size": (1, 100 * 1024 * 1024),
    "max_files_per_scan": (100, 500_000),
    "max_total_bytes": (1024, 4 * 1024 * 1024 * 1024),
    "max_scan_depth": (1, 100),
    # Phase 2 limits (plan §11)
    "max_sql_input_bytes": (1024, 100 * 1024 * 1024),
    "max_sec_files": (1, 10_000),
    "max_total_file_bytes": (1024, 4 * 1024 * 1024 * 1024),
    "max_statements": (1, 100_000),
    "max_findings": (10, 10_000),
    "max_candidates": (1, 1_000),
    "max_parser_warnings": (10, 1_000),
    "max_parse_errors": (1, 1_000),
    "max_analysis_tables": (10, 10_000),
    "max_analysis_columns": (10, 50_000),
    "max_analysis_joins": (1, 5_000),
    "max_analysis_predicates": (1, 10_000),
    "max_message_length": (100, 10_000),
    "max_snippet_length": (50, 5_000),
    # Phase 3 limits (plan_phase3 §4, §7)
    "max_explain_sql_bytes": (1024, 4_194_304),
    "max_plan_response_bytes": (65_536, 16_777_216),
    "max_plan_nodes": (50, 5_000),
    "max_plan_depth": (8, 256),
    "max_plan_condition_chars": (64, 8_192),
    "db_pool_min_size": (1, 10),
    "db_pool_max_size": (1, 20),
    "max_database_adapters": (1, 16),
    "db_connect_timeout_seconds": (1, 60),
    "db_acquire_timeout_seconds": (1, 60),
    "explain_ttl_seconds": (60, 86_400),
    "max_explain_candidates": (1, 50),
    "explain_statement_timeout_seconds": (1, 300),
    "explain_lock_timeout_seconds": (1, 60),
    "explain_total_timeout_seconds": (2, 360),
    "runtime_enrichment_timeout_seconds": (5, 600),
    "max_schema_files": (1, 10_000),
    "max_schema_file_bytes": (1024, 16_777_216),
    "max_schema_total_bytes": (1024, 64 * 1024 * 1024),
}


class EzsqlConfig(BaseModel):
    """EZSQL runtime configuration.

    All fields have safe defaults. Values from ``.ezsql/config.toml`` override
    defaults; numeric fields are clamped to valid ranges (T4.3).
    """

    # Root resolution (§6.3 — Option B: root param primary, this is fallback).
    project_root: str | None = None

    # Database (Phase 3; stored but unused in Phase 1).
    default_dialect: str = "postgres"
    database_url_env: str = "DATABASE_URL"

    # LLM escalation (Phase 4; stored but unused in Phase 1).
    llm_api_key_env: str = "OPENAI_API_KEY"
    llm_token_budget: int = 4000

    # Write gate (post-v1; ignored in Phase 1).
    allow_writes: bool = False

    # Cache (§14).
    cache_max_size_mb: int = 50
    cache_max_entries: int = 4096

    # Task registry (Phase 2+; stored but unused in Phase 1 — task is no-op).
    task_ttl_seconds: int = 3600

    # Scan safety limits (T2 — DoS protection).
    max_file_size: int = 1024 * 1024  # 1 MiB
    max_files_per_scan: int = 50_000
    max_total_bytes: int = 256 * 1024 * 1024  # 256 MiB
    max_scan_depth: int = 20

    # Phase 2: SQL input limits (plan §11)
    max_sql_input_bytes: int = 4_194_304  # 4 MiB
    max_sec_files: int = 200
    max_total_file_bytes: int = 32 * 1024 * 1024  # 32 MiB
    max_statements: int = 10_000

    # Phase 2: Output bounding (plan §11.4)
    max_findings: int = 1_000
    max_candidates: int = 50
    max_parser_warnings: int = 100
    max_parse_errors: int = 100
    max_analysis_tables: int = 500
    max_analysis_columns: int = 5_000
    max_analysis_joins: int = 200
    max_analysis_predicates: int = 1_000
    max_message_length: int = 2_048
    max_snippet_length: int = 512

    # Phase 3: live planner evidence limits (plan_phase3 §4)
    max_explain_sql_bytes: int = 262_144  # 256 KiB
    max_plan_response_bytes: int = 2_097_152  # 2 MiB
    max_plan_nodes: int = 500
    max_plan_depth: int = 64
    max_plan_condition_chars: int = 1_024

    # Phase 3: adapter lifecycle (plan_phase3 §7)
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5
    max_database_adapters: int = 4
    db_connect_timeout_seconds: int = 10
    db_acquire_timeout_seconds: int = 5
    explain_ttl_seconds: int = 3_600
    max_explain_candidates: int = 5
    explain_statement_timeout_seconds: int = 30
    explain_lock_timeout_seconds: int = 5
    explain_total_timeout_seconds: int = 45
    runtime_enrichment_timeout_seconds: int = 90

    # Phase 3: repository schema loader bounds (plan_phase3 §6)
    max_schema_files: int = 1_000
    max_schema_file_bytes: int = 4 * 1024 * 1024  # 4 MiB
    max_schema_total_bytes: int = 32 * 1024 * 1024  # 32 MiB

    def get_database_url(self) -> str | None:
        """Resolve database URL from the configured environment variable.

        Returns the *value* of the env var named by ``database_url_env``.
        The value never appears in logs, cache, or tool I/O — only the name
        is stored in config (plan §16).
        """
        return os.environ.get(self.database_url_env)

    def get_llm_api_key(self) -> str | None:
        """Resolve LLM API key from the configured environment variable."""
        return os.environ.get(self.llm_api_key_env)

    @field_validator("database_url_env", "llm_api_key_env")
    @classmethod
    def _validate_env_names(cls, v: str) -> str:
        """Env-var names must match a conservative identifier pattern (plan_phase3 §7)."""
        if not _ENV_NAME_PATTERN.match(v):
            raise ValueError(
                f"env-var name '{v}' is not a valid identifier "
                f"(must match [A-Za-z_][A-Za-z0-9_]*)"
            )
        return v

    @field_validator("cache_max_size_mb", "cache_max_entries", "task_ttl_seconds",
                     "max_file_size", "max_files_per_scan", "max_total_bytes",
                     "max_scan_depth",
                     "max_sql_input_bytes", "max_sec_files", "max_total_file_bytes",
                     "max_statements", "max_findings", "max_candidates",
                     "max_parser_warnings", "max_parse_errors",
                     "max_analysis_tables", "max_analysis_columns",
                     "max_analysis_joins", "max_analysis_predicates",
                     "max_message_length", "max_snippet_length",
                     "max_explain_sql_bytes", "max_plan_response_bytes",
                     "max_plan_nodes", "max_plan_depth", "max_plan_condition_chars",
                     "db_pool_min_size", "db_pool_max_size", "max_database_adapters",
                     "db_connect_timeout_seconds", "db_acquire_timeout_seconds",
                     "explain_ttl_seconds", "max_explain_candidates",
                     "explain_statement_timeout_seconds", "explain_lock_timeout_seconds",
                     "explain_total_timeout_seconds", "runtime_enrichment_timeout_seconds",
                     "max_schema_files", "max_schema_file_bytes", "max_schema_total_bytes")
    @classmethod
    def _clamp_numeric(cls, v: int, info: Any) -> int:
        """Clamp numeric fields to valid ranges (T4.3)."""
        field_name = info.field_name
        if field_name in _CLAMP_RANGES:
            lo, hi = _CLAMP_RANGES[field_name]
            if v < lo:
                logger.warning("config field %s=%d below min %d; clamped", field_name, v, lo)
                return lo
            if v > hi:
                logger.warning("config field %s=%d above max %d; clamped", field_name, v, hi)
                return hi
        return v

    @model_validator(mode="after")
    def _validate_relational(self) -> "EzsqlConfig":
        """Relational validation (plan_phase3 §7).

        - min pool size ≤ max pool size
        - lock timeout ≤ statement timeout
        - each stage timeout ≤ its enclosing total timeout
        """
        if self.db_pool_min_size > self.db_pool_max_size:
            # Clamp min to max rather than reject — the pool still works.
            object.__setattr__(self, "db_pool_min_size", self.db_pool_max_size)
        if self.explain_lock_timeout_seconds > self.explain_statement_timeout_seconds:
            object.__setattr__(
                self, "explain_lock_timeout_seconds", self.explain_statement_timeout_seconds
            )
        if self.db_connect_timeout_seconds > self.explain_total_timeout_seconds:
            object.__setattr__(
                self, "db_connect_timeout_seconds", self.explain_total_timeout_seconds
            )
        if self.db_acquire_timeout_seconds > self.explain_total_timeout_seconds:
            object.__setattr__(
                self, "db_acquire_timeout_seconds", self.explain_total_timeout_seconds
            )
        if self.explain_statement_timeout_seconds > self.explain_total_timeout_seconds:
            object.__setattr__(
                self, "explain_statement_timeout_seconds", self.explain_total_timeout_seconds
            )
        if self.explain_total_timeout_seconds > self.runtime_enrichment_timeout_seconds:
            object.__setattr__(
                self, "explain_total_timeout_seconds", self.runtime_enrichment_timeout_seconds
            )
        return self


def load_config(root: Path) -> EzsqlConfig:
    """Load EZSQL config from ``<root>/.ezsql/config.toml``.

    Missing file → all defaults (honest degradation). Malformed TOML →
    log warning, fall back to defaults. Env-var *names* only are stored;
    values are resolved at call time (plan §16).
    """
    config_path = root / _CONFIG_DIR / _CONFIG_FILE
    if not config_path.is_file():
        return EzsqlConfig()

    try:
        with open(config_path, "rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Failed to load %s: %s; using defaults", config_path, exc)
        return EzsqlConfig()

    # Flatten one level: [ezsql] section or top-level keys.
    section: dict[str, Any] = raw.get("ezsql", raw)

    # Strict unknown-key behavior (plan_phase3 §7): misspelled keys are
    # rejected and reported rather than silently ignored.
    known_fields = set(EzsqlConfig.model_fields)
    unknown = sorted(set(section) - known_fields)
    if unknown:
        logger.warning(
            "config rejected: unknown keys %s; using defaults", ",".join(unknown)
        )
        return EzsqlConfig()

    try:
        return EzsqlConfig.model_validate(section)
    except ValueError as exc:
        # Log field names only — never values (may contain secrets).
        fields = _extract_error_fields(exc)
        logger.warning(
            "config validation failed for fields %s; using defaults", fields
        )
        return EzsqlConfig()


def _extract_error_fields(exc: ValueError) -> str:
    """Extract field names (not values) from a pydantic validation error."""
    lines: list[str] = []
    text = str(exc)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("Invalid", "Value error")):
            # pydantic lines look like "field_name\n  Value error, ..."
            field = line.split("\n")[0].strip()
            if field and not field[0].isdigit():
                lines.append(field)
    return ",".join(lines[:10])
