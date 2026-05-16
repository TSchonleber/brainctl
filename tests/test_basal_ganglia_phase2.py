"""Tests for basal ganglia Phase 2 (TD-error broadcast + dispatch shadow)."""
import sqlite3
import sys
import tempfile
import os
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_basal_ganglia import (
    tool_bg_td_emit, tool_bg_shadow_stats, tool_bg_action_register,
)
from agentmemory.bg_shadow import (
    consult_for_dispatch, broadcast_td_error, _OUTCOME_UTILITY,
)


class _NoCloseConn:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


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
    conn.close()
    return tmpf.name


def test_consult_for_dispatch_skips_unregistered_actions():
    db_path = _setup_tempdb()
    try:
        # Unregistered → no-op, no DB write
        result = consult_for_dispatch(
            action_key="tool_not_in_catalog",
            agent_id="test",
            arguments={"x": 1},
            db_path=db_path,
        )
        assert result is None
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM bg_shadow_decisions").fetchone()[0]
        assert count == 0
        # bg_* prefix never shadowed
        assert consult_for_dispatch(action_key="bg_status", agent_id="t", db_path=db_path) is None
        conn.close()
    finally:
        os.unlink(db_path)


def test_consult_for_dispatch_logs_registered_action():
    db_path = _setup_tempdb()
    try:
        # Register an action directly via SQL (avoiding the open_db patch)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('motor', 'tool:memory_add')")
        conn.commit()
        conn.close()

        result = consult_for_dispatch(
            action_key="memory_add",
            agent_id="test-agent",
            arguments={"project": "brainctl", "category": "convention"},
            db_path=db_path,
        )
        assert result is not None
        assert result["action_key"] == "tool:memory_add"
        assert result["loop"] == "motor"
        assert result["decision"] == "approve"
        assert result["net_signal"] == 0.0

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT action_key, loop, decision FROM bg_shadow_decisions").fetchone()
        assert row == ("tool:memory_add", "motor", "approve")
        conn.close()
    finally:
        os.unlink(db_path)


def test_broadcast_td_error_computes_delta_from_outcome():
    db_path = _setup_tempdb()
    try:
        r1 = broadcast_td_error(task_id="t1", agent_id="a", outcome="success", db_path=db_path)
        assert r1 is not None
        assert r1["delta"] == 1.0
        r2 = broadcast_td_error(task_id="t2", agent_id="a", outcome="failure", db_path=db_path)
        assert r2["delta"] == -1.0
        r3 = broadcast_td_error(task_id="t3", agent_id="a", outcome="partial", db_path=db_path)
        assert r3["delta"] == _OUTCOME_UTILITY["partial"]

        # Unknown outcome → utility 0.0, delta 0.0
        r4 = broadcast_td_error(task_id="t4", agent_id="a", outcome="weird", db_path=db_path)
        assert r4["delta"] == 0.0

        # Explicit utility overrides outcome mapping
        r5 = broadcast_td_error(task_id="t5", agent_id="a", utility=0.42, db_path=db_path)
        assert r5["delta"] == 0.42
    finally:
        os.unlink(db_path)


def test_broadcast_td_error_uses_bellman_with_v():
    db_path = _setup_tempdb()
    try:
        # δ = utility + γ·V(s') − V(s) = 0.5 + 0.95·1.0 − 0.2 = 1.25
        r = broadcast_td_error(
            task_id="t", agent_id="a", utility=0.5,
            v_current=0.2, v_next=1.0, gamma=0.95, db_path=db_path,
        )
        assert r is not None
        assert abs(r["delta"] - 1.25) < 1e-9
    finally:
        os.unlink(db_path)


def test_shadow_stats_summary_with_block_decision():
    db_path = _setup_tempdb()
    try:
        # Register action and seed striatal weights that produce a strong NoGo
        conn = sqlite3.connect(db_path)
        cur = conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('motor', 'tool:risky')")
        action_id = cur.lastrowid
        conn.execute(
            "INSERT INTO bg_striatal_weights (action_id, context_hash, w_go, w_nogo) VALUES (?, 'ctx', 0.0, 1.0)",
            (action_id,),
        )
        # And one neutral action
        conn.execute("INSERT INTO bg_actions (loop, action_key) VALUES ('dlpfc', 'tool:neutral')")
        conn.commit()
        conn.close()

        # Consult both — risky should block, neutral should approve
        r_risky = consult_for_dispatch(action_key="risky", agent_id="a", db_path=db_path)
        r_neutral = consult_for_dispatch(action_key="neutral", agent_id="a", db_path=db_path)
        assert r_risky["decision"] == "block"
        assert r_neutral["decision"] == "approve"

        # Stats — patch open_db so the tool reads our temp DB
        import agentmemory.mcp_tools_basal_ganglia as bgm
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bgm.open_db = lambda x: _NoCloseConn(conn)  # type: ignore[assignment]
        r = tool_bg_shadow_stats(days=7)
        assert r["ok"] is True
        assert r["total_decisions"] == 2
        decisions = {d["decision"]: d["n"] for d in r["by_decision"]}
        assert decisions == {"approve": 1, "block": 1}
        assert r["divergence_rate"] == 0.5
    finally:
        os.unlink(db_path)
