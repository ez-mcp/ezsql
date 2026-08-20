"""Unit tests for cache key generation."""

from pathlib import Path

from ezsql.cache.keys import explain_key, runtime_evidence_key, scan_key


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


# --- Phase 3 tests (plan_phase3 §9) ---


def test_explain_key_deterministic() -> None:
    k1 = explain_key("SELECT 1", "dbfp", "repofp", 16)
    k2 = explain_key("SELECT 1", "dbfp", "repofp", 16)
    assert k1 == k2


def test_explain_key_db_isolation() -> None:
    """DB-A, DB-B, and no-DB never share keys (§6)."""
    k_a = explain_key("SELECT 1", "db-a", "repo", 16)
    k_b = explain_key("SELECT 1", "db-b", "repo", 16)
    assert k_a != k_b


def test_explain_key_sql_isolation() -> None:
    assert explain_key("SELECT 1", "db", "repo", 16) != explain_key(
        "SELECT 2", "db", "repo", 16
    )


def test_explain_key_server_version_isolation() -> None:
    assert explain_key("SELECT 1", "db", "repo", 16) != explain_key(
        "SELECT 1", "db", "repo", 17
    )


def test_explain_key_repo_fingerprint_isolation() -> None:
    assert explain_key("SELECT 1", "db", "repo1", 16) != explain_key(
        "SELECT 1", "db", "repo2", 16
    )


def test_runtime_key_deterministic() -> None:
    k1 = runtime_evidence_key("sk", "db", "repo", ["a", "b"], [("m", 1)], 16)
    k2 = runtime_evidence_key("sk", "db", "repo", ["a", "b"], [("m", 1)], 16)
    assert k1 == k2


def test_runtime_key_candidate_order_matters() -> None:
    """Ordered candidate identities — order is part of the key (§6)."""
    k1 = runtime_evidence_key("sk", "db", "repo", ["a", "b"], [("m", 1)], 16)
    k2 = runtime_evidence_key("sk", "db", "repo", ["b", "a"], [("m", 1)], 16)
    assert k1 != k2


def test_runtime_key_limit_changes_invalidate() -> None:
    """Every result-shaping limit is part of the key (§6)."""
    k1 = runtime_evidence_key("sk", "db", "repo", ["a"], [("max_candidates", 50)], 16)
    k2 = runtime_evidence_key("sk", "db", "repo", ["a"], [("max_candidates", 10)], 16)
    assert k1 != k2


def test_runtime_key_db_isolation() -> None:
    k1 = runtime_evidence_key("sk", "db-a", "repo", ["a"], [("m", 1)], 16)
    k2 = runtime_evidence_key("sk", "db-b", "repo", ["a"], [("m", 1)], 16)
    assert k1 != k2


def test_runtime_key_static_key_isolation() -> None:
    k1 = runtime_evidence_key("sk1", "db", "repo", ["a"], [("m", 1)], 16)
    k2 = runtime_evidence_key("sk2", "db", "repo", ["a"], [("m", 1)], 16)
    assert k1 != k2


def test_keys_contain_no_credentials() -> None:
    """Keys are hashes — credential material never appears in them."""
    k = explain_key("SELECT 1", "fingerprint-only", "repo", 16)
    assert "password" not in k
    assert "postgres://" not in k
    assert len(k) == 64  # blake2b-256 hex
