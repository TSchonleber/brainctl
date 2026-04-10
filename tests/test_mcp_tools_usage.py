"""Tests for mcp_tools_usage -- LLM usage tracking & rate limiting."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_usage as usage_mod


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the module at a fresh temp DB for every test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))  # initialise schema (runs init_schema.sql)
    monkeypatch.setattr(usage_mod, "DB_PATH", db_file)
    return db_file


def _insert_agent(db_path: Path, agent_id: str = "test-agent") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?, ?, 'test', 'active', "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        assert isinstance(usage_mod.TOOLS, list)

    def test_tools_has_five_entries(self):
        assert len(usage_mod.TOOLS) == 5

    def test_dispatch_is_dict(self):
        assert isinstance(usage_mod.DISPATCH, dict)

    def test_tool_names_match_dispatch_keys(self):
        tool_names = {t.name for t in usage_mod.TOOLS}
        dispatch_keys = set(usage_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_all_expected_names_present(self):
        expected = {"usage_log", "usage_summary", "usage_check", "budget_set", "usage_fleet"}
        actual = {t.name for t in usage_mod.TOOLS}
        assert actual == expected

    def test_each_tool_has_input_schema(self):
        for tool in usage_mod.TOOLS:
            assert hasattr(tool, "inputSchema"), f"{tool.name} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"

    def test_usage_log_schema_requires_model(self):
        tool = next(t for t in usage_mod.TOOLS if t.name == "usage_log")
        assert "model" in tool.inputSchema["required"]

    def test_budget_set_schema_requires_monthly_limit(self):
        tool = next(t for t in usage_mod.TOOLS if t.name == "budget_set")
        assert "monthly_limit_usd" in tool.inputSchema["required"]


# ---------------------------------------------------------------------------
# usage_log
# ---------------------------------------------------------------------------

class TestUsageLog:
    def test_writes_row_correctly(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        result = usage_mod.tool_usage_log(
            agent_id="test-agent",
            model="claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.001,
            tool_name="memory_recall",
            project="brainctl",
        )
        assert result["ok"] is True
        assert result["total_tokens"] == 150
        assert result["cost_usd"] == 0.001
        assert "id" in result

        # Verify in DB
        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM llm_usage_log WHERE id = ?", (result["id"],)).fetchone()
        conn.close()
        assert row is not None
        assert row["model"] == "claude-sonnet-4-6"
        assert row["prompt_tokens"] == 100
        assert row["completion_tokens"] == 50
        assert row["total_tokens"] == 150
        assert row["tool_name"] == "memory_recall"
        assert row["project"] == "brainctl"

    def test_no_project_is_fine(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        result = usage_mod.tool_usage_log(
            agent_id="test-agent",
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=100,
        )
        assert result["ok"] is True
        assert result["total_tokens"] == 300

    def test_cost_defaults_to_zero(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        result = usage_mod.tool_usage_log(
            agent_id="test-agent",
            model="gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=5,
        )
        assert result["ok"] is True
        assert result["cost_usd"] == 0.0

    def test_model_required(self, isolated_db):
        result = usage_mod.tool_usage_log(agent_id="test-agent", model="")
        assert result["ok"] is False
        assert "model" in result["error"].lower()

    def test_multiple_calls_accumulate(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        for _ in range(3):
            result = usage_mod.tool_usage_log(
                agent_id="test-agent", model="claude-sonnet-4-6",
                prompt_tokens=100, completion_tokens=50, cost_usd=0.01,
            )
            assert result["ok"] is True

        conn = sqlite3.connect(str(isolated_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM llm_usage_log WHERE agent_id = 'test-agent'"
        ).fetchone()[0]
        conn.close()
        assert count == 3

    def test_dispatch_usage_log(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        fn = usage_mod.DISPATCH["usage_log"]
        result = fn(
            agent_id="test-agent",
            model="claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.001,
        )
        assert result["ok"] is True
        assert result["total_tokens"] == 150


# ---------------------------------------------------------------------------
# usage_summary
# ---------------------------------------------------------------------------

class TestUsageSummary:
    def test_returns_correct_totals(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.01,
        )
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="gpt-4o",
            prompt_tokens=200, completion_tokens=100, cost_usd=0.02,
        )
        result = usage_mod.tool_usage_summary(agent_id="test-agent")
        assert result["ok"] is True
        assert result["total_tokens"] == 450
        assert abs(result["total_cost_usd"] - 0.03) < 1e-9
        assert result["call_count"] == 2

    def test_returns_zeros_for_empty_month(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        result = usage_mod.tool_usage_summary(agent_id="test-agent", month="2020-01")
        assert result["ok"] is True
        assert result["total_tokens"] == 0
        assert result["total_cost_usd"] == 0.0
        assert result["call_count"] == 0
        assert result["by_model"] == {}

    def test_explicit_month_param(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        # Log will be in current month, so querying a different month gives zero
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.01,
        )
        result = usage_mod.tool_usage_summary(agent_id="test-agent", month="1999-12")
        assert result["ok"] is True
        assert result["call_count"] == 0
        assert result["month"] == "1999-12"

    def test_by_model_breakdown(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.01,
        )
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="gpt-4o",
            prompt_tokens=200, completion_tokens=100, cost_usd=0.02,
        )
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=50, completion_tokens=25, cost_usd=0.005,
        )
        result = usage_mod.tool_usage_summary(agent_id="test-agent")
        assert result["ok"] is True
        by_model = result["by_model"]
        assert "claude-sonnet-4-6" in by_model
        assert "gpt-4o" in by_model
        assert by_model["claude-sonnet-4-6"]["calls"] == 2
        assert by_model["gpt-4o"]["calls"] == 1
        assert by_model["claude-sonnet-4-6"]["tokens"] == 225  # (100+50) + (50+25)


# ---------------------------------------------------------------------------
# usage_check
# ---------------------------------------------------------------------------

class TestUsageCheck:
    def test_green_under_threshold(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        # Set budget: $100, alert at 80%, hard at 100%
        usage_mod.tool_budget_set(
            agent_id="test-agent", monthly_limit_usd=100.0,
            alert_threshold=0.8, hard_limit=1.0,
        )
        # Log $10 spend
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=1000, completion_tokens=500, cost_usd=10.0,
        )
        result = usage_mod.tool_usage_check(agent_id="test-agent")
        assert result["ok"] is True
        assert result["status"] == "green"
        assert result["allowed"] is True
        assert result["pct_used"] == 0.1

    def test_warning_between_thresholds(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        usage_mod.tool_budget_set(
            agent_id="test-agent", monthly_limit_usd=100.0,
            alert_threshold=0.8, hard_limit=1.0,
        )
        # Log $85 spend (85% > 80% alert, < 100% hard)
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=1000, completion_tokens=500, cost_usd=85.0,
        )
        result = usage_mod.tool_usage_check(agent_id="test-agent")
        assert result["ok"] is True
        assert result["status"] == "warning"
        assert result["allowed"] is True

    def test_blocked_over_hard_limit(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        usage_mod.tool_budget_set(
            agent_id="test-agent", monthly_limit_usd=100.0,
            alert_threshold=0.8, hard_limit=1.0,
        )
        # Log $105 spend (105% > 100% hard)
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=1000, completion_tokens=500, cost_usd=105.0,
        )
        result = usage_mod.tool_usage_check(agent_id="test-agent")
        assert result["ok"] is True
        assert result["status"] == "blocked"
        assert result["allowed"] is False

    def test_green_with_no_budget_set(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        # No budget row -> default $10, 0 spend = green
        result = usage_mod.tool_usage_check(agent_id="test-agent")
        assert result["ok"] is True
        assert result["status"] == "green"
        assert result["allowed"] is True
        assert result["limit_usd"] == 10.0
        assert result["current_spend_usd"] == 0.0

    def test_check_after_budget_set_round_trip(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        usage_mod.tool_budget_set(
            agent_id="test-agent", monthly_limit_usd=50.0,
            alert_threshold=0.5, hard_limit=0.9,
        )
        usage_mod.tool_usage_log(
            agent_id="test-agent", model="claude-sonnet-4-6",
            prompt_tokens=100, completion_tokens=50, cost_usd=30.0,
        )
        result = usage_mod.tool_usage_check(agent_id="test-agent")
        assert result["ok"] is True
        assert result["limit_usd"] == 50.0
        # 30/50 = 0.6 which is >= 0.5 alert but < 0.9 hard
        assert result["status"] == "warning"
        assert result["pct_used"] == 0.6


# ---------------------------------------------------------------------------
# budget_set
# ---------------------------------------------------------------------------

class TestBudgetSet:
    def test_creates_budget(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        result = usage_mod.tool_budget_set(
            agent_id="test-agent", monthly_limit_usd=50.0,
        )
        assert result["ok"] is True
        assert result["agent_id"] == "test-agent"
        assert result["monthly_limit_usd"] == 50.0

    def test_updates_existing_budget(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        usage_mod.tool_budget_set(agent_id="test-agent", monthly_limit_usd=50.0)
        result = usage_mod.tool_budget_set(agent_id="test-agent", monthly_limit_usd=100.0)
        assert result["ok"] is True
        assert result["monthly_limit_usd"] == 100.0

        # Verify single row
        conn = sqlite3.connect(str(isolated_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_budget WHERE agent_id = 'test-agent'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_custom_reset_day(self, isolated_db):
        _insert_agent(isolated_db, "test-agent")
        result = usage_mod.tool_budget_set(
            agent_id="test-agent", monthly_limit_usd=25.0, reset_day=15,
        )
        assert result["ok"] is True

        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT reset_day FROM agent_budget WHERE agent_id = 'test-agent'"
        ).fetchone()
        conn.close()
        assert row["reset_day"] == 15


# ---------------------------------------------------------------------------
# usage_fleet
# ---------------------------------------------------------------------------

class TestUsageFleet:
    def test_fleet_totals(self, isolated_db):
        for aid in ("agent-a", "agent-b"):
            _insert_agent(isolated_db, aid)
        usage_mod.tool_usage_log(
            agent_id="agent-a", model="claude-sonnet-4-6",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.10,
        )
        usage_mod.tool_usage_log(
            agent_id="agent-b", model="gpt-4o",
            prompt_tokens=200, completion_tokens=100, cost_usd=0.20,
        )
        result = usage_mod.tool_usage_fleet()
        assert result["ok"] is True
        assert abs(result["fleet_total_usd"] - 0.30) < 1e-9
        assert result["fleet_total_tokens"] == 450

    def test_top_agents_sorted_by_spend(self, isolated_db):
        for aid in ("agent-a", "agent-b", "agent-c"):
            _insert_agent(isolated_db, aid)
        usage_mod.tool_usage_log(
            agent_id="agent-a", model="m", prompt_tokens=10, completion_tokens=5, cost_usd=0.01,
        )
        usage_mod.tool_usage_log(
            agent_id="agent-b", model="m", prompt_tokens=10, completion_tokens=5, cost_usd=0.50,
        )
        usage_mod.tool_usage_log(
            agent_id="agent-c", model="m", prompt_tokens=10, completion_tokens=5, cost_usd=0.10,
        )
        result = usage_mod.tool_usage_fleet()
        assert result["ok"] is True
        agents = result["agents"]
        costs = [a["total_cost_usd"] for a in agents]
        assert costs == sorted(costs, reverse=True)
        assert agents[0]["agent_id"] == "agent-b"

    def test_fleet_empty_db(self, isolated_db):
        result = usage_mod.tool_usage_fleet()
        assert result["ok"] is True
        assert result["fleet_total_usd"] == 0.0
        assert result["fleet_total_tokens"] == 0
        assert result["agents"] == []

    def test_fleet_explicit_month(self, isolated_db):
        result = usage_mod.tool_usage_fleet(month="2020-01")
        assert result["ok"] is True
        assert result["month"] == "2020-01"
        assert result["agents"] == []
