"""Tests for brainctl merge — merging two brain.db files."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.merge as merge_mod
import agentmemory.mcp_tools_merge as mcp_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def source_db(tmp_path):
    """Create a source brain.db with some content."""
    path = str(tmp_path / "source.db")
    b = Brain(db_path=path, agent_id="source-agent")
    b.remember("Source memory A", category="lesson", confidence=0.7)
    b.remember("Source memory B", category="decision", confidence=0.9)
    b.log("Source event 1", event_type="observation")
    b.entity("SourceEntity", "concept", observations=["fact1", "fact2"])
    return path


@pytest.fixture
def target_db(tmp_path):
    """Create a target brain.db with existing content."""
    path = str(tmp_path / "target.db")
    b = Brain(db_path=path, agent_id="target-agent")
    b.remember("Target memory X", category="preference", confidence=0.8)
    b.log("Target event 1", event_type="observation")
    b.entity("TargetEntity", "concept", observations=["obs1"])
    return path


@pytest.fixture
def empty_db(tmp_path):
    """Create an empty (schema-only) brain.db."""
    path = str(tmp_path / "empty.db")
    Brain(db_path=path, agent_id="empty-agent")
    return path


@pytest.fixture(autouse=True)
def _set_mcp_db_path(target_db):
    """Point the MCP module at the target DB for each test."""
    orig = mcp_mod.DB_PATH
    mcp_mod.DB_PATH = Path(target_db)
    yield
    mcp_mod.DB_PATH = orig


# ---------------------------------------------------------------------------
# Test 1: Merging empty DB into populated one changes nothing
# ---------------------------------------------------------------------------

class TestMergeEmptyIntoPopulated:
    def test_merge_empty_into_populated(self, empty_db, target_db):
        # Count rows in target before merge
        conn = sqlite3.connect(target_db)
        before_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        before_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        report = merge_mod.merge(source_path=empty_db, target_path=target_db)

        conn = sqlite3.connect(target_db)
        after_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        after_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        # No new memories or events (empty source has only the default agent)
        assert after_mem == before_mem
        assert after_ev == before_ev
        assert report["rows_copied"] <= 1  # at most the default agent row from empty source


# ---------------------------------------------------------------------------
# Test 2: Memories from source appear in target after merge
# ---------------------------------------------------------------------------

class TestMergeMemoriesCopied:
    def test_merge_memories_copied(self, source_db, target_db):
        report = merge_mod.merge(source_path=source_db, target_path=target_db)

        conn = sqlite3.connect(target_db)
        mem = conn.execute(
            "SELECT content FROM memories WHERE content = 'Source memory A'"
        ).fetchone()
        conn.close()

        assert mem is not None, "Source memory A should be present in target after merge"
        assert report["rows_copied"] > 0

    def test_merge_memories_confidence_kept(self, source_db, target_db):
        """Source memory should appear with its confidence intact."""
        merge_mod.merge(source_path=source_db, target_path=target_db)
        conn = sqlite3.connect(target_db)
        row = conn.execute(
            "SELECT confidence FROM memories WHERE content = 'Source memory B'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - 0.9) < 0.001


# ---------------------------------------------------------------------------
# Test 3: Merging same DB twice doesn't create duplicates
# ---------------------------------------------------------------------------

class TestMergeNoDuplicates:
    def test_merge_no_duplicates(self, source_db, target_db):
        merge_mod.merge(source_path=source_db, target_path=target_db)

        conn = sqlite3.connect(target_db)
        count_before = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content = 'Source memory A'"
        ).fetchone()[0]
        conn.close()

        # Merge again
        report2 = merge_mod.merge(source_path=source_db, target_path=target_db)

        conn = sqlite3.connect(target_db)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content = 'Source memory A'"
        ).fetchone()[0]
        conn.close()

        assert count_before == count_after == 1, "Second merge should not create duplicates"
        assert report2["conflicts_resolved"] > 0, "Second merge should detect conflicts"


# ---------------------------------------------------------------------------
# Test 4: Agents from source appear in target
# ---------------------------------------------------------------------------

class TestMergeAgentsMerged:
    def test_merge_agents_merged(self, source_db, target_db):
        merge_mod.merge(source_path=source_db, target_path=target_db)

        conn = sqlite3.connect(target_db)
        agent = conn.execute(
            "SELECT id FROM agents WHERE id = 'source-agent'"
        ).fetchone()
        conn.close()

        assert agent is not None, "source-agent should be present in target after merge"

    def test_existing_target_agent_not_overwritten(self, source_db, target_db):
        """Target agents should remain after merge."""
        merge_mod.merge(source_path=source_db, target_path=target_db)
        conn = sqlite3.connect(target_db)
        agent = conn.execute(
            "SELECT id FROM agents WHERE id = 'target-agent'"
        ).fetchone()
        conn.close()
        assert agent is not None, "target-agent should still be present after merge"


# ---------------------------------------------------------------------------
# Test 5: Events from source are appended
# ---------------------------------------------------------------------------

class TestMergeEventsAppended:
    def test_merge_events_appended(self, source_db, target_db):
        conn = sqlite3.connect(target_db)
        before_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        merge_mod.merge(source_path=source_db, target_path=target_db)

        conn = sqlite3.connect(target_db)
        after_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        assert after_count > before_count, "Source events should be appended to target"

    def test_merge_events_have_new_ids(self, source_db, target_db):
        """Appended events should not share IDs with pre-existing events."""
        # Get max event ID in target before merge
        conn = sqlite3.connect(target_db)
        max_before = conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        conn.close()

        merge_mod.merge(source_path=source_db, target_path=target_db)

        # Source events had id=1; after merge they should get new auto-assigned IDs
        conn = sqlite3.connect(target_db)
        src_events = conn.execute(
            "SELECT id FROM events WHERE summary = 'Source event 1'"
        ).fetchall()
        conn.close()

        assert len(src_events) == 1
        # The new event should have an ID strictly greater than the original max
        # (because it was inserted without specifying id)
        new_id = src_events[0][0]
        assert new_id > max_before


# ---------------------------------------------------------------------------
# Test 6: dry_run=True doesn't modify target
# ---------------------------------------------------------------------------

class TestDryRunNoChanges:
    def test_dry_run_no_changes(self, source_db, target_db):
        conn = sqlite3.connect(target_db)
        before_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        before_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        conn.close()

        report = merge_mod.merge(source_path=source_db, target_path=target_db, dry_run=True)

        conn = sqlite3.connect(target_db)
        after_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        after_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        after_agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        conn.close()

        assert after_mem == before_mem, "dry_run should not change memory count"
        assert after_ev == before_ev, "dry_run should not change event count"
        assert after_agents == before_agents, "dry_run should not change agent count"
        assert report["dry_run"] is True


# ---------------------------------------------------------------------------
# Test 7: Merge report has required keys
# ---------------------------------------------------------------------------

class TestMergeReportStructure:
    def test_merge_report_has_required_keys(self, source_db, target_db):
        report = merge_mod.merge(source_path=source_db, target_path=target_db)
        assert "tables_merged" in report
        assert "rows_copied" in report
        assert "conflicts_resolved" in report
        assert "skipped" in report
        assert "dry_run" in report

    def test_merge_report_types(self, source_db, target_db):
        report = merge_mod.merge(source_path=source_db, target_path=target_db)
        assert isinstance(report["tables_merged"], list)
        assert isinstance(report["rows_copied"], int)
        assert isinstance(report["conflicts_resolved"], int)
        assert isinstance(report["skipped"], list)
        assert isinstance(report["dry_run"], bool)

    def test_dry_run_report_has_required_keys(self, source_db, target_db):
        report = merge_mod.status(source_path=source_db, target_path=target_db)
        assert "tables_merged" in report
        assert "rows_copied" in report
        assert "conflicts_resolved" in report
        assert report["dry_run"] is True

    def test_missing_source_raises(self, target_db):
        with pytest.raises(FileNotFoundError):
            merge_mod.merge(source_path="/nonexistent/brain.db", target_path=target_db)

    def test_missing_target_raises(self, source_db):
        with pytest.raises(FileNotFoundError):
            merge_mod.merge(source_path=source_db, target_path="/nonexistent/brain.db")


# ---------------------------------------------------------------------------
# Test 8: MCP tool returns ok=True
# ---------------------------------------------------------------------------

class TestMcpToolWorks:
    def test_mcp_tool_merge_status_ok(self, source_db, target_db):
        result = mcp_mod.tool_merge_status(
            agent_id="test",
            source_path=source_db,
        )
        assert result["ok"] is True
        assert "tables_merged" in result
        assert "rows_copied" in result

    def test_mcp_tool_merge_execute_dry_run(self, source_db, target_db):
        result = mcp_mod.tool_merge_execute(
            agent_id="test",
            source_path=source_db,
            dry_run=True,
        )
        assert result["ok"] is True
        assert result["dry_run"] is True

    def test_mcp_tool_merge_execute_commits(self, source_db, target_db):
        result = mcp_mod.tool_merge_execute(
            agent_id="test",
            source_path=source_db,
            dry_run=False,
        )
        assert result["ok"] is True
        assert result["rows_copied"] > 0

        # Verify data actually landed
        conn = sqlite3.connect(target_db)
        mem = conn.execute(
            "SELECT content FROM memories WHERE content = 'Source memory A'"
        ).fetchone()
        conn.close()
        assert mem is not None

    def test_mcp_tool_missing_source_returns_error(self, target_db):
        result = mcp_mod.tool_merge_status(
            agent_id="test",
            source_path="/nonexistent/path/brain.db",
        )
        assert result["ok"] is False
        assert "error" in result

    def test_mcp_tool_empty_source_path_returns_error(self, target_db):
        result = mcp_mod.tool_merge_status(
            agent_id="test",
            source_path="",
        )
        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# Test 9: Confidence conflict resolution (keep higher)
# ---------------------------------------------------------------------------

class TestConfidenceConflict:
    def test_higher_confidence_wins(self, tmp_path):
        """When same memory exists in both DBs, keep higher confidence."""
        src_path = str(tmp_path / "src.db")
        tgt_path = str(tmp_path / "tgt.db")

        Brain(db_path=src_path, agent_id="agent1").remember(
            "Shared memory", category="lesson", confidence=0.95
        )
        Brain(db_path=tgt_path, agent_id="agent1").remember(
            "Shared memory", category="lesson", confidence=0.5
        )

        merge_mod.merge(source_path=src_path, target_path=tgt_path)

        conn = sqlite3.connect(tgt_path)
        row = conn.execute(
            "SELECT confidence FROM memories WHERE content = 'Shared memory' AND agent_id = 'agent1'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] >= 0.95 - 0.001, "Higher confidence from source should be applied"

    def test_lower_confidence_does_not_overwrite(self, tmp_path):
        """When source has lower confidence, target keeps its own."""
        src_path = str(tmp_path / "src.db")
        tgt_path = str(tmp_path / "tgt.db")

        Brain(db_path=src_path, agent_id="agent1").remember(
            "Shared memory", category="lesson", confidence=0.3
        )
        Brain(db_path=tgt_path, agent_id="agent1").remember(
            "Shared memory", category="lesson", confidence=0.9
        )

        merge_mod.merge(source_path=src_path, target_path=tgt_path)

        conn = sqlite3.connect(tgt_path)
        row = conn.execute(
            "SELECT confidence FROM memories WHERE content = 'Shared memory' AND agent_id = 'agent1'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] >= 0.9 - 0.001, "Target's higher confidence should be preserved"


# ---------------------------------------------------------------------------
# Test 10: Table filter (--tables)
# ---------------------------------------------------------------------------

class TestTableFilter:
    def test_tables_filter_limits_merge(self, source_db, target_db):
        """When --tables=agents, only agents should be merged (not memories)."""
        conn = sqlite3.connect(target_db)
        before_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()

        report = merge_mod.merge(
            source_path=source_db,
            target_path=target_db,
            tables=["agents"],
        )

        conn = sqlite3.connect(target_db)
        after_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()

        assert after_mem == before_mem, "memories should not be merged when tables=['agents']"
        assert "agents" in report["tables_merged"]
        assert "memories" not in report["tables_merged"]

    def test_entities_merged_and_observations_merged(self, tmp_path):
        """Merging entities with overlapping name should union observations."""
        src_path = str(tmp_path / "src.db")
        tgt_path = str(tmp_path / "tgt.db")

        Brain(db_path=src_path, agent_id="agent1").entity(
            "SharedEnt", "concept", observations=["obs-from-src"]
        )
        Brain(db_path=tgt_path, agent_id="agent1").entity(
            "SharedEnt", "concept", observations=["obs-from-tgt"]
        )

        merge_mod.merge(source_path=src_path, target_path=tgt_path)

        conn = sqlite3.connect(tgt_path)
        row = conn.execute(
            "SELECT observations FROM entities WHERE name = 'SharedEnt' AND retired_at IS NULL"
        ).fetchone()
        conn.close()

        assert row is not None
        obs = json.loads(row[0])
        assert "obs-from-src" in obs, "Source observation should be merged into entity"
        assert "obs-from-tgt" in obs, "Original target observation should be preserved"
