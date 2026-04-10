"""Tests for retrieval enhancements: multi_pass, temporal_expand_hours, vector flag.

Issues: #36 (multi_pass SDM convergence), #35 (temporal_expand_hours TCM),
        #19 (vector flag on tool_search).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_server as ms


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_memories(tmp_path, monkeypatch):
    """Fresh DB with a handful of memories."""
    db_file = tmp_path / "brain.db"
    # Brain must be created BEFORE monkeypatching to ensure schema is initialized
    from agentmemory.brain import Brain
    brain = Brain(db_path=str(db_file), agent_id="test")
    brain.remember("caching strategy for the API endpoint reduces latency", category="convention")
    brain.remember("authentication flow requires JWT token validation", category="convention")
    brain.remember("database connection pooling improves throughput", category="convention")
    monkeypatch.setattr(ms, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# multi_pass tests (#36)
# ---------------------------------------------------------------------------


class TestMultiPass:
    def test_multi_pass_false_returns_ok(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching API",
            multi_pass=False,
        )
        assert result["ok"] is True

    def test_multi_pass_true_returns_ok(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching API",
            multi_pass=True,
        )
        assert result["ok"] is True

    def test_multi_pass_returns_memories_list(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching API endpoint",
            multi_pass=True,
        )
        assert "memories" in result
        assert isinstance(result["memories"], list)

    def test_multi_pass_does_not_duplicate_results(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching",
            multi_pass=True,
        )
        ids = [m["id"] for m in result["memories"]]
        assert len(ids) == len(set(ids)), "multi_pass produced duplicate IDs"

    def test_multi_pass_accepts_multi_pass_param(self, db_with_memories):
        # Calling with multi_pass=True must not raise
        result = ms.tool_memory_search(
            agent_id="test",
            query="authentication JWT",
            multi_pass=True,
        )
        assert result["ok"] is True

    def test_multi_pass_empty_db_ok(self, tmp_path, monkeypatch):
        db_file = tmp_path / "brain_empty.db"
        from agentmemory.brain import Brain
        Brain(db_path=str(db_file), agent_id="test")
        monkeypatch.setattr(ms, "DB_PATH", db_file)
        result = ms.tool_memory_search(
            agent_id="test",
            query="nothing here",
            multi_pass=True,
        )
        assert result["ok"] is True
        assert result["memories"] == []


# ---------------------------------------------------------------------------
# temporal_expand_hours tests (#35)
# ---------------------------------------------------------------------------


class TestTemporalExpand:
    def test_temporal_expand_zero_is_default(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching",
            temporal_expand_hours=0,
        )
        assert result["ok"] is True

    def test_temporal_expand_accepts_nonzero(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching",
            temporal_expand_hours=24,
        )
        assert result["ok"] is True

    def test_temporal_expand_returns_memories(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching",
            temporal_expand_hours=24,
        )
        assert "memories" in result
        assert isinstance(result["memories"], list)

    def test_temporal_expand_no_duplicates(self, db_with_memories):
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching",
            temporal_expand_hours=24,
        )
        ids = [m["id"] for m in result["memories"]]
        assert len(ids) == len(set(ids)), "temporal_expand produced duplicate IDs"

    def test_temporal_neighbors_flagged(self, db_with_memories):
        # All 3 memories were written close together, so temporal expand should
        # flag added neighbors (if any). Just verify no crash and structure is ok.
        result = ms.tool_memory_search(
            agent_id="test",
            query="caching",
            temporal_expand_hours=1,
        )
        for m in result["memories"]:
            if m.get("_temporal_neighbor"):
                # must still have id and content
                assert "id" in m


# ---------------------------------------------------------------------------
# vector flag on tool_search (#19)
# ---------------------------------------------------------------------------


class TestVectorSearch:
    def test_vector_false_returns_ok(self, db_with_memories):
        result = ms.tool_search(agent_id="test", query="caching API", vector=False)
        assert result["ok"] is True

    def test_vector_true_does_not_crash(self, db_with_memories):
        # Ollama may not be running, but the function must not crash
        result = ms.tool_search(agent_id="test", query="caching API", vector=True)
        assert result["ok"] is True

    def test_vector_true_returns_results_key(self, db_with_memories):
        result = ms.tool_search(agent_id="test", query="API", vector=True)
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_vector_false_results_non_empty_with_matches(self, db_with_memories):
        result = ms.tool_search(agent_id="test", query="caching", vector=False)
        assert result["ok"] is True
        # FTS should find at least one result
        assert result["count"] >= 1

    def test_search_accepts_vector_param(self, db_with_memories):
        # Both True and False must be accepted without error
        r1 = ms.tool_search(agent_id="test", query="JWT", vector=False)
        r2 = ms.tool_search(agent_id="test", query="JWT", vector=True)
        assert r1["ok"] is True
        assert r2["ok"] is True
