"""Tests for mcp_tools_temporal_abstraction — abstract_summarize, zoom_out, zoom_in, temporal_map."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_temporal_abstraction as ta


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    from agentmemory.brain import Brain
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(ta, "DB_PATH", db_file)
    return db_file


def _insert_memory(db_file, content, temporal_level="moment", created_at=None, agent_id="test-agent",
                   category="convention", confidence=0.7):
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    ta._ensure_temporal_level_col(conn)
    ts = created_at or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO memories (content, category, confidence, agent_id, temporal_level, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (content, category, confidence, agent_id, temporal_level, ts),
    )
    conn.commit()
    mid = conn.execute("SELECT id FROM memories ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    return mid


# ---------------------------------------------------------------------------
# abstract_summarize tests
# ---------------------------------------------------------------------------


class TestAbstractSummarize:
    def test_invalid_level_rejected(self, patch_db):
        result = ta.tool_abstract_summarize(agent_id="test-agent", level="decade")
        assert result["ok"] is False

    def test_no_children_returns_error(self, patch_db):
        # No moment-level memories exist
        result = ta.tool_abstract_summarize(agent_id="test-agent", level="session")
        assert result["ok"] is False

    def test_dry_run_no_write(self, patch_db):
        anchor = datetime(2026, 1, 1, 12, 0, 0)
        for i in range(3):
            ts = (anchor - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S")
            _insert_memory(patch_db, f"event {i}", temporal_level="moment",
                           created_at=ts)
        result = ta.tool_abstract_summarize(
            agent_id="test-agent", level="session",
            anchor_time=anchor.isoformat(), dry_run=True,
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        # No new memory written
        conn = sqlite3.connect(str(patch_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE temporal_level = 'session'"
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_creates_summary_memory(self, patch_db):
        anchor = datetime(2026, 1, 1, 12, 0, 0)
        for i in range(3):
            ts = (anchor - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S")
            _insert_memory(patch_db, f"task {i} completed.", temporal_level="moment", created_at=ts)
        result = ta.tool_abstract_summarize(
            agent_id="test-agent", level="session",
            anchor_time=anchor.isoformat(),
        )
        assert result["ok"] is True
        assert result["memory_id"] is not None
        assert result["level"] == "session"
        assert result["constituent_count"] >= 1

    def test_summary_memory_has_correct_level(self, patch_db):
        anchor = datetime(2026, 1, 1, 12, 0, 0)
        _insert_memory(patch_db, "event.", temporal_level="moment",
                       created_at=anchor.strftime("%Y-%m-%dT%H:%M:%S"))
        result = ta.tool_abstract_summarize(
            agent_id="test-agent", level="session",
            anchor_time=anchor.isoformat(),
        )
        assert result["ok"] is True
        conn = sqlite3.connect(str(patch_db))
        row = conn.execute(
            "SELECT temporal_level FROM memories WHERE id = ?", (result["memory_id"],)
        ).fetchone()
        conn.close()
        assert row[0] == "session"

    def test_returns_derived_from_ids(self, patch_db):
        anchor = datetime(2026, 1, 1, 12, 0, 0)
        mids = []
        for i in range(2):
            ts = (anchor - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S")
            mids.append(_insert_memory(patch_db, f"event {i}.", temporal_level="moment",
                                       created_at=ts))
        result = ta.tool_abstract_summarize(
            agent_id="test-agent", level="session",
            anchor_time=anchor.isoformat(),
        )
        assert result["ok"] is True
        assert len(result["derived_from_ids"]) >= 1


# ---------------------------------------------------------------------------
# zoom_out tests
# ---------------------------------------------------------------------------


class TestZoomOut:
    def test_requires_memory_id(self, patch_db):
        result = ta.tool_zoom_out(agent_id="test-agent")
        assert result["ok"] is False

    def test_nonexistent_memory(self, patch_db):
        result = ta.tool_zoom_out(agent_id="test-agent", memory_id=99999)
        assert result["ok"] is False

    def test_moment_with_no_parents(self, patch_db):
        mid = _insert_memory(patch_db, "event", temporal_level="moment")
        result = ta.tool_zoom_out(agent_id="test-agent", memory_id=mid)
        assert result["ok"] is True
        assert result["current_level"] == "moment"
        assert isinstance(result["hierarchy_above"], list)

    def test_returns_parent_if_exists(self, patch_db):
        ts_moment = datetime(2026, 1, 1, 10, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        ts_session = datetime(2026, 1, 1, 11, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        mid = _insert_memory(patch_db, "moment event", temporal_level="moment",
                              created_at=ts_moment)
        _insert_memory(patch_db, "session summary", temporal_level="session",
                       created_at=ts_session)
        result = ta.tool_zoom_out(agent_id="test-agent", memory_id=mid)
        assert result["ok"] is True
        # Should find the session-level parent
        levels = [h["temporal_level"] for h in result["hierarchy_above"]]
        assert "session" in levels


# ---------------------------------------------------------------------------
# zoom_in tests
# ---------------------------------------------------------------------------


class TestZoomIn:
    def test_requires_memory_id(self, patch_db):
        result = ta.tool_zoom_in(agent_id="test-agent")
        assert result["ok"] is False

    def test_moment_has_no_children(self, patch_db):
        mid = _insert_memory(patch_db, "moment", temporal_level="moment")
        result = ta.tool_zoom_in(agent_id="test-agent", memory_id=mid)
        assert result["ok"] is True
        assert result["children"] == []

    def test_session_finds_moment_children(self, patch_db):
        ts_session = datetime(2026, 1, 1, 12, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        ts_moment = datetime(2026, 1, 1, 11, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        session_mid = _insert_memory(patch_db, "session summary", temporal_level="session",
                                     created_at=ts_session)
        _insert_memory(patch_db, "moment event", temporal_level="moment",
                       created_at=ts_moment)
        result = ta.tool_zoom_in(agent_id="test-agent", memory_id=session_mid)
        assert result["ok"] is True
        assert result["child_level"] == "moment"
        # Should find the moment-level child
        assert len(result["children"]) >= 1

    def test_limit_respected(self, patch_db):
        ts_session = datetime(2026, 1, 1, 12, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        session_mid = _insert_memory(patch_db, "session", temporal_level="session",
                                     created_at=ts_session)
        for i in range(5):
            ts = datetime(2026, 1, 1, 11 - i, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
            _insert_memory(patch_db, f"event {i}", temporal_level="moment", created_at=ts)
        result = ta.tool_zoom_in(agent_id="test-agent", memory_id=session_mid, limit=2)
        assert result["ok"] is True
        assert len(result["children"]) <= 2


# ---------------------------------------------------------------------------
# temporal_map tests
# ---------------------------------------------------------------------------


class TestTemporalMap:
    def test_empty_db_ok(self, patch_db):
        result = ta.tool_temporal_map(agent_id="test-agent")
        assert result["ok"] is True
        assert result["total_memories"] == 0

    def test_returns_all_levels(self, patch_db):
        result = ta.tool_temporal_map(agent_id="test-agent")
        assert result["ok"] is True
        levels = {r["temporal_level"] for r in result["levels"]}
        assert {"moment", "session", "day", "week", "month", "quarter"}.issubset(levels)

    def test_counts_memories_at_correct_level(self, patch_db):
        _insert_memory(patch_db, "event 1", temporal_level="moment")
        _insert_memory(patch_db, "event 2", temporal_level="moment")
        _insert_memory(patch_db, "session", temporal_level="session")
        result = ta.tool_temporal_map(agent_id="test-agent")
        assert result["ok"] is True
        level_map = {r["temporal_level"]: r["count"] for r in result["levels"]}
        assert level_map["moment"] == 2
        assert level_map["session"] == 1
        assert result["total_memories"] == 3
