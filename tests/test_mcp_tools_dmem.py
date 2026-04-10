"""Tests for D-MEM RPE routing (issue #31): memory_promote and tier_stats."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_dmem as dmem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    """Each test gets an isolated Brain DB."""
    from agentmemory.brain import Brain
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(dmem, "DB_PATH", db_file)
    return db_file


def _insert_memory(db_file, agent_id="test-agent", write_tier="construct", indexed=0):
    """Insert a memory directly and return its ID."""
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    # Ensure agent row exists
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type) VALUES (?, ?, 'assistant')",
        (agent_id, agent_id),
    )
    conn.execute(
        "INSERT INTO memories (content, category, confidence, agent_id, write_tier, indexed, created_at) "
        "VALUES ('test memory content', 'convention', 0.8, ?, ?, ?, '2026-01-01T00:00:00')",
        (agent_id, write_tier, indexed),
    )
    conn.commit()
    mid = conn.execute("SELECT id FROM memories ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    return mid


# ---------------------------------------------------------------------------
# memory_promote tests
# ---------------------------------------------------------------------------


class TestMemoryPromote:
    def test_requires_memory_id(self, patch_db):
        result = dmem.tool_memory_promote(agent_id="test-agent")
        assert result["ok"] is False
        assert "memory_id" in result["error"]

    def test_nonexistent_memory(self, patch_db):
        result = dmem.tool_memory_promote(agent_id="test-agent", memory_id=99999)
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_wrong_agent_id(self, patch_db):
        mid = _insert_memory(patch_db)
        result = dmem.tool_memory_promote(agent_id="other-agent", memory_id=mid)
        assert result["ok"] is False

    def test_dry_run_no_changes(self, patch_db):
        mid = _insert_memory(patch_db, write_tier="construct", indexed=0)
        result = dmem.tool_memory_promote(agent_id="test-agent", memory_id=mid, dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        # No change in DB
        conn = sqlite3.connect(str(patch_db))
        row = conn.execute("SELECT indexed, write_tier FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row[0] == 0
        assert row[1] == "construct"

    def test_promotes_construct_memory(self, patch_db):
        mid = _insert_memory(patch_db, write_tier="construct", indexed=0)
        result = dmem.tool_memory_promote(agent_id="test-agent", memory_id=mid)
        assert result["ok"] is True
        assert result["promoted"] is True
        # DB state updated
        conn = sqlite3.connect(str(patch_db))
        row = conn.execute("SELECT indexed, write_tier, promoted_at FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row[0] == 1
        assert row[1] == "full"
        assert row[2] is not None  # promoted_at set

    def test_already_full_is_idempotent(self, patch_db):
        mid = _insert_memory(patch_db, write_tier="full", indexed=1)
        result = dmem.tool_memory_promote(agent_id="test-agent", memory_id=mid)
        assert result["ok"] is True
        assert result.get("already_full") is True

    def test_fts_indexed_after_promote(self, patch_db):
        """After promotion, the memory should appear in FTS MATCH search."""
        # Use a unique token to avoid false positives from existing content
        unique_token = "xq9plortzmem42"
        conn = sqlite3.connect(str(patch_db))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO memories (content, category, confidence, agent_id, write_tier, indexed, created_at) "
            "VALUES (?, 'convention', 0.8, 'test-agent', 'construct', 0, '2026-01-01T00:00:00')",
            (f"memory about {unique_token}",),
        )
        conn.commit()
        mid = conn.execute("SELECT id FROM memories ORDER BY id DESC LIMIT 1").fetchone()[0]

        # Verify NOT findable via FTS MATCH before promotion
        before = conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?", (unique_token,)
        ).fetchone()[0]
        conn.close()
        assert before == 0, "Unindexed memory should not appear in FTS MATCH"

        dmem.tool_memory_promote(agent_id="test-agent", memory_id=mid)

        conn = sqlite3.connect(str(patch_db))
        after = conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?", (unique_token,)
        ).fetchone()[0]
        conn.close()
        assert after == 1, "Promoted memory should appear in FTS MATCH"

    def test_promotion_event_logged(self, patch_db):
        mid = _insert_memory(patch_db, write_tier="construct", indexed=0)
        dmem.tool_memory_promote(agent_id="test-agent", memory_id=mid)
        conn = sqlite3.connect(str(patch_db))
        evt = conn.execute(
            "SELECT event_type FROM events WHERE event_type = 'memory_promoted' LIMIT 1"
        ).fetchone()
        conn.close()
        assert evt is not None


# ---------------------------------------------------------------------------
# tier_stats tests
# ---------------------------------------------------------------------------


class TestTierStats:
    def test_empty_db_returns_ok(self, patch_db):
        result = dmem.tool_tier_stats(agent_id="test-agent")
        assert result["ok"] is True
        assert result["total"] == 0
        assert result["tiers"] == {}

    def test_counts_tiers(self, patch_db):
        # Insert 2 full, 3 construct
        for _ in range(2):
            _insert_memory(patch_db, write_tier="full", indexed=1)
        for _ in range(3):
            _insert_memory(patch_db, write_tier="construct", indexed=0)
        result = dmem.tool_tier_stats(agent_id="test-agent")
        assert result["ok"] is True
        assert result["total"] == 5
        assert result["tiers"]["full"]["count"] == 2
        assert result["tiers"]["construct"]["count"] == 3

    def test_unindexed_count_matches_construct(self, patch_db):
        for _ in range(3):
            _insert_memory(patch_db, write_tier="construct", indexed=0)
        _insert_memory(patch_db, write_tier="full", indexed=1)
        result = dmem.tool_tier_stats(agent_id="test-agent")
        assert result["unindexed_count"] == 3

    def test_percentages_sum_to_100(self, patch_db):
        _insert_memory(patch_db, write_tier="full", indexed=1)
        _insert_memory(patch_db, write_tier="construct", indexed=0)
        result = dmem.tool_tier_stats(agent_id="test-agent")
        total_pct = sum(t["pct"] for t in result["tiers"].values())
        assert abs(total_pct - 100.0) < 0.2  # floating-point tolerance

    def test_excludes_retired_memories(self, patch_db):
        mid = _insert_memory(patch_db, write_tier="full", indexed=1)
        # Retire the memory
        conn = sqlite3.connect(str(patch_db))
        conn.execute("UPDATE memories SET retired_at = '2026-01-01T00:00:00' WHERE id = ?", (mid,))
        conn.commit()
        conn.close()
        result = dmem.tool_tier_stats(agent_id="test-agent")
        assert result["total"] == 0

    def test_agent_isolation(self, patch_db):
        """Stats for one agent don't include another's memories."""
        _insert_memory(patch_db, agent_id="test-agent", write_tier="full", indexed=1)
        _insert_memory(patch_db, agent_id="other-agent", write_tier="construct", indexed=0)
        result = dmem.tool_tier_stats(agent_id="test-agent")
        assert result["total"] == 1
        assert "construct" not in result["tiers"]
