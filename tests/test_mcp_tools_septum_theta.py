"""Tests for mcp_tools_septum_theta — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_076 = REPO_ROOT / "db" / "migrations" / "076_septum_theta.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_076.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_septum_theta as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_state(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        state = conn.execute(
            "SELECT theta_frequency_hz, theta_bin, cycle_count, enabled FROM septum_state"
        ).fetchone()
        assert state == (6.0, 0, 0, 0)
    finally:
        conn.close()


def test_tick_advances_phase_and_bin(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_septum_tick()
    assert out["ok"] is True
    assert out["theta_bin"] == 1
    assert out["cycle_count"] == 0


def test_eight_ticks_complete_one_cycle(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    for _ in range(7):
        mod.tool_septum_tick()
    # 8th tick should wrap to bin 0 and increment cycle
    final = mod.tool_septum_tick()
    assert final["theta_bin"] == 0
    assert final["cycle_count"] == 1


def test_phase_lock_records_current_bin(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_septum_tick()  # bin 1
    mod.tool_septum_tick()  # bin 2
    out = mod.tool_septum_phase_lock(memory_id=42, operation="write")
    assert out["ok"] is True
    assert out["theta_bin"] == 2


def test_phase_lock_validates_operation(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_septum_phase_lock(memory_id=1, operation="bogus")
    assert "error" in out


def test_query_bin_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Lock memory 100 at bin 0 (initial)
    mod.tool_septum_phase_lock(memory_id=100, operation="write")
    # Tick to bin 3, lock memory 200
    for _ in range(3):
        mod.tool_septum_tick()
    mod.tool_septum_phase_lock(memory_id=200, operation="write")
    bin_0 = mod.tool_septum_query_bin(theta_bin=0)
    assert len(bin_0["memories"]) == 1
    assert bin_0["memories"][0]["memory_id"] == 100
    bin_3 = mod.tool_septum_query_bin(theta_bin=3)
    assert len(bin_3["memories"]) == 1
    assert bin_3["memories"][0]["memory_id"] == 200


def test_query_bin_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_septum_query_bin(theta_bin=99)


def test_set_frequency_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_septum_set_frequency(theta_frequency_hz=2.0)
    assert "error" in mod.tool_septum_set_frequency(theta_frequency_hz=15.0)
    out = mod.tool_septum_set_frequency(theta_frequency_hz=5.5)
    assert out["ok"] is True
    assert out["state"]["theta_frequency_hz"] == 5.5


def test_status_returns_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_septum_status()
    assert out["ok"] is True
    assert out["state"]["theta_bin"] == 0
    assert out["last_5_ticks"] == []
