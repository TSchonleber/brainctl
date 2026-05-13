"""Tests for procedural MCP tool module."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_procedural as pt


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(pt, "DB_PATH", db_file)
    return db_file


class TestExports:
    def test_tools_and_dispatch_exposed(self):
        names = {tool.name for tool in pt.TOOLS}
        assert "procedure_add" in names
        assert "procedure_search" in names
        assert "procedure_feedback" in names
        assert "procedure_backfill" in names
        assert "procedure_stats" in names
        assert "procedure_add" in pt.DISPATCH
        assert callable(pt.DISPATCH["procedure_add"])


class TestProceduralTools:
    def test_add_get_search_feedback_cycle(self):
        add = pt.tool_procedure_add(
            agent_id="test-agent",
            goal="Deploy to staging safely",
            title="Staging deploy",
            description="Run tests, apply migrations, deploy, verify health checks.",
            steps=["Run tests", "Apply migrations", "Deploy", "Verify health checks"],
            tools=["pytest", "brainctl", "deployctl"],
        )
        assert add["ok"] is True

        fetched = pt.tool_procedure_get(procedure_id=add["id"])
        assert fetched["ok"] is True
        assert fetched["title"] == "Staging deploy"

        search = pt.tool_procedure_search(query="How do I deploy to staging?", limit=5)
        assert search["ok"] is True
        assert search["procedures"]
        assert search["procedures"][0]["title"] == "Staging deploy"

        feedback = pt.tool_procedure_feedback(
            agent_id="test-agent",
            procedure_id=add["id"],
            success=True,
            usefulness_score=0.8,
            validated=True,
        )
        assert feedback["ok"] is True
        assert feedback["execution_count"] == 1

    def test_backfill_and_stats(self):
        brain = Brain(db_path=str(pt.DB_PATH), agent_id="test-agent")
        brain.remember(
            "Rollback checklist: first pause deploys, then redeploy the previous release, finally verify health checks.",
            category="lesson",
        )
        brain.close()

        backfill = pt.tool_procedure_backfill(agent_id="test-agent", limit=20)
        stats = pt.tool_procedure_stats()

        assert backfill["ok"] is True
        assert stats["ok"] is True
        assert stats["total"] >= 1
