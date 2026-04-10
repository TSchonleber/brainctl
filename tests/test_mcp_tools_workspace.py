"""Tests for mcp_tools_workspace — workspace coordination MCP tools."""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_workspace as ws_mod


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Point the module at a fresh temp DB for every test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))
    monkeypatch.setattr(ws_mod, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_memory(db_file: Path, content: str = "test memory", category: str = "decision",
                confidence: float = 0.9, scope: str = "global") -> int:
    """Insert a bare memory row and return its id."""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Brain.__init__ creates the 'default' agent; use it to satisfy the FK.
    conn.execute(
        "INSERT INTO memories (agent_id, content, category, confidence, scope, memory_type, created_at, updated_at) "
        "VALUES ('default', ?, ?, ?, ?, 'episodic', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (content, category, confidence, scope),
    )
    conn.commit()
    mid = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()
    return mid


# ---------------------------------------------------------------------------
# Module interface
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(ws_mod.TOOLS, list)
        assert all(isinstance(t, Tool) for t in ws_mod.TOOLS)

    def test_tool_names(self):
        names = {t.name for t in ws_mod.TOOLS}
        expected = {
            "workspace_status", "workspace_history", "workspace_broadcast",
            "workspace_ack", "workspace_phi", "workspace_ingest",
        }
        assert names == expected

    def test_dispatch_keys_match_tool_names(self):
        names = {t.name for t in ws_mod.TOOLS}
        assert set(ws_mod.DISPATCH.keys()) == names

    def test_dispatch_values_are_callable(self):
        for name, fn in ws_mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"


# ---------------------------------------------------------------------------
# workspace_status
# ---------------------------------------------------------------------------

class TestWorkspaceStatus:
    def test_returns_ok_on_empty_db(self):
        result = ws_mod.tool_workspace_status()
        assert result.get("ok") is True
        assert "broadcasts" in result
        assert isinstance(result["broadcasts"], list)

    def test_active_broadcast_appears(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        # manually insert a broadcast
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'test-agent', 0.9, 'test summary', 'global', 'manual')",
            (mid,)
        )
        conn.commit()
        conn.close()

        result = ws_mod.tool_workspace_status()
        assert result["ok"] is True
        assert result["active_broadcasts"] >= 1
        ids = [b["memory_id"] for b in result["broadcasts"]]
        assert mid in ids

    def test_scope_filter(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'agent', 0.8, 'summary', 'project:alpha', 'manual')",
            (mid,)
        )
        conn.commit()
        conn.close()

        result_alpha = ws_mod.tool_workspace_status(scope="project:alpha")
        result_beta = ws_mod.tool_workspace_status(scope="project:beta")
        assert result_alpha["active_broadcasts"] >= 1
        assert result_beta["active_broadcasts"] == 0

    def test_n_limits_results(self, patch_db_path):
        # Insert 5 memories and broadcasts
        conn = sqlite3.connect(str(patch_db_path))
        for i in range(5):
            conn.execute(
                "INSERT INTO memories (agent_id, content, category, confidence, scope, memory_type, created_at, updated_at) "
                "VALUES ('default', ?, 'decision', 0.9, 'global', 'episodic', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
                (f"memory {i}",)
            )
            mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
                "VALUES (?, 'agent', 0.8, 'summary', 'global', 'manual')",
                (mid,)
            )
        conn.commit()
        conn.close()

        result = ws_mod.tool_workspace_status(n=2)
        assert result["ok"] is True
        assert len(result["broadcasts"]) <= 2


# ---------------------------------------------------------------------------
# workspace_history
# ---------------------------------------------------------------------------

