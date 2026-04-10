"""Tests for mcp_tools_consolidation — replay priority, reconsolidation window."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_consolidation as con_mod
from agentmemory.brain import Brain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_plus(minutes: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _now_minus(minutes: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Each test gets a fresh Brain DB; module points at it."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(con_mod, "DB_PATH", db_file)
    return brain


@pytest.fixture
def db_with_memories(tmp_path, monkeypatch):
    """Brain with 3 memories, one having high replay_priority."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(con_mod, "DB_PATH", db_file)

    # Write memories first, then update replay_priority in a separate connection
    mids = []
    for label in ("alpha", "beta", "gamma"):
        mids.append(brain.remember(f"Memory {label}", category="convention"))

    # Close any open brain connections before updating via raw sqlite3
    import sqlite3
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "UPDATE memories SET replay_priority = 3.0, ripple_tags = 2 WHERE id = ?",
        (mids[0],),
    )
    conn.commit()
    conn.close()
    return brain, db_file


@pytest.fixture
def db_with_labile(tmp_path, monkeypatch):
    """Brain with a memory that has an open lability window."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(con_mod, "DB_PATH", db_file)

    mid = brain.remember("Mutable memory content", category="convention")

    import sqlite3
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "UPDATE memories SET labile_until = ?, labile_agent_id = 'claude-code', "
        "retrieval_prediction_error = 0.45 WHERE id = ?",
        (_now_plus(15), mid),
    )
    conn.commit()
    conn.close()
    return brain, mid


# ---------------------------------------------------------------------------
# replay_boost tests
# ---------------------------------------------------------------------------


class TestReplayBoost:
    def test_boost_single_memory_ok(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        rows = sqlite3.connect(str(db_file)).execute("SELECT id FROM memories LIMIT 1").fetchall()
        mid = rows[0][0]
        result = con_mod.tool_replay_boost(agent_id="test", memory_id=mid, delta=1.0)
        assert result["ok"] is True
        assert result["memory_id"] == mid
        assert result["delta"] == 1.0

    def test_boost_increases_priority(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        rows = conn.execute("SELECT id, replay_priority FROM memories LIMIT 1").fetchall()
        mid, before = rows[0][0], rows[0][1]
        conn.close()

        con_mod.tool_replay_boost(agent_id="test", memory_id=mid, delta=0.5)

        conn2 = sqlite3.connect(str(db_file))
        after = conn2.execute("SELECT replay_priority FROM memories WHERE id = ?", (mid,)).fetchone()[0]
        conn2.close()
        assert after > before

    def test_boost_clamped_at_10(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        rows = conn.execute("SELECT id FROM memories LIMIT 1").fetchall()
        mid = rows[0][0]
        conn.close()

        con_mod.tool_replay_boost(agent_id="test", memory_id=mid, delta=5.0)
        con_mod.tool_replay_boost(agent_id="test", memory_id=mid, delta=5.0)
        con_mod.tool_replay_boost(agent_id="test", memory_id=mid, delta=5.0)

        conn2 = sqlite3.connect(str(db_file))
        after = conn2.execute("SELECT replay_priority FROM memories WHERE id = ?", (mid,)).fetchone()[0]
        conn2.close()
        assert after <= 10.0

    def test_boost_scope_affects_multiple(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3

        conn = sqlite3.connect(str(db_file))
        # Set all to scope 'project:test'
        conn.execute("UPDATE memories SET scope = 'project:test'")
        conn.commit()
        conn.close()

        result = con_mod.tool_replay_boost(agent_id="test", scope="project:test", delta=0.3)
        assert result["ok"] is True
        assert result["affected"] >= 1

    def test_boost_requires_memory_id_or_scope(self, db_with_memories):
        result = con_mod.tool_replay_boost(agent_id="test")
        assert result["ok"] is False

    def test_boost_rejects_both_memory_id_and_scope(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        rows = sqlite3.connect(str(db_file)).execute("SELECT id FROM memories LIMIT 1").fetchall()
        mid = rows[0][0]

        result = con_mod.tool_replay_boost(agent_id="test", memory_id=mid, scope="project:x")
        assert result["ok"] is False

    def test_boost_nonexistent_memory(self, db_with_memories):
        result = con_mod.tool_replay_boost(agent_id="test", memory_id=99999)
        assert result["ok"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# replay_queue tests
# ---------------------------------------------------------------------------


class TestReplayQueue:
    def test_returns_ok_true(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.tool_replay_queue(agent_id="test")
        assert result["ok"] is True

    def test_returns_queue_list(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.tool_replay_queue(agent_id="test")
        assert "queue" in result
        assert isinstance(result["queue"], list)

    def test_queue_sorted_by_priority_desc(self, db_with_memories):
        brain, _ = db_with_memories
        # Boost priority: alpha has 3.0, others 0.0
        result = con_mod.tool_replay_queue(agent_id="test", min_priority=0.0)
        queue = result["queue"]
        if len(queue) >= 2:
            priorities = [r["replay_priority"] for r in queue]
            assert priorities == sorted(priorities, reverse=True)

    def test_min_priority_filters(self, db_with_memories):
        brain, db_file = db_with_memories
        # alpha has priority 3.0; others 0.0
        result = con_mod.tool_replay_queue(agent_id="test", min_priority=2.0)
        queue = result["queue"]
        for item in queue:
            assert item["replay_priority"] >= 2.0

    def test_limit_respected(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.tool_replay_queue(agent_id="test", limit=1, min_priority=0.0)
        assert len(result["queue"]) <= 1

    def test_queue_items_have_required_fields(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.tool_replay_queue(agent_id="test", min_priority=0.0)
        for item in result["queue"]:
            assert "id" in item
            assert "replay_priority" in item
            assert "ripple_tags" in item


# ---------------------------------------------------------------------------
# reconsolidation_check tests
# ---------------------------------------------------------------------------


class TestReconsolidationCheck:
    def test_check_labile_memory_is_labile(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_reconsolidation_check(agent_id="claude-code", memory_id=mid)
        assert result["ok"] is True
        assert result["labile"] is True

    def test_check_stable_memory_not_labile(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        mid = sqlite3.connect(str(db_file)).execute("SELECT id FROM memories LIMIT 1").fetchone()[0]
        result = con_mod.tool_reconsolidation_check(agent_id="test", memory_id=mid)
        assert result["ok"] is True
        assert result["labile"] is False

    def test_check_expired_window_not_labile(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        mid = conn.execute("SELECT id FROM memories LIMIT 1").fetchone()[0]
        conn.execute(
            "UPDATE memories SET labile_until = ?, labile_agent_id = 'test' WHERE id = ?",
            (_now_minus(5), mid),
        )
        conn.commit()
        conn.close()

        result = con_mod.tool_reconsolidation_check(agent_id="test", memory_id=mid)
        assert result["ok"] is True
        assert result["labile"] is False
        assert "expired" in result["reason"]

    def test_check_returns_seconds_remaining(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_reconsolidation_check(agent_id="claude-code", memory_id=mid)
        assert "seconds_remaining" in result
        assert result["seconds_remaining"] > 0

    def test_check_nonexistent_memory(self, db_with_memories):
        result = con_mod.tool_reconsolidation_check(agent_id="test", memory_id=99999)
        assert result["ok"] is False

    def test_check_wrong_agent_not_labile(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_reconsolidation_check(agent_id="some-other-agent", memory_id=mid)
        assert result["ok"] is True
        assert result["labile"] is False
        assert "different agent" in result["reason"]


# ---------------------------------------------------------------------------
# reconsolidate tests
# ---------------------------------------------------------------------------


class TestReconsolidate:
    def test_reconsolidate_replace_ok(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_reconsolidate(
            agent_id="claude-code",
            memory_id=mid,
            new_content="Updated memory content",
        )
        assert result["ok"] is True
        assert result["lability_closed"] is True

    def test_reconsolidate_updates_content(self, db_with_labile):
        brain, mid = db_with_labile
        import sqlite3
        db_file = con_mod.DB_PATH
        con_mod.tool_reconsolidate(
            agent_id="claude-code",
            memory_id=mid,
            new_content="Brand new content",
        )
        row = sqlite3.connect(str(db_file)).execute(
            "SELECT content FROM memories WHERE id = ?", (mid,)
        ).fetchone()
        assert row[0] == "Brand new content"

    def test_reconsolidate_closes_window(self, db_with_labile):
        brain, mid = db_with_labile
        import sqlite3
        db_file = con_mod.DB_PATH
        con_mod.tool_reconsolidate(
            agent_id="claude-code",
            memory_id=mid,
            new_content="Updated",
        )
        row = sqlite3.connect(str(db_file)).execute(
            "SELECT labile_until FROM memories WHERE id = ?", (mid,)
        ).fetchone()
        assert row[0] is None

    def test_reconsolidate_append_mode(self, db_with_labile):
        brain, mid = db_with_labile
        import sqlite3
        db_file = con_mod.DB_PATH
        old_row = sqlite3.connect(str(db_file)).execute(
            "SELECT content FROM memories WHERE id = ?", (mid,)
        ).fetchone()
        old = old_row[0]

        con_mod.tool_reconsolidate(
            agent_id="claude-code",
            memory_id=mid,
            new_content="Appended info",
            merge_mode="append",
        )
        new_row = sqlite3.connect(str(db_file)).execute(
            "SELECT content FROM memories WHERE id = ?", (mid,)
        ).fetchone()
        assert old in new_row[0]
        assert "Appended info" in new_row[0]

    def test_reconsolidate_fails_without_window(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        mid = sqlite3.connect(str(db_file)).execute("SELECT id FROM memories LIMIT 1").fetchone()[0]

        result = con_mod.tool_reconsolidate(
            agent_id="test",
            memory_id=mid,
            new_content="New content",
        )
        assert result["ok"] is False
        assert "not labile" in result["error"]

    def test_reconsolidate_fails_wrong_agent(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_reconsolidate(
            agent_id="wrong-agent",
            memory_id=mid,
            new_content="Sneaky update",
        )
        assert result["ok"] is False
        assert "not labile" in result["error"]

    def test_reconsolidate_requires_new_content(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_reconsolidate(
            agent_id="claude-code",
            memory_id=mid,
            new_content="",
        )
        assert result["ok"] is False

    def test_reconsolidate_nonexistent_memory(self, db_with_memories):
        result = con_mod.tool_reconsolidate(
            agent_id="test",
            memory_id=99999,
            new_content="content",
        )
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# consolidation_stats tests
# ---------------------------------------------------------------------------


class TestConsolidationStats:
    def test_stats_returns_ok_true(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.tool_consolidation_stats(agent_id="test")
        assert result["ok"] is True

    def test_stats_has_required_fields(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.tool_consolidation_stats(agent_id="test")
        for field in ("total_active_memories", "queued_for_replay",
                      "high_priority_replay", "avg_replay_priority",
                      "total_ripple_tags", "currently_labile"):
            assert field in result, f"Missing field: {field}"

    def test_stats_counts_labile(self, db_with_labile):
        brain, mid = db_with_labile
        result = con_mod.tool_consolidation_stats(agent_id="test")
        assert result["ok"] is True
        assert result["currently_labile"] >= 1

    def test_stats_scope_filter(self, db_with_memories):
        brain, db_file = db_with_memories
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        conn.execute("UPDATE memories SET scope = 'project:filtered'")
        conn.commit()
        conn.close()

        result = con_mod.tool_consolidation_stats(agent_id="test", scope="project:filtered")
        assert result["ok"] is True
        assert result["total_active_memories"] >= 1

    def test_stats_empty_scope_returns_zero(self, db_with_memories):
        result = con_mod.tool_consolidation_stats(agent_id="test", scope="project:nonexistent")
        assert result["ok"] is True
        assert result["total_active_memories"] == 0


# ---------------------------------------------------------------------------
# DISPATCH routing tests
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_has_all_tools(self):
        expected = {"replay_boost", "replay_queue", "reconsolidation_check",
                    "reconsolidate", "consolidation_stats",
                    "memory_calibration", "attention_snapshot"}
        assert expected.issubset(set(con_mod.DISPATCH.keys()))

    def test_dispatch_replay_queue_ok(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.DISPATCH["replay_queue"](min_priority=0.0)
        assert result["ok"] is True

    def test_dispatch_consolidation_stats_ok(self, db_with_memories):
        brain, _ = db_with_memories
        result = con_mod.DISPATCH["consolidation_stats"]()
        assert result["ok"] is True
