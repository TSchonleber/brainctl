"""Tests for basal ganglia Phase 3: eligibility traces + weight updates."""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.bg_shadow import (
    consult_for_dispatch, broadcast_td_error, sweep_eligibility_traces,
    _apply_three_factor_update,
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
        "/Users/r4vager/agentmemory/db/migrations/054_basal_ganglia.sql",
        "/Users/r4vager/agentmemory/db/migrations/055_basal_ganglia_shadow.sql",
    ):
        with open(migration) as f:
            conn.executescript(f.read())
    # Register an action for our tests
    conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('motor', 'tool:learner')")
    conn.commit()
    conn.close()
    return tmpf.name


def test_consult_deposits_an_eligibility_trace():
    db = _setup_tempdb()
    try:
        result = consult_for_dispatch(
            action_key="learner", agent_id="a1",
            arguments={"project": "p"}, db_path=db,
        )
        assert result is not None
        assert result["context_hash"]
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM bg_eligibility_traces").fetchone()[0]
        assert count == 1
        strength = conn.execute("SELECT trace_strength FROM bg_eligibility_traces").fetchone()[0]
        assert abs(strength - 1.0) < 1e-9
        conn.close()
    finally:
        os.unlink(db)


def test_positive_delta_raises_w_go_and_lowers_w_nogo():
    db = _setup_tempdb()
    try:
        consult_for_dispatch(action_key="learner", agent_id="a", arguments={"project": "p"}, db_path=db)
        r = broadcast_td_error(task_id="t1", agent_id="a", outcome="success", db_path=db)
        assert r is not None
        assert r["traces_consumed"] == 1
        assert r["weight_updates"] == 1

        conn = sqlite3.connect(db)
        w_go, w_nogo = conn.execute(
            "SELECT w_go, w_nogo FROM bg_striatal_weights"
        ).fetchone()
        # lr=0.1, trace=1.0, δ=+1.0  → w_go += 0.1, w_nogo -= 0.05 (clamped to 0)
        assert abs(w_go - 0.1) < 1e-9
        assert w_nogo == 0.0  # clamped
        conn.close()
    finally:
        os.unlink(db)


def test_negative_delta_raises_w_nogo_and_lowers_w_go():
    db = _setup_tempdb()
    try:
        consult_for_dispatch(action_key="learner", agent_id="a", arguments={"project": "p"}, db_path=db)
        r = broadcast_td_error(task_id="t1", agent_id="a", outcome="failure", db_path=db)
        assert r["weight_updates"] == 1

        conn = sqlite3.connect(db)
        w_go, w_nogo = conn.execute("SELECT w_go, w_nogo FROM bg_striatal_weights").fetchone()
        # lr=0.1, trace=1.0, δ=-1.0  → w_nogo += 0.1, w_go -= 0.05 (clamped to 0)
        assert w_go == 0.0  # clamped
        assert abs(w_nogo - 0.1) < 1e-9
        conn.close()
    finally:
        os.unlink(db)


def test_repeated_outcomes_accumulate_with_trace_decay():
    db = _setup_tempdb()
    try:
        # Three successes — same context, eligibility trace decays each time
        for i in range(3):
            consult_for_dispatch(action_key="learner", agent_id="a", arguments={"project": "p"}, db_path=db)
            broadcast_td_error(task_id=f"t{i}", agent_id="a", outcome="success", db_path=db)

        conn = sqlite3.connect(db)
        n_updates, w_go = conn.execute(
            "SELECT n_updates, w_go FROM bg_striatal_weights"
        ).fetchone()
        # Several rounds — n_updates should reflect cumulative
        assert n_updates >= 3
        # w_go should be strictly higher than a single round (>0.1) but
        # bounded above by 1.0 — actual value depends on which traces survive
        # and how many fresh deposits hit
        assert w_go > 0.1
        assert w_go <= 1.0
        conn.close()
    finally:
        os.unlink(db)


def test_sweep_removes_weak_or_expired_traces():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        # Insert one strong (default) trace, one weak (strength < 0.05),
        # one already-expired
        action_id = conn.execute("SELECT id FROM bg_actions WHERE action_key='tool:learner'").fetchone()[0]
        conn.execute(
            "INSERT INTO bg_eligibility_traces (action_id, context_hash, trace_strength, expires_at) "
            "VALUES (?, 'strong', 1.0, strftime('%Y-%m-%dT%H:%M:%S', 'now', '+1 hour'))",
            (action_id,),
        )
        conn.execute(
            "INSERT INTO bg_eligibility_traces (action_id, context_hash, trace_strength, expires_at) "
            "VALUES (?, 'weak', 0.01, strftime('%Y-%m-%dT%H:%M:%S', 'now', '+1 hour'))",
            (action_id,),
        )
        conn.execute(
            "INSERT INTO bg_eligibility_traces (action_id, context_hash, trace_strength, expires_at) "
            "VALUES (?, 'expired', 1.0, '2020-01-01T00:00:00')",
            (action_id,),
        )
        conn.commit()
        conn.close()

        r = sweep_eligibility_traces(db_path=db)
        assert r["ok"] is True
        assert r["removed"] == 2  # weak + expired
        assert r["remaining"] == 1
    finally:
        os.unlink(db)


def test_three_factor_update_helper_directly():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        action_id = conn.execute("SELECT id FROM bg_actions").fetchone()[0]
        # Apply δ=+0.5, trace_strength=1.0, lr=0.1
        new_go, new_nogo = _apply_three_factor_update(
            conn, action_id=action_id, context_hash="t",
            trace_strength=1.0, delta=0.5,
        )
        # w_go +=  0.1 * 1.0 * 0.5 = 0.05; w_nogo -= 0.025 → clamped to 0
        assert abs(new_go - 0.05) < 1e-9
        assert new_nogo == 0.0
        conn.commit()
        conn.close()
    finally:
        os.unlink(db)
