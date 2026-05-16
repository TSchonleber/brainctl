"""Tests for cerebellum Phase 1: schema + 4 tools + predict-observe loop."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_cerebellum import (
    tool_cerebellum_status, tool_cerebellum_module_register,
    tool_cerebellum_predict, tool_cerebellum_observe,
    VALID_PARTNERS, VALID_PREDICTION_KINDS,
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
    with open(str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "056_cerebellum.sql")) as f:
        conn.executescript(f.read())
    conn.close()
    return tmpf.name


def _patched_module(conn):
    import agentmemory.mcp_tools_cerebellum as m
    m.open_db = lambda x: _NoCloseConn(conn)  # type: ignore[assignment]
    return m


def test_migration_creates_all_tables():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cerebellum_%'"
        ).fetchall()}
        assert tables == {
            "cerebellum_modules", "cerebellum_weights", "cerebellum_predictions",
            "cerebellum_traces", "cerebellum_boundaries",
        }
        conn.close()
    finally:
        os.unlink(db)


def test_module_register_idempotent_and_validates():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched_module(conn)
        r1 = tool_cerebellum_module_register(partner="motor_partner", prediction_kind="success_probability")
        assert r1["ok"] is True
        r2 = tool_cerebellum_module_register(partner="motor_partner", prediction_kind="success_probability", description="updated")
        assert r2["ok"] is True
        assert r2["module"]["description"] == "updated"
        count = conn.execute("SELECT COUNT(*) FROM cerebellum_modules WHERE partner='motor_partner'").fetchone()[0]
        assert count == 1

        bad_p = tool_cerebellum_module_register(partner="bogus", prediction_kind="success_probability")
        assert bad_p["ok"] is False
        bad_k = tool_cerebellum_module_register(partner="motor_partner", prediction_kind="bogus")
        assert bad_k["ok"] is False
        conn.close()
    finally:
        os.unlink(db)


def test_predict_observe_closes_the_loop_and_learns():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched_module(conn)

        ctx = {"project": "p", "agent_id": "a", "scope": "global"}
        # Cold start: prediction should be 0
        r1 = tool_cerebellum_predict(partner="motor_partner", prediction_kind="success_probability", context=ctx)
        assert r1["ok"] is True
        assert r1["predicted_value"] == 0.0
        assert r1["confidence"] == 0.0

        # Observe with target=1.0 — δ_forward = 1.0 - 0.0 = 1.0
        r2 = tool_cerebellum_observe(prediction_id=r1["prediction_id"], observed_value=1.0)
        assert r2["ok"] is True
        # weight += lr × trace × δ = 0.05 × 1.0 × 1.0 = 0.05
        assert abs(r2["weight_after"] - 0.05) < 1e-9
        assert r2["delta_forward"] == 1.0
        # Boundary marker fires when |δ| ≥ 0.5
        assert r2["boundary_id"] is not None

        # Re-predict on same context: should return the learned weight
        r3 = tool_cerebellum_predict(partner="motor_partner", prediction_kind="success_probability", context=ctx)
        assert abs(r3["predicted_value"] - 0.05) < 1e-9

        conn.close()
    finally:
        os.unlink(db)


def test_status_aggregates_modules_and_predictions():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched_module(conn)
        tool_cerebellum_module_register(partner="motor_partner", prediction_kind="success_probability")
        tool_cerebellum_module_register(partner="dlpfc_partner", prediction_kind="expected_latency_ms")
        # One full predict→observe cycle
        r = tool_cerebellum_predict(partner="motor_partner", prediction_kind="success_probability", context="ctxA")
        tool_cerebellum_observe(prediction_id=r["prediction_id"], observed_value=0.8)

        s = tool_cerebellum_status(top_n=10)
        assert s["ok"] is True
        # Must include both registered modules
        partners_seen = {m["partner"] for m in s["modules"]}
        assert "motor_partner" in partners_seen
        assert "dlpfc_partner" in partners_seen
        assert s["pending_observations"] == 0
        conn.close()
    finally:
        os.unlink(db)


def test_partner_and_kind_enums_match_check_constraints():
    # Ensure the Python validators match the migration's CHECK constraints.
    assert VALID_PARTNERS == {
        "motor_partner", "oculomotor_partner", "dlpfc_partner",
        "lofc_partner", "acc_partner",
    }
    assert VALID_PREDICTION_KINDS == {
        "success_probability", "expected_latency_ms", "expected_outcome_class",
    }


def test_boundary_marker_fires_only_above_threshold():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched_module(conn)
        tool_cerebellum_module_register(partner="acc_partner", prediction_kind="success_probability")

        # Small error — δ = 0.2 should NOT fire a boundary
        r1 = tool_cerebellum_predict(partner="acc_partner", prediction_kind="success_probability", context="c1")
        r1o = tool_cerebellum_observe(prediction_id=r1["prediction_id"], observed_value=0.2)
        assert r1o["boundary_id"] is None

        # Large error — δ = 0.9 SHOULD fire a boundary
        r2 = tool_cerebellum_predict(partner="acc_partner", prediction_kind="success_probability", context="c2")
        r2o = tool_cerebellum_observe(prediction_id=r2["prediction_id"], observed_value=0.9)
        assert r2o["boundary_id"] is not None
        conn.close()
    finally:
        os.unlink(db)
