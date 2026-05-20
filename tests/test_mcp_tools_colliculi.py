"""Tests for mcp_tools_colliculi — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_080 = REPO_ROOT / "db" / "migrations" / "080_colliculi.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_080.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_colliculi as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_patterns(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM colliculi_trigger_patterns").fetchone()[0]
        assert n == 5
        state = conn.execute("SELECT sc_tonic, ic_tonic FROM colliculi_state").fetchone()
        assert state == (0.3, 0.3)
    finally:
        conn.close()


def test_orient_via_pattern(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_colliculi_orient(
        sub_nucleus="sc", pattern_name="new_entity_seen", agent_id="a1",
        target_description="entity 'Olive'",
    )
    assert out["ok"] is True
    # default_strength for new_entity_seen = 0.5
    assert out["strength"] == 0.5


def test_orient_explicit_strength(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_colliculi_orient(
        sub_nucleus="ic", strength=0.7, target_description="loud crash",
    )
    assert out["ok"] is True
    assert out["strength"] == 0.7
    assert out["pattern_id"] is None


def test_orient_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_colliculi_orient(sub_nucleus="bogus")
    assert "error" in mod.tool_colliculi_orient(sub_nucleus="sc")
    assert "error" in mod.tool_colliculi_orient(sub_nucleus="sc", strength=1.5)
    # Cross-nucleus mismatch
    assert "error" in mod.tool_colliculi_orient(sub_nucleus="ic", pattern_name="new_entity_seen")


def test_register_pattern_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    first = mod.tool_colliculi_register_pattern(
        name="custom_audio", sub_nucleus="ic", pattern_kind="sudden_volume_change",
        default_strength=0.4,
    )
    second = mod.tool_colliculi_register_pattern(
        name="custom_audio", sub_nucleus="ic", pattern_kind="sudden_volume_change",
        default_strength=0.6,
    )
    assert first["ok"] and second["ok"]
    assert second["pattern"]["default_strength"] == 0.6


def test_register_pattern_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_colliculi_register_pattern(name="x", sub_nucleus="bogus", pattern_kind="other")
    assert "error" in mod.tool_colliculi_register_pattern(name="x", sub_nucleus="sc", pattern_kind="bogus")


def test_status_aggregates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_colliculi_orient(sub_nucleus="sc", strength=0.5)
    mod.tool_colliculi_orient(sub_nucleus="ic", strength=0.4, aras_drive_fired=True)
    out = mod.tool_colliculi_status()
    assert out["aggregate_1h"]["n"] == 2
    assert out["aggregate_1h"]["n_sc"] == 1
    assert out["aggregate_1h"]["n_ic"] == 1
    assert out["aggregate_1h"]["n_aras_drive_fired"] == 1


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_colliculi_orient(sub_nucleus="sc", strength=0.3)
    mod.tool_colliculi_orient(sub_nucleus="sc", strength=0.7)
    mod.tool_colliculi_orient(sub_nucleus="ic", strength=0.5)
    all_h = mod.tool_colliculi_history(limit=10)
    assert len(all_h["history"]) == 3
    sc_only = mod.tool_colliculi_history(limit=10, sub_nucleus="sc")
    assert len(sc_only["history"]) == 2
    strong = mod.tool_colliculi_history(limit=10, min_strength=0.6)
    assert len(strong["history"]) == 1
