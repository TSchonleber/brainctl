"""Tests for basal ganglia Phase 1 (schema + 3 read/CRUD tools)."""
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_basal_ganglia import (
    tool_bg_status,
    tool_bg_action_register,
    tool_bg_modulator_set,
    VALID_LOOPS,
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
    with open(str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "054_basal_ganglia.sql")) as f:
        conn.executescript(f.read())


def _patched(conn):
    import agentmemory.mcp_tools_basal_ganglia as m
    m.open_db = lambda x: _NoCloseConn(conn)  # type: ignore[assignment]
    return m


def test_migration_creates_all_tables_and_seed_row():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'bg_%'"
    ).fetchall()}
    assert tables == {
        "bg_actions", "bg_striatal_weights", "bg_eligibility_traces",
        "bg_td_events", "bg_holds", "bg_modulators", "bg_chunks",
    }
    mod = conn.execute("SELECT id, tonic_da, lc_ne, serotonin FROM bg_modulators WHERE id=1").fetchone()
    assert tuple(mod) == (1, 0.5, 0.5, 0.5)


def test_bg_action_register_idempotent_and_validates_loop():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    _patched(conn)

    r1 = tool_bg_action_register(loop="motor", action_key="tool:test", description="first")
    assert r1["ok"] is True
    r2 = tool_bg_action_register(loop="motor", action_key="tool:test", description="updated")
    assert r2["ok"] is True
    count = conn.execute("SELECT COUNT(*) FROM bg_actions WHERE action_key='tool:test'").fetchone()[0]
    assert count == 1

    bad = tool_bg_action_register(loop="not_a_loop", action_key="x")
    assert bad["ok"] is False
    assert "must be one of" in bad["error"]


def test_bg_modulator_set_clamps_and_bounds_check():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    _patched(conn)

    # No-op (no kwargs) → error
    nope = tool_bg_modulator_set()
    assert nope["ok"] is False

    # Values out of [0, 1] get clamped
    r = tool_bg_modulator_set(tonic_da=1.5, lc_ne=-0.5, serotonin=0.8, set_by="test")
    assert r["ok"] is True
    assert r["modulators"]["tonic_da"] == 1.0
    assert r["modulators"]["lc_ne"] == 0.0
    assert r["modulators"]["serotonin"] == 0.8


def test_bg_status_aggregates_loops_actions_and_modulators():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration(conn)
    _patched(conn)

    for loop in ("motor", "dlpfc", "acc"):
        tool_bg_action_register(loop=loop, action_key=f"tool:{loop}_demo")
    tool_bg_modulator_set(tonic_da=0.2, lc_ne=0.9, serotonin=0.4)

    r = tool_bg_status(top_n=10)
    assert r["ok"] is True
    assert r["modulators"]["tonic_da"] == 0.2
    assert r["modulators"]["lc_ne"] == 0.9
    assert r["modulators"]["serotonin"] == 0.4
    loops_seen = {row["loop"] for row in r["actions_by_loop"]}
    assert loops_seen == {"motor", "dlpfc", "acc"}
    assert len(r["top_actions"]) == 3


def test_valid_loops_set_matches_check_constraint():
    # The migration's CHECK constraint must agree with VALID_LOOPS or
    # action_register will silently mismatch. Five Alexander/DeLong/Strick
    # parallel loops.
    assert VALID_LOOPS == {"motor", "oculomotor", "dlpfc", "lofc", "acc"}
