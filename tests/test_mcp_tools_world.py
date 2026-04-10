"""Tests for mcp_tools_world — world model MCP tool implementations."""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_world as world_mod


@pytest.fixture(autouse=True)
def _patch_db_path(tmp_path, monkeypatch):
    """Point module DB_PATH at a temp database for every test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))  # initialises schema
    monkeypatch.setattr(world_mod, "DB_PATH", db_file)
    return db_file


def _seed_agent(db_file: Path, agent_id: str = "test-agent") -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?, ?, 'test', 'active', "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()
    conn.close()


def _seed_event(db_file: Path, agent_id: str, project: str, event_type: str = "result",
                importance: float = 0.5) -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO events (agent_id, event_type, summary, project, importance, created_at) "
        "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, event_type, f"test event: {event_type}", project, importance),
    )
    conn.commit()
    conn.close()


def _seed_expertise(db_file: Path, agent_id: str, domain: str,
                    strength: float = 0.8, evidence_count: int = 5) -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT OR REPLACE INTO agent_expertise "
        "(agent_id, domain, strength, evidence_count) VALUES (?, ?, ?, ?)",
        (agent_id, domain, strength, evidence_count),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# TOOLS and DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(world_mod.TOOLS, list)
        assert all(isinstance(t, Tool) for t in world_mod.TOOLS)

    def test_dispatch_is_dict(self):
        assert isinstance(world_mod.DISPATCH, dict)

    def test_tool_names_match_dispatch(self):
        tool_names = {t.name for t in world_mod.TOOLS}
        assert tool_names == set(world_mod.DISPATCH.keys())

    def test_expected_tool_names_present(self):
        expected = {
            "world_rebuild_caps", "world_agent", "world_project",
            "world_status", "world_predict", "world_resolve",
        }
        tool_names = {t.name for t in world_mod.TOOLS}
        assert expected == tool_names


# ---------------------------------------------------------------------------
# world_predict + world_resolve round-trip
# ---------------------------------------------------------------------------

class TestPredictResolve:
    def test_predict_returns_snapshot_id(self, tmp_path):
        result = world_mod.tool_world_predict(
            subject="task-42",
            predicted="will complete by Friday",
        )
        assert result["ok"] is True
        assert isinstance(result["snapshot_id"], int)
        assert result["snapshot_id"] > 0
        assert result["subject"] == "task-42"

    def test_resolve_links_to_prediction(self, tmp_path):
        pred = world_mod.tool_world_predict(
            subject="task-99",
            predicted="will succeed",
        )
        snapshot_id = pred["snapshot_id"]

        res = world_mod.tool_world_resolve(
            snapshot_id=snapshot_id,
            actual="succeeded",
            error=0.05,
        )
        assert res["ok"] is True
        assert res["snapshot_id"] == snapshot_id
        assert "resolved_at" in res

    def test_resolve_nonexistent_snapshot(self, tmp_path):
        result = world_mod.tool_world_resolve(snapshot_id=999999, actual="done")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_predict_missing_subject(self):
        result = world_mod.tool_world_predict(predicted="something")
        assert result["ok"] is False

    def test_predict_missing_predicted(self):
        result = world_mod.tool_world_predict(subject="task-1")
        assert result["ok"] is False

    def test_resolve_actual_persisted(self, tmp_path, _patch_db_path):
        pred = world_mod.tool_world_predict(subject="s1", predicted="p1")
        world_mod.tool_world_resolve(snapshot_id=pred["snapshot_id"], actual="a1")

        conn = sqlite3.connect(str(_patch_db_path))
        row = conn.execute(
            "SELECT actual_state, resolved_at FROM world_model_snapshots WHERE id=?",
            (pred["snapshot_id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "a1"
        assert row[1] is not None


# ---------------------------------------------------------------------------
# world_status on empty DB
# ---------------------------------------------------------------------------

class TestWorldStatus:
    def test_status_empty_db(self):
        result = world_mod.tool_world_status()
        assert result["ok"] is True
        assert "snapshot_at" in result
        assert isinstance(result["active_agents"], list)
        assert isinstance(result["project_dynamics"], list)
        assert isinstance(result["capability_hotspots"], list)
        assert isinstance(result["capability_gaps"], list)
        assert "memory_health" in result
        assert isinstance(result["highlights"], list)

    def test_status_default_window(self):
        result = world_mod.tool_world_status()
        assert result["window_days"] == 7

    def test_status_custom_window(self):
        result = world_mod.tool_world_status(days=30)
        assert result["window_days"] == 30

    def test_status_includes_org_state(self):
        result = world_mod.tool_world_status()
        assert "org_state" in result
        # "unknown" returned when neuromodulation_state is missing; valid DB states listed in schema
        assert result["org_state"] in (
            "normal", "incident", "sprint", "strategic_planning", "focused_work", "unknown"
        )

    def test_status_shows_seeded_agent_activity(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "alpha-agent")
        _seed_event(_patch_db_path, "alpha-agent", "proj-x", importance=0.8)
        result = world_mod.tool_world_status(days=30)
        agent_ids = [r["agent_id"] for r in result["active_agents"]]
        assert "alpha-agent" in agent_ids


# ---------------------------------------------------------------------------
# world_project
# ---------------------------------------------------------------------------

class TestWorldProject:
    def test_missing_project_arg(self):
        result = world_mod.tool_world_project()
        assert result["ok"] is False

    def test_no_events_returns_ok_with_zero(self, tmp_path):
        result = world_mod.tool_world_project(project="nonexistent-project-xyz")
        assert result["ok"] is True
        assert result["total_events"] == 0

    def test_seeded_events_counted(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "worker")
        for i in range(3):
            _seed_event(_patch_db_path, "worker", "my-project", "result")
        _seed_event(_patch_db_path, "worker", "my-project", "error")

        result = world_mod.tool_world_project(project="my-project", days=30)
        assert result["ok"] is True
        assert result["total_events"] == 4
        assert result["event_type_counts"]["result"] == 3
        assert result["event_type_counts"]["error"] == 1

    def test_velocity_calculation(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "worker2")
        for i in range(7):
            _seed_event(_patch_db_path, "worker2", "vel-proj")
        result = world_mod.tool_world_project(project="vel-proj", days=7)
        assert result["ok"] is True
        assert abs(result["velocity_per_day"] - 1.0) < 0.01

    def test_block_rate_for_errors(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "bworker")
        _seed_event(_patch_db_path, "bworker", "fail-proj", "result")
        _seed_event(_patch_db_path, "bworker", "fail-proj", "error")
        result = world_mod.tool_world_project(project="fail-proj", days=30)
        assert result["ok"] is True
        assert abs(result["error_block_rate"] - 0.5) < 0.01


# ---------------------------------------------------------------------------
# world_agent
# ---------------------------------------------------------------------------

class TestWorldAgent:
    def test_missing_agent_arg(self):
        result = world_mod.tool_world_agent()
        assert result["ok"] is False

    def test_unknown_agent(self, tmp_path):
        result = world_mod.tool_world_agent(agent="ghost-agent-not-real")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_known_agent_no_caps(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "capless-agent")
        result = world_mod.tool_world_agent(agent="capless-agent")
        assert result["ok"] is True
        assert result["agent_id"] == "capless-agent"
        assert result["capabilities"] == []

    def test_known_agent_with_caps(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "skilled-agent")
        _seed_expertise(_patch_db_path, "skilled-agent", "memory")
        # Rebuild caps to populate agent_capabilities
        world_mod.tool_world_rebuild_caps(agent="skilled-agent")
        result = world_mod.tool_world_agent(agent="skilled-agent")
        assert result["ok"] is True
        assert len(result["capabilities"]) > 0
        cap_names = [c["capability"] for c in result["capabilities"]]
        assert "memory_ops" in cap_names


# ---------------------------------------------------------------------------
# world_rebuild_caps
# ---------------------------------------------------------------------------

class TestWorldRebuildCaps:
    def test_empty_db_no_active_agents(self):
        # Brain() init creates a default agent, so >= 0 active agents is correct
        result = world_mod.tool_world_rebuild_caps()
        assert result["ok"] is True
        assert result["agents_processed"] >= 0

    def test_rebuild_single_agent(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "rebuild-agent")
        _seed_expertise(_patch_db_path, "rebuild-agent", "sql")
        result = world_mod.tool_world_rebuild_caps(agent="rebuild-agent")
        assert result["ok"] is True
        assert result["agents_processed"] == 1
        assert result["results"][0]["agent_id"] == "rebuild-agent"
        assert result["results"][0]["capabilities_written"] > 0

    def test_rebuild_all_active_agents(self, tmp_path, _patch_db_path):
        for aid in ["agent-a", "agent-b"]:
            _seed_agent(_patch_db_path, aid)
            _seed_expertise(_patch_db_path, aid, "research")
        result = world_mod.tool_world_rebuild_caps()
        assert result["ok"] is True
        # Brain() creates a default agent so total >= 2 (the seeded ones)
        assert result["agents_processed"] >= 2

    def test_caps_persisted_in_db(self, tmp_path, _patch_db_path):
        _seed_agent(_patch_db_path, "persist-agent")
        _seed_expertise(_patch_db_path, "persist-agent", "temporal")
        world_mod.tool_world_rebuild_caps(agent="persist-agent")

        conn = sqlite3.connect(str(_patch_db_path))
        rows = conn.execute(
            "SELECT capability FROM agent_capabilities WHERE agent_id=?",
            ("persist-agent",),
        ).fetchall()
        conn.close()
        caps = [r[0] for r in rows]
        assert "temporal_reasoning" in caps
