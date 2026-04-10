"""Tests for enhanced Brain class: FTS5 search, triggers, handoffs, doctor, vsearch, consolidate, tier_stats."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain


@pytest.fixture
def brain(tmp_path):
    """Fresh Brain with isolated DB."""
    return Brain(db_path=str(tmp_path / "brain.db"), agent_id="test-agent")


# ---------------------------------------------------------------------------
# search() — FTS5
# ---------------------------------------------------------------------------


class TestSearchFTS5:
    def test_basic_search(self, brain):
        brain.remember("JWT tokens expire after 24 hours", category="convention")
        results = brain.search("JWT tokens")
        assert len(results) >= 1
        assert "JWT" in results[0]["content"]

    def test_search_with_stemming(self, brain):
        brain.remember("deploying the application to production", category="environment")
        # FTS5 porter stemmer: "deployed" should match "deploying"
        results = brain.search("deployed")
        assert len(results) >= 1

    def test_search_returns_ranked_results(self, brain):
        brain.remember("rate limiting configuration", category="convention")
        brain.remember("rate limiting is 100 requests per 15 seconds", category="integration")
        brain.remember("database connection pool size", category="environment")
        results = brain.search("rate limiting")
        assert len(results) >= 2
        # Top results should be about rate limiting, not database
        assert "rate" in results[0]["content"].lower()

    def test_search_excludes_retired(self, brain):
        mid = brain.remember("temporary fact", category="convention")
        brain.forget(mid)
        results = brain.search("temporary fact")
        assert len(results) == 0

    def test_search_limit(self, brain):
        for i in range(10):
            brain.remember(f"memory number {i} about testing", category="lesson")
        results = brain.search("testing", limit=3)
        assert len(results) <= 3

    def test_search_empty_query(self, brain):
        brain.remember("something", category="lesson")
        results = brain.search("")
        assert results == []

    def test_search_special_characters(self, brain):
        brain.remember("use the --force flag carefully", category="convention")
        results = brain.search("--force flag")
        assert len(results) >= 1

    def test_search_result_fields(self, brain):
        brain.remember("test content", category="lesson", confidence=0.9)
        results = brain.search("test content")
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "content" in r
        assert "category" in r
        assert "confidence" in r
        assert "created_at" in r


# ---------------------------------------------------------------------------
# trigger() + check_triggers()
# ---------------------------------------------------------------------------


class TestTriggers:
    def test_create_trigger(self, brain):
        tid = brain.trigger("when deploy fails", "deploy,failure", "check rollback")
        assert isinstance(tid, int)
        assert tid > 0

    def test_check_triggers_match(self, brain):
        brain.trigger("deploy issue", "deploy,failure,rollback", "check rollback procedure")
        matches = brain.check_triggers("the deploy failed with a rollback")
        assert len(matches) >= 1
        assert "deploy" in matches[0]["matched_keywords"] or "failure" in matches[0]["matched_keywords"]

    def test_check_triggers_no_match(self, brain):
        brain.trigger("deploy issue", "deploy,failure", "check rollback")
        matches = brain.check_triggers("the database is running slow")
        assert len(matches) == 0

    def test_trigger_priority_order(self, brain):
        brain.trigger("low priority", "alert", "log it", priority="low")
        brain.trigger("critical alert", "alert", "page oncall", priority="critical")
        matches = brain.check_triggers("new alert detected")
        assert len(matches) == 2
        assert matches[0]["priority"] == "critical"
        assert matches[1]["priority"] == "low"

    def test_trigger_invalid_priority(self, brain):
        with pytest.raises(ValueError):
            brain.trigger("test", "test", "test", priority="urgent")

    def test_trigger_expiry(self, brain):
        brain.trigger("old trigger", "expired", "do nothing", expires="2020-01-01T00:00:00")
        matches = brain.check_triggers("this is expired content")
        assert len(matches) == 0


# ---------------------------------------------------------------------------
# handoff() + resume()
# ---------------------------------------------------------------------------


class TestHandoffs:
    def test_create_handoff(self, brain):
        hid = brain.handoff(
            goal="finish API integration",
            current_state="auth module complete",
            open_loops="rate limiting not implemented",
            next_step="add retry logic with exponential backoff",
        )
        assert isinstance(hid, int)
        assert hid > 0

    def test_resume_consumes_handoff(self, brain):
        brain.handoff("goal", "state", "loops", "next")
        packet = brain.resume()
        assert packet != {}
        assert packet["goal"] == "goal"
        assert packet["status"] == "consumed"
        # Second resume should return empty
        packet2 = brain.resume()
        assert packet2 == {}

    def test_resume_empty(self, brain):
        assert brain.resume() == {}

    def test_resume_with_project(self, brain):
        brain.handoff("goal A", "state A", "loops A", "next A", project="alpha")
        brain.handoff("goal B", "state B", "loops B", "next B", project="beta")
        packet = brain.resume(project="alpha")
        assert packet["goal"] == "goal A"

    def test_handoff_requires_nonempty(self, brain):
        with pytest.raises(ValueError):
            brain.handoff("", "state", "loops", "next")
        with pytest.raises(ValueError):
            brain.handoff("goal", "  ", "loops", "next")

    def test_handoff_with_title(self, brain):
        hid = brain.handoff("goal", "state", "loops", "next", title="Sprint 42 handoff")
        packet = brain.resume()
        assert packet["title"] == "Sprint 42 handoff"


# ---------------------------------------------------------------------------
# doctor()
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_healthy_db(self, brain):
        result = brain.doctor()
        assert result["ok"] is True
        assert result["healthy"] is True
        assert result["issues"] == []
        assert result["fts5_available"] is True
        assert isinstance(result["db_size_mb"], float)
        assert result["db_path"] == str(brain.db_path)

    def test_reports_active_memories(self, brain):
        brain.remember("fact one", category="lesson")
        brain.remember("fact two", category="lesson")
        result = brain.doctor()
        assert result["active_memories"] == 2

    def test_reports_vec_availability(self, brain):
        result = brain.doctor()
        assert "vec_available" in result
        assert isinstance(result["vec_available"], bool)


# ---------------------------------------------------------------------------
# vsearch()
# ---------------------------------------------------------------------------


class TestVsearch:
    def test_returns_list(self, brain):
        result = brain.vsearch("anything")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# consolidate()
# ---------------------------------------------------------------------------


class TestConsolidate:
    def test_empty_consolidation(self, brain):
        result = brain.consolidate()
        assert result["ok"] is True
        assert result["processed"] == 0
        assert result["promoted"] == 0

    def test_promotes_eligible_memory(self, brain):
        db_file = brain.db_path
        mid = brain.remember("important pattern observed repeatedly", category="lesson", confidence=0.9)
        # Set high replay_priority and ripple_tags to make eligible
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "UPDATE memories SET replay_priority = 5.0, ripple_tags = 5 WHERE id = ?",
            (mid,)
        )
        conn.commit()
        conn.close()
        result = brain.consolidate()
        assert result["ok"] is True
        assert result["promoted"] >= 1
        # Verify memory_type changed
        conn = sqlite3.connect(str(db_file))
        row = conn.execute("SELECT memory_type FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row[0] == "semantic"

    def test_does_not_promote_low_confidence(self, brain):
        mid = brain.remember("weak observation", category="lesson", confidence=0.3)
        conn = sqlite3.connect(str(brain.db_path))
        conn.execute(
            "UPDATE memories SET replay_priority = 5.0, ripple_tags = 5 WHERE id = ?",
            (mid,)
        )
        conn.commit()
        conn.close()
        result = brain.consolidate()
        assert result["promoted"] == 0


# ---------------------------------------------------------------------------
# tier_stats()
# ---------------------------------------------------------------------------


class TestTierStats:
    def test_empty_db(self, brain):
        result = brain.tier_stats()
        assert result["ok"] is True
        assert result["total"] == 0

    def test_counts_memories(self, brain):
        brain.remember("fact one", category="lesson")
        brain.remember("fact two", category="lesson")
        result = brain.tier_stats()
        assert result["ok"] is True
        assert result["total"] == 2
