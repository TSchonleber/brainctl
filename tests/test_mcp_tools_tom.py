"""Tests for the Theory of Mind MCP tools."""
from __future__ import annotations
import sys
import os
import sqlite3
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_tom as tom_module
from agentmemory.mcp_tools_tom import (
    TOOLS, DISPATCH,
    tool_tom_update,
    tool_tom_belief_set,
    tool_tom_belief_invalidate,
    tool_tom_conflicts_list,
    tool_tom_conflicts_resolve,
    tool_tom_perspective_set,
    tool_tom_perspective_get,
    tool_tom_gap_scan,
    tool_tom_inject,
    tool_tom_status,
)
from mcp.types import Tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_db(tmp_path: Path) -> Path:
    """Create a Brain DB and point the module at it; return the db path."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="default")
    tom_module.DB_PATH = db_file
    return db_file


def _insert_agent(db_file: Path, agent_id: str) -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?, ?, 'test', 'active', "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list_of_tool(self, tmp_path):
        assert isinstance(TOOLS, list)
        assert len(TOOLS) == 10
        for t in TOOLS:
            assert isinstance(t, Tool)

    def test_dispatch_keys_match_tool_names(self, tmp_path):
        tool_names = {t.name for t in TOOLS}
        assert tool_names == set(DISPATCH.keys())

    def test_dispatch_values_are_callable(self, tmp_path):
        for name, fn in DISPATCH.items():
            assert callable(fn), f"{name} is not callable"


# ---------------------------------------------------------------------------
# tom_belief_set / tom_belief_invalidate
# ---------------------------------------------------------------------------

class TestBeliefSetAndInvalidate:
    def test_belief_set_creates_new(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-1")

        result = tool_tom_belief_set(
            agent_id="mcp-client",
            target_agent_id="agent-1",
            topic="project:foo:status",
            content="The project is on track",
        )
        assert result["ok"] is True
        assert result["action"] == "created"
        assert result["agent_id"] == "agent-1"

    def test_belief_set_updates_existing(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-1")

        tool_tom_belief_set(
            agent_id="mcp-client",
            target_agent_id="agent-1",
            topic="project:foo:status",
            content="Initial belief",
        )
        result = tool_tom_belief_set(
            agent_id="mcp-client",
            target_agent_id="agent-1",
            topic="project:foo:status",
            content="Updated belief",
        )
        assert result["ok"] is True
        assert result["action"] == "updated"

    def test_belief_set_missing_required_fields(self, tmp_path):
        _setup_db(tmp_path)
        result = tool_tom_belief_set(agent_id="mcp-client", target_agent_id="", topic="t", content="c")
        assert result["ok"] is False
        assert "target_agent_id" in result["error"]

    def test_belief_invalidate_creates_conflict(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-2")

        tool_tom_belief_set(
            agent_id="mcp-client",
            target_agent_id="agent-2",
            topic="task:ABC-1:status",
            content="In progress",
        )
        result = tool_tom_belief_invalidate(
            agent_id="mcp-client",
            target_agent_id="agent-2",
            topic="task:ABC-1:status",
            reason="Task was cancelled",
        )
        assert result["ok"] is True

        # Verify conflict was created
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM belief_conflicts WHERE agent_a_id=? AND topic=? AND resolved_at IS NULL",
            ("agent-2", "task:ABC-1:status"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["conflict_type"] == "staleness"

    def test_belief_invalidate_no_active_belief(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-2")

        result = tool_tom_belief_invalidate(
            agent_id="mcp-client",
            target_agent_id="agent-2",
            topic="nonexistent:topic",
            reason="some reason",
        )
        assert result["ok"] is False
        assert "No active belief" in result["error"]


# ---------------------------------------------------------------------------
# tom_conflicts_list / tom_conflicts_resolve
# ---------------------------------------------------------------------------

class TestConflicts:
    def _create_conflict(self, db_file: Path, agent_id: str, topic: str) -> int:
        """Helper to insert a conflict directly."""
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """INSERT INTO belief_conflicts
               (topic, agent_a_id, belief_a, belief_b, conflict_type, severity,
                detected_at, requires_supervisor_intervention)
               VALUES (?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S','now'),?)""",
            (topic, agent_id, "Belief A", "Belief B", "factual", 0.8, 1),
        )
        conn.commit()
        conflict_id = cur.lastrowid
        conn.close()
        return conflict_id

    def test_conflicts_list_returns_open(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-3")
        self._create_conflict(db, "agent-3", "topic:x")
        self._create_conflict(db, "agent-3", "topic:y")

        result = tool_tom_conflicts_list(agent_id="mcp-client")
        assert result["ok"] is True
        assert result["open_conflicts"] >= 2

    def test_conflicts_list_filter_by_agent(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-3")
        _insert_agent(db, "agent-4")
        self._create_conflict(db, "agent-3", "topic:a3")
        self._create_conflict(db, "agent-4", "topic:a4")

        result = tool_tom_conflicts_list(agent_id="mcp-client", filter_agent="agent-3")
        assert result["ok"] is True
        agents = {c["agent_a_id"] for c in result["conflicts"]}
        assert "agent-3" in agents
        assert "agent-4" not in agents

    def test_conflicts_resolve(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-3")
        cid = self._create_conflict(db, "agent-3", "topic:resolve")

        result = tool_tom_conflicts_resolve(
            agent_id="mcp-client",
            conflict_id=cid,
            resolution="Supervisor confirmed correct belief",
        )
        assert result["ok"] is True
        assert result["conflict_id"] == cid

        # Verify it no longer shows in open conflicts
        after = tool_tom_conflicts_list(agent_id="mcp-client", filter_agent="agent-3")
        open_ids = [c["id"] for c in after["conflicts"]]
        assert cid not in open_ids

    def test_conflicts_resolve_not_found(self, tmp_path):
        _setup_db(tmp_path)
        result = tool_tom_conflicts_resolve(
            agent_id="mcp-client", conflict_id=99999, resolution="whatever"
        )
        assert result["ok"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# tom_perspective_set / tom_perspective_get
# ---------------------------------------------------------------------------

class TestPerspective:
    def test_perspective_set_and_get(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "observer-1")
        _insert_agent(db, "subject-1")

        set_result = tool_tom_perspective_set(
            agent_id="mcp-client",
            observer="observer-1",
            subject="subject-1",
            topic="project:bar:status",
            belief="Subject thinks project is done",
            gap="Subject doesn't know about v2 requirements",
            confusion=0.75,
        )
        assert set_result["ok"] is True
        assert set_result["action"] == "created"

        get_result = tool_tom_perspective_get(
            agent_id="mcp-client",
            observer="observer-1",
            subject="subject-1",
        )
        assert get_result["ok"] is True
        assert len(get_result["perspective_models"]) == 1
        model = get_result["perspective_models"][0]
        assert model["topic"] == "project:bar:status"
        assert model["confusion_risk"] == 0.75

    def test_perspective_set_updates(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "observer-1")
        _insert_agent(db, "subject-1")

        tool_tom_perspective_set(
            agent_id="mcp-client",
            observer="observer-1",
            subject="subject-1",
            topic="some:topic",
            confusion=0.5,
        )
        result = tool_tom_perspective_set(
            agent_id="mcp-client",
            observer="observer-1",
            subject="subject-1",
            topic="some:topic",
            confusion=0.2,
        )
        assert result["ok"] is True
        assert result["action"] == "updated"

    def test_perspective_get_empty(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "observer-1")
        _insert_agent(db, "subject-1")

        result = tool_tom_perspective_get(
            agent_id="mcp-client", observer="observer-1", subject="subject-1"
        )
        assert result["ok"] is True
        assert result["perspective_models"] == []

    def test_perspective_get_missing_required(self, tmp_path):
        _setup_db(tmp_path)
        result = tool_tom_perspective_get(agent_id="mcp-client", observer="", subject="s")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# tom_gap_scan
# ---------------------------------------------------------------------------

class TestGapScan:
    def test_gap_scan_no_tasks(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-5")

        result = tool_tom_gap_scan(agent_id="mcp-client", target_agent_id="agent-5")
        assert result["ok"] is True
        assert result["gaps"] == []
        assert result["missing"] == 0

    def test_gap_scan_with_missing_belief(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-5")

        # Insert a task for agent-5 with no corresponding belief
        conn = sqlite3.connect(str(db))
        conn.execute(
            """INSERT INTO tasks (assigned_agent_id, title, status, priority,
               created_at, updated_at)
               VALUES ('agent-5', 'Fix bug XYZ', 'in_progress', 'high',
               strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"""
        )
        conn.commit()
        conn.close()

        result = tool_tom_gap_scan(agent_id="mcp-client", target_agent_id="agent-5")
        assert result["ok"] is True
        assert result["missing"] >= 1
        statuses = [g["status"] for g in result["gaps"]]
        assert "MISSING" in statuses

    def test_gap_scan_missing_required(self, tmp_path):
        _setup_db(tmp_path)
        result = tool_tom_gap_scan(agent_id="mcp-client", target_agent_id="")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# tom_inject
# ---------------------------------------------------------------------------

class TestInject:
    def test_inject_with_content(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "supervisor")
        _insert_agent(db, "worker")

        result = tool_tom_inject(
            agent_id="mcp-client",
            target_agent_id="worker",
            topic="project:foo:v2_requirements",
            content="v2 requires OAuth support and rate limiting",
            observer="supervisor",
        )
        assert result["ok"] is True
        assert result["memory_id"] is not None
        assert result["confusion_risk_after"] == 0.1

        # Verify belief was written
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        belief = conn.execute(
            "SELECT * FROM agent_beliefs WHERE agent_id=? AND topic=?",
            ("worker", "project:foo:v2_requirements"),
        ).fetchone()
        conn.close()
        assert belief is not None
        assert belief["confidence"] == 0.9

    def test_inject_uses_gap_from_perspective(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "supervisor")
        _insert_agent(db, "worker")

        # Set up a perspective model with a knowledge gap
        tool_tom_perspective_set(
            agent_id="mcp-client",
            observer="supervisor",
            subject="worker",
            topic="deploy:env:prod",
            gap="Worker doesn't know prod requires VPN access",
            confusion=0.9,
        )

        result = tool_tom_inject(
            agent_id="mcp-client",
            target_agent_id="worker",
            topic="deploy:env:prod",
            # No content — should pull from perspective gap
            observer="supervisor",
        )
        assert result["ok"] is True
        assert result["memory_id"] is not None

    def test_inject_no_content_no_gap_fails(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "worker")

        result = tool_tom_inject(
            agent_id="mcp-client",
            target_agent_id="worker",
            topic="nonexistent:topic",
        )
        assert result["ok"] is False
        assert "No content" in result["error"]

    def test_inject_missing_required(self, tmp_path):
        _setup_db(tmp_path)
        result = tool_tom_inject(agent_id="mcp-client", target_agent_id="", topic="t")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# tom_update / tom_status
# ---------------------------------------------------------------------------

class TestUpdateAndStatus:
    def test_update_specific_agent(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-x")

        result = tool_tom_update(agent_id="mcp-client", target_agent_id="agent-x")
        assert result["ok"] is True
        assert result["agents_updated"] == 1
        assert len(result["results"]) == 1
        bdi = result["results"][0]
        assert bdi["agent_id"] == "agent-x"
        assert "knowledge_coverage_score" in bdi

    def test_update_all_active_agents(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-a")
        _insert_agent(db, "agent-b")

        result = tool_tom_update(agent_id="mcp-client")
        assert result["ok"] is True
        assert result["agents_updated"] >= 2

    def test_status_after_update(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-y")

        tool_tom_update(agent_id="mcp-client", target_agent_id="agent-y")
        result = tool_tom_status(agent_id="mcp-client", target_agent_id="agent-y")
        assert result["ok"] is True
        assert len(result["agents"]) == 1
        assert result["agents"][0]["agent_id"] == "agent-y"

    def test_status_empty_before_update(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-y")

        result = tool_tom_status(agent_id="mcp-client", target_agent_id="agent-y")
        assert result["ok"] is True
        assert result["agents"] == []

    def test_status_all_agents(self, tmp_path):
        db = _setup_db(tmp_path)
        _insert_agent(db, "agent-p")
        _insert_agent(db, "agent-q")

        tool_tom_update(agent_id="mcp-client")
        result = tool_tom_status(agent_id="mcp-client")
        assert result["ok"] is True
        agent_ids = [a["agent_id"] for a in result["agents"]]
        assert "agent-p" in agent_ids
        assert "agent-q" in agent_ids


# ---------------------------------------------------------------------------
# ToM tables missing (graceful degradation)
# ---------------------------------------------------------------------------

class TestMissingTables:
    def test_graceful_when_no_tom_tables(self, tmp_path):
        """If brain.db has no ToM tables, tools return ok=False with helpful message."""
        db_file = tmp_path / "minimal.db"
        conn = sqlite3.connect(str(db_file))
        # Create minimal schema without ToM tables
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, content TEXT)"
        )
        conn.commit()
        conn.close()

        tom_module.DB_PATH = db_file

        result = tool_tom_belief_set(
            agent_id="mcp-client",
            target_agent_id="x",
            topic="t",
            content="c",
        )
        assert result["ok"] is False
        assert "Theory of Mind" in result["error"]
