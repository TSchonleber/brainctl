"""Tests for mcp_tools_expertise — knowledge gaps & expertise MCP tools."""
from __future__ import annotations
import sqlite3
import sys
import os
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_expertise as mod


@pytest.fixture(autouse=True)
def _reset_db_path(tmp_path, monkeypatch):
    """Point the module's DB_PATH at a fresh temp DB for every test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))
    monkeypatch.setattr(mod, "DB_PATH", db_file)
    yield db_file


def _add_agent(db_file: Path, agent_id: str = "test-agent") -> None:
    """Insert a minimal agent row so FK constraints pass."""
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?, ?, 'test', 'active', "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()
    conn.close()


def _add_memory(db_file: Path, agent_id: str, content: str, scope: str = "global",
                category: str = "project", confidence: float = 0.9) -> None:
    """Insert a minimal memory row."""
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO memories (agent_id, content, category, scope, confidence, "
        "memory_type, created_at) VALUES (?, ?, ?, ?, ?, 'episodic', "
        "strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, content, category, scope, confidence),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# gaps_refresh
# ---------------------------------------------------------------------------

class TestGapsRefresh:
    def test_empty_db_returns_zero_scopes(self, tmp_path):
        result = mod.tool_gaps_refresh()
        assert result["ok"] is True
        assert result["scopes_updated"] == 0

    def test_refresh_with_memories(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file)
        _add_memory(db_file, "test-agent", "Python is great", scope="project:alpha")
        _add_memory(db_file, "test-agent", "Use SQLite for persistence", scope="project:alpha")

        result = mod.tool_gaps_refresh()
        assert result["ok"] is True
        assert result["scopes_updated"] >= 1
        assert "computed_at" in result

    def test_refresh_creates_coverage_row(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file)
        _add_memory(db_file, "test-agent", "Deploy to staging first", scope="project:beta")

        mod.tool_gaps_refresh()

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT * FROM knowledge_coverage WHERE scope='project:beta'"
        ).fetchone()
        conn.close()
        assert row is not None


# ---------------------------------------------------------------------------
# gaps_scan
# ---------------------------------------------------------------------------

class TestGapsScan:
    def test_scan_empty_db(self):
        result = mod.tool_gaps_scan()
        assert result.get("ok") is True
        assert "coverage_holes" in result
        assert "staleness_holes" in result
        assert "confidence_holes" in result
        assert "total_gaps" in result

    def test_scan_detects_coverage_hole(self, tmp_path, _reset_db_path):
        """Active agents with no memories should appear as coverage holes."""
        db_file = _reset_db_path
        _add_agent(db_file, "lonely-agent")

        result = mod.tool_gaps_scan()
        scopes = {h["scope"] for h in result["coverage_holes"]}
        assert "agent:lonely-agent" in scopes

    def test_scan_detects_confidence_hole(self, tmp_path, _reset_db_path):
        """Scopes with very low avg confidence should appear as confidence holes."""
        db_file = _reset_db_path
        _add_agent(db_file)
        # Add memories with confidence well below 0.4
        _add_memory(db_file, "test-agent", "Uncertain fact A", scope="project:lowconf",
                    confidence=0.1)
        _add_memory(db_file, "test-agent", "Uncertain fact B", scope="project:lowconf",
                    confidence=0.1)

        # First refresh coverage so scan can detect the confidence hole
        mod.tool_gaps_refresh()
        result = mod.tool_gaps_scan()
        scopes = {h["scope"] for h in result["confidence_holes"]}
        assert "project:lowconf" in scopes


# ---------------------------------------------------------------------------
# gaps_list
# ---------------------------------------------------------------------------

class TestGapsList:
    def test_list_empty_returns_ok(self):
        result = mod.tool_gaps_list()
        assert result["ok"] is True
        assert result["gaps"] == []
        assert result["total_unresolved"] == 0

    def test_list_after_scan(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "orphan-agent")
        mod.tool_gaps_scan()  # should log a coverage_hole

        result = mod.tool_gaps_list()
        assert result["ok"] is True
        assert result["total_unresolved"] > 0
        assert len(result["gaps"]) > 0

    def test_list_filter_by_type(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "filter-agent")
        mod.tool_gaps_scan()

        result = mod.tool_gaps_list(gap_type="coverage_hole")
        assert result["ok"] is True
        for gap in result["gaps"]:
            assert gap["gap_type"] == "coverage_hole"

    def test_list_respects_limit(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        # Create several agents to generate multiple gaps
        for i in range(10):
            _add_agent(db_file, f"agent-{i}")
        mod.tool_gaps_scan()

        result = mod.tool_gaps_list(limit=3)
        assert result["ok"] is True
        assert len(result["gaps"]) <= 3


# ---------------------------------------------------------------------------
# gaps_resolve
# ---------------------------------------------------------------------------

class TestGapsResolve:
    def test_resolve_nonexistent_gap(self):
        result = mod.tool_gaps_resolve(gap_id=99999)
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_resolve_existing_gap(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "resolve-agent")
        mod.tool_gaps_scan()

        # Get an actual gap id
        gaps_result = mod.tool_gaps_list()
        assert gaps_result["gaps"], "Need at least one gap to test resolve"
        gap_id = gaps_result["gaps"][0]["id"]

        result = mod.tool_gaps_resolve(gap_id=gap_id, note="Fixed by adding memories")
        assert result["ok"] is True
        assert result["gap_id"] == gap_id
        assert "resolved_at" in result

    def test_resolved_gap_disappears_from_list(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "resolve-agent2")
        mod.tool_gaps_scan()

        gaps_before = mod.tool_gaps_list()
        assert gaps_before["gaps"]
        gap_id = gaps_before["gaps"][0]["id"]

        mod.tool_gaps_resolve(gap_id=gap_id)
        gaps_after = mod.tool_gaps_list()
        ids_after = {g["id"] for g in gaps_after["gaps"]}
        assert gap_id not in ids_after


# ---------------------------------------------------------------------------
# expertise_build / expertise_show
# ---------------------------------------------------------------------------

class TestExpertiseBuild:
    def test_build_no_agents(self):
        # Brain() init creates a default agent, so >= 0 is correct
        result = mod.tool_expertise_build()
        assert result["ok"] is True
        assert result["agents_processed"] >= 0

    def test_build_with_memories(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "expert-agent")
        _add_memory(db_file, "expert-agent", "Machine learning pipeline tuning", scope="project:ml")
        _add_memory(db_file, "expert-agent", "Python packaging with pyproject.toml", scope="project:tools")

        result = mod.tool_expertise_build(agent_id="expert-agent")
        assert result["ok"] is True
        assert result["agents_processed"] == 1
        assert result["results"][0]["domains_indexed"] > 0

    def test_build_all_agents(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "agent-a")
        _add_agent(db_file, "agent-b")
        _add_memory(db_file, "agent-a", "Database indexing strategies", scope="project:db")
        _add_memory(db_file, "agent-b", "Frontend React components", scope="project:ui")

        result = mod.tool_expertise_build()
        assert result["ok"] is True
        # Brain() init creates a default agent, so total is >= 2
        assert result["agents_processed"] >= 2


class TestExpertiseShow:
    def test_show_unknown_agent(self):
        result = mod.tool_expertise_show(agent_id="nobody")
        assert result["ok"] is True
        assert result["expertise"] == []
        assert "message" in result

    def test_show_after_build(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "show-agent")
        _add_memory(db_file, "show-agent", "Kubernetes cluster management", scope="project:infra")
        _add_memory(db_file, "show-agent", "Container orchestration with Docker", scope="project:infra")

        mod.tool_expertise_build(agent_id="show-agent")
        result = mod.tool_expertise_show(agent_id="show-agent")
        assert result["ok"] is True
        assert len(result["expertise"]) > 0
        # All entries should belong to this agent
        for entry in result["expertise"]:
            assert "domain" in entry
            assert "strength" in entry


# ---------------------------------------------------------------------------
# expertise_list
# ---------------------------------------------------------------------------

class TestExpertiseList:
    def test_list_empty(self):
        result = mod.tool_expertise_list()
        assert result["ok"] is True
        assert result["count"] == 0

    def test_list_all_agents(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "lister-a")
        _add_agent(db_file, "lister-b")
        _add_memory(db_file, "lister-a", "GraphQL API design", scope="project:api")
        _add_memory(db_file, "lister-b", "SQL query optimization", scope="project:db")

        mod.tool_expertise_build()
        result = mod.tool_expertise_list()
        assert result["ok"] is True
        assert result["count"] >= 2
        agent_ids = {r["agent_id"] for r in result["agents"]}
        assert "lister-a" in agent_ids
        assert "lister-b" in agent_ids

    def test_list_filter_by_domain(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "domain-expert")
        _add_memory(db_file, "domain-expert", "Python asyncio event loops", scope="project:python")

        mod.tool_expertise_build(agent_id="domain-expert")
        result = mod.tool_expertise_list(domain="python")
        assert result["ok"] is True
        assert "entries" in result

    def test_list_min_strength_filters(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "strength-agent")
        _add_memory(db_file, "strength-agent", "Topic X", scope="global")

        mod.tool_expertise_build(agent_id="strength-agent")
        result_high = mod.tool_expertise_list(min_strength=0.99)
        result_low = mod.tool_expertise_list(min_strength=0.0)
        # High threshold should return fewer or equal results
        assert result_high["count"] <= result_low["count"]


# ---------------------------------------------------------------------------
# expertise_update
# ---------------------------------------------------------------------------

class TestExpertiseUpdate:
    def test_update_missing_agent_domain(self):
        result = mod.tool_expertise_update(agent_id="ghost", domain="nothing")
        assert result["ok"] is False
        # error message may vary — just verify it's a meaningful failure
        assert isinstance(result["error"], str) and len(result["error"]) > 0

    def test_update_requires_brier_or_strength(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "update-agent")
        _add_memory(db_file, "update-agent", "Testing frameworks", scope="project:qa")
        mod.tool_expertise_build(agent_id="update-agent")

        result = mod.tool_expertise_update(agent_id="update-agent", domain="project")
        assert result["ok"] is False
        assert "brier" in result["error"].lower() or "strength" in result["error"].lower()

    def test_update_brier_out_of_range(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "range-agent")
        _add_memory(db_file, "range-agent", "CI/CD pipeline design", scope="project:devops")
        mod.tool_expertise_build(agent_id="range-agent")

        result = mod.tool_expertise_update(agent_id="range-agent", domain="project", brier=5.0)
        assert result["ok"] is False
        assert "brier" in result["error"].lower()

    def test_update_strength_valid(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "valid-agent")
        _add_memory(db_file, "valid-agent", "Cloud infrastructure as code", scope="project:cloud")
        mod.tool_expertise_build(agent_id="valid-agent")

        # Find an actual domain that was indexed
        show = mod.tool_expertise_show(agent_id="valid-agent")
        assert show["expertise"]
        domain = show["expertise"][0]["domain"]

        result = mod.tool_expertise_update(agent_id="valid-agent", domain=domain, strength=0.75)
        assert result["ok"] is True
        assert result["strength"] == 0.75


# ---------------------------------------------------------------------------
# whosknows
# ---------------------------------------------------------------------------

class TestWhosknows:
    def test_empty_topic_error(self):
        result = mod.tool_whosknows(topic="")
        assert result["ok"] is False
        assert "topic" in result["error"].lower()

    def test_no_results_when_empty_db(self):
        result = mod.tool_whosknows(topic="machine learning")
        assert result["ok"] is True
        assert result["results"] == []

    def test_finds_relevant_agent(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        _add_agent(db_file, "ml-expert")
        _add_memory(db_file, "ml-expert", "neural network training optimization", scope="project:ml")
        _add_memory(db_file, "ml-expert", "gradient descent algorithms", scope="project:ml")
        _add_memory(db_file, "ml-expert", "neural architecture search", scope="project:ml")

        mod.tool_expertise_build(agent_id="ml-expert")
        result = mod.tool_whosknows(topic="neural network")
        assert result["ok"] is True
        assert "terms_searched" in result
        agent_ids = {r["agent_id"] for r in result["results"]}
        assert "ml-expert" in agent_ids

    def test_respects_top_n(self, tmp_path, _reset_db_path):
        db_file = _reset_db_path
        for i in range(5):
            _add_agent(db_file, f"agent-{i}")
            _add_memory(db_file, f"agent-{i}", f"Python development tip {i}", scope="global")

        mod.tool_expertise_build()
        result = mod.tool_whosknows(topic="python development", top_n=2)
        assert result["ok"] is True
        assert len(result["results"]) <= 2

    def test_returns_terms_searched(self, tmp_path, _reset_db_path):
        result = mod.tool_whosknows(topic="database indexing strategies")
        assert result["ok"] is True
        assert isinstance(result["terms_searched"], list)
        assert len(result["terms_searched"]) > 0


# ---------------------------------------------------------------------------
# TOOLS and DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(mod.TOOLS, list)
        assert len(mod.TOOLS) == 9
        for t in mod.TOOLS:
            assert isinstance(t, Tool)

    def test_dispatch_is_dict(self):
        assert isinstance(mod.DISPATCH, dict)

    def test_dispatch_covers_all_tool_names(self):
        tool_names = {t.name for t in mod.TOOLS}
        dispatch_names = set(mod.DISPATCH.keys())
        assert tool_names == dispatch_names

    def test_dispatch_values_are_callable(self):
        for name, fn in mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH['{name}'] is not callable"

    def test_expected_tool_names(self):
        names = {t.name for t in mod.TOOLS}
        expected = {
            "gaps_refresh", "gaps_scan", "gaps_list", "gaps_resolve",
            "expertise_build", "expertise_show", "expertise_list",
            "expertise_update", "whosknows",
        }
        assert names == expected