class TestWorkspaceHistory:
    def test_returns_ok_empty(self):
        result = ws_mod.tool_workspace_history()
        assert result.get("ok") is True
        assert "history" in result
        assert isinstance(result["history"], list)

    def test_broadcast_appears_in_history(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'hist-agent', 0.75, 'hist summary', 'global', 'manual')",
            (mid,)
        )
        conn.commit()
        conn.close()

        result = ws_mod.tool_workspace_history()
        assert result["ok"] is True
        agent_ids = [r["agent_id"] for r in result["history"]]
        assert "hist-agent" in agent_ids

    def test_agent_filter(self, patch_db_path):
        mid1 = _add_memory(patch_db_path, content="m1")
        mid2 = _add_memory(patch_db_path, content="m2")
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'agent-a', 0.7, 's', 'global', 'manual')", (mid1,)
        )
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'agent-b', 0.7, 's', 'global', 'manual')", (mid2,)
        )
        conn.commit()
        conn.close()

        result = ws_mod.tool_workspace_history(agent="agent-a")
        assert result["ok"] is True
        assert all(r["agent_id"] == "agent-a" for r in result["history"])

    def test_since_pagination(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        conn = sqlite3.connect(str(patch_db_path))
        for _ in range(3):
            conn.execute(
                "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
                "VALUES (?, 'agent', 0.7, 's', 'global', 'manual')", (mid,)
            )
        conn.commit()
        all_ids = [r[0] for r in conn.execute("SELECT id FROM workspace_broadcasts ORDER BY id").fetchall()]
        conn.close()

        first_id = all_ids[0]
        result = ws_mod.tool_workspace_history(since=first_id)
        assert result["ok"] is True
        returned_ids = [r["id"] for r in result["history"]]
        assert first_id not in returned_ids
        assert len(returned_ids) == len(all_ids) - 1


# ---------------------------------------------------------------------------
# workspace_broadcast
# ---------------------------------------------------------------------------

class TestWorkspaceBroadcast:
    def test_broadcast_creates_record(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        result = ws_mod.tool_workspace_broadcast(memory_id=mid)
        assert result.get("ok") is True
        assert "broadcast_id" in result
        assert isinstance(result["broadcast_id"], int)

    def test_missing_memory_returns_error(self):
        result = ws_mod.tool_workspace_broadcast(memory_id=999999)
        assert result.get("ok") is False
        assert "not found" in result.get("error", "").lower()

    def test_salience_returned(self, patch_db_path):
        mid = _add_memory(patch_db_path, category="decision", confidence=1.0)
        result = ws_mod.tool_workspace_broadcast(memory_id=mid)
        assert result["ok"] is True
        assert isinstance(result["salience"], float)
        assert 0.0 <= result["salience"] <= 1.0

    def test_custom_scope(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        result = ws_mod.tool_workspace_broadcast(memory_id=mid, scope="project:brain")
        assert result["ok"] is True
        assert result["scope"] == "project:brain"

    def test_custom_summary(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        result = ws_mod.tool_workspace_broadcast(memory_id=mid, summary="custom summary text")
        assert result["ok"] is True
        # Verify it was stored
        conn = sqlite3.connect(str(patch_db_path))
        row = conn.execute(
            "SELECT summary FROM workspace_broadcasts WHERE id=?", (result["broadcast_id"],)
        ).fetchone()
        conn.close()
        assert row[0] == "custom summary text"


# ---------------------------------------------------------------------------
# workspace_ack
# ---------------------------------------------------------------------------

class TestWorkspaceAck:
    def _insert_broadcast(self, db_file: Path) -> int:
        mid = _add_memory(db_file)
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'agent', 0.8, 's', 'global', 'manual')", (mid,)
        )
        conn.commit()
        bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return bid

    def test_ack_returns_ok(self, patch_db_path):
        bid = self._insert_broadcast(patch_db_path)
        result = ws_mod.tool_workspace_ack(broadcast_id=bid, agent="acking-agent")
        assert result.get("ok") is True
        assert result["broadcast_id"] == bid
        assert result["agent_id"] == "acking-agent"

    def test_ack_increments_count(self, patch_db_path):
        bid = self._insert_broadcast(patch_db_path)
        ws_mod.tool_workspace_ack(broadcast_id=bid, agent="a1")
        conn = sqlite3.connect(str(patch_db_path))
        count = conn.execute(
            "SELECT ack_count FROM workspace_broadcasts WHERE id=?", (bid,)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_duplicate_ack_idempotent(self, patch_db_path):
        bid = self._insert_broadcast(patch_db_path)
        ws_mod.tool_workspace_ack(broadcast_id=bid, agent="a1")
        result2 = ws_mod.tool_workspace_ack(broadcast_id=bid, agent="a1")
        assert result2.get("ok") is True
        assert result2.get("already_acked") is True


# ---------------------------------------------------------------------------
# workspace_phi
# ---------------------------------------------------------------------------

class TestWorkspacePhi:
    def test_returns_ok_empty(self):
        result = ws_mod.tool_workspace_phi()
        assert result.get("ok") is True
        assert "phi_org" in result
        assert "ack_rate" in result
        assert "total_broadcasts" in result

    def test_phi_zero_with_no_broadcasts(self):
        result = ws_mod.tool_workspace_phi()
        assert result["ok"] is True
        assert result["total_broadcasts"] == 0
        assert result["phi_org"] == 0.0

    def test_phi_stores_snapshot(self, patch_db_path):
        ws_mod.tool_workspace_phi()
        conn = sqlite3.connect(str(patch_db_path))
        count = conn.execute("SELECT COUNT(*) FROM workspace_phi").fetchone()[0]
        conn.close()
        assert count >= 1

    def test_breakdown_field_present_when_requested(self, patch_db_path):
        mid = _add_memory(patch_db_path)
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'phi-agent', 0.8, 's', 'global', 'manual')", (mid,)
        )
        conn.commit()
        conn.close()
        result = ws_mod.tool_workspace_phi(breakdown=True)
        assert result["ok"] is True
        assert "agent_breakdown" in result
        assert isinstance(result["agent_breakdown"], list)

    def test_breakdown_not_present_by_default(self):
        result = ws_mod.tool_workspace_phi()
        assert "agent_breakdown" not in result

    def test_warn_flag_false_when_no_broadcasts(self):
        result = ws_mod.tool_workspace_phi()
        assert result["warn"] is False


# ---------------------------------------------------------------------------
# workspace_ingest
# ---------------------------------------------------------------------------

class TestWorkspaceIngest:
    def test_returns_ok_empty(self):
        result = ws_mod.tool_workspace_ingest()
        assert result.get("ok") is True
        assert "scanned" in result
        assert "ignited" in result

    def test_dry_run_does_not_write(self, patch_db_path):
        # Insert a memory that will exceed the threshold with low config threshold
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('ignition_threshold', '0.01')"
        )
        conn.commit()
        conn.close()

        _add_memory(patch_db_path, category="decision", confidence=1.0)

        result = ws_mod.tool_workspace_ingest(dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True

        # Nothing should have been written
        conn = sqlite3.connect(str(patch_db_path))
        count = conn.execute("SELECT COUNT(*) FROM workspace_broadcasts").fetchone()[0]
        conn.close()
        assert count == 0

    def test_ingest_broadcasts_above_threshold(self, patch_db_path):
        # Set a very low threshold so the memory fires
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('ignition_threshold', '0.01')"
        )
        conn.commit()
        conn.close()

        _add_memory(patch_db_path, category="decision", confidence=1.0)

        result = ws_mod.tool_workspace_ingest(dry_run=False)
        assert result["ok"] is True
        assert result["ignited"] >= 1
        assert len(result["broadcasts"]) == result["ignited"]

        conn = sqlite3.connect(str(patch_db_path))
        count = conn.execute("SELECT COUNT(*) FROM workspace_broadcasts").fetchone()[0]
        conn.close()
        assert count >= 1

    def test_already_broadcast_not_re_ingested(self, patch_db_path):
        # Set low threshold
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('ignition_threshold', '0.01')"
        )
        conn.commit()
        conn.close()

        mid = _add_memory(patch_db_path, category="decision", confidence=1.0)

        # First ingest — should fire
        result1 = ws_mod.tool_workspace_ingest(dry_run=False)
        assert result1["ignited"] >= 1

        # Second ingest — already broadcast, should NOT fire again
        result2 = ws_mod.tool_workspace_ingest(dry_run=False)
        assert result2["ignited"] == 0

    def test_threshold_respected(self, patch_db_path):
        # Set a very high threshold — nothing should fire
        conn = sqlite3.connect(str(patch_db_path))
        conn.execute(
            "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('ignition_threshold', '0.99')"
        )
        conn.commit()
        conn.close()

        _add_memory(patch_db_path, category="preference", confidence=0.1)

        result = ws_mod.tool_workspace_ingest(dry_run=False)
        assert result["ok"] is True
        assert result["ignited"] == 0


# ---------------------------------------------------------------------------
# Helper: _ws_compute_salience
# ---------------------------------------------------------------------------

class TestComputeSalience:
    def test_decision_high_confidence_global(self):
        s = ws_mod._ws_compute_salience("decision", 1.0, "global")
        # 0.30 (decision) + 0.50 (confidence) + 0.10 (global) = 0.90
        assert abs(s - 0.90) < 1e-4

    def test_capped_at_one(self):
        s = ws_mod._ws_compute_salience("decision", 1.0, "global", '["critical"]')
        assert s <= 1.0

    def test_unknown_category_uses_default_weight(self):
        s = ws_mod._ws_compute_salience("totally_unknown", 0.0, "global")
        # 0.10 (default) + 0.0 (confidence) + 0.10 (global) = 0.20
        assert abs(s - 0.20) < 1e-4

    def test_critical_tag_boosts_salience(self):
        base = ws_mod._ws_compute_salience("preference", 0.5, "global")
        boosted = ws_mod._ws_compute_salience("preference", 0.5, "global", '["critical"]')
        assert boosted > base
