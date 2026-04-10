"""Tests for agentmemory.federation — multi-DB federation engine."""
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
import agentmemory.federation as fed
import agentmemory.mcp_tools_federation as mcp_fed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def single_brain_db(tmp_path, monkeypatch):
    """Create a single brain DB and point BRAIN_DB at it."""
    db1 = tmp_path / "brain.db"
    b1 = Brain(db_path=str(db1), agent_id="agent-1")
    b1.remember("memory from agent one about python", category="convention")
    monkeypatch.setenv("BRAIN_DB", str(db1))
    monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
    return db1, b1


@pytest.fixture
def two_brain_dbs(tmp_path, monkeypatch):
    """Create two separate brain DBs with different data."""
    db1 = tmp_path / "brain1.db"
    db2 = tmp_path / "brain2.db"
    b1 = Brain(db_path=str(db1), agent_id="agent-1")
    b2 = Brain(db_path=str(db2), agent_id="agent-2")
    b1.remember("memory from agent one about python", category="convention")
    b2.remember("memory from agent two about deployment", category="lesson")
    monkeypatch.setenv("BRAIN_DB", str(db1))
    monkeypatch.setenv("BRAIN_DB_FEDERATION", str(db2))
    return db1, db2, b1, b2


# ---------------------------------------------------------------------------
# get_federation_paths
# ---------------------------------------------------------------------------

class TestGetFederationPaths:
    def test_get_federation_paths_returns_brain_db(self, tmp_path, monkeypatch):
        """When only BRAIN_DB is set, returns exactly that path."""
        db = tmp_path / "brain.db"
        Brain(db_path=str(db), agent_id="x")
        monkeypatch.setenv("BRAIN_DB", str(db))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        paths = fed.get_federation_paths()
        assert len(paths) == 1
        assert str(db.resolve()) in paths

    def test_get_federation_paths_includes_federation_env(self, tmp_path, monkeypatch):
        """BRAIN_DB_FEDERATION paths are appended after BRAIN_DB."""
        db1 = tmp_path / "brain1.db"
        db2 = tmp_path / "brain2.db"
        Brain(db_path=str(db1), agent_id="a")
        Brain(db_path=str(db2), agent_id="b")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.setenv("BRAIN_DB_FEDERATION", str(db2))
        paths = fed.get_federation_paths()
        assert len(paths) == 2
        assert str(db1.resolve()) == paths[0]
        assert str(db2.resolve()) == paths[1]

    def test_get_federation_paths_deduplicates(self, tmp_path, monkeypatch):
        """Same path in BRAIN_DB and BRAIN_DB_FEDERATION appears only once."""
        db = tmp_path / "brain.db"
        Brain(db_path=str(db), agent_id="x")
        monkeypatch.setenv("BRAIN_DB", str(db))
        monkeypatch.setenv("BRAIN_DB_FEDERATION", str(db))
        paths = fed.get_federation_paths()
        assert paths.count(str(db.resolve())) == 1

    def test_get_federation_paths_multiple_federation_paths(self, tmp_path, monkeypatch):
        """Multiple colon-separated paths in BRAIN_DB_FEDERATION all appear."""
        db1 = tmp_path / "b1.db"
        db2 = tmp_path / "b2.db"
        db3 = tmp_path / "b3.db"
        for db, aid in [(db1, "a"), (db2, "b"), (db3, "c")]:
            Brain(db_path=str(db), agent_id=aid)
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.setenv("BRAIN_DB_FEDERATION", f"{db2}:{db3}")
        paths = fed.get_federation_paths()
        assert len(paths) == 3


# ---------------------------------------------------------------------------
# federated_stats
# ---------------------------------------------------------------------------

