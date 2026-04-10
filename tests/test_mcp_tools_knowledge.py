"""Tests for mcp_tools_knowledge — knowledge index & synthesis MCP tools."""
from __future__ import annotations
import sys
import os
import json
import sqlite3
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_knowledge as mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_db(tmp_path: Path) -> Path:
    """Create a fresh brain.db and point the module at it."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    # Point the module-level DB_PATH at the test DB
    mod.DB_PATH = db_file
    return db_file


def _insert_event(db_file: Path, agent_id: str, summary: str,
                  event_type: str = "observation", importance: float = 0.8,
                  project: str | None = None) -> int:
    """Insert an event directly into the test DB, return event id."""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Ensure agent exists
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?,?,?,?,'2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')",
        (agent_id, agent_id, "test", "active"),
    )
    cur = conn.execute(
        "INSERT INTO events (agent_id, event_type, summary, importance, project, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2024-01-01T00:00:00Z')",
        (agent_id, event_type, summary, importance, project),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


def _insert_memory(db_file: Path, agent_id: str, content: str,
                   category: str = "project") -> int:
    """Insert a memory directly, bypassing Brain.remember (for test control)."""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?,?,?,?,'2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')",
        (agent_id, agent_id, "test", "active"),
    )
    cur = conn.execute(
        "INSERT INTO memories (agent_id, category, content, confidence, "
        "created_at, updated_at) VALUES (?, ?, ?, 0.9, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')",
        (agent_id, category, content),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


# ---------------------------------------------------------------------------
# Module interface tests
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list(self, tmp_path):
        assert isinstance(mod.TOOLS, list)
        assert len(mod.TOOLS) == 5

    def test_dispatch_is_dict(self, tmp_path):
        assert isinstance(mod.DISPATCH, dict)
        assert set(mod.DISPATCH.keys()) == {
            "knowledge_index", "knowledge_report", "distill", "promote", "dreams"
        }

    def test_tool_names_match_dispatch(self, tmp_path):
        tool_names = {t.name for t in mod.TOOLS}
        assert tool_names == set(mod.DISPATCH.keys())

    def test_all_tools_have_input_schema(self, tmp_path):
        for tool in mod.TOOLS:
            assert hasattr(tool, "inputSchema"), f"{tool.name} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"

    def test_dispatch_callables(self, tmp_path):
        for name, fn in mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH['{name}'] is not callable"


# ---------------------------------------------------------------------------
# knowledge_index tests
# ---------------------------------------------------------------------------

class TestKnowledgeIndex:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_knowledge_index()
        assert result["ok"] is True
        assert "memories_by_category" in result
        assert "entities_by_type" in result
        assert "decisions" in result
        assert "stats" in result

    def test_stats_reflect_data(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.remember("Python is used here", category="project")
        b.remember("Always write tests", category="lesson")

        result = mod.tool_knowledge_index()
        assert result["ok"] is True
        assert result["stats"]["total_memories"] == 2

    def test_memories_grouped_by_category(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.remember("deploy to staging first", category="lesson")
        b.remember("prefer dark mode", category="preference")

        result = mod.tool_knowledge_index()
        assert result["ok"] is True
        cats = result["memories_by_category"]
        assert "lesson" in cats
        assert "preference" in cats
        assert all("id" in m for m in cats["lesson"])

    def test_category_filter(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.remember("use postgres", category="environment")
        b.remember("v1 is done", category="project")

        result = mod.tool_knowledge_index(category="environment")
        assert result["ok"] is True
        assert "environment" in result["memories_by_category"]
        assert "project" not in result["memories_by_category"]

    def test_entities_grouped_by_type(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.entity("Alice", "person", observations=["Engineer"])
        b.entity("BrainDB", "project", observations=["Memory system"])

        result = mod.tool_knowledge_index()
        assert result["ok"] is True
        ets = result["entities_by_type"]
        assert "person" in ets
        assert "project" in ets

    def test_decisions_included(self, tmp_path):
        db_file = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('test-agent','test-agent','test','active','2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO decisions (agent_id, title, rationale, created_at) VALUES (?,?,?,?)",
            ("test-agent", "Use SQLite", "Simplicity over complexity", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        result = mod.tool_knowledge_index()
        assert result["ok"] is True
        assert len(result["decisions"]) >= 1
        assert any(d["title"] == "Use SQLite" for d in result["decisions"])

    def test_generated_at_present(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_knowledge_index()
        assert "generated_at" in result
        assert result["generated_at"].endswith("Z")


# ---------------------------------------------------------------------------
# knowledge_report tests
# ---------------------------------------------------------------------------

class TestKnowledgeReport:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_knowledge_report()
        assert result["ok"] is True
        assert "stats" in result
        assert "memories_by_category" in result
        assert "entities" in result
        assert "decisions" in result
        assert "recent_events" in result

    def test_stats_counts_active(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.remember("fact one", category="project")
        b.remember("fact two", category="lesson")

        result = mod.tool_knowledge_report()
        assert result["ok"] is True
        assert result["stats"]["active_memories"] == 2

    def test_topic_filter_memories(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.remember("postgres is the primary database", category="environment")
        b.remember("deploy to staging first", category="lesson")

        result = mod.tool_knowledge_report(topic="postgres")
        assert result["ok"] is True
        cats = result["memories_by_category"]
        assert "environment" in cats
        assert all("postgres" in m["content"].lower() for cat_mems in cats.values() for m in cat_mems)

    def test_entity_focus(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.entity("Alice", "person", observations=["engineer", "coffee lover"])

        result = mod.tool_knowledge_report(entity="Alice")
        assert result["ok"] is True
        ef = result["entity_focus"]
        assert ef is not None
        assert ef["name"] == "Alice"
        assert "observations" in ef
        assert len(ef["observations"]) > 0

    def test_entity_focus_not_found(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_knowledge_report(entity="NonExistent")
        assert result["ok"] is True
        assert result["entity_focus"] is not None
        assert "error" in result["entity_focus"]

    def test_agent_filter(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b1 = Brain(db_path=str(db_file), agent_id="agent-a")
        b2 = Brain(db_path=str(db_file), agent_id="agent-b")
        b1.remember("agent-a fact", category="project")
        b2.remember("agent-b fact", category="project")

        result = mod.tool_knowledge_report(agent_filter="agent-a")
        assert result["ok"] is True
        cats = result["memories_by_category"]
        all_contents = [m["content"] for cat_mems in cats.values() for m in cat_mems]
        assert all("agent-a" in c for c in all_contents)

    def test_events_included(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(db_file, "test-agent", "deployed v2.0", importance=0.9)

        result = mod.tool_knowledge_report()
        assert result["ok"] is True
        assert any("deployed" in e["summary"] for e in result["recent_events"])

    def test_entity_relations_included(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        b.entity("Alice", "person", observations=["engineer"])
        b.entity("BrainProject", "project", observations=["memory system"])
        b.relate("Alice", "works_on", "BrainProject")

        result = mod.tool_knowledge_report()
        assert result["ok"] is True
        rels = result["entity_relations"]
        assert any(r["src"] == "Alice" and r["relation"] == "works_on" for r in rels)


# ---------------------------------------------------------------------------
# distill tests
# ---------------------------------------------------------------------------

class TestDistill:
    def test_dry_run_no_writes(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(db_file, "test-agent", "important result", importance=0.9)

        result = mod.tool_distill(dry_run=True, threshold=0.7)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["candidates_found"] >= 1

        # Verify nothing was written
        conn = sqlite3.connect(str(db_file))
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE source_event_id IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_promotes_above_threshold(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(db_file, "test-agent", "high importance result", importance=0.95)
        _insert_event(db_file, "test-agent", "low importance note", importance=0.2)

        result = mod.tool_distill(threshold=0.7, dry_run=False)
        assert result["ok"] is True
        assert result["promoted_count"] == 1
        assert result["promotions"][0]["category"] in (
            "project", "lesson", "decision", "environment", "identity",
            "convention", "preference", "integration", "user",
        )

    def test_does_not_double_promote(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(db_file, "test-agent", "ship it", importance=0.9)

        result1 = mod.tool_distill(threshold=0.7, dry_run=False)
        assert result1["ok"] is True
        assert result1["promoted_count"] == 1

        result2 = mod.tool_distill(threshold=0.7, dry_run=False)
        assert result2["ok"] is True
        assert result2["promoted_count"] == 0

    def test_dry_run_shows_category_inference(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(
            db_file, "test-agent",
            "lesson: never deploy on Friday",
            event_type="observation",
            importance=0.9,
        )

        result = mod.tool_distill(threshold=0.7, dry_run=True)
        assert result["ok"] is True
        promos = result["promotions"]
        assert len(promos) >= 1
        # category inference is heuristic — verify it returns a valid category string
        assert isinstance(promos[0]["would_promote_as"], str)
        assert len(promos[0]["would_promote_as"]) > 0

    def test_skip_types_excluded(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(db_file, "test-agent", "session started", event_type="session_start", importance=0.95)

        result = mod.tool_distill(threshold=0.7, dry_run=True)
        assert result["ok"] is True
        # session_start events should be excluded
        assert result["candidates_found"] == 0

    def test_filter_agent(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_event(db_file, "agent-x", "agent-x high", importance=0.9)
        _insert_event(db_file, "agent-y", "agent-y high", importance=0.9)

        result = mod.tool_distill(threshold=0.7, filter_agent="agent-x", dry_run=True)
        assert result["ok"] is True
        assert all(p["agent_id"] == "agent-x" for p in result["promotions"])

    def test_limit_respected(self, tmp_path):
        db_file = _setup_db(tmp_path)
        for i in range(5):
            _insert_event(db_file, "test-agent", f"result {i}", importance=0.9)

        result = mod.tool_distill(threshold=0.7, limit=3, dry_run=False)
        assert result["ok"] is True
        assert result["promoted_count"] == 3


# ---------------------------------------------------------------------------
# promote tests
# ---------------------------------------------------------------------------

class TestPromote:
    def test_promotes_event_to_memory(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_event(db_file, "test-agent", "deployed v3", importance=0.8)

        result = mod.tool_promote(event_id=eid)
        assert result["ok"] is True
        assert "memory_id" in result
        assert result["from_event"] == eid

    def test_promoted_memory_in_db(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_event(db_file, "test-agent", "shipped milestone", importance=0.85)

        result = mod.tool_promote(event_id=eid)
        mid = result["memory_id"]

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row is not None
        assert row["source_event_id"] == eid
        assert row["content"] == "shipped milestone"

    def test_custom_content_and_category(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_event(db_file, "test-agent", "raw event text", importance=0.8)

        result = mod.tool_promote(
            event_id=eid,
            content="curated memory content",
            category="lesson",
        )
        assert result["ok"] is True

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT content, category FROM memories WHERE id = ?", (result["memory_id"],)).fetchone()
        conn.close()
        assert row["content"] == "curated memory content"
        assert row["category"] == "lesson"

    def test_event_not_found(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_promote(event_id=99999)
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_custom_confidence(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_event(db_file, "test-agent", "uncertain result", importance=0.8)

        result = mod.tool_promote(event_id=eid, confidence=0.6)
        assert result["ok"] is True

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT confidence FROM memories WHERE id = ?", (result["memory_id"],)).fetchone()
        conn.close()
        assert abs(row["confidence"] - 0.6) < 1e-6

    def test_logs_memory_promoted_event(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_event(db_file, "test-agent", "key result", importance=0.9)

        result = mod.tool_promote(event_id=eid)
        assert result["ok"] is True

        conn = sqlite3.connect(str(db_file))
        promo_event = conn.execute(
            "SELECT * FROM events WHERE event_type = 'memory_promoted' AND agent_id = 'test-agent'"
        ).fetchone()
        conn.close()
        assert promo_event is not None

    def test_category_inferred_from_event_type(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_event(db_file, "test-agent", "a decision was made",
                            event_type="decision", importance=0.9)

        result = mod.tool_promote(event_id=eid)
        assert result["ok"] is True
        assert result["category"] == "decision"


# ---------------------------------------------------------------------------
# dreams tests
# ---------------------------------------------------------------------------

class TestDreams:
    def test_no_table_returns_empty(self, tmp_path):
        # dream_hypotheses is in the base schema, so result is empty list not a message
        _setup_db(tmp_path)
        result = mod.tool_dreams()
        assert result["ok"] is True
        assert isinstance(result["hypotheses"], list)

    def test_with_dream_hypotheses_table(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        mid_a = b.remember("memory A content", category="project")
        mid_b = b.remember("memory B content", category="lesson")

        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dream_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_a_id INTEGER,
                memory_b_id INTEGER,
                hypothesis_memory_id INTEGER,
                similarity REAL,
                status TEXT DEFAULT 'incubating',
                created_at TEXT,
                promoted_at TEXT,
                retired_at TEXT,
                retirement_reason TEXT
            )
        """)
        conn.execute(
            "INSERT INTO dream_hypotheses (memory_a_id, memory_b_id, similarity, status, created_at) "
            "VALUES (?, ?, 0.87, 'incubating', '2024-01-01T00:00:00Z')",
            (mid_a, mid_b),
        )
        conn.commit()
        conn.close()

        result = mod.tool_dreams(status="incubating")
        assert result["ok"] is True
        assert result["count"] == 1
        h = result["hypotheses"][0]
        assert h["similarity"] == pytest.approx(0.87)
        assert h["status"] == "incubating"
        assert h["memory_a"]["id"] == mid_a
        assert h["memory_b"]["id"] == mid_b

    def test_status_filter_works(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        mid_a = b.remember("A", category="project")
        mid_b = b.remember("B", category="lesson")
        mid_c = b.remember("C", category="project")

        conn = sqlite3.connect(str(db_file))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dream_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_a_id INTEGER, memory_b_id INTEGER,
                hypothesis_memory_id INTEGER, similarity REAL,
                status TEXT DEFAULT 'incubating',
                created_at TEXT, promoted_at TEXT, retired_at TEXT, retirement_reason TEXT
            )
        """)
        conn.execute(
            "INSERT INTO dream_hypotheses (memory_a_id, memory_b_id, similarity, status, created_at) "
            "VALUES (?, ?, 0.7, 'incubating', '2024-01-01T00:00:00Z')",
            (mid_a, mid_b),
        )
        conn.execute(
            "INSERT INTO dream_hypotheses (memory_a_id, memory_b_id, similarity, status, created_at) "
            "VALUES (?, ?, 0.9, 'promoted', '2024-01-01T00:00:00Z')",
            (mid_b, mid_c),
        )
        conn.commit()
        conn.close()

        result_inc = mod.tool_dreams(status="incubating")
        assert result_inc["count"] == 1

        result_promo = mod.tool_dreams(status="promoted")
        assert result_promo["count"] == 1

    def test_limit_respected(self, tmp_path):
        db_file = _setup_db(tmp_path)
        b = Brain(db_path=str(db_file), agent_id="test-agent")
        mids = [b.remember(f"memory {i}", category="project") for i in range(6)]

        conn = sqlite3.connect(str(db_file))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dream_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_a_id INTEGER, memory_b_id INTEGER,
                hypothesis_memory_id INTEGER, similarity REAL,
                status TEXT DEFAULT 'incubating',
                created_at TEXT, promoted_at TEXT, retired_at TEXT, retirement_reason TEXT
            )
        """)
        for i in range(5):
            conn.execute(
                "INSERT INTO dream_hypotheses (memory_a_id, memory_b_id, similarity, status, created_at) "
                "VALUES (?, ?, 0.8, 'incubating', '2024-01-01T00:00:00Z')",
                (mids[i], mids[i + 1] if i + 1 < len(mids) else mids[0]),
            )
        conn.commit()
        conn.close()

        result = mod.tool_dreams(limit=3)
        assert result["ok"] is True
        assert result["count"] == 3

    def test_response_structure(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_dreams()
        # Even with no table, required keys must be present
        assert "ok" in result
        assert "hypotheses" in result


# ---------------------------------------------------------------------------
# _infer_category_from_content tests
# ---------------------------------------------------------------------------

class TestInferCategory:
    def test_empty_content(self):
        assert mod._infer_category_from_content("") == "project"

    def test_lesson_keyword(self):
        assert mod._infer_category_from_content("lesson: never run migrations on prod") == "lesson"

    def test_decision_keyword(self):
        assert mod._infer_category_from_content("we decided to use Redis for caching") == "decision"

    def test_environment_keyword(self):
        assert mod._infer_category_from_content("database schema updated to version 3") == "environment"

    def test_fallback_to_project(self):
        assert mod._infer_category_from_content("the quick brown fox jumps") == "project"
