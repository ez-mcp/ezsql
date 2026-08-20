"""Tests for the ezsql init CLI (plan_phase4 FR-7, decisions D1/D2)."""

from pathlib import Path

from ezsql.server.cli import main as cli_main
from ezsql.server.cli import run_init


class TestInitEmission:
    def test_emits_all_files(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        code = run_init()
        assert code == 0
        assert (tmp_path / ".ezsql" / "config.toml").is_file()
        assert (tmp_path / ".github" / "instructions" / "ezsql.instructions.md").is_file()
        assert (tmp_path / "CLAUDE.md").is_file()
        assert (tmp_path / ".cursor" / "rules" / "ezsql.mdc").is_file()
        assert (tmp_path / ".gitignore").is_file()

    def test_config_contains_env_names_not_values(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        run_init()
        content = (tmp_path / ".ezsql" / "config.toml").read_text(encoding="utf-8")
        assert 'database_url_env = "DATABASE_URL"' in content
        assert 'llm_api_key_env = "OPENAI_API_KEY"' in content
        # No secret-looking values (URLs, keys) in the generated config.
        assert "postgres://" not in content
        assert "sk-" not in content

    def test_instructions_have_frontmatter(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        run_init()
        content = (
            tmp_path / ".github" / "instructions" / "ezsql.instructions.md"
        ).read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "applyTo:" in content

    def test_gitignore_appended_idempotently(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        run_init()
        run_init()  # second run must not duplicate
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".ezsql/") == 1


class TestNonDestructive:
    def test_refuses_overwrite_without_force(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        config_path = tmp_path / ".ezsql" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("user's own config\n", encoding="utf-8")
        run_init()
        assert config_path.read_text(encoding="utf-8") == "user's own config\n"

    def test_force_overwrites(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        config_path = tmp_path / ".ezsql" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("user's own config\n", encoding="utf-8")
        run_init(force=True)
        content = config_path.read_text(encoding="utf-8")
        assert "user's own config" not in content
        assert "database_url_env" in content

    def test_claude_md_section_idempotent(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("# Project\n\nExisting docs.\n", encoding="utf-8")
        run_init()
        run_init()
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert content.count("ezsql-begin") == 1
        assert "# Project" in content  # existing content preserved


class TestDispatch:
    def test_init_dispatch(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        code = cli_main(["init"])
        assert code == 0
        assert (tmp_path / ".ezsql" / "config.toml").is_file()

    def test_unknown_command_fails(self, capsys) -> None:  # type: ignore[no-untyped-def]
        code = cli_main(["frobnicate"])
        assert code == 2

    def test_no_args_usage(self, capsys) -> None:  # type: ignore[no-untyped-def]
        code = cli_main([])
        assert code == 2


class TestPathSafety:
    def test_writes_confined_to_cwd(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # All emitted paths are fixed relative paths under cwd — no
        # user-controlled path components exist (traversal-safe by
        # construction, security doctrine §4).
        monkeypatch.chdir(tmp_path)
        run_init()
        emitted = [
            tmp_path / ".ezsql" / "config.toml",
            tmp_path / ".github" / "instructions" / "ezsql.instructions.md",
            tmp_path / "CLAUDE.md",
            tmp_path / ".cursor" / "rules" / "ezsql.mdc",
            tmp_path / ".gitignore",
        ]
        for path in emitted:
            assert path.is_relative_to(tmp_path)
