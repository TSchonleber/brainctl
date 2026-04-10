"""Tests for mcp_tools_belief_merge — CRDT-inspired belief merge and conflict resolution."""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_belief_merge as merge_mod
from agentmemory.mcp_tools_belief_merge import (
    TOOLS, DISPATCH,
    tool_belief_conflicts_scan,
    tool_belief_merge,
    tool_belief_propagate,
    tool_belief_consensus,
    tool_belief_diff,
)
from mcp.types import Tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_db(tmp_path: Path) -> Path:
    """Create a full-schema DB and return its path."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="default")
    return db_file


def _seed_agent(db_path: Path, agent_id: str) -> None:
    """Insert a minimal agent row so FK constraints pass."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?,?,?,?,"
        "strftime('%Y-%m-%dT%H:%M:%S','now'),strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id, "test", "active"),
    )
    conn.commit()
    conn.close()


def _insert_belief(db_path: Path, agent_id: str, topic: str, content: str,
                   confidence: float = 0.8, last_updated_at: str | None = None) -> int:
    """Insert an agent belief and return its id."""
    conn = sqlite3.connect(str(db_path))
    now = last_updated_at or "2026-01-01T12:00:00"
    conn.execute(
        "INSERT OR REPLACE INTO agent_beliefs "
        "(agent_id, topic, belief_content, confidence, is_assumption, "
        "last_updated_at, created_at, updated_at) "
        "VALUES (?,?,?,?,0,?,?,?)",
        (agent_id, topic, content, confidence, now, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?", (agent_id, topic)
    ).fetchone()
    conn.close()
    return row[0]


@pytest.fixture(autouse=True)
def point_module_to_tmp(tmp_path, monkeypatch):
    """Redirect the module's DB_PATH to a fresh tmp database for each test."""
    db_file = _init_db(tmp_path)
    monkeypatch.setattr(merge_mod, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# Module interface contract
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list_of_tool(self):
        assert isinstance(TOOLS, list)
        assert len(TOOLS) == 5
        for t in TOOLS:
            assert isinstance(t, Tool)

    def test_dispatch_keys_match_tool_names(self):
        tool_names = {t.name for t in TOOLS}
        assert tool_names == set(DISPATCH.keys())

    def test_dispatch_values_are_callable(self):
        for name, fn in DISPATCH.items():
            assert callable(fn), f"{name!r} is not callable"

    def test_expected_tool_names_present(self):
        names = {t.name for t in TOOLS}
        expected = {
            "belief_conflicts_scan",
            "belief_merge",
            "belief_propagate",
            "belief_consensus",
            "belief_diff",
        }
        assert names == expected


# ---------------------------------------------------------------------------
# belief_conflicts_scan
# ---------------------------------------------------------------------------

class TestBeliefConflictsScan:
    def test_no_beliefs_returns_empty(self):
        result = tool_belief_conflicts_scan()
        assert result["ok"] is True
        assert result["conflict_count"] == 0
        assert result["conflicts"] == []

    def test_single_agent_no_conflict(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "solo-agent")
        _insert_belief(db_file, "solo-agent", "project:x:status", "active")

        result = tool_belief_conflicts_scan()
        assert result["ok"] is True
        assert result["conflict_count"] == 0

    def test_detects_conflict_between_two_agents(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "agent-alpha")
        _seed_agent(db_file, "agent-beta")
        _insert_belief(db_file, "agent-alpha", "project:y:status", "complete", confidence=0.9)
        _insert_belief(db_file, "agent-beta",  "project:y:status", "in-progress", confidence=0.8)

        result = tool_belief_conflicts_scan()
        assert result["ok"] is True
        assert result["conflict_count"] == 1
        conflict = result["conflicts"][0]
        assert conflict["topic"] == "project:y:status"
        assert len(conflict["agents"]) == 2
        assert len(conflict["beliefs"]) == 2

    def test_severity_high_when_confident_equally(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "agent-1")
        _seed_agent(db_file, "agent-2")
        _insert_belief(db_file, "agent-1", "global:truth", "yes", confidence=0.9)
        _insert_belief(db_file, "agent-2", "global:truth", "no",  confidence=0.91)

        result = tool_belief_conflicts_scan()
        assert result["ok"] is True
        assert result["conflicts"][0]["severity"] == "high"

    def test_severity_low_when_large_confidence_gap(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "strong-a")
        _seed_agent(db_file, "weak-b")
        _insert_belief(db_file, "strong-a", "global:fact", "correct", confidence=0.95)
        _insert_belief(db_file, "weak-b",   "global:fact", "wrong",   confidence=0.35)

        result = tool_belief_conflicts_scan()
        assert result["ok"] is True
        assert result["conflicts"][0]["severity"] == "low"

    def test_below_min_confidence_excluded(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "low-conf-a")
        _seed_agent(db_file, "low-conf-b")
        _insert_belief(db_file, "low-conf-a", "soft:topic", "maybe", confidence=0.2)
        _insert_belief(db_file, "low-conf-b", "soft:topic", "maybe not", confidence=0.2)

        result = tool_belief_conflicts_scan(min_confidence=0.3)
        assert result["ok"] is True
        assert result["conflict_count"] == 0

    def test_topic_filter(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "a")
        _seed_agent(db_file, "b")
        _insert_belief(db_file, "a", "project:foo:status", "done", confidence=0.8)
        _insert_belief(db_file, "b", "project:foo:status", "open", confidence=0.8)
        _insert_belief(db_file, "a", "project:bar:status", "done", confidence=0.8)
        _insert_belief(db_file, "b", "project:bar:status", "open", confidence=0.8)

        result = tool_belief_conflicts_scan(topic_filter="project:foo")
        assert result["ok"] is True
        assert result["conflict_count"] == 1
        assert "foo" in result["conflicts"][0]["topic"]

    def test_invalidated_beliefs_excluded(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "old-a")
        _seed_agent(db_file, "new-b")
        _insert_belief(db_file, "old-a", "topic:z", "stale content", confidence=0.8)
        _insert_belief(db_file, "new-b", "topic:z", "fresh content", confidence=0.8)

        # Invalidate old-a's belief
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "UPDATE agent_beliefs SET invalidated_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE agent_id='old-a' AND topic='topic:z'"
        )
        conn.commit()
        conn.close()

        result = tool_belief_conflicts_scan()
        # Only new-b remains active — no conflict
        assert result["ok"] is True
        assert result["conflict_count"] == 0


# ---------------------------------------------------------------------------
# belief_merge
# ---------------------------------------------------------------------------

class TestBeliefMerge:
    def test_dry_run_returns_preview_without_changes(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "m-agent-a")
        _seed_agent(db_file, "m-agent-b")
        _insert_belief(db_file, "m-agent-a", "project:merge:status", "done",        confidence=0.9)
        _insert_belief(db_file, "m-agent-b", "project:merge:status", "in-progress", confidence=0.7)

        result = tool_belief_merge(topic="project:merge:status", strategy="highest_confidence", dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["winner_belief_id"] is not None

        # Verify nothing was invalidated in the DB
        conn = sqlite3.connect(str(db_file))
        active = conn.execute(
            "SELECT COUNT(*) FROM agent_beliefs WHERE topic=? AND invalidated_at IS NULL",
            ("project:merge:status",)
        ).fetchone()[0]
        conn.close()
        assert active == 2

    def test_highest_confidence_strategy(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "hc-a")
        _seed_agent(db_file, "hc-b")
        bid_a = _insert_belief(db_file, "hc-a", "topic:hc", "winner content", confidence=0.95)
        bid_b = _insert_belief(db_file, "hc-b", "topic:hc", "loser content",  confidence=0.60)

        result = tool_belief_merge(topic="topic:hc", strategy="highest_confidence", dry_run=False)
        assert result["ok"] is True
        assert result["dry_run"] is False
        assert result["winner_belief_id"] == bid_a
        assert bid_b in result["invalidated_ids"]

        # Verify in DB
        conn = sqlite3.connect(str(db_file))
        row_b = conn.execute(
            "SELECT invalidated_at FROM agent_beliefs WHERE id=?", (bid_b,)
        ).fetchone()
        conn.close()
        assert row_b[0] is not None

    def test_most_recent_strategy(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "mr-a")
        _seed_agent(db_file, "mr-b")
        bid_old = _insert_belief(db_file, "mr-a", "topic:mr", "old content",   confidence=0.8, last_updated_at="2025-01-01T00:00:00")
        bid_new = _insert_belief(db_file, "mr-b", "topic:mr", "newer content", confidence=0.7, last_updated_at="2026-03-01T00:00:00")

        result = tool_belief_merge(topic="topic:mr", strategy="most_recent", dry_run=False)
        assert result["ok"] is True
        assert result["winner_belief_id"] == bid_new
        assert bid_old in result["invalidated_ids"]

    def test_weighted_average_strategy_creates_merged_belief(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "wa-a")
        _seed_agent(db_file, "wa-b")
        _seed_agent(db_file, "mcp-client")
        _insert_belief(db_file, "wa-a", "topic:wa", "view A", confidence=0.8)
        _insert_belief(db_file, "wa-b", "topic:wa", "view B", confidence=0.6)

        result = tool_belief_merge(
            topic="topic:wa", strategy="weighted_average",
            agent_id="mcp-client", dry_run=False
        )
        assert result["ok"] is True
        assert "Merged belief" in result["merged_content"]
        assert result["confidence"] > 0

        # New merged belief should exist
        conn = sqlite3.connect(str(db_file))
        merged = conn.execute(
            "SELECT belief_content FROM agent_beliefs WHERE agent_id='mcp-client' "
            "AND topic='topic:wa' AND invalidated_at IS NULL"
        ).fetchone()
        conn.close()
        assert merged is not None
        assert "view A" in merged[0]
        assert "view B" in merged[0]

    def test_human_review_strategy_creates_conflict_record(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "hr-a")
        _seed_agent(db_file, "hr-b")
        _insert_belief(db_file, "hr-a", "topic:hr", "view A", confidence=0.8)
        _insert_belief(db_file, "hr-b", "topic:hr", "view B", confidence=0.7)

        result = tool_belief_merge(topic="topic:hr", strategy="human_review", dry_run=False)
        assert result["ok"] is True

        # Verify belief_conflicts record was created
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        conflicts = conn.execute(
            "SELECT * FROM belief_conflicts WHERE topic='topic:hr' AND resolved_at IS NULL"
        ).fetchall()
        conn.close()
        assert len(conflicts) >= 1
        assert conflicts[0]["requires_supervisor_intervention"] == 1

    def test_human_review_marks_beliefs_as_assumptions(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "asm-a")
        _seed_agent(db_file, "asm-b")
        _insert_belief(db_file, "asm-a", "topic:asm", "view A", confidence=0.8)
        _insert_belief(db_file, "asm-b", "topic:asm", "view B", confidence=0.7)

        tool_belief_merge(topic="topic:asm", strategy="human_review", dry_run=False)

        conn = sqlite3.connect(str(db_file))
        rows = conn.execute(
            "SELECT is_assumption FROM agent_beliefs WHERE topic='topic:asm' AND invalidated_at IS NULL"
        ).fetchall()
        conn.close()
        assert all(r[0] == 1 for r in rows)

    def test_unknown_strategy_returns_error(self):
        result = tool_belief_merge(topic="any:topic", strategy="magic_merge")
        assert result["ok"] is False
        assert "Unknown strategy" in result["error"]

    def test_insufficient_beliefs_returns_error(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "lone-agent")
        _insert_belief(db_file, "lone-agent", "topic:lone", "only belief", confidence=0.8)

        result = tool_belief_merge(topic="topic:lone", strategy="highest_confidence", dry_run=False)
        assert result["ok"] is False
        assert "Not enough active beliefs" in result["error"]


# ---------------------------------------------------------------------------
# belief_consensus
# ---------------------------------------------------------------------------

class TestBeliefConsensus:
    def test_no_beliefs_returns_empty(self):
        result = tool_belief_consensus(topic="nonexistent:topic")
        assert result["ok"] is True
        assert result["agent_count"] == 0
        assert result["consensus_content"] is None

    def test_unanimous_agreement_score_is_one(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "con-a")
        _seed_agent(db_file, "con-b")
        _seed_agent(db_file, "con-c")
        for agent in ["con-a", "con-b", "con-c"]:
            _insert_belief(db_file, agent, "topic:consensus", "same truth", confidence=0.8)

        result = tool_belief_consensus(topic="topic:consensus")
        assert result["ok"] is True
        assert result["agreement_score"] == 1.0
        assert result["consensus_content"] == "same truth"
        assert result["agent_count"] == 3

    def test_split_agreement_score_below_one(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "split-a")
        _seed_agent(db_file, "split-b")
        _insert_belief(db_file, "split-a", "topic:split", "option A", confidence=0.8)
        _insert_belief(db_file, "split-b", "topic:split", "option B", confidence=0.8)

        result = tool_belief_consensus(topic="topic:split")
        assert result["ok"] is True
        assert result["agreement_score"] < 1.0
        assert result["agent_count"] == 2

    def test_consensus_picks_highest_summed_confidence(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "maj-a")
        _seed_agent(db_file, "maj-b")
        _seed_agent(db_file, "maj-c")
        _insert_belief(db_file, "maj-a", "topic:majority", "majority view", confidence=0.9)
        _insert_belief(db_file, "maj-b", "topic:majority", "majority view", confidence=0.8)
        _insert_belief(db_file, "maj-c", "topic:majority", "minority view", confidence=0.7)

        result = tool_belief_consensus(topic="topic:majority")
        assert result["ok"] is True
        assert result["consensus_content"] == "majority view"
        assert result["agreement_score"] > 0.5

    def test_min_confidence_filter_excludes_weak_beliefs(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "mf-a")
        _seed_agent(db_file, "mf-b")
        _insert_belief(db_file, "mf-a", "topic:weak", "strong opinion", confidence=0.8)
        _insert_belief(db_file, "mf-b", "topic:weak", "other view",    confidence=0.15)

        result = tool_belief_consensus(topic="topic:weak", min_confidence=0.3)
        assert result["ok"] is True
        assert result["agent_count"] == 1
        assert result["consensus_content"] == "strong opinion"


# ---------------------------------------------------------------------------
# belief_diff
# ---------------------------------------------------------------------------

class TestBeliefDiff:
    def test_no_shared_topics(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "diff-a")
        _seed_agent(db_file, "diff-b")
        _insert_belief(db_file, "diff-a", "topic:only-a", "some content", confidence=0.8)
        _insert_belief(db_file, "diff-b", "topic:only-b", "other content", confidence=0.8)

        result = tool_belief_diff(agent_a="diff-a", agent_b="diff-b")
        assert result["ok"] is True
        assert result["shared_topics"] == 0
        assert result["divergent"] == []
        assert result["aligned"] == []

    def test_aligned_topics_detected(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "aligned-a")
        _seed_agent(db_file, "aligned-b")
        _insert_belief(db_file, "aligned-a", "shared:topic", "same content", confidence=0.8)
        _insert_belief(db_file, "aligned-b", "shared:topic", "same content", confidence=0.7)

        result = tool_belief_diff(agent_a="aligned-a", agent_b="aligned-b")
        assert result["ok"] is True
        assert result["shared_topics"] == 1
        assert len(result["aligned"]) == 1
        assert len(result["divergent"]) == 0
        assert result["aligned"][0]["shared_content"] == "same content"

    def test_divergent_topics_detected(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "div-a")
        _seed_agent(db_file, "div-b")
        _insert_belief(db_file, "div-a", "shared:view", "view A", confidence=0.9)
        _insert_belief(db_file, "div-b", "shared:view", "view B", confidence=0.6)

        result = tool_belief_diff(agent_a="div-a", agent_b="div-b")
        assert result["ok"] is True
        assert result["shared_topics"] == 1
        assert len(result["divergent"]) == 1
        entry = result["divergent"][0]
        assert entry["topic"] == "shared:view"
        assert entry["belief_a"] == "view A"
        assert entry["belief_b"] == "view B"
        assert abs(entry["delta"] - 0.3) < 1e-5

    def test_mixed_aligned_and_divergent(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "mix-a")
        _seed_agent(db_file, "mix-b")
        _insert_belief(db_file, "mix-a", "topic:same",  "agree",    confidence=0.8)
        _insert_belief(db_file, "mix-b", "topic:same",  "agree",    confidence=0.8)
        _insert_belief(db_file, "mix-a", "topic:diff",  "option A", confidence=0.9)
        _insert_belief(db_file, "mix-b", "topic:diff",  "option B", confidence=0.7)

        result = tool_belief_diff(agent_a="mix-a", agent_b="mix-b")
        assert result["ok"] is True
        assert result["shared_topics"] == 2
        assert len(result["aligned"]) == 1
        assert len(result["divergent"]) == 1

    def test_limit_respected(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "lim-a")
        _seed_agent(db_file, "lim-b")
        for i in range(10):
            _insert_belief(db_file, "lim-a", f"topic:{i}", f"A view {i}", confidence=0.8)
            _insert_belief(db_file, "lim-b", f"topic:{i}", f"B view {i}", confidence=0.7)

        result = tool_belief_diff(agent_a="lim-a", agent_b="lim-b", limit=3)
        assert result["ok"] is True
        total = len(result["divergent"]) + len(result["aligned"])
        assert total <= 3


# ---------------------------------------------------------------------------
# belief_propagate
# ---------------------------------------------------------------------------

class TestBeliefPropagate:
    def test_no_source_belief_returns_error(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "prop-src")

        result = tool_belief_propagate(
            source_agent_id="prop-src",
            topic="nonexistent:topic",
        )
        assert result["ok"] is False
        assert "No active belief found" in result["error"]

    def test_propagate_returns_expected_fields(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "src-agent")
        _insert_belief(db_file, "src-agent", "topic:prop", "propagated truth", confidence=0.8)

        result = tool_belief_propagate(
            source_agent_id="src-agent",
            topic="topic:prop",
            min_shared_context_score=0.0,  # bypass context check
        )
        assert result["ok"] is True
        assert result["topic"] == "topic:prop"
        assert result["source_agent_id"] == "src-agent"
        assert abs(result["original_confidence"] - 0.8) < 1e-6
        # Propagated confidence should apply decay
        expected_prop = round(0.8 * 0.85, 6)
        assert abs(result["propagated_confidence"] - expected_prop) < 1e-5

    def test_propagation_decay_applied(self):
        db_file = merge_mod.DB_PATH
        _seed_agent(db_file, "decay-src")
        _seed_agent(db_file, "decay-dst")
        _insert_belief(db_file, "decay-src", "topic:decay", "truth", confidence=1.0)

        # Add workspace_acks overlap so shared context score > 0
        conn = sqlite3.connect(str(db_file))
        # Insert a real memory so workspace_broadcasts FK is satisfied
        conn.execute(
            "INSERT INTO memories (agent_id, category, scope, content, created_at, updated_at) "
            "VALUES ('decay-src', 'project', 'global', 'test memory', "
            "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        memory_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Insert a broadcast tied to that memory
        conn.execute(
            "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) "
            "VALUES (?, 'decay-src', 0.5, 'test', 'global', 'test')",
            (memory_id,),
        )
        broadcast_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO workspace_acks (broadcast_id, agent_id, acked_at) VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (broadcast_id, "decay-src"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_acks (broadcast_id, agent_id, acked_at) VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (broadcast_id, "decay-dst"),
        )
        conn.commit()
        conn.close()

        result = tool_belief_propagate(
            source_agent_id="decay-src",
            topic="topic:decay",
            min_shared_context_score=0.5,
        )
        assert result["ok"] is True
        assert "decay-dst" in result["propagated_to"]

        # Verify inserted belief in DB has decayed confidence
        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT confidence FROM agent_beliefs WHERE agent_id='decay-dst' AND topic='topic:decay'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - 0.85) < 1e-5
