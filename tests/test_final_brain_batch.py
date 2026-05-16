"""Minimal smoke tests for the final batch of 6 brain subsystems
(ACC, DMN, drives, insula, PFC, entorhinal grid) — Phase 1.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO = Path(__file__).resolve().parent.parent
MIGS = REPO / "db" / "migrations"


def _setup(migrations: list[str]) -> str:
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    conn = sqlite3.connect(tmpf.name)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    for m in migrations:
        with open(str(MIGS / m)) as f:
            conn.executescript(f.read())
    conn.close()
    return tmpf.name


def test_acc_evaluate_records_event():
    db = _setup(["060_acc.sql"])
    try:
        with patch("agentmemory.mcp_tools_acc.DB_PATH", db):
            from agentmemory.mcp_tools_acc import tool_acc_evaluate, tool_acc_status
            r = tool_acc_evaluate(op_kind="memory_add", op_scope="project:t", agent_id="a")
            assert r["ok"] is True
            assert r["conflict_score"] == 0.0  # no peers
            assert r["action"] in ("log", "warn", "ignore", "hold_fired")
            s = tool_acc_status(top_n=5)
            assert s["ok"] is True
    finally:
        os.unlink(db)


def test_dmn_simulate_quarantines_speculative_memory():
    db = _setup(["061_dmn.sql"])
    try:
        with patch("agentmemory.mcp_tools_dmn.DB_PATH", db):
            from agentmemory.mcp_tools_dmn import (
                tool_dmn_simulate, tool_dmn_speculative_list,
            )
            r = tool_dmn_simulate(
                agent_id="a", seed_type="entity", seed_id=1,
                scope="p", scenario="test scenario",
                speculative_content="test speculative content",
                plausibility=0.6, novelty=0.7, utility=0.8,
            )
            assert r["ok"] is True
            assert r["speculative_memory_id"] is not None
            assert abs(r["composite_score"] - 0.7) < 1e-9
            spec = tool_dmn_speculative_list(validation_state="pending")
            assert len(spec["items"]) == 1
    finally:
        os.unlink(db)


def test_drive_sample_and_recommend_mode():
    db = _setup(["062_drives.sql"])
    try:
        with patch("agentmemory.mcp_tools_drives.DB_PATH", db):
            from agentmemory.mcp_tools_drives import (
                tool_drive_sample, tool_drive_status, tool_drive_recommend_mode,
            )
            r = tool_drive_sample()
            assert r["ok"] is True
            assert len(r["drives"]) == 5  # 5 seeded drives
            s = tool_drive_status()
            assert s["ok"] is True
            assert len(s["drives"]) == 5
            rec = tool_drive_recommend_mode()
            assert rec["ok"] is True
    finally:
        os.unlink(db)


def test_insula_sample_and_state():
    db = _setup(["063_insula.sql"])
    try:
        with patch("agentmemory.mcp_tools_insula.DB_PATH", db):
            from agentmemory.mcp_tools_insula import (
                tool_insula_sample, tool_insula_state, tool_insula_subscribe,
            )
            r = tool_insula_sample()
            assert r["ok"] is True
            assert "write_pressure" in r["signals"]
            assert r["felt_state_label"] in (
                "calm", "strained", "overloaded", "uncertain", "fatigued",
            )
            s = tool_insula_state()
            assert s["ok"] is True
            assert s["state"] is not None
            r2 = tool_insula_subscribe(
                subsystem="thalamus", signal_name="consolidation_debt",
                threshold=0.7, action_hint="request_mode_consolidate",
            )
            assert r2["ok"] is True
    finally:
        os.unlink(db)


def test_pfc_slots_roundtrip():
    db = _setup(["064_pfc_slots.sql"])
    try:
        with patch("agentmemory.mcp_tools_pfc.DB_PATH", db):
            from agentmemory.mcp_tools_pfc import (
                tool_pfc_slot_set, tool_pfc_slot_get, tool_pfc_status,
            )
            r = tool_pfc_slot_set(agent_id="a", slot="dlpfc", content={"task": "x"})
            assert r["ok"] is True
            g = tool_pfc_slot_get(agent_id="a", slot="dlpfc")
            assert g["exists"] is True
            assert g["content_parsed"] == {"task": "x"}
            s = tool_pfc_status()
            assert s["ok"] is True
            # Slot enum is enforced
            bad = tool_pfc_slot_set(agent_id="a", slot="bogus", content={})
            assert bad["ok"] is False
    finally:
        os.unlink(db)


def test_entorhinal_grid_indexing():
    db = _setup(["065_entorhinal_grid.sql"])
    try:
        with patch("agentmemory.mcp_tools_entorhinal_grid.DB_PATH", db):
            from agentmemory.mcp_tools_entorhinal_grid import (
                tool_entorhinal_activate, tool_entorhinal_lookup, tool_entorhinal_status,
            )
            r = tool_entorhinal_activate(memory_id=1, content="hello world")
            assert r["ok"] is True
            assert len(r["cells_activated"]) == 3  # 3 scales
            r2 = tool_entorhinal_lookup(content="hello world")
            assert r2["ok"] is True
            # Same content should match itself
            assert any(m["memory_id"] == 1 for m in r2["matches"])
            s = tool_entorhinal_status()
            assert s["ok"] is True
            assert s["activations_total"] >= 3
    finally:
        os.unlink(db)
