"""Configuration loading and secret resolution for EZSQL.

Config is loaded from ``<root>/.ezsql/config.toml`` if present. Missing file →
all defaults (honest degradation, plan §3.7). Secrets are stored as env-var
*names* only (plan §16); values are resolved in-process at call time, never
in tool I/O, logs, or cache.
"""

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

logger = logging.getLogger("ezsql.config")

_CONFIG_DIR = ".ezsql"
_CONFIG_FILE = "config.toml"

# Clamping ranges for numeric config fields (T4.3 — attacker-controlled config).
_CLAMP_RANGES: dict[str, tuple[int, int]] = {
    "cache_max_size_mb": (1, 1024),
    "cache_max_entries": (16, 65536),
    "task_ttl_seconds": (60, 86400),
    "max_file_size": (1, 100 * 1024 * 1024),
    "max_files_per_scan": (100, 500_000),
    "max_total_bytes": (1024, 4 * 1024 * 1024 * 1024),
    "max_scan_depth": (1, 100),
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

    @field_validator("cache_max_size_mb", "cache_max_entries", "task_ttl_seconds",
                     "max_file_size", "max_files_per_scan", "max_total_bytes",
                     "max_scan_depth")
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
    return EzsqlConfig.model_validate(section)


__all__ = ["EzsqlConfig", "load_config"]