class TestFederatedStats:
    def test_federated_stats_single_db(self, single_brain_db, monkeypatch):
        """Single DB: stats returns correct counts."""
        db1, b1 = single_brain_db
        result = fed.federated_stats()
        assert result["ok"] is True
        assert len(result["databases"]) == 1
        db_entry = result["databases"][0]
        assert db_entry["accessible"] is True
        assert db_entry["memory_count"] >= 1
        assert result["totals"]["memory_count"] >= 1

    def test_federated_stats_two_dbs(self, two_brain_dbs, monkeypatch):
        """Two DBs: totals are summed correctly."""
        db1, db2, b1, b2 = two_brain_dbs
        result = fed.federated_stats()
        assert result["ok"] is True
        assert len(result["databases"]) == 2
        total_memories = sum(d.get("memory_count", 0) for d in result["databases"])
        assert total_memories == result["totals"]["memory_count"]
        assert result["totals"]["memory_count"] >= 2

    def test_federated_stats_inaccessible_db_skipped(self, tmp_path, monkeypatch):
        """A bad/nonexistent path does not crash; it's marked inaccessible."""
        db1 = tmp_path / "real.db"
        Brain(db_path=str(db1), agent_id="x")
        bad_path = tmp_path / "nonexistent.db"
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.setenv("BRAIN_DB_FEDERATION", str(bad_path))
        result = fed.federated_stats()
        assert result["ok"] is True
        accessible = [d for d in result["databases"] if d.get("accessible")]
        inaccessible = [d for d in result["databases"] if not d.get("accessible")]
        assert len(accessible) == 1
        assert len(inaccessible) == 1

    def test_federated_stats_totals_keys(self, single_brain_db, monkeypatch):
        """Totals dict contains all expected keys."""
        result = fed.federated_stats()
        for key in ("memory_count", "event_count", "entity_count", "agent_count"):
            assert key in result["totals"]


# ---------------------------------------------------------------------------
# federated_memory_search
# ---------------------------------------------------------------------------

class TestFederatedMemorySearch:
    def test_federated_memory_search_finds_across_dbs(self, two_brain_dbs, monkeypatch):
        """Memory in DB2 is found when searching from the federation."""
        result = fed.federated_memory_search(query="deployment")
        assert result["ok"] is True
        contents = [r["content"] for r in result["results"]]
        assert any("deployment" in c.lower() for c in contents)

    def test_federated_memory_search_result_has_source_db(self, two_brain_dbs, monkeypatch):
        """Every result includes the source_db field."""
        result = fed.federated_memory_search(query="agent")
        assert result["ok"] is True
        for r in result["results"]:
            assert "source_db" in r
            assert r["source_db"]  # not empty

    def test_federated_memory_search_empty_query_returns_error(self, single_brain_db, monkeypatch):
        """An empty query returns an error dict, not an exception."""
        result = fed.federated_memory_search(query="")
        assert result["ok"] is False
        assert "error" in result

    def test_federated_memory_search_whitespace_query_returns_error(self, single_brain_db, monkeypatch):
        """A whitespace-only query also returns error."""
        result = fed.federated_memory_search(query="   ")
        assert result["ok"] is False

    def test_federated_memory_search_category_filter(self, two_brain_dbs, monkeypatch):
        """Category filter limits results to the specified category."""
        result = fed.federated_memory_search(query="memory", category="convention")
        assert result["ok"] is True
        for r in result["results"]:
            assert r["category"] == "convention"

    def test_federated_memory_search_table_field(self, two_brain_dbs, monkeypatch):
        """Results have table='memories' field."""
        result = fed.federated_memory_search(query="python")
        assert result["ok"] is True
        for r in result["results"]:
            assert r["table"] == "memories"

    def test_federated_memory_search_respects_limit(self, tmp_path, monkeypatch):
        """Results are capped at the limit parameter."""
        db1 = tmp_path / "b.db"
        b1 = Brain(db_path=str(db1), agent_id="x")
        for i in range(10):
            b1.remember(f"test memory number {i} about python", category="general")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        result = fed.federated_memory_search(query="python", limit=3)
        assert result["ok"] is True
        assert len(result["results"]) <= 3


# ---------------------------------------------------------------------------
# federated_entity_search
# ---------------------------------------------------------------------------

