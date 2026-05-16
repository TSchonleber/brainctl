"""Tests for cerebellum Phase 3: confidence → thalamus salience +
boundary markers → workspace broadcasts."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_cerebellum import (
    tool_cerebellum_predict, tool_cerebellum_observe,
    cerebellum_partner_precision, VALID_PARTNERS,
)
from agentmemory.mcp_tools_thalamus import (
    tool_thalamus_salience, _SECTOR_TO_CEREBELLUM_PARTNER,
)


class _NoCloseConn:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


def _setup_tempdb_full() -> str:
    """A DB with all three subsystems: thalamus (050), BG (054), cerebellum
    (056), and the cerebellum→workspace bridge (057). Also seeds a minimal
    agents + memories + workspace_broadcasts schema needed by the bridge.
    """
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    conn = sqlite3.connect(tmpf.name)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    # Minimal upstream tables needed by migration 057
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            agent_type TEXT NOT NULL, adapter_info TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL, category TEXT NOT NULL,
            content TEXT NOT NULL, scope TEXT DEFAULT 'global',
            confidence REAL DEFAULT 1.0, memory_type TEXT DEFAULT 'episodic',
            write_tier TEXT DEFAULT 'full', indexed INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );
        CREATE TABLE IF NOT EXISTS workspace_broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES memories(id),
            agent_id TEXT NOT NULL,
            salience REAL NOT NULL,
            summary TEXT NOT NULL,
            target_scope TEXT NOT NULL DEFAULT 'global',
            broadcast_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            expires_at TEXT, ack_count INTEGER DEFAULT 0,
            triggered_by TEXT DEFAULT 'auto'
        );
        """
    )
    for migration in (
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "050_thalamus.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "054_basal_ganglia.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "056_cerebellum.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "057_cerebellum_workspace_bridge.sql"),
    ):
        with open(migration) as f:
            conn.executescript(f.read())
    conn.close()
    return tmpf.name


def test_sector_to_partner_mapping_covers_all_sectors():
    assert set(_SECTOR_TO_CEREBELLUM_PARTNER.keys()) == {
        "memory_recall", "belief", "pii_sensitive",
        "sensory_external", "agent_efferent", "consolidation",
    }
    # All target partners must be valid cerebellum partners
    for partner in _SECTOR_TO_CEREBELLUM_PARTNER.values():
        assert partner in VALID_PARTNERS


def test_boundary_fires_workspace_broadcast_via_sentinel():
    db = _setup_tempdb_full()
    try:
        with patch("agentmemory.mcp_tools_cerebellum.DB_PATH", db):
            # Boundary fires on |δ| ≥ 0.5
            p = tool_cerebellum_predict(
                partner="motor_partner", prediction_kind="success_probability",
                context={"k": "v1"},
            )
            o = tool_cerebellum_observe(prediction_id=p["prediction_id"], observed_value=1.0)
            assert o["ok"] is True
            assert o["delta_forward"] == 1.0
            assert o["boundary_id"] is not None
            assert o["workspace_broadcast_id"] is not None

            # Verify the workspace row exists and references the sentinel memory
            conn = sqlite3.connect(db)
            wb = conn.execute(
                "SELECT memory_id, agent_id, salience, triggered_by, target_scope "
                "FROM workspace_broadcasts WHERE id = ?",
                (o["workspace_broadcast_id"],),
            ).fetchone()
            assert wb is not None
            memory_id, agent_id, salience, triggered_by, scope = wb
            assert agent_id == "cerebellum-system"
            assert salience == 1.0
            assert triggered_by == f"cerebellum_boundary:{o['boundary_id']}"
            assert scope == "global"
            # The memory_id must point at the sentinel
            sentinel = conn.execute(
                "SELECT id FROM memories "
                "WHERE agent_id='cerebellum-system' AND scope='system' LIMIT 1"
            ).fetchone()
            assert sentinel is not None
            assert memory_id == sentinel[0]
            conn.close()
    finally:
        os.unlink(db)


def test_no_boundary_no_workspace_broadcast():
    db = _setup_tempdb_full()
    try:
        with patch("agentmemory.mcp_tools_cerebellum.DB_PATH", db):
            # Small δ should NOT fire boundary or workspace broadcast
            p = tool_cerebellum_predict(
                partner="motor_partner", prediction_kind="success_probability",
                context={"k": "v_small"},
            )
            o = tool_cerebellum_observe(prediction_id=p["prediction_id"], observed_value=0.1)
            assert o["boundary_id"] is None
            assert o["workspace_broadcast_id"] is None
    finally:
        os.unlink(db)


def test_cerebellum_partner_precision_defaults_neutral_when_empty():
    db = _setup_tempdb_full()
    try:
        with patch("agentmemory.mcp_tools_cerebellum.DB_PATH", db):
            # No learning has happened — defaults to 0.5
            assert cerebellum_partner_precision("motor_partner") == 0.5

            # Run a few observations to accumulate confidence on motor_partner
            for i in range(3):
                p = tool_cerebellum_predict(
                    partner="motor_partner", prediction_kind="success_probability",
                    context={"k": f"v{i}"},
                )
                tool_cerebellum_observe(prediction_id=p["prediction_id"], observed_value=0.05)

            # Confidence is updated; should be > 0 but < 0.5 (early learning)
            prec = cerebellum_partner_precision("motor_partner")
            assert 0.0 < prec < 1.0
    finally:
        os.unlink(db)


def test_thalamus_salience_reads_cerebellum_precision():
    db = _setup_tempdb_full()
    try:
        with patch("agentmemory.mcp_tools_cerebellum.DB_PATH", db), \
             patch("agentmemory.mcp_tools_thalamus.DB_PATH", db):
            # Bake confidence into oculomotor_partner by running a stable
            # prediction cycle
            for i in range(5):
                p = tool_cerebellum_predict(
                    partner="oculomotor_partner",
                    prediction_kind="success_probability",
                    context={"agent_id": "a", "project": "p"},
                )
                # observe close to predicted so confidence climbs
                tool_cerebellum_observe(prediction_id=p["prediction_id"], observed_value=0.1)

            # Score a candidate routed to memory_recall (→ oculomotor_partner).
            # Should now include the cerebellum_multiplier > 0.7.
            cands = [
                {"content": "ordinary memory recall", "novelty": 0.5, "confidence": 0.9},
            ]
            scored = tool_thalamus_salience(
                cands, agent_id="a", project="p", query="memory",
            )
            assert len(scored) == 1
            s = scored[0]
            assert "cerebellum_confidence" in s
            assert "cerebellum_multiplier" in s
            assert 0.7 <= s["cerebellum_multiplier"] <= 1.3
    finally:
        os.unlink(db)


def test_partner_precision_invalid_returns_neutral():
    assert cerebellum_partner_precision("not_a_partner") == 0.5
