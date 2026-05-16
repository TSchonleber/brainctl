"""Tests for cerebellum Phase 2: auto-wire predict/observe at dispatch."""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.cerebellum_shadow import (
    consult_for_dispatch, observe_dispatch, _should_skip, _LOOP_TO_PARTNER,
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
        "/Users/r4vager/agentmemory/db/migrations/056_cerebellum.sql",
    ):
        with open(migration) as f:
            conn.executescript(f.read())
    # Register one action so partner resolution succeeds
    conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('oculomotor', 'tool:demo')")
    conn.commit()
    conn.close()
    return tmpf.name


def test_should_skip_blocks_recursion_paths():
    assert _should_skip("cerebellum_status") is True
    assert _should_skip("bg_status") is True
    assert _should_skip("thalamus_status") is True
    assert _should_skip("stats") is True
    assert _should_skip("memory_search") is False


def test_loop_to_partner_covers_all_five_loops():
    assert _LOOP_TO_PARTNER == {
        "motor": "motor_partner",
        "oculomotor": "oculomotor_partner",
        "dlpfc": "dlpfc_partner",
        "lofc": "lofc_partner",
        "acc": "acc_partner",
    }


def test_consult_skips_unregistered_action():
    db = _setup_tempdb()
    try:
        r = consult_for_dispatch(action_key="totally_unknown_tool", agent_id="a", db_path=db)
        assert r is None
        conn = sqlite3.connect(db)
        # No cerebellum rows should be written
        n = conn.execute("SELECT COUNT(*) FROM cerebellum_predictions").fetchone()[0]
        assert n == 0
        conn.close()
    finally:
        os.unlink(db)


def test_consult_creates_three_predictions_for_registered_action():
    db = _setup_tempdb()
    try:
        r = consult_for_dispatch(
            action_key="demo", agent_id="agent1",
            arguments={"project": "p1", "query": "q1"}, db_path=db,
        )
        assert r is not None
        assert r["partner"] == "oculomotor_partner"
        assert set(r["predictions"].keys()) == {
            "success_probability", "expected_latency_ms", "expected_outcome_class",
        }
        # 3 modules auto-registered, 3 predictions logged, 3 traces deposited
        conn = sqlite3.connect(db)
        n_modules = conn.execute(
            "SELECT COUNT(*) FROM cerebellum_modules WHERE partner='oculomotor_partner'"
        ).fetchone()[0]
        n_predictions = conn.execute("SELECT COUNT(*) FROM cerebellum_predictions").fetchone()[0]
        n_traces = conn.execute("SELECT COUNT(*) FROM cerebellum_traces").fetchone()[0]
        assert n_modules == 3
        assert n_predictions == 3
        assert n_traces == 3
        conn.close()
    finally:
        os.unlink(db)


def test_observe_closes_all_predictions_on_success():
    db = _setup_tempdb()
    try:
        # Patch the live DB path used by tool_cerebellum_observe via the
        # cerebellum_shadow helper. The simplest mechanism is to monkey-patch
        # get_db_path in both modules.
        with patch("agentmemory.cerebellum_shadow.get_db_path", return_value=db), \
             patch("agentmemory.mcp_tools_cerebellum.get_db_path", return_value=db), \
             patch("agentmemory.mcp_tools_cerebellum.DB_PATH", db):
            r = consult_for_dispatch(action_key="demo", agent_id="a", arguments={}, db_path=db)
            assert r is not None
            time.sleep(0.005)
            o = observe_dispatch(r, error=None, db_path=db)
            assert o is not None
            assert o["partner"] == "oculomotor_partner"
            assert set(o["closed"].keys()) == {
                "success_probability", "expected_latency_ms", "expected_outcome_class",
            }
            # success_probability and expected_outcome_class should both
            # observe 1.0 → δ_forward = 1.0 → weight = lr × trace × δ = 0.05
            assert abs(o["closed"]["success_probability"]["weight_after"] - 0.05) < 1e-9
            assert abs(o["closed"]["expected_outcome_class"]["weight_after"] - 0.05) < 1e-9
    finally:
        os.unlink(db)


def test_observe_records_failure_when_error_passed():
    db = _setup_tempdb()
    try:
        with patch("agentmemory.cerebellum_shadow.get_db_path", return_value=db), \
             patch("agentmemory.mcp_tools_cerebellum.get_db_path", return_value=db), \
             patch("agentmemory.mcp_tools_cerebellum.DB_PATH", db):
            r = consult_for_dispatch(action_key="demo", agent_id="a", arguments={}, db_path=db)
            o = observe_dispatch(r, error=RuntimeError("boom"), db_path=db)
            assert o is not None
            # On error, observed=0.0, δ_forward = 0 - 0 = 0, weight stays 0
            assert o["closed"]["success_probability"]["delta_forward"] == 0.0
            assert o["closed"]["success_probability"]["weight_after"] == 0.0
    finally:
        os.unlink(db)
