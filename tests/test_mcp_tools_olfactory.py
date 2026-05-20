"""Tests for mcp_tools_olfactory — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_082 = REPO_ROOT / "db" / "migrations" / "082_olfactory.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_082.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_olfactory as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_with_defaults(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        state = conn.execute(
            "SELECT total_imprints, enforcement_mode FROM olfactory_state"
        ).fetchone()
        assert state == (0, "shadow")
    finally:
        conn.close()


def test_imprint_creates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_olfactory_imprint(
        content="madeleine in tea", valence=0.8, arousal=0.7,
        content_kind="phrase", bound_memory_id=42,
    )
    assert out["ok"] is True
    assert out["preexisting"] is False


def test_imprint_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    first = mod.tool_olfactory_imprint(content="x", valence=0.5)
    second = mod.tool_olfactory_imprint(content="x", valence=-0.5)
    assert first["ok"] and second["ok"]
    assert first["imprint_id"] == second["imprint_id"]
    assert second["preexisting"] is True
    # Recall should show updated valence
    recall = mod.tool_olfactory_recall(content="x")
    assert recall["imprint"]["valence"] == -0.5


def test_imprint_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_olfactory_imprint(content="x", valence=2.0)
    assert "error" in mod.tool_olfactory_imprint(content="x", valence=0.5, arousal=1.5)
    assert "error" in mod.tool_olfactory_imprint(content="", valence=0.5)


def test_recall_increments_count(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_olfactory_imprint(content="lemon", valence=0.3)
    out1 = mod.tool_olfactory_recall(content="lemon")
    assert out1["matched"] is True
    assert out1["imprint"]["times_recalled"] == 1
    out2 = mod.tool_olfactory_recall(content="lemon")
    assert out2["imprint"]["times_recalled"] == 2


def test_recall_unmatched(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_olfactory_recall(content="never-seen")
    assert out["ok"] is True
    assert out["matched"] is False
    assert out["imprint"] is None


def test_status_valence_distribution(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_olfactory_imprint(content="a", valence=0.8)
    mod.tool_olfactory_imprint(content="b", valence=-0.7)
    mod.tool_olfactory_imprint(content="c", valence=0.0)
    out = mod.tool_olfactory_status()
    dist = {row["bucket"]: row["n"] for row in out["valence_distribution"]}
    assert dist.get("positive") == 1
    assert dist.get("negative") == 1
    assert dist.get("neutral") == 1


def test_set_enforcement(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_olfactory_set(enforcement_mode="enforce")
    assert out["ok"] is True
    assert out["state"]["enforcement_mode"] == "enforce"
    assert "error" in mod.tool_olfactory_set(enforcement_mode="bogus")
