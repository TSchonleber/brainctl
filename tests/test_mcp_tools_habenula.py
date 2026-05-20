"""Tests for mcp_tools_habenula — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_070 = REPO_ROOT / "db" / "migrations" / "070_habenula.sql"


def _bootstrap(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT
        );
        """
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_070.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_habenula as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_applies_with_seeds(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM habenula_triggers ORDER BY id").fetchall()]
        assert names == [
            "reward_omission", "retrieval_failure", "repeated_low_utility",
            "aversive_valence", "task_abandoned",
        ]
        state = conn.execute("SELECT tonic_activity, suggested_da_damp FROM habenula_state").fetchone()
        assert state == (0.0, 0.0)
        sv = conn.execute("SELECT version FROM schema_version WHERE version=70").fetchone()
        assert sv == (70,)
    finally:
        conn.close()


def test_status_empty(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_habenula_status()
    assert out["ok"] is True
    assert out["aggregate_24h"]["n"] == 0
    assert out["registered_triggers"] == 5


def test_fire_via_trigger_uses_defaults(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_habenula_fire(trigger_name="reward_omission", agent_id="a1")
    assert out["ok"] is True
    assert out["signed_pe"] == -0.15
    assert out["event_kind"] == "omission"
    # state advanced
    status = mod.tool_habenula_status()
    assert status["state"]["rolling_disappointment_24h"] == 1
    assert status["state"]["last_firing_at"] is not None


def test_fire_explicit_pe(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_habenula_fire(
        signed_pe=-0.5, event_kind="aversive", agent_id="a1", notes="explicit"
    )
    assert out["ok"] is True
    assert out["signed_pe"] == -0.5


def test_fire_rejects_positive_pe(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_habenula_fire(signed_pe=0.5, event_kind="aversive")
    assert "error" in out


def test_fire_rejects_missing_args(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_habenula_fire()
    assert "error" in out


def test_fire_unknown_trigger(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_habenula_fire(trigger_name="nope")
    assert "error" in out


def test_register_trigger_idempotent_and_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    good = mod.tool_habenula_register_trigger(
        name="custom_aversive", event_kind="aversive", default_pe=-0.5,
    )
    again = mod.tool_habenula_register_trigger(
        name="custom_aversive", event_kind="aversive", default_pe=-0.4,
    )
    assert good["ok"] and again["ok"]
    assert good["trigger"]["id"] == again["trigger"]["id"]
    assert again["trigger"]["default_pe"] == -0.4
    # Validation
    bad = mod.tool_habenula_register_trigger(
        name="bad", event_kind="not-a-kind",
    )
    assert "error" in bad


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_habenula_fire(trigger_name="reward_omission", agent_id="a1")
    mod.tool_habenula_fire(trigger_name="aversive_valence", agent_id="a2")
    mod.tool_habenula_fire(trigger_name="reward_omission", agent_id="a1")
    all_h = mod.tool_habenula_history(limit=10)
    assert len(all_h["history"]) == 3
    a1 = mod.tool_habenula_history(limit=10, agent_id="a1")
    assert len(a1["history"]) == 2
    omissions = mod.tool_habenula_history(limit=10, event_kind="omission")
    assert len(omissions["history"]) == 2


def test_reset_clears_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_habenula_fire(trigger_name="aversive_valence", agent_id="a1")
    pre = mod.tool_habenula_status()
    assert pre["state"]["rolling_disappointment_24h"] == 1
    out = mod.tool_habenula_reset(agent_id="a1")
    assert out["ok"] is True
    assert out["prior_state"]["rolling_disappointment_24h"] == 1
    post = mod.tool_habenula_status()
    assert post["state"]["rolling_disappointment_24h"] == 0
    assert post["state"]["tonic_activity"] == 0.0
