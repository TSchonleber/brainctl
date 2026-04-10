"""Tests for mcp_tools_allostatic — consolidation_schedule, allostatic_prime, demand_forecast."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_allostatic as allo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    """Each test gets an isolated Brain DB."""
    from agentmemory.brain import Brain
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(allo, "DB_PATH", db_file)
    return db_file


@pytest.fixture
def populated_db(patch_db):
    """DB with memories, access_log entries, and ripple_tags for signal coverage."""
    db_file = patch_db
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")

    # Insert several memories in the same category
    for i in range(5):
        conn.execute(
            "INSERT INTO memories (content, category, confidence, agent_id, replay_priority, ripple_tags, created_at) "
            "VALUES (?, 'project', 0.8, 'test-agent', ?, ?, '2026-01-01T00:00:00')",
            (f"memory content {i}", float(i) * 0.3, i),
        )
    conn.commit()

    # Get the memory IDs
    mids = [r[0] for r in conn.execute("SELECT id FROM memories ORDER BY id").fetchall()]

    # Add access_log entries for the last 24h so signals fire
    for mid in mids[:3]:
        conn.execute(
            "INSERT INTO access_log (target_table, target_id, agent_id, action, created_at) "
            "VALUES ('memories', ?, 'test-agent', 'read', strftime('%Y-%m-%dT%H:%M:%S','now','-1 hour'))",
            (mid,),
        )
    conn.commit()
    conn.close()
    return db_file, mids


# ---------------------------------------------------------------------------
# consolidation_schedule tests
# ---------------------------------------------------------------------------


class TestConsolidationSchedule:
    def test_empty_db_returns_ok(self, patch_db):
        result = allo.tool_consolidation_schedule(agent_id="test-agent")
        assert result["ok"] is True
        assert result["forecast_count"] == 0

    def test_dry_run_does_not_write(self, populated_db):
        db_file, _ = populated_db
        result = allo.tool_consolidation_schedule(agent_id="test-agent", dry_run=True)
        assert result["ok"] is True
        assert result.get("dry_run") is True
        # Nothing written to DB
        conn = sqlite3.connect(str(db_file))
        allo._ensure_forecasts_table(conn)
        count = conn.execute("SELECT COUNT(*) FROM consolidation_forecasts").fetchone()[0]
        conn.close()
        assert count == 0

    def test_writes_forecasts(self, populated_db):
        db_file, _ = populated_db
        result = allo.tool_consolidation_schedule(agent_id="test-agent")
        assert result["ok"] is True
        assert result["forecast_count"] > 0
        conn = sqlite3.connect(str(db_file))
        count = conn.execute(
            "SELECT COUNT(*) FROM consolidation_forecasts WHERE agent_id = 'test-agent'"
        ).fetchone()[0]
        conn.close()
        assert count > 0

    def test_deduplicates_on_second_call(self, populated_db):
        allo.tool_consolidation_schedule(agent_id="test-agent")
        result2 = allo.tool_consolidation_schedule(agent_id="test-agent")
        assert result2["ok"] is True
        # Second call should write 0 new rows (already pending)
        assert result2["forecast_count"] == 0

    def test_forecasts_have_expected_fields(self, populated_db):
        result = allo.tool_consolidation_schedule(agent_id="test-agent", dry_run=True)
        for f in result.get("forecasts", []):
            for field in ("memory_id", "signal_source", "confidence", "predicted_demand_at"):
                assert field in f, f"Missing field: {field}"

    def test_respects_limit(self, populated_db):
        result = allo.tool_consolidation_schedule(agent_id="test-agent", limit=2, dry_run=True)
        assert result["ok"] is True
        assert len(result.get("forecasts", [])) <= 2

    def test_confidence_in_range(self, populated_db):
        result = allo.tool_consolidation_schedule(agent_id="test-agent", dry_run=True)
        for f in result.get("forecasts", []):
            assert 0.0 <= f["confidence"] <= 1.0

    def test_signal_source_valid(self, populated_db):
        valid_sources = {"project_activity", "access_recency", "temporal_pattern"}
        result = allo.tool_consolidation_schedule(agent_id="test-agent", dry_run=True)
        for f in result.get("forecasts", []):
            assert f["signal_source"] in valid_sources


# ---------------------------------------------------------------------------
# allostatic_prime tests
# ---------------------------------------------------------------------------


class TestAllostaticPrime:
    def test_no_forecasts_returns_note(self, patch_db):
        result = allo.tool_allostatic_prime(agent_id="test-agent")
        assert result["ok"] is True
        assert result["primed"] == 0
        assert "note" in result

    def test_boosts_replay_priority(self, populated_db):
        db_file, mids = populated_db
        allo.tool_consolidation_schedule(agent_id="test-agent")
        # Record baseline priorities
        conn = sqlite3.connect(str(db_file))
        before = {r[0]: r[1] for r in conn.execute("SELECT id, replay_priority FROM memories").fetchall()}
        conn.close()

        result = allo.tool_allostatic_prime(agent_id="test-agent", boost_delta=1.0)
        assert result["ok"] is True
        assert result["primed"] > 0

        conn = sqlite3.connect(str(db_file))
        after = {r[0]: r[1] for r in conn.execute("SELECT id, replay_priority FROM memories").fetchall()}
        conn.close()
        # At least one memory should have higher priority
        assert any(after[mid] > before[mid] for mid in mids)

    def test_replay_priority_capped_at_10(self, populated_db):
        db_file, _ = populated_db
        # Set all memories to priority 9.9
        conn = sqlite3.connect(str(db_file))
        conn.execute("UPDATE memories SET replay_priority = 9.9")
        conn.commit()
        conn.close()

        allo.tool_consolidation_schedule(agent_id="test-agent")
        allo.tool_allostatic_prime(agent_id="test-agent", boost_delta=5.0)

        conn = sqlite3.connect(str(db_file))
        max_priority = conn.execute("SELECT MAX(replay_priority) FROM memories").fetchone()[0]
        conn.close()
        assert max_priority <= 10.0

    def test_negative_boost_rejected(self, patch_db):
        result = allo.tool_allostatic_prime(agent_id="test-agent", boost_delta=-1.0)
        assert result["ok"] is False

    def test_zero_boost_rejected(self, patch_db):
        result = allo.tool_allostatic_prime(agent_id="test-agent", boost_delta=0.0)
        assert result["ok"] is False

    def test_returns_primed_count(self, populated_db):
        allo.tool_consolidation_schedule(agent_id="test-agent")
        result = allo.tool_allostatic_prime(agent_id="test-agent")
        assert result["ok"] is True
        assert isinstance(result["primed"], int)
        assert result["primed"] >= 0


# ---------------------------------------------------------------------------
# demand_forecast tests
# ---------------------------------------------------------------------------


class TestDemandForecast:
    def test_empty_returns_ok(self, patch_db):
        result = allo.tool_demand_forecast(agent_id="test-agent")
        assert result["ok"] is True
        assert result["pending_count"] == 0
        assert result["forecasts"] == []

    def test_shows_pending_forecasts(self, populated_db):
        allo.tool_consolidation_schedule(agent_id="test-agent")
        result = allo.tool_demand_forecast(agent_id="test-agent")
        assert result["ok"] is True
        assert result["pending_count"] > 0

    def test_signal_breakdown_populated(self, populated_db):
        allo.tool_consolidation_schedule(agent_id="test-agent")
        result = allo.tool_demand_forecast(agent_id="test-agent")
        assert isinstance(result["signal_breakdown"], dict)
        assert sum(result["signal_breakdown"].values()) == result["pending_count"]

    def test_forecast_items_have_expected_fields(self, populated_db):
        allo.tool_consolidation_schedule(agent_id="test-agent")
        result = allo.tool_demand_forecast(agent_id="test-agent")
        for item in result["forecasts"]:
            for field in ("id", "memory_id", "predicted_demand_at", "confidence",
                          "signal_source", "fulfilled_at", "content", "category"):
                assert field in item, f"Missing field: {field}"

    def test_excludes_fulfilled_by_default(self, populated_db):
        db_file, _ = populated_db
        allo.tool_consolidation_schedule(agent_id="test-agent")
        # Mark one forecast as fulfilled
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "UPDATE consolidation_forecasts SET fulfilled_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE id = (SELECT id FROM consolidation_forecasts LIMIT 1)"
        )
        conn.commit()
        conn.close()

        result = allo.tool_demand_forecast(agent_id="test-agent", include_fulfilled=False)
        for item in result["forecasts"]:
            assert item["fulfilled_at"] is None

    def test_include_fulfilled_shows_all(self, populated_db):
        db_file, _ = populated_db
        allo.tool_consolidation_schedule(agent_id="test-agent")
        # Mark one forecast fulfilled
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "UPDATE consolidation_forecasts SET fulfilled_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE id = (SELECT id FROM consolidation_forecasts LIMIT 1)"
        )
        conn.commit()
        conn.close()

        result = allo.tool_demand_forecast(agent_id="test-agent", include_fulfilled=True)
        fulfilled = [i for i in result["forecasts"] if i["fulfilled_at"] is not None]
        assert len(fulfilled) >= 1

    def test_limit_respected(self, populated_db):
        allo.tool_consolidation_schedule(agent_id="test-agent")
        result = allo.tool_demand_forecast(agent_id="test-agent", limit=1)
        assert len(result["forecasts"]) <= 1
