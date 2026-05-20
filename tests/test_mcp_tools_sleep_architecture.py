"""Tests for mcp_tools_sleep_architecture — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_074 = REPO_ROOT / "db" / "migrations" / "074_sleep_architecture.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_074.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_sleep_architecture as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_5_stages(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        stages = [r[0] for r in conn.execute(
            "SELECT stage FROM sleep_stage_catalog ORDER BY id"
        ).fetchall()]
        assert stages == ["awake", "nrem1", "nrem2", "nrem3_sws", "rem"]
        state = conn.execute(
            "SELECT current_stage, cycle_number FROM sleep_cycle_state"
        ).fetchone()
        assert state == ("awake", 0)
    finally:
        conn.close()


def test_status_returns_state_and_catalog(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_sleep_status()
    assert out["ok"] is True
    assert out["state"]["current_stage"] == "awake"
    assert len(out["stage_catalog"]) == 5
    assert out["current_stage_meta"]["stage"] == "awake"


def test_transition_moves_stage_and_records(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_sleep_transition(to_stage="nrem1", reason="user opened the eyelids")
    assert out["ok"] is True
    assert out["from_stage"] == "awake"
    assert out["to_stage"] == "nrem1"
    assert out["cycle_number"] >= 1   # entering sleep starts cycle 1
    status = mod.tool_sleep_status()
    assert status["state"]["current_stage"] == "nrem1"


def test_transition_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_sleep_transition(to_stage="not-a-stage")
    assert "error" in out


def test_transition_noop_on_same_stage(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_sleep_transition(to_stage="awake")
    assert out.get("no_op") is True


def test_advance_walks_the_canonical_cycle(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    expected = ["nrem1", "nrem2", "nrem3_sws", "rem", "nrem2"]
    for i, expected_stage in enumerate(expected):
        out = mod.tool_sleep_advance(reason=f"step-{i}")
        assert out["ok"] is True
        assert out["to_stage"] == expected_stage


def test_advance_increments_cycle_at_rem_to_nrem2(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Walk through: awake → nrem1 (cycle 1) → nrem2 → nrem3_sws → rem → nrem2 (cycle 2)
    mod.tool_sleep_advance()  # nrem1
    cycle_after_nrem1 = mod.tool_sleep_status()["state"]["cycle_number"]
    mod.tool_sleep_advance()  # nrem2
    mod.tool_sleep_advance()  # nrem3_sws
    mod.tool_sleep_advance()  # rem
    mod.tool_sleep_advance()  # nrem2 again (cycle++)
    cycle_after_rem_to_nrem2 = mod.tool_sleep_status()["state"]["cycle_number"]
    assert cycle_after_rem_to_nrem2 == cycle_after_nrem1 + 1


def test_operation_permitted_when_in_stage(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # awake permits 'all'
    out = mod.tool_sleep_operation_permitted(operation="memory_search")
    assert out["permitted"] is True
    # transition to nrem3_sws which permits swr_replay but not bisociation
    mod.tool_sleep_transition(to_stage="nrem3_sws")
    sws_replay = mod.tool_sleep_operation_permitted(operation="swr_replay")
    assert sws_replay["permitted"] is True
    sws_bisoc = mod.tool_sleep_operation_permitted(operation="bisociation")
    assert sws_bisoc["permitted"] is False


def test_operation_permitted_in_rem(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_sleep_transition(to_stage="rem")
    rem_bisoc = mod.tool_sleep_operation_permitted(operation="bisociation")
    assert rem_bisoc["permitted"] is True
    rem_replay = mod.tool_sleep_operation_permitted(operation="swr_replay")
    assert rem_replay["permitted"] is False  # SWS-specific op


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_sleep_transition(to_stage="nrem1")
    mod.tool_sleep_transition(to_stage="nrem2")
    mod.tool_sleep_transition(to_stage="rem")
    all_h = mod.tool_sleep_history(limit=10)
    assert len(all_h["transitions"]) == 3
    rem_only = mod.tool_sleep_history(limit=10, to_stage="rem")
    assert len(rem_only["transitions"]) == 1
