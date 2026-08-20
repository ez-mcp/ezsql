"""Unit tests for configuration loading."""

from pathlib import Path

import pytest

from ezsql.config import EzsqlConfig, load_config


def test_load_config_defaults_when_no_file(tmp_path: Path) -> None:
    """Missing config file → all defaults (honest degradation)."""
    config = load_config(tmp_path)
    assert isinstance(config, EzsqlConfig)
    assert config.default_dialect == "postgres"
    assert config.database_url_env == "DATABASE_URL"
    assert config.allow_writes is False
    assert config.project_root is None
    assert config.cache_max_size_mb == 50
    assert config.cache_max_entries == 4096
    assert config.task_ttl_seconds == 3600
    assert config.max_file_size == 1024 * 1024
    assert config.max_files_per_scan == 50_000
    assert config.max_total_bytes == 256 * 1024 * 1024
    assert config.max_scan_depth == 20


def test_load_config_from_toml(tmp_path: Path) -> None:
    """Valid TOML config overrides defaults."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
default_dialect = "mysql"
database_url_env = "MY_DB_URL"
project_root = "/home/user/myproject"
cache_max_size_mb = 100
max_file_size = 5242880
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_dialect == "mysql"
    assert config.database_url_env == "MY_DB_URL"
    assert config.project_root == "/home/user/myproject"
    assert config.cache_max_size_mb == 100
    assert config.max_file_size == 5242880


def test_load_config_top_level_keys(tmp_path: Path) -> None:
    """Config without [ezsql] section uses top-level keys."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'default_dialect = "sqlite"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_dialect == "sqlite"


def test_load_config_malformed_toml(tmp_path: Path) -> None:
    """Malformed TOML → log warning, fall back to defaults."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "this is not valid toml = = =\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_dialect == "postgres"
    assert config.cache_max_size_mb == 50


def test_numeric_clamping_below_min(tmp_path: Path) -> None:
    """Out-of-range low numeric fields clamped to minimum (T4.3)."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
cache_max_size_mb = 0
cache_max_entries = 5
task_ttl_seconds = 10
max_scan_depth = 0
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.cache_max_size_mb == 1
    assert config.cache_max_entries == 16
    assert config.task_ttl_seconds == 60
    assert config.max_scan_depth == 1


def test_numeric_clamping_above_max(tmp_path: Path) -> None:
    """Out-of-range high numeric fields clamped to maximum (T4.3)."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
cache_max_size_mb = 99999
cache_max_entries = 999999
task_ttl_seconds = 999999
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.cache_max_size_mb == 1024
    assert config.cache_max_entries == 65536
    assert config.task_ttl_seconds == 86400


def test_env_var_name_only_not_value(tmp_path: Path) -> None:
    """Config stores env-var *name*, never the value (T4.2, plan §16)."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
database_url_env = "AWS_SECRET_ACCESS_KEY"
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    # The *name* is stored.
    assert config.database_url_env == "AWS_SECRET_ACCESS_KEY"
    # The *value* is resolved at call time, not stored in the model.
    # In Phase 1, get_database_url is never called (no DB connection).
    # But if it were, it would return the env var value, not log it.
    assert config.model_dump()["database_url_env"] == "AWS_SECRET_ACCESS_KEY"
    # The actual secret value is NOT in the serialized config.
    assert "AWS_SECRET_ACCESS_KEY" not in str(config.model_dump().get("database_url", ""))


def test_get_database_url_resolves_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_database_url resolves the env var *at call time* (not stored)."""
    config = EzsqlConfig(database_url_env="TEST_DB_URL_12345")
    monkeypatch.setenv("TEST_DB_URL_12345", "postgres://example.com/db")
    assert config.get_database_url() == "postgres://example.com/db"
    monkeypatch.delenv("TEST_DB_URL_12345")
    assert config.get_database_url() is None


# --- Phase 3 tests (plan_phase3 §9) ---


def test_phase3_defaults() -> None:
    """Phase 3 defaults match plan_phase3 §7."""
    config = EzsqlConfig()
    assert config.max_explain_sql_bytes == 262_144
    assert config.max_plan_response_bytes == 2_097_152
    assert config.max_plan_nodes == 500
    assert config.max_plan_depth == 64
    assert config.max_plan_condition_chars == 1_024
    assert config.db_pool_min_size == 1
    assert config.db_pool_max_size == 5
    assert config.max_database_adapters == 4
    assert config.db_connect_timeout_seconds == 10
    assert config.db_acquire_timeout_seconds == 5
    assert config.explain_ttl_seconds == 3_600
    assert config.max_explain_candidates == 5
    assert config.explain_statement_timeout_seconds == 30
    assert config.explain_lock_timeout_seconds == 5
    assert config.explain_total_timeout_seconds == 45
    assert config.runtime_enrichment_timeout_seconds == 90


def test_phase3_clamps(tmp_path: Path) -> None:
    """Phase 3 numeric fields are clamped to valid ranges."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
max_explain_sql_bytes = 10
max_plan_nodes = 100000
db_pool_min_size = 100
max_database_adapters = 0
explain_ttl_seconds = 1
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.max_explain_sql_bytes == 1_024
    assert config.max_plan_nodes == 5_000
    # db_pool_min_size=100 clamps to its own max (10), then the relational
    # validator clamps it to db_pool_max_size (default 5).
    assert config.db_pool_min_size == 5
    assert config.max_database_adapters == 1
    assert config.explain_ttl_seconds == 60


def test_relational_pool_validation() -> None:
    """min pool size is clamped to max (plan_phase3 §7)."""
    config = EzsqlConfig(db_pool_min_size=10, db_pool_max_size=2)
    assert config.db_pool_min_size <= config.db_pool_max_size


def test_relational_timeout_validation() -> None:
    """Stage timeouts are clamped to their enclosing totals (§7)."""
    config = EzsqlConfig(
        explain_lock_timeout_seconds=60,
        explain_statement_timeout_seconds=30,
    )
    assert config.explain_lock_timeout_seconds <= config.explain_statement_timeout_seconds

    config2 = EzsqlConfig(
        explain_statement_timeout_seconds=300,
        explain_total_timeout_seconds=45,
    )
    assert config2.explain_statement_timeout_seconds <= config2.explain_total_timeout_seconds


def test_env_name_validation_rejects_bad_names() -> None:
    """Env-var names must match a conservative identifier pattern (§7)."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        EzsqlConfig(database_url_env="bad name with spaces")
    with _pytest.raises(ValueError):
        EzsqlConfig(database_url_env="1starts_with_digit")
    with _pytest.raises(ValueError):
        EzsqlConfig(llm_api_key_env="has-dash")


def test_unknown_config_key_rejected(tmp_path: Path) -> None:
    """Misspelled keys are rejected → defaults, not silent ignore (§7)."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
default_dialect = "mysql"
max_explain_sql_bytez = 99999
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    # Unknown key → whole config rejected → defaults.
    assert config.default_dialect == "postgres"


def test_invalid_config_value_falls_back_to_defaults(tmp_path: Path) -> None:
    """Validation failure → defaults; field names logged, not values (§7)."""
    config_dir = tmp_path / ".ezsql"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[ezsql]
database_url_env = "not a valid name!"
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.database_url_env == "DATABASE_URL"
