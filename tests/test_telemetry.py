"""Tests for agentmemory.telemetry — unified health dashboard."""
from __future__ import annotations

from pathlib import Path

from agentmemory.brain import Brain
from agentmemory.telemetry import get_dashboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_GRADES = {"A", "B+", "B", "C", "D", "F"}


def _make_brain(tmp_path: Path, agent_id: str = "test-agent") -> Brain:
    db_file = tmp_path / f"brain_{agent_id}.db"
    return Brain(db_path=str(db_file), agent_id=agent_id)


# ---------------------------------------------------------------------------
# 1. health_score is a float between 0 and 1
# ---------------------------------------------------------------------------

class TestGetDashboardReturnsHealthScore:
    def test_get_dashboard_returns_health_score(self, tmp_path):
        brain = _make_brain(tmp_path)
        result = get_dashboard(str(brain.db_path))

        assert "health_score" in result
        score = result["health_score"]
        assert isinstance(score, float), f"health_score must be float, got {type(score)}"
        assert 0.0 <= score <= 1.0, f"health_score {score} out of [0,1] range"


# ---------------------------------------------------------------------------
# 2. grade is a valid letter grade
# ---------------------------------------------------------------------------

class TestGradePresent:
    def test_grade_present(self, tmp_path):
        brain = _make_brain(tmp_path)
        result = get_dashboard(str(brain.db_path))

        assert "grade" in result
        assert result["grade"] in VALID_GRADES, (
            f"grade '{result['grade']}' not in {VALID_GRADES}"
        )


# ---------------------------------------------------------------------------
# 3. memory section has required keys
# ---------------------------------------------------------------------------

class TestMemorySectionHasRequiredKeys:
    def test_memory_section_has_required_keys(self, tmp_path):
        brain = _make_brain(tmp_path)
        brain.remember("test memory", category="general")
        result = get_dashboard(str(brain.db_path))

        assert "memory" in result
        memory = result["memory"]
        for key in ("count", "active", "retired", "avg_confidence"):
            assert key in memory, f"memory section missing key: {key}"

    def test_memory_counts_are_non_negative(self, tmp_path):
        brain = _make_brain(tmp_path)
        result = get_dashboard(str(brain.db_path))

        m = result["memory"]
        assert m["count"] >= 0
        assert m["active"] >= 0
        assert m["retired"] >= 0
        assert 0.0 <= m["avg_confidence"] <= 1.0


# ---------------------------------------------------------------------------
# 4. alerts is a list
# ---------------------------------------------------------------------------

class TestAlertsIsList:
    def test_alerts_is_list(self, tmp_path):
        brain = _make_brain(tmp_path)
        result = get_dashboard(str(brain.db_path))

        assert "alerts" in result
        assert isinstance(result["alerts"], list), "alerts must be a list"

    def test_alerts_contains_strings(self, tmp_path):
        brain = _make_brain(tmp_path)
        result = get_dashboard(str(brain.db_path))

        for alert in result["alerts"]:
            assert isinstance(alert, str), f"alert entry is not a string: {alert!r}"


# ---------------------------------------------------------------------------
# 5. agent filter works — counts are agent-scoped
# ---------------------------------------------------------------------------

class TestAgentFilterWorks:
    def test_agent_filter_works(self, tmp_path):
        # Create two brains with the same DB but different agent IDs
        db_file = tmp_path / "shared_brain.db"
        brain_a = Brain(db_path=str(db_file), agent_id="agent-alpha")
        brain_b = Brain(db_path=str(db_file), agent_id="agent-beta")

        # agent-alpha gets 3 memories, agent-beta gets 1
        for i in range(3):
            brain_a.remember(f"Alpha memory {i}", category="general")
        brain_b.remember("Beta memory 0", category="general")

        result_a = get_dashboard(str(db_file), agent_id="agent-alpha")
        result_b = get_dashboard(str(db_file), agent_id="agent-beta")
        result_all = get_dashboard(str(db_file))

        # Filtered results should differ
        assert result_a["memory"]["count"] == 3
        assert result_b["memory"]["count"] == 1
        assert result_all["memory"]["count"] == 4


# ---------------------------------------------------------------------------
# 6. empty DB returns valid dashboard (no crash)
# ---------------------------------------------------------------------------

class TestEmptyDbReturnsOk:
    def test_empty_db_returns_ok(self, tmp_path):
        brain = _make_brain(tmp_path, agent_id="empty-agent")
        result = get_dashboard(str(brain.db_path))

        # Must return all required top-level keys
        for key in ("health_score", "grade", "memory", "events", "entities",
                    "decisions", "budget", "alerts", "computed_at"):
            assert key in result, f"Missing key in empty-DB dashboard: {key}"

        # Score still valid
        assert 0.0 <= result["health_score"] <= 1.0
        assert result["grade"] in VALID_GRADES

    def test_empty_db_memory_counts_zero(self, tmp_path):
        brain = _make_brain(tmp_path, agent_id="empty-agent")
        result = get_dashboard(str(brain.db_path))

        assert result["memory"]["count"] == 0
        assert result["memory"]["active"] == 0


# ---------------------------------------------------------------------------
# 7. MCP tool module is importable
# ---------------------------------------------------------------------------

class TestMcpToolImportable:
    def test_mcp_tool_importable(self):
        import agentmemory.mcp_tools_telemetry as tmod
        assert hasattr(tmod, "TOOLS")
        assert hasattr(tmod, "DISPATCH")
        assert hasattr(tmod, "tool_telemetry")

    def test_tools_list_is_non_empty(self):
        from agentmemory.mcp_tools_telemetry import TOOLS
        assert len(TOOLS) >= 1

    def test_telemetry_tool_name(self):
        from agentmemory.mcp_tools_telemetry import TOOLS
        names = {t.name for t in TOOLS}
        assert "telemetry" in names


# ---------------------------------------------------------------------------
# 8. MCP dispatch works
# ---------------------------------------------------------------------------

class TestMcpDispatchWorks:
    def test_mcp_dispatch_works(self, tmp_path, monkeypatch):
        import agentmemory.mcp_tools_telemetry as tmod

        brain = _make_brain(tmp_path)
        brain.remember("dispatch test memory", category="general")

        monkeypatch.setattr(tmod, "DB_PATH", brain.db_path)

        fn = tmod.DISPATCH["telemetry"]
        result = fn()

        assert result["ok"] is True
        assert "health_score" in result
        assert 0.0 <= result["health_score"] <= 1.0

    def test_mcp_dispatch_with_agent_filter(self, tmp_path, monkeypatch):
        import agentmemory.mcp_tools_telemetry as tmod

        brain = _make_brain(tmp_path, agent_id="dispatch-agent")
        brain.remember("dispatch filtered memory", category="general")

        monkeypatch.setattr(tmod, "DB_PATH", brain.db_path)

        fn = tmod.DISPATCH["telemetry"]
        result = fn(agent_id="dispatch-agent")

        assert result["ok"] is True
        assert result["memory"]["count"] == 1
