"""Tests for mcp_tools_mammillary — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_081 = REPO_ROOT / "db" / "migrations" / "081_mammillary.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_081.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_mammillary as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_with_defaults(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        state = conn.execute(
            "SELECT total_transits, transits_24h, enabled FROM mammillary_state"
        ).fetchone()
        assert state == (0, 0, 1)
    finally:
        conn.close()


def test_log_transit(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_mammillary_log_transit(
        memory_id=42, direction="full_loop", agent_id="a1",
    )
    assert out["ok"] is True
    assert out["direction"] == "full_loop"
    status = mod.tool_mammillary_status()
    assert status["state"]["total_transits"] == 1


def test_log_transit_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_mammillary_log_transit(memory_id=1, direction="bogus")
    assert "error" in mod.tool_mammillary_log_transit(memory_id=1, direction="full_loop", transit_strength=1.5)


def test_memory_history_consolidated_flag(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Memory 99 has only a partial leg (hippocampus_to_atn)
    mod.tool_mammillary_log_transit(memory_id=99, direction="hippocampus_to_atn")
    hist = mod.tool_mammillary_memory_history(memory_id=99)
    assert hist["full_loop_count"] == 0
    assert hist["consolidated"] is False
    # Add a full_loop event
    mod.tool_mammillary_log_transit(memory_id=99, direction="full_loop")
    hist = mod.tool_mammillary_memory_history(memory_id=99)
    assert hist["full_loop_count"] == 1
    assert hist["consolidated"] is True


def test_status_aggregates_24h(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    for mid in [1, 2, 1, 3]:
        mod.tool_mammillary_log_transit(memory_id=mid, direction="full_loop")
    status = mod.tool_mammillary_status()
    assert status["aggregate_24h"]["n"] == 4
    assert status["aggregate_24h"]["unique_memories"] == 3
    assert status["aggregate_24h"]["n_full"] == 4


def test_reset_24h(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_mammillary_log_transit(memory_id=1, direction="full_loop")
    mod.tool_mammillary_log_transit(memory_id=2, direction="full_loop")
    out = mod.tool_mammillary_reset_24h()
    assert out["ok"] is True
    assert out["prior_24h_count"] == 2
    status = mod.tool_mammillary_status()
    assert status["state"]["transits_24h"] == 0
    # total_transits NOT reset
    assert status["state"]["total_transits"] == 2