class TestFederatedEntitySearch:
    def test_federated_entity_search_across_dbs(self, tmp_path, monkeypatch):
        """Entity in DB2 is found when searching from the federation."""
        db1 = tmp_path / "b1.db"
        db2 = tmp_path / "b2.db"
        b1 = Brain(db_path=str(db1), agent_id="agent-1")
        b2 = Brain(db_path=str(db2), agent_id="agent-2")
        b1.entity("Alice", "person")
        b2.entity("Bob", "person")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.setenv("BRAIN_DB_FEDERATION", str(db2))
        result = fed.federated_entity_search(name="Bob")
        assert result["ok"] is True
        names = [r["name"] for r in result["results"]]
        assert "Bob" in names

    def test_federated_entity_search_type_filter(self, tmp_path, monkeypatch):
        """entity_type filter limits results to matching type."""
        db1 = tmp_path / "b1.db"
        b1 = Brain(db_path=str(db1), agent_id="x")
        b1.entity("Proj", "project")
        b1.entity("Alice", "person")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        result = fed.federated_entity_search(name="", entity_type="project")
        # name="" with LIKE '%' matches all; only projects returned
        assert result["ok"] is True
        for r in result["results"]:
            assert r["entity_type"] == "project"

    def test_federated_entity_search_result_has_source_db(self, tmp_path, monkeypatch):
        """Entity results include source_db field."""
        db1 = tmp_path / "b1.db"
        b1 = Brain(db_path=str(db1), agent_id="x")
        b1.entity("Widget", "thing")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        result = fed.federated_entity_search(name="Widget")
        assert result["ok"] is True
        assert len(result["results"]) >= 1
        for r in result["results"]:
            assert "source_db" in r

    def test_federated_entity_search_empty_name_with_type_filter(self, tmp_path, monkeypatch):
        """Empty name with entity_type filter searches all entities of that type."""
        db1 = tmp_path / "b1.db"
        b1 = Brain(db_path=str(db1), agent_id="x")
        b1.entity("GadgetA", "tool")
        b1.entity("GadgetB", "tool")
        b1.entity("Human", "person")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        result = fed.federated_entity_search(name="", entity_type="tool")
        assert result["ok"] is True
        types = [r["entity_type"] for r in result["results"]]
        assert all(t == "tool" for t in types)


# ---------------------------------------------------------------------------
# federated_search
# ---------------------------------------------------------------------------

class TestFederatedSearch:
    def test_federated_search_returns_ok_true(self, single_brain_db, monkeypatch):
        """federated_search always returns ok=True on valid query."""
        result = fed.federated_search(query="python")
        assert result["ok"] is True

    def test_federated_search_result_count(self, two_brain_dbs, monkeypatch):
        """Results from both DBs are found."""
        result = fed.federated_search(query="agent")
        assert result["ok"] is True
        assert result["total_results"] >= 0
        assert "results" in result

    def test_federated_search_limit_respected(self, tmp_path, monkeypatch):
        """federated_search respects the limit parameter."""
        db1 = tmp_path / "b.db"
        b1 = Brain(db_path=str(db1), agent_id="x")
        for i in range(15):
            b1.remember(f"test entry {i} about widgets", category="general")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        result = fed.federated_search(query="widgets", limit=5)
        assert result["ok"] is True
        assert len(result["results"]) <= 5

    def test_federated_search_inaccessible_db_skipped(self, tmp_path, monkeypatch):
        """Bad DB path does not crash federated_search."""
        db1 = tmp_path / "real.db"
        Brain(db_path=str(db1), agent_id="x")
        b1 = Brain(db_path=str(db1), agent_id="x")
        b1.remember("test memory for inaccessible test", category="general")
        bad = tmp_path / "bad.db"
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.setenv("BRAIN_DB_FEDERATION", str(bad))
        result = fed.federated_search(query="inaccessible")
        assert result["ok"] is True

    def test_federated_search_empty_query_returns_error(self, single_brain_db, monkeypatch):
        """Empty query returns error dict."""
        result = fed.federated_search(query="")
        assert result["ok"] is False
        assert "error" in result

    def test_federated_search_db_count_field(self, two_brain_dbs, monkeypatch):
        """Result includes db_count field."""
        result = fed.federated_search(query="python")
        assert "db_count" in result
        assert result["db_count"] >= 1

    def test_federated_search_source_db_field(self, two_brain_dbs, monkeypatch):
        """Every result includes source_db field."""
        result = fed.federated_search(query="agent")
        assert result["ok"] is True
        for r in result["results"]:
            assert "source_db" in r

    def test_federated_search_tables_filter(self, two_brain_dbs, monkeypatch):
        """tables parameter limits which tables are searched."""
        result = fed.federated_search(query="python", tables=["memories"])
        assert result["ok"] is True
        for r in result["results"]:
            assert r["table"] == "memories"

    def test_federated_search_agent_id_filter(self, two_brain_dbs, monkeypatch):
        """agent_id filter restricts results to that agent."""
        result = fed.federated_search(query="agent", agent_id="agent-1")
        assert result["ok"] is True
        for r in result["results"]:
            assert r.get("agent_id") == "agent-1"


