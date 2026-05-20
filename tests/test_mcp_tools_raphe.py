"""Tests for mcp_tools_raphe — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_077 = REPO_ROOT / "db" / "migrations" / "077_raphe.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_077.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_raphe as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_subtypes(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in conn.execute("SELECT subtype FROM raphe_subtype_catalog").fetchall()]
        assert sorted(names) == ["drn", "mrn"]
        state = conn.execute(
            "SELECT tonic_5ht, time_horizon_seconds, mood_baseline FROM raphe_state"
        ).fetchone()
        assert state == (0.5, 300, 0.0)
    finally:
        conn.close()


def test_status_returns_state_and_catalog(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_raphe_status()
    assert out["ok"] is True
    assert len(out["subtype_catalog"]) == 2
    assert out["aggregate_24h"]["n"] == 0


def test_fire_drn(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_raphe_fire(subtype="drn", magnitude=0.7, trigger_kind="patience_required")
    assert out["ok"] is True
    assert out["subtype"] == "drn"
    status = mod.tool_raphe_status()
    assert status["state"]["total_firings"] == 1


def test_fire_mrn(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_raphe_fire(subtype="mrn", magnitude=0.4, trigger_kind="mood_stabilization")
    assert out["ok"] is True
    assert out["subtype"] == "mrn"


def test_fire_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_raphe_fire(subtype="nope", magnitude=0.5)
    assert "error" in mod.tool_raphe_fire(subtype="drn", magnitude=1.5)
    assert "error" in mod.tool_raphe_fire(subtype="drn", magnitude=0.5, trigger_kind="bogus")


def test_set_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_raphe_set_state(tonic_5ht=0.75, time_horizon_seconds=600, mood_baseline=0.3)
    assert out["ok"] is True
    assert out["state"]["tonic_5ht"] == 0.75
    assert out["state"]["time_horizon_seconds"] == 600
    assert out["state"]["mood_baseline"] == 0.3


def test_set_state_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_raphe_set_state(tonic_5ht=1.5)
    assert "error" in mod.tool_raphe_set_state(time_horizon_seconds=0)
    assert "error" in mod.tool_raphe_set_state(mood_baseline=-2.0)
    assert "error" in mod.tool_raphe_set_state()


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_raphe_fire(subtype="drn", magnitude=0.5, trigger_kind="patience_required")
    mod.tool_raphe_fire(subtype="mrn", magnitude=0.3, trigger_kind="mood_stabilization")
    mod.tool_raphe_fire(subtype="drn", magnitude=0.4, trigger_kind="long_horizon_plan")
    all_h = mod.tool_raphe_history(limit=10)
    assert len(all_h["history"]) == 3
    drn = mod.tool_raphe_history(limit=10, subtype="drn")
    assert len(drn["history"]) == 2
    patience = mod.tool_raphe_history(limit=10, trigger_kind="patience_required")
    assert len(patience["history"]) == 1
