"""Tests for the Brain Python API (src/agentmemory/brain.py)."""
import json
import os
import sqlite3
from pathlib import Path

import pytest


# ── Initialization ──────────────────────────────────────────────────────────


class TestBrainInit:
    def test_creates_db_file(self, brain):
        assert Path(brain.db_path).exists()

    def test_default_agent_id(self, tmp_path):
        from agentmemory.brain import Brain
        b = Brain(db_path=str(tmp_path / "b.db"))
        assert b.agent_id == "default"

    def test_custom_agent_id(self, brain):
        assert brain.agent_id == "test-agent"

    def test_schema_tables_exist(self, brain):
        conn = sqlite3.connect(str(brain.db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        for expected in ("memories", "events", "entities", "knowledge_edges", "decisions"):
            assert expected in tables, f"Missing table: {expected}"

    def test_reuses_existing_db(self, tmp_path):
        from agentmemory.brain import Brain
        db = tmp_path / "reuse.db"
        b1 = Brain(db_path=str(db))
        mid = b1.remember("hello")
        b2 = Brain(db_path=str(db))
        results = b2.search("hello")
        assert any(r["id"] == mid for r in results)

    def test_env_var_default(self, tmp_path, monkeypatch):
        from agentmemory.brain import Brain
        db = tmp_path / "env.db"
        monkeypatch.setenv("BRAIN_DB", str(db))
        b = Brain()  # no explicit path
        assert Path(b.db_path) == db


# ── remember() ──────────────────────────────────────────────────────────────


class TestRemember:
    def test_returns_int_id(self, brain):
        mid = brain.remember("test memory")
        assert isinstance(mid, int) and mid > 0

    def test_default_category(self, brain):
        mid = brain.remember("some fact")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT category FROM memories WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert row[0] == "general"

    def test_custom_category(self, brain):
        mid = brain.remember("dark mode", category="preference")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT category FROM memories WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert row[0] == "preference"

    def test_confidence(self, brain):
        mid = brain.remember("uncertain", confidence=0.4)
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT confidence FROM memories WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert abs(row[0] - 0.4) < 1e-6

    def test_tags_as_string(self, brain):
        mid = brain.remember("tagged", tags="a,b,c")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT tags FROM memories WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert json.loads(row[0]) == ["a", "b", "c"]

    def test_tags_as_list(self, brain):
        mid = brain.remember("tagged2", tags=["x", "y"])
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT tags FROM memories WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert json.loads(row[0]) == ["x", "y"]

    def test_multiple_memories_unique_ids(self, brain):
        ids = [brain.remember(f"mem {i}") for i in range(5)]
        assert len(set(ids)) == 5


# ── search() ────────────────────────────────────────────────────────────────


class TestSearch:
    def test_finds_matching(self, brain_with_data):
        results = brain_with_data.search("dark mode")
        assert len(results) >= 1
        assert any("dark mode" in r["content"] for r in results)

    def test_returns_list_of_dicts(self, brain_with_data):
        results = brain_with_data.search("Python")
        assert isinstance(results, list)
        if results:
            assert isinstance(results[0], dict)
            assert "id" in results[0]
            assert "content" in results[0]

    def test_no_results_for_nonsense(self, brain_with_data):
        results = brain_with_data.search("xyzzy_nonexistent_12345")
        assert results == []

    def test_limit(self, brain):
        for i in range(20):
            brain.remember(f"repeated content item {i}")
        results = brain.search("repeated content", limit=5)
        assert len(results) <= 5

    def test_retired_excluded(self, brain):
        mid = brain.remember("will be forgotten")
        brain.forget(mid)
        results = brain.search("will be forgotten")
        assert all(r["id"] != mid for r in results)


# ── forget() ────────────────────────────────────────────────────────────────


class TestForget:
    def test_soft_delete(self, brain):
        mid = brain.remember("ephemeral")
        brain.forget(mid)
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT retired_at FROM memories WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert row[0] is not None


# ── entity() ────────────────────────────────────────────────────────────────


class TestEntity:
    def test_create_returns_id(self, brain):
        eid = brain.entity("Alice", "person")
        assert isinstance(eid, int) and eid > 0

    def test_idempotent_by_name(self, brain):
        id1 = brain.entity("Bob", "person")
        id2 = brain.entity("Bob", "person")
        assert id1 == id2

    def test_observations_stored(self, brain):
        eid = brain.entity("Carol", "person", observations=["Smart", "Tall"])
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT observations FROM entities WHERE id=?", (eid,)).fetchone()
        conn.close()
        assert json.loads(row[0]) == ["Smart", "Tall"]

    def test_properties_stored(self, brain):
        eid = brain.entity("Acme", "org", properties={"industry": "tech"})
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT properties FROM entities WHERE id=?", (eid,)).fetchone()
        conn.close()
        assert json.loads(row[0]) == {"industry": "tech"}

    def test_different_names_different_ids(self, brain):
        id1 = brain.entity("X", "thing")
        id2 = brain.entity("Y", "thing")
        assert id1 != id2


# ── relate() ────────────────────────────────────────────────────────────────


class TestRelate:
    def test_creates_edge(self, brain):
        brain.entity("A", "node")
        brain.entity("B", "node")
        brain.relate("A", "connects_to", "B")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute(
            "SELECT relation_type FROM knowledge_edges WHERE source_table='entities'"
        ).fetchone()
        conn.close()
        assert row[0] == "connects_to"

    def test_missing_entity_raises(self, brain):
        brain.entity("Exists", "node")
        with pytest.raises(ValueError, match="Entity not found"):
            brain.relate("Exists", "links", "Ghost")

    def test_both_missing_raises(self, brain):
        with pytest.raises(ValueError):
            brain.relate("NoA", "rel", "NoB")


# ── log() ───────────────────────────────────────────────────────────────────


class TestLog:
    def test_returns_int_id(self, brain):
        eid = brain.log("something happened")
        assert isinstance(eid, int) and eid > 0

    def test_default_event_type(self, brain):
        eid = brain.log("ping")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT event_type FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        assert row[0] == "observation"

    def test_custom_event_type_and_project(self, brain):
        eid = brain.log("deployed", event_type="deploy", project="myapp")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT event_type, project FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        assert row[0] == "deploy"
        assert row[1] == "myapp"

    def test_importance(self, brain):
        eid = brain.log("critical", importance=0.95)
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute("SELECT importance FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        assert abs(row[0] - 0.95) < 1e-6


# ── decide() ────────────────────────────────────────────────────────────────


class TestDecide:
    def test_returns_id(self, brain):
        did = brain.decide("Use SQLite", "Simple and reliable")
        assert isinstance(did, int) and did > 0

    def test_stored_correctly(self, brain):
        did = brain.decide("Go async", "Better performance", project="api")
        conn = sqlite3.connect(str(brain.db_path))
        row = conn.execute(
            "SELECT title, rationale, project FROM decisions WHERE id=?", (did,)
        ).fetchone()
        conn.close()
        assert row[0] == "Go async"
        assert row[1] == "Better performance"
        assert row[2] == "api"


# ── stats() ─────────────────────────────────────────────────────────────────


class TestStats:
    def test_returns_dict(self, brain):
        s = brain.stats()
        assert isinstance(s, dict)

    def test_empty_db_zeros(self, brain):
        s = brain.stats()
        assert s["memories"] == 0
        assert s["events"] == 0
        assert s["entities"] == 0

    def test_counts_after_inserts(self, brain_with_data):
        s = brain_with_data.stats()
        assert s["memories"] == 3
        assert s["events"] == 2
        assert s["entities"] == 2
        assert s["knowledge_edges"] == 1
        assert s["active_memories"] == 3

    def test_active_excludes_retired(self, brain):
        mid = brain.remember("temp")
        brain.forget(mid)
        s = brain.stats()
        assert s["memories"] == 1
        assert s["active_memories"] == 0


# ── agent_id scoping ───────────────────────────────────────────────────────


class TestAgentScoping:
    def test_memories_scoped_to_agent(self, tmp_path):
        from agentmemory.brain import Brain
        db = str(tmp_path / "shared.db")
        b1 = Brain(db_path=db, agent_id="agent-1")
        b2 = Brain(db_path=db, agent_id="agent-2")
        b1.remember("secret of agent 1")
        b2.remember("secret of agent 2")
        # Both visible via LIKE search (no agent filtering in search)
        r1 = b1.search("secret")
        assert len(r1) == 2  # Brain.search doesn't filter by agent_id
