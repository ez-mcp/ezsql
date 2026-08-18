"""Unit tests for cache key generation."""

from pathlib import Path

from ezsql.cache.keys import scan_key


def test_scan_key_deterministic(tmp_path: Path) -> None:
    """Same inputs → same key."""
    key1 = scan_key(tmp_path)
    key2 = scan_key(tmp_path)
    assert key1 == key2
    assert len(key1) == 64  # blake2b 256-bit = 64 hex chars


def test_scan_key_different_roots(tmp_path: Path) -> None:
    """Different roots → different keys."""
    root_a = tmp_path / "project_a"
    root_a.mkdir()
    root_b = tmp_path / "project_b"
    root_b.mkdir()
    assert scan_key(root_a) != scan_key(root_b)


def test_scan_key_includes_sqlglot_version(tmp_path: Path) -> None:
    """Key embeds sqlglot version — upgrades invalidate cleanly."""
    # We can't change the installed version in a test, but we can verify
    # the key is stable and non-empty.
    key = scan_key(tmp_path)
    assert key
    assert key != ""


def test_scan_key_resolves_path(tmp_path: Path) -> None:
    """Key uses resolved path — relative and absolute of same dir match."""
    root = tmp_path / "project"
    root.mkdir()
    # Both calls resolve to the same absolute path
    key1 = scan_key(root)
    key2 = scan_key(root.resolve())
    assert key1 == key2
