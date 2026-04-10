"""Tests for mcp_tools_reflexion — reflexion & outcome MCP tools."""
from __future__ import annotations
import sys
import os
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_reflexion as mod


@pytest.fixture(autouse=True)
def _set_db_path(tmp_path):
    """Point the module at a fresh temp DB for each test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))  # initialises schema
    mod.DB_PATH = db_file
    yield
    # Reset to avoid cross-test leakage (best effort)
    mod.DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _write_lesson(**kwargs) -> dict:
    """Write a reflexion lesson with sensible defaults."""
    args = {
        "agent": "test-agent",
        "failure_class": "TOOL_MISUSE",
        "trigger": "When using search tool with empty query",
        "lesson": "Always validate query is non-empty before calling search",
    }
    args.update(kwargs)
    return mod.handle_reflexion_write(args)


# ---------------------------------------------------------------------------
# reflexion_write
# ---------------------------------------------------------------------------

class TestReflexionWrite:
    def test_write_returns_ok(self):
        result = _write_lesson()
        assert result["ok"] is True
        assert "lesson_id" in result
        assert isinstance(result["lesson_id"], int)

    def test_write_sets_failure_class(self):
        result = _write_lesson(failure_class="CONTEXT_LOSS")
        assert result["failure_class"] == "CONTEXT_LOSS"

    def test_write_applies_default_confidence(self):
        result = _write_lesson(failure_class="HALLUCINATION")
        assert abs(result["confidence"] - 0.55) < 1e-9

    def test_write_accepts_custom_confidence(self):
        result = _write_lesson(confidence=0.9)
        assert abs(result["confidence"] - 0.9) < 1e-9

    def test_write_rejects_invalid_failure_class(self):
        result = _write_lesson(failure_class="BOGUS")
        assert result["ok"] is False
        assert "failure_class" in result["error"].lower() or "Invalid" in result["error"]

    def test_write_requires_trigger(self):
        result = mod.handle_reflexion_write({
            "failure_class": "TOOL_MISUSE",
            "lesson": "some lesson",
        })
        assert result["ok"] is False
        assert "trigger" in result["error"]

    def test_write_requires_lesson(self):
        result = mod.handle_reflexion_write({
            "failure_class": "TOOL_MISUSE",
            "trigger": "some trigger",
        })
        assert result["ok"] is False
        assert "lesson" in result["error"]

    def test_write_applies_default_override_level(self):
        result = _write_lesson(failure_class="TOOL_MISUSE")
        assert result["override_level"] == "HARD_OVERRIDE"

    def test_write_generalizable_defaults_for_hallucination(self):
        result = _write_lesson(failure_class="HALLUCINATION", agent="hermes")
        assert "agent:hermes" in result["generalizable_to"]

    def test_write_custom_generalizable_to(self):
        result = _write_lesson(generalizable_to="scope:global,agent_type:pipeline")
        assert "scope:global" in result["generalizable_to"]
        assert "agent_type:pipeline" in result["generalizable_to"]


# ---------------------------------------------------------------------------
# reflexion_list
# ---------------------------------------------------------------------------

class TestReflexionList:
    def test_list_returns_list(self):
        _write_lesson()
        result = mod.handle_reflexion_list({"agent": "test-agent"})
        assert isinstance(result, list)

    def test_list_shows_active_by_default(self):
        _write_lesson()
        result = mod.handle_reflexion_list({})
        assert all(r["status"] == "active" for r in result)

    def test_list_filters_by_failure_class(self):
        _write_lesson(failure_class="TOOL_MISUSE")
        _write_lesson(failure_class="CONTEXT_LOSS")
        result = mod.handle_reflexion_list({"failure_class": "TOOL_MISUSE"})
        assert all(r["failure_class"] == "TOOL_MISUSE" for r in result)
        assert len(result) >= 1

    def test_list_respects_limit(self):
        for _ in range(5):
            _write_lesson()
        result = mod.handle_reflexion_list({"limit": 2})
        assert len(result) <= 2

    def test_list_empty_when_no_lessons(self):
        result = mod.handle_reflexion_list({})
        assert result == []

    def test_list_filter_by_source_agent(self):
        _write_lesson(agent="agent-alpha")
        _write_lesson(agent="agent-beta")
        result = mod.handle_reflexion_list({"source_agent": "agent-alpha"})
        assert all(r["source_agent_id"] == "agent-alpha" for r in result)

    def test_list_filter_by_status_retired(self):
        r = _write_lesson()
        lid = r["lesson_id"]
        mod.handle_reflexion_retire({"lesson_id": lid, "agent": "test-agent"})
        active = mod.handle_reflexion_list({"status": "active"})
        retired = mod.handle_reflexion_list({"status": "retired"})
        assert not any(row["id"] == lid for row in active)
        assert any(row["id"] == lid for row in retired)


# ---------------------------------------------------------------------------
# reflexion_query
# ---------------------------------------------------------------------------

class TestReflexionQuery:
    def test_query_returns_list(self):
        _write_lesson(trigger="empty query in search tool", lesson="validate inputs")
        result = mod.handle_reflexion_query({"task_description": "search tool usage"})
        assert isinstance(result, list)

    def test_query_empty_description_returns_list(self):
        _write_lesson()
        result = mod.handle_reflexion_query({"task_description": ""})
        assert isinstance(result, list)

    def test_query_increments_times_retrieved(self):
        r = _write_lesson(trigger="database connection pool exhausted", lesson="limit connections")
        lid = r["lesson_id"]
        mod.handle_reflexion_query({"task_description": "database connection pool exhausted"})
        db = mod._db()
        row = db.execute("SELECT times_retrieved FROM reflexion_lessons WHERE id=?", (lid,)).fetchone()
        db.close()
        assert row["times_retrieved"] >= 1

    def test_query_respects_min_confidence(self):
        _write_lesson(confidence=0.4, failure_class="REASONING_ERROR")
        result = mod.handle_reflexion_query({"task_description": "search", "min_confidence": 0.9})
        # Should not return lessons below threshold
        assert all(r["confidence"] >= 0.9 for r in result)

    def test_query_respects_top_k(self):
        for i in range(10):
            _write_lesson(trigger=f"unique trigger keyword alpha {i}", lesson=f"lesson {i}")
        result = mod.handle_reflexion_query({"task_description": "unique trigger keyword alpha", "top_k": 3})
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# reflexion_success
# ---------------------------------------------------------------------------

class TestReflexionSuccess:
    def test_success_increments_consecutive_successes(self):
        r = _write_lesson()
        lid = r["lesson_id"]
        result = mod.handle_reflexion_success({"agent": "test-agent", "lesson_ids": str(lid)})
        assert result["ok"] is True
        db = mod._db()
        row = db.execute("SELECT consecutive_successes FROM reflexion_lessons WHERE id=?", (lid,)).fetchone()
        db.close()
        assert row["consecutive_successes"] == 1

    def test_success_boosts_confidence(self):
        r = _write_lesson(confidence=0.8)
        lid = r["lesson_id"]
        mod.handle_reflexion_success({"lesson_ids": str(lid)})
        db = mod._db()
        row = db.execute("SELECT confidence FROM reflexion_lessons WHERE id=?", (lid,)).fetchone()
        db.close()
        assert row["confidence"] >= 0.82

    def test_success_archives_at_threshold(self):
        # Set expiration_n=1 so one success triggers archival
        r = _write_lesson(expiration_n=1)
        lid = r["lesson_id"]
        result = mod.handle_reflexion_success({"lesson_ids": str(lid)})
        assert lid in result["archived"]
        db = mod._db()
        row = db.execute("SELECT status FROM reflexion_lessons WHERE id=?", (lid,)).fetchone()
        db.close()
        assert row["status"] == "archived"

    def test_success_handles_multiple_lesson_ids(self):
        lid1 = _write_lesson()["lesson_id"]
        lid2 = _write_lesson()["lesson_id"]
        result = mod.handle_reflexion_success({"lesson_ids": f"{lid1},{lid2}"})
        assert result["ok"] is True
        assert lid1 in result["updated"] or lid1 in result["archived"]
        assert lid2 in result["updated"] or lid2 in result["archived"]

    def test_success_requires_lesson_ids(self):
        result = mod.handle_reflexion_success({})
        assert result["ok"] is False
        assert "lesson_ids" in result["error"]

    def test_success_skips_nonexistent_ids(self):
        result = mod.handle_reflexion_success({"lesson_ids": "999999"})
        assert result["ok"] is True
        assert 999999 not in result["updated"]
        assert 999999 not in result["archived"]


# ---------------------------------------------------------------------------
# reflexion_failure_recurrence
# ---------------------------------------------------------------------------

class TestReflexionFailureRecurrence:
    def test_failure_recurrence_decreases_confidence(self):
        r = _write_lesson(confidence=0.8)
        lid = r["lesson_id"]
        result = mod.handle_reflexion_failure_recurrence({"lesson_id": lid})
        assert result["ok"] is True
        assert abs(result["new_confidence"] - 0.65) < 1e-9

    def test_failure_recurrence_resets_consecutive_successes(self):
        r = _write_lesson()
        lid = r["lesson_id"]
        mod.handle_reflexion_success({"lesson_ids": str(lid)})  # bump to 1
        mod.handle_reflexion_failure_recurrence({"lesson_id": lid})
        db = mod._db()
        row = db.execute("SELECT consecutive_successes FROM reflexion_lessons WHERE id=?", (lid,)).fetchone()
        db.close()
        assert row["consecutive_successes"] == 0

    def test_failure_recurrence_not_found(self):
        result = mod.handle_reflexion_failure_recurrence({"lesson_id": 999999})
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_failure_recurrence_requires_lesson_id(self):
        result = mod.handle_reflexion_failure_recurrence({})
        assert result["ok"] is False

    def test_failure_recurrence_confidence_floor_is_zero(self):
        r = _write_lesson(confidence=0.05)
        lid = r["lesson_id"]
        result = mod.handle_reflexion_failure_recurrence({"lesson_id": lid})
        assert result["new_confidence"] >= 0.0


# ---------------------------------------------------------------------------
# reflexion_retire
# ---------------------------------------------------------------------------

class TestReflexionRetire:
    def test_retire_sets_status(self):
        r = _write_lesson()
        lid = r["lesson_id"]
        result = mod.handle_reflexion_retire({"lesson_id": lid})
        assert result["ok"] is True
        db = mod._db()
        row = db.execute("SELECT status FROM reflexion_lessons WHERE id=?", (lid,)).fetchone()
        db.close()
        assert row["status"] == "retired"

    def test_retire_returns_reason(self):
        r = _write_lesson()
        lid = r["lesson_id"]
        result = mod.handle_reflexion_retire({"lesson_id": lid, "reason": "outdated"})
        assert result["reason"] == "outdated"

    def test_retire_default_reason(self):
        r = _write_lesson()
        lid = r["lesson_id"]
        result = mod.handle_reflexion_retire({"lesson_id": lid})
        assert result["reason"] == "manual retirement"

    def test_retire_not_found(self):
        result = mod.handle_reflexion_retire({"lesson_id": 999999})
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_retire_requires_lesson_id(self):
        result = mod.handle_reflexion_retire({})
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# outcome_annotate / outcome_report (graceful degradation)
# ---------------------------------------------------------------------------

class TestOutcomeGracefulDegradation:
    def test_annotate_returns_error_when_module_missing(self):
        # outcome_eval is an external library not bundled in this repo
        result = mod.handle_outcome_annotate({"task_id": "t1", "outcome": "success"})
        # Either succeeds (module available) or returns ok=False with descriptive error
        if not result.get("ok"):
            assert "outcome_eval" in result["error"] or "error" in result

    def test_report_returns_error_when_module_missing(self):
        result = mod.handle_outcome_report({"period": 30})
        if not result.get("ok"):
            assert "outcome_eval" in result["error"] or "error" in result

    def test_annotate_requires_task_id(self):
        result = mod.handle_outcome_annotate({"outcome": "success"})
        # Will fail either on validation or import — either way ok=False
        assert result.get("ok") is False

    def test_annotate_requires_outcome(self):
        result = mod.handle_outcome_annotate({"task_id": "t1"})
        assert result.get("ok") is False


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        assert isinstance(mod.TOOLS, list)
        assert len(mod.TOOLS) == 8

    def test_dispatch_is_dict(self):
        assert isinstance(mod.DISPATCH, dict)
        assert len(mod.DISPATCH) == 8

    def test_tool_names_match_dispatch(self):
        tool_names = {t.name for t in mod.TOOLS}
        dispatch_keys = set(mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_dispatch_values_are_callable(self):
        for name, fn in mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"

    def test_all_expected_tool_names_present(self):
        names = {t.name for t in mod.TOOLS}
        expected = {
            "reflexion_write", "reflexion_list", "reflexion_query",
            "reflexion_success", "reflexion_failure_recurrence", "reflexion_retire",
            "outcome_annotate", "outcome_report",
        }
        assert names == expected
