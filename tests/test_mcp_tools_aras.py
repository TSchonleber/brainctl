"""Tests for mcp_tools_aras — ARAS Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_069 = REPO_ROOT / "db" / "migrations" / "069_aras.sql"


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT
        );
        """
    )


def _apply(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_069.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_aras as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_applies_with_seeds(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM aras_triggers ORDER BY id").fetchall()]
        assert names == ["novel_query", "high_pe_event", "consolidation_complete", "idle_30min", "explicit_user_alert"]
        state = conn.execute("SELECT sleep_wake_mode, arousal_level FROM aras_state").fetchone()
        assert state == ("awake_relaxed", 0.5)
        sv = conn.execute("SELECT version FROM schema_version WHERE version=69").fetchone()
        assert sv == (69,)
    finally:
        conn.close()


def test_status_empty(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_aras_status()
    assert out["ok"] is True
    assert out["state"]["sleep_wake_mode"] == "awake_relaxed"
    assert out["last_5_transitions"] == []
    assert out["registered_triggers"] == 5


def test_transition_writes_and_updates_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_aras_transition(to_mode="awake_focused", reason="test", agent_id="a1")
    assert out["ok"] is True
    assert out["from_mode"] == "awake_relaxed"
    assert out["to_mode"] == "awake_focused"
    # soft-pull from 0.5 toward 0.75 = 0.625
    assert abs(out["arousal_after"] - 0.625) < 0.01
    # state updated
    status = mod.tool_aras_status()
    assert status["state"]["sleep_wake_mode"] == "awake_focused"


def test_transition_rejects_invalid_mode(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_aras_transition(to_mode="invalid_mode")
    assert "error" in out


def test_drive_applies_delta(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_aras_drive(trigger_name="novel_query", magnitude=1.0)
    assert out["ok"] is True
    # novel_query default_arousal_delta = 0.05
    assert abs(out["arousal_delta_applied"] - 0.05) < 1e-6
    assert out["new_arousal_level"] > 0.5


def test_drive_auto_transition_on_large_delta(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # explicit_user_alert delta = 0.30 → above 0.2 threshold → auto-transition
    out = mod.tool_aras_drive(trigger_name="explicit_user_alert", magnitude=1.0, agent_id="a1")
    assert out["ok"] is True
    assert out["auto_transition_id"] is not None
    status = mod.tool_aras_status(agent_id="a1")
    assert status["state"]["sleep_wake_mode"] == "hyperalert"


def test_drive_rejects_unknown_trigger(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_aras_drive(trigger_name="nope", magnitude=0.5)
    assert "error" in out


def test_register_trigger_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    first = mod.tool_aras_register_trigger(
        name="custom_alert", trigger_kind="explicit_alert",
        default_arousal_delta=0.15, default_target_mode="awake_focused",
    )
    second = mod.tool_aras_register_trigger(
        name="custom_alert", trigger_kind="explicit_alert",
        default_arousal_delta=0.20, default_target_mode="hyperalert",
    )
    assert first["ok"] is True and second["ok"] is True
    assert first["trigger"]["id"] == second["trigger"]["id"]
    assert second["trigger"]["default_arousal_delta"] == 0.20


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_aras_transition(to_mode="awake_focused", agent_id="a1")
    mod.tool_aras_transition(to_mode="drowsy", agent_id="a2")
    mod.tool_aras_transition(to_mode="hyperalert", agent_id="a1")
    all_h = mod.tool_aras_history(limit=10)
    assert len(all_h["history"]) == 3
    a1 = mod.tool_aras_history(limit=10, agent_id="a1")
    assert len(a1["history"]) == 2
    hyper = mod.tool_aras_history(limit=10, to_mode="hyperalert")
    assert len(hyper["history"]) == 1
