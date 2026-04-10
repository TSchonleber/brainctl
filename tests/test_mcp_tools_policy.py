"""Tests for mcp_tools_policy — policy system MCP tools."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_policy as pt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Create a fresh brain.db for each test and point the module at it."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(pt, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH exports
# ---------------------------------------------------------------------------

class TestExports:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(pt.TOOLS, list)
        assert all(isinstance(t, Tool) for t in pt.TOOLS)

    def test_tool_names(self):
        names = {t.name for t in pt.TOOLS}
        assert names == {"policy_match", "policy_add", "policy_feedback", "policy_list"}

    def test_dispatch_is_dict(self):
        assert isinstance(pt.DISPATCH, dict)
        assert set(pt.DISPATCH.keys()) == {"policy_match", "policy_add", "policy_feedback", "policy_list"}

    def test_dispatch_values_are_callable(self):
        for name, fn in pt.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"


# ---------------------------------------------------------------------------
# policy_add
# ---------------------------------------------------------------------------

class TestPolicyAdd:
    def test_add_returns_policy_id(self):
        result = pt.tool_policy_add(
            agent_id="test",
            name="never-delete-prod",
            trigger="about to delete production data",
            directive="stop and ask for explicit confirmation",
        )
        assert result["ok"] is True
        assert result["policy_id"].startswith("pol_")
        assert result["name"] == "never-delete-prod"

    def test_add_missing_name_fails(self):
        result = pt.tool_policy_add(
            agent_id="test",
            name="",
            trigger="something",
            directive="do it",
        )
        assert result["ok"] is False
        assert "name" in result["error"].lower()

    def test_add_missing_trigger_fails(self):
        result = pt.tool_policy_add(
            agent_id="test",
            name="my-policy",
            trigger="",
            directive="do it",
        )
        assert result["ok"] is False
        assert "trigger" in result["error"].lower()

    def test_add_missing_directive_fails(self):
        result = pt.tool_policy_add(
            agent_id="test",
            name="my-policy",
            trigger="some trigger",
            directive="",
        )
        assert result["ok"] is False
        assert "directive" in result["error"].lower()

    def test_add_with_all_options(self):
        result = pt.tool_policy_add(
            agent_id="test",
            name="deploy-gate",
            trigger="deploying to production",
            directive="run full test suite first",
            category="workflow",
            scope="project:myapp",
            priority=80,
            confidence=0.9,
            half_life=14,
        )
        assert result["ok"] is True

    def test_add_created_at_in_result(self):
        result = pt.tool_policy_add(
            agent_id="test",
            name="check-tests",
            trigger="before merge",
            directive="verify all tests pass",
        )
        assert "created_at" in result
        assert result["created_at"]  # non-empty


# ---------------------------------------------------------------------------
# policy_list
# ---------------------------------------------------------------------------

class TestPolicyList:
    def _add(self, name, trigger="t", directive="d", **kw):
        return pt.tool_policy_add(
            agent_id="test", name=name, trigger=trigger, directive=directive, **kw
        )

    def test_list_empty_db(self):
        result = pt.tool_policy_list(agent_id="test")
        assert result["ok"] is True
        assert result["policies"] == []
        assert result["count"] == 0

    def test_list_returns_added_policies(self):
        self._add("pol-a")
        self._add("pol-b")
        result = pt.tool_policy_list(agent_id="test")
        assert result["ok"] is True
        assert result["count"] == 2
        names = {p["name"] for p in result["policies"]}
        assert {"pol-a", "pol-b"} == names

    def test_list_filter_by_category(self):
        self._add("safety-pol", category="safety")
        self._add("workflow-pol", category="workflow")
        result = pt.tool_policy_list(agent_id="test", category="safety")
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["policies"][0]["name"] == "safety-pol"

    def test_list_filter_by_scope(self):
        self._add("global-pol", scope="global")
        self._add("scoped-pol", scope="project:x")
        result = pt.tool_policy_list(agent_id="test", scope="project:x")
        assert result["ok"] is True
        # "global" policies are included alongside scoped ones
        assert result["count"] == 2

    def test_list_has_confidence_effective(self):
        self._add("conf-pol", confidence=0.8)
        result = pt.tool_policy_list(agent_id="test")
        assert result["ok"] is True
        pol = result["policies"][0]
        assert "confidence_effective" in pol
        # Freshly created — no decay, effective ~= initial
        assert pol["confidence_effective"] > 0.0

    def test_list_stale_flagged(self):
        # Create a policy and hammer it with failures
        add_result = self._add("fragile-policy", confidence=0.9)
        pid = add_result["policy_id"]
        for _ in range(6):
            pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="failure")
        result = pt.tool_policy_list(agent_id="test")
        assert result["ok"] is True
        assert pid in result["stale_flagged"]

    def test_list_status_all(self):
        self._add("active-pol")
        result = pt.tool_policy_list(agent_id="test", status="all")
        assert result["ok"] is True
        assert result["count"] >= 1


# ---------------------------------------------------------------------------
# policy_match
# ---------------------------------------------------------------------------

class TestPolicyMatch:
    def _add(self, name, trigger, directive, **kw):
        return pt.tool_policy_add(
            agent_id="test", name=name, trigger=trigger, directive=directive, **kw
        )

    def test_match_requires_context(self):
        result = pt.tool_policy_match(agent_id="test", context="")
        assert result["ok"] is False
        assert "context" in result["error"].lower()

    def test_match_empty_db_returns_empty(self):
        result = pt.tool_policy_match(agent_id="test", context="deploy production")
        assert result["ok"] is True
        assert result["policies"] == []

    def test_match_finds_relevant_policy(self):
        self._add(
            "prod-safety",
            trigger="deploying to production environment",
            directive="require sign-off from lead engineer",
            confidence=0.9,
        )
        result = pt.tool_policy_match(agent_id="test", context="deploying production")
        assert result["ok"] is True
        assert len(result["policies"]) >= 1
        assert result["policies"][0]["name"] == "prod-safety"

    def test_match_respects_top_k(self):
        for i in range(5):
            self._add(f"policy-{i}", trigger="general trigger", directive="do something")
        result = pt.tool_policy_match(agent_id="test", context="general trigger", top_k=2)
        assert result["ok"] is True
        assert len(result["policies"]) <= 2

    def test_match_fallback_when_fts_empty(self):
        # Short words won't hit FTS (filtered out), but fallback query should work
        self._add("tiny-pol", trigger="go", directive="stop")
        result = pt.tool_policy_match(agent_id="test", context="go")
        assert result["ok"] is True
        # Fallback path ran without error
        assert "policies" in result

    def test_match_result_has_required_fields(self):
        self._add("fields-pol", trigger="testing fields", directive="check them")
        result = pt.tool_policy_match(agent_id="test", context="testing fields")
        assert result["ok"] is True
        if result["policies"]:
            pol = result["policies"][0]
            for field in ("policy_id", "name", "confidence_effective", "trigger_condition", "action_directive"):
                assert field in pol, f"Missing field: {field}"

    def test_match_filter_by_category(self):
        self._add("safe-pol", trigger="deleting records", directive="confirm first", category="safety")
        self._add("work-pol", trigger="deleting records", directive="log it", category="workflow")
        result = pt.tool_policy_match(
            agent_id="test", context="deleting records", category="safety"
        )
        assert result["ok"] is True
        assert all(p["category"] == "safety" for p in result["policies"])

    def test_match_stale_mode_warn(self):
        """Stale policies (low effective confidence) should appear in stale_excluded."""
        self._add(
            "stale-pol",
            trigger="some context here",
            directive="do stuff",
            confidence=0.5,
            half_life=1,  # decays very fast
        )
        # Force staleness by requiring higher confidence than the policy can provide
        result = pt.tool_policy_match(
            agent_id="test",
            context="some context here",
            min_confidence=0.99,
            staleness_mode="warn",
        )
        assert result["ok"] is True
        # The policy should be excluded from results and appear in stale_excluded
        assert len(result["stale_excluded"]) >= 1
        excluded_names = {p["name"] for p in result["stale_excluded"]}
        assert "stale-pol" in excluded_names


# ---------------------------------------------------------------------------
# policy_feedback
# ---------------------------------------------------------------------------

class TestPolicyFeedback:
    def _add_policy(self, name="test-policy", confidence=0.5):
        r = pt.tool_policy_add(
            agent_id="test",
            name=name,
            trigger="some trigger condition",
            directive="some action directive",
            confidence=confidence,
        )
        return r["policy_id"]

    def test_feedback_requires_policy_id(self):
        result = pt.tool_policy_feedback(agent_id="test", policy_id="", outcome="success")
        assert result["ok"] is False
        assert "policy_id" in result["error"].lower()

    def test_feedback_requires_valid_outcome(self):
        pid = self._add_policy()
        result = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="maybe")
        assert result["ok"] is False
        assert "outcome" in result["error"].lower()

    def test_feedback_not_found(self):
        result = pt.tool_policy_feedback(
            agent_id="test", policy_id="pol_doesnotexist", outcome="success"
        )
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_success_increases_confidence(self):
        pid = self._add_policy(confidence=0.5)
        result = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="success")
        assert result["ok"] is True
        assert result["outcome"] == "success"
        assert result["confidence_after"] > result["confidence_before"]

    def test_failure_decreases_confidence(self):
        pid = self._add_policy(confidence=0.5)
        result = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="failure")
        assert result["ok"] is True
        assert result["outcome"] == "failure"
        assert result["confidence_after"] < result["confidence_before"]

    def test_confidence_capped_at_1(self):
        pid = self._add_policy(confidence=0.99)
        result = pt.tool_policy_feedback(
            agent_id="test", policy_id=pid, outcome="success", boost=0.5
        )
        assert result["ok"] is True
        assert result["confidence_after"] <= 1.0

    def test_confidence_floored_at_0_1(self):
        pid = self._add_policy(confidence=0.1)
        result = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="failure")
        assert result["ok"] is True
        assert result["confidence_after"] >= 0.1

    def test_feedback_count_increments(self):
        pid = self._add_policy()
        r1 = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="success")
        r2 = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="success")
        assert r1["feedback_count"] == 1
        assert r2["feedback_count"] == 2

    def test_stale_warning_after_many_failures(self):
        pid = self._add_policy(confidence=0.9)
        for _ in range(5):
            pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="failure")
        result = pt.tool_policy_feedback(agent_id="test", policy_id=pid, outcome="failure")
        assert result["ok"] is True
        assert "stale_warning" in result

    def test_lookup_by_name_works(self):
        self._add_policy(name="named-policy")
        result = pt.tool_policy_feedback(
            agent_id="test", policy_id="named-policy", outcome="success"
        )
        assert result["ok"] is True
        assert result["name"] == "named-policy"

    def test_notes_returned_in_result(self):
        pid = self._add_policy()
        result = pt.tool_policy_feedback(
            agent_id="test", policy_id=pid, outcome="success", notes="Worked great!"
        )
        assert result["ok"] is True
        assert result["notes"] == "Worked great!"
