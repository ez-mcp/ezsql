"""Unit tests for root resolution (plan §6.3 — Option B)."""

from pathlib import Path

from ezsql.config import EzsqlConfig
from ezsql.server.models import FailureEnvelope
from ezsql.server.roots import resolve_root


def test_root_param_primary(tmp_path: Path) -> None:
    """root_param is the primary resolution mechanism."""
    config = EzsqlConfig(project_root="/some/other/path")
    result = resolve_root(str(tmp_path), config)
    assert isinstance(result, Path)
    assert result == tmp_path.resolve()


def test_config_fallback(tmp_path: Path) -> None:
    """When root_param is None, config.project_root is used."""
    config = EzsqlConfig(project_root=str(tmp_path))
    result = resolve_root(None, config)
    assert isinstance(result, Path)
    assert result == tmp_path.resolve()


def test_both_missing() -> None:
    """Neither root_param nor config → FailureEnvelope(kind='missing_root')."""
    config = EzsqlConfig()
    result = resolve_root(None, config)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "missing_root"
    assert result.recoverable is True
    assert len(result.next_steps) > 0


def test_empty_root_param_falls_to_config(tmp_path: Path) -> None:
    """Empty string root_param falls through to config."""
    config = EzsqlConfig(project_root=str(tmp_path))
    result = resolve_root("", config)
    assert isinstance(result, Path)
    assert result == tmp_path.resolve()


def test_whitespace_root_param_falls_to_config(tmp_path: Path) -> None:
    """Whitespace-only root_param falls through to config."""
    config = EzsqlConfig(project_root=str(tmp_path))
    result = resolve_root("   ", config)
    assert isinstance(result, Path)


def test_nonexistent_path() -> None:
    """Nonexistent path → FailureEnvelope(kind='invalid_root')."""
    config = EzsqlConfig()
    result = resolve_root("/nonexistent/path/that/does/not/exist", config)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "invalid_root"
    assert result.recoverable is True


def test_file_not_dir(tmp_path: Path) -> None:
    """Path to a file (not dir) → FailureEnvelope(kind='invalid_root')."""
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("hello", encoding="utf-8")
    config = EzsqlConfig()
    result = resolve_root(str(file_path), config)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "invalid_root"


def test_relative_path_resolved(tmp_path: Path) -> None:
    """Relative path is resolved to absolute."""
    # Change to tmp_path and use a relative subdir
    subdir = tmp_path / "project"
    subdir.mkdir()
    config = EzsqlConfig()
    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = resolve_root("project", config)
        assert isinstance(result, Path)
        assert result.is_absolute()
        assert result == subdir.resolve()
    finally:
        os.chdir(old_cwd)


def test_never_uses_cwd() -> None:
    """resolve_root never falls back to Path.cwd() — it fails instead."""
    config = EzsqlConfig()
    result = resolve_root(None, config)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "missing_root"
    # Verify it's not silently returning cwd
    assert result.kind != "ok"


def test_symlink_to_dir_resolved(tmp_path: Path) -> None:
    """Symlink to a directory is resolved to the target."""
    real_dir = tmp_path / "real_project"
    real_dir.mkdir()
    link = tmp_path / "link_to_project"
    link.symlink_to(real_dir)
    config = EzsqlConfig()
    result = resolve_root(str(link), config)
    assert isinstance(result, Path)
    assert result == real_dir.resolve()


def test_symlink_to_file_rejected(tmp_path: Path) -> None:
    """Symlink to a file (not dir) → FailureEnvelope (T1.2)."""
    real_file = tmp_path / "real_file.txt"
    real_file.write_text("hello", encoding="utf-8")
    link = tmp_path / "link_to_file"
    link.symlink_to(real_file)
    config = EzsqlConfig()
    result = resolve_root(str(link), config)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "invalid_root"
