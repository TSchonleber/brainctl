"""Tests for locus coeruleus Phase 1 (schema + read/CRUD tools)."""
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_locus_coeruleus import (
    tool_lc_fire,
    tool_lc_register_trigger,
    tool_lc_set_mode,
    tool_lc_signal_history,
    tool_lc_status,
)


class _NoCloseConn:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _apply_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration = Path(__file__).resolve().parent.parent / "db" / "migrations" / "067_locus_coeruleus.sql"
    with open(migration) as f:
        conn.executescript(f.read())


def _patched(conn: sqlite3.Connection):
    import agentmemory.mcp_tools_locus_coeruleus as m

    original_open_db = m.open_db
    m.open_db = lambda x: _NoCloseConn(conn)  # type: ignore[assignment]
    return m, original_open_db


def test_migration_applies_and_seeds():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM lc_triggers").fetchall()}
    state = conn.execute("SELECT id, mode, ne_reservoir FROM lc_state WHERE id=1").fetchone()
    assert names == {"cerebellum_high_pe", "bg_large_td_error", "novel_entity_sighting", "explicit_user_alert"}
    assert tuple(state) == (1, "tonic_mid", 0.5)


def test_lc_status_empty_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    mod, original_open_db = _patched(conn)
    try:
        result = tool_lc_status()
        assert result["ok"] is True
        assert result["state"]["mode"] == "tonic_mid"
        assert result["recent_24h"]["count"] == 0
    finally:
        mod.open_db = original_open_db


def test_lc_register_trigger_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    mod, original_open_db = _patched(conn)
    try:
        first = tool_lc_register_trigger("test_trigger", "other", None, None, 0.07, "first")
        second = tool_lc_register_trigger("test_trigger", "other", None, None, 0.08, "second")
        count = conn.execute("SELECT COUNT(*) FROM lc_triggers WHERE name='test_trigger'").fetchone()[0]
        assert first["ok"] is True and second["ok"] is True
        assert count == 1
        assert second["trigger"]["default_ne_delta"] == 0.08
    finally:
        mod.open_db = original_open_db


def test_lc_fire_round_trip():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    mod, original_open_db = _patched(conn)
    try:
        result = tool_lc_fire("cerebellum_high_pe", 0.73, agent_id="agent-a", source_event_id=42)
        row = conn.execute(
            "SELECT surprise_magnitude, ne_delta_applied, mode FROM lc_firings WHERE id=?",
            (result["firing_id"],),
        ).fetchone()
        assert result["ok"] is True
        assert result["ne_delta_applied"] == 0.15
        assert tuple(row) == (0.73, 0.15, "phasic")
    finally:
        mod.open_db = original_open_db


def test_lc_set_mode_validates():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    mod, original_open_db = _patched(conn)
    try:
        bad = tool_lc_set_mode("invalid")
        good = tool_lc_set_mode("tonic_high", reason="test")
        mode = conn.execute("SELECT mode FROM lc_state WHERE id=1").fetchone()[0]
        assert bad["ok"] is False
        assert good["ok"] is True
        assert mode == "tonic_high"
    finally:
        mod.open_db = original_open_db


def test_lc_signal_history_filters_and_pagination():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    mod, original_open_db = _patched(conn)
    try:
        tool_lc_fire("cerebellum_high_pe", 0.73, agent_id="agent-a")
        tool_lc_fire("bg_large_td_error", 0.9, agent_id="agent-b")
        history = tool_lc_signal_history(limit=1, agent_id="agent-a")
        assert len(history) == 1
        assert history[0]["agent_id"] == "agent-a"
    finally:
        mod.open_db = original_open_db