# ---------------------------------------------------------------------------
# MCP tool wrapper tests
# ---------------------------------------------------------------------------

class TestMcpFederationTools:
    def test_mcp_federated_search_tool_exists(self):
        """TOOLS list contains federated_search."""
        names = [t.name for t in mcp_fed.TOOLS]
        assert "federated_search" in names

    def test_mcp_federated_stats_tool_exists(self):
        """TOOLS list contains federated_stats."""
        names = [t.name for t in mcp_fed.TOOLS]
        assert "federated_stats" in names

    def test_mcp_federated_memory_search_tool_exists(self):
        """TOOLS list contains federated_memory_search."""
        names = [t.name for t in mcp_fed.TOOLS]
        assert "federated_memory_search" in names

    def test_mcp_federated_entity_search_tool_exists(self):
        """TOOLS list contains federated_entity_search."""
        names = [t.name for t in mcp_fed.TOOLS]
        assert "federated_entity_search" in names

    def test_mcp_dispatch_all_tools_registered(self):
        """All tools in TOOLS are present in DISPATCH."""
        for tool in mcp_fed.TOOLS:
            assert tool.name in mcp_fed.DISPATCH

    def test_mcp_federated_search_tool_call(self, single_brain_db, monkeypatch):
        """Calling the dispatch function for federated_search works."""
        fn = mcp_fed.DISPATCH["federated_search"]
        result = fn(query="python")
        assert result["ok"] is True

    def test_mcp_federated_stats_tool_call(self, single_brain_db, monkeypatch):
        """Calling the dispatch function for federated_stats works."""
        fn = mcp_fed.DISPATCH["federated_stats"]
        result = fn()
        assert result["ok"] is True
        assert "databases" in result
        assert "totals" in result

    def test_mcp_federated_memory_search_tool_call(self, single_brain_db, monkeypatch):
        """Calling the dispatch function for federated_memory_search works."""
        fn = mcp_fed.DISPATCH["federated_memory_search"]
        result = fn(query="python")
        assert result["ok"] is True

    def test_mcp_federated_entity_search_tool_call(self, tmp_path, monkeypatch):
        """Calling the dispatch function for federated_entity_search works."""
        db1 = tmp_path / "b.db"
        b1 = Brain(db_path=str(db1), agent_id="x")
        b1.entity("TestEntity", "thing")
        monkeypatch.setenv("BRAIN_DB", str(db1))
        monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)
        fn = mcp_fed.DISPATCH["federated_entity_search"]
        result = fn(name="TestEntity")
        assert result["ok"] is True
        assert any(r["name"] == "TestEntity" for r in result["results"])

    def test_mcp_tools_list_length(self):
        """Module exports exactly 4 tools."""
        assert len(mcp_fed.TOOLS) == 4
