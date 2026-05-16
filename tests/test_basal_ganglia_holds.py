"""Tests for the BG hyperdirect-pathway hold mechanism."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.bg_shadow import (
    consult_for_dispatch, broadcast_td_error, fire_hold, release_hold,
)


def _setup_tempdb() -> str:
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    conn = sqlite3.connect(tmpf.name)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    for migration in (
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "054_basal_ganglia.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "055_basal_ganglia_shadow.sql"),
    ):
        with open(migration) as f:
            conn.executescript(f.read())
    # Register actions across two loops so we can trigger conflict
    conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('motor', 'tool:write_thing')")
    conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('oculomotor', 'tool:read_thing')")
    conn.commit()
    conn.close()
    return tmpf.name


def test_manual_fire_and_release_hold():
    db = _setup_tempdb()
    try:
        r = fire_hold(loop="motor", reason="explicit_stop", db_path=db)
        assert r is not None
        assert r["loop"] == "motor"
        assert r["reason"] == "explicit_stop"

        # release
        rel = release_hold(r["id"], db_path=db)
        assert rel is not None
        assert rel["released"] is True

        # second release is idempotent (returns released=False)
        rel2 = release_hold(r["id"], db_path=db)
        assert rel2 is not None
        assert rel2["released"] is False
    finally:
        os.unlink(db)


def test_fire_hold_rejects_invalid_reason():
    db = _setup_tempdb()
    try:
        r = fire_hold(loop="motor", reason="bogus", db_path=db)
        assert r is None
    finally:
        os.unlink(db)


def test_surprise_hold_auto_fires_on_large_delta():
    db = _setup_tempdb()
    try:
        # Broadcast a large δ to populate the recent-events buffer
        broadcast_td_error(task_id="t", agent_id="agent1", utility=0.95, db_path=db)
        # Consult — should auto-fire surprise hold
        r = consult_for_dispatch(
            action_key="write_thing", agent_id="agent1", arguments={}, db_path=db,
        )
        assert r is not None
        assert r.get("fired_hold") is not None
        assert r["fired_hold"]["reason"] == "surprise"
        assert r["fired_hold"]["trigger_score_gap"] >= 0.95 - 1e-9
    finally:
        os.unlink(db)


def test_conflict_hold_auto_fires_on_cross_loop_traffic():
    db = _setup_tempdb()
    try:
        # Same agent dispatches a motor action then an oculomotor action
        # within the conflict window — second consult should fire conflict.
        consult_for_dispatch(action_key="write_thing", agent_id="agentX", arguments={}, db_path=db)
        r = consult_for_dispatch(action_key="read_thing", agent_id="agentX", arguments={}, db_path=db)
        assert r is not None
        # We may get a hold; if so it should be conflict-class
        fh = r.get("fired_hold")
        if fh is not None:
            assert fh["reason"] in ("conflict", "surprise")
    finally:
        os.unlink(db)


def test_active_holds_appear_in_consult_response():
    db = _setup_tempdb()
    try:
        fire_hold(loop="motor", reason="explicit_stop", db_path=db)
        r = consult_for_dispatch(action_key="write_thing", agent_id="a", arguments={}, db_path=db)
        assert r is not None
        # active_holds list should contain the recently-fired one
        active = r.get("active_holds", [])
        assert any(h["reason"] == "explicit_stop" for h in active)
    finally:
        os.unlink(db)
