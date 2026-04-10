"""Tests for mcp_tools_agents — agent management, tasks & context MCP tools."""
from __future__ import annotations
import sys
import sqlite3
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_agents as mod


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Give each test its own in-memory-like DB and patch the module's DB_PATH."""
    db_file = tmp_path / "brain.db"
    Brain(str(db_file))  # initialises the schema
    monkeypatch.setattr(mod, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# agent_register
# ---------------------------------------------------------------------------

class TestAgentRegister:
    def test_register_new_agent(self):
        result = mod.tool_agent_register(id="bot-1", name="Bot One")
        assert result["ok"] is True
        assert result["agent_id"] == "bot-1"

    def test_register_creates_row_in_db(self, isolated_db):
        mod.tool_agent_register(id="bot-2", name="Bot Two", type="cli")
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT * FROM agents WHERE id='bot-2'").fetchone()
        conn.close()
        assert row is not None

    def test_register_upsert_replaces_existing(self):
        mod.tool_agent_register(id="bot-3", name="Bot Three")
        result = mod.tool_agent_register(id="bot-3", name="Bot Three Renamed")
        assert result["ok"] is True
        assert result["agent_id"] == "bot-3"

    def test_register_missing_id_returns_error(self):
        result = mod.tool_agent_register(id="", name="No ID")
        assert result["ok"] is False
        assert "id" in result["error"].lower()

    def test_register_missing_name_returns_error(self):
        result = mod.tool_agent_register(id="bot-4", name="")
        assert result["ok"] is False
        assert "name" in result["error"].lower()


# ---------------------------------------------------------------------------
# agent_list
# ---------------------------------------------------------------------------

class TestAgentList:
    def test_empty_list(self):
        result = mod.tool_agent_list()
        assert result["ok"] is True
        assert isinstance(result["agents"], list)

    def test_returns_registered_agents(self):
        mod.tool_agent_register(id="list-a", name="List A")
        mod.tool_agent_register(id="list-b", name="List B")
        result = mod.tool_agent_list()
        assert result["ok"] is True
        ids = [a["id"] for a in result["agents"]]
        assert "list-a" in ids
        assert "list-b" in ids

    def test_list_includes_all_columns(self):
        mod.tool_agent_register(id="col-test", name="Column Test", type="service")
        result = mod.tool_agent_list()
        agent = next(a for a in result["agents"] if a["id"] == "col-test")
        assert "display_name" in agent
        assert agent["display_name"] == "Column Test"
        assert agent["agent_type"] == "service"


# ---------------------------------------------------------------------------
# agent_ping
# ---------------------------------------------------------------------------

class TestAgentPing:
    def test_ping_existing_agent(self):
        mod.tool_agent_register(id="ping-agent", name="Ping Agent")
        result = mod.tool_agent_ping(agent="ping-agent")
        assert result["ok"] is True
        assert result["agent"] == "ping-agent"
        assert "pinged_at" in result

    def test_ping_updates_last_seen(self, isolated_db):
        mod.tool_agent_register(id="ts-agent", name="TS Agent")
        conn = sqlite3.connect(str(isolated_db))
        before = conn.execute("SELECT last_seen_at FROM agents WHERE id='ts-agent'").fetchone()[0]
        conn.close()
        mod.tool_agent_ping(agent="ts-agent")
        conn = sqlite3.connect(str(isolated_db))
        after = conn.execute("SELECT last_seen_at FROM agents WHERE id='ts-agent'").fetchone()[0]
        conn.close()
        # after should be >= before (may equal if same second)
        assert after >= before

    def test_ping_empty_agent_returns_error(self):
        result = mod.tool_agent_ping(agent="")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# agent_model
# ---------------------------------------------------------------------------

class TestAgentModel:
    def test_unknown_agent_returns_error_no_tom(self):
        """agent_model for an agent_id that doesn't exist returns ok=False."""
        # ToM tables ARE in base schema — test unknown agent instead
        result = mod.tool_agent_model(agent_id_target="nonexistent-agent-xyz")
        assert result["ok"] is False

    def test_unknown_agent_returns_error(self, isolated_db):
        """If ToM tables existed but agent doesn't — should say not found."""
        # Manually create minimal ToM table to bypass the table-check
        conn = sqlite3.connect(str(isolated_db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_beliefs "
            "(id INTEGER PRIMARY KEY, agent_id TEXT, topic TEXT, belief_content TEXT, "
            "confidence REAL, is_assumption INTEGER, last_updated_at TEXT, invalidated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_bdi_state (agent_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS belief_conflicts "
            "(id INTEGER PRIMARY KEY, agent_a_id TEXT, agent_b_id TEXT, topic TEXT, "
            "conflict_type TEXT, severity REAL, belief_a TEXT, belief_b TEXT, "
            "requires_supervisor_intervention INTEGER, resolved_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_perspective_models "
            "(id INTEGER PRIMARY KEY, observer_agent_id TEXT, subject_agent_id TEXT, "
            "topic TEXT, knowledge_gap TEXT, confusion_risk REAL)"
        )
        conn.commit()
        conn.close()
        result = mod.tool_agent_model(agent_id_target="nonexistent-999")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_returns_model_for_known_agent(self, isolated_db):
        """Happy path: agent exists, ToM tables present, model returned."""
        conn = sqlite3.connect(str(isolated_db))
        # Create minimal ToM tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_beliefs (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                topic TEXT,
                belief_content TEXT,
                confidence REAL,
                is_assumption INTEGER DEFAULT 0,
                last_updated_at TEXT,
                invalidated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_bdi_state (
                agent_id TEXT PRIMARY KEY,
                beliefs_summary TEXT,
                desires_summary TEXT,
                intentions_summary TEXT,
                knowledge_coverage_score REAL,
                belief_staleness_score REAL,
                confusion_risk_score REAL,
                last_full_assessment_at TEXT
            );
            CREATE TABLE IF NOT EXISTS belief_conflicts (
                id INTEGER PRIMARY KEY,
                agent_a_id TEXT,
                agent_b_id TEXT,
                topic TEXT,
                conflict_type TEXT,
                severity REAL,
                belief_a TEXT,
                belief_b TEXT,
                requires_supervisor_intervention INTEGER DEFAULT 0,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_perspective_models (
                id INTEGER PRIMARY KEY,
                observer_agent_id TEXT,
                subject_agent_id TEXT,
                topic TEXT,
                knowledge_gap TEXT,
                confusion_risk REAL
            );
        """)
        # Insert the agent directly (bypassing FK enforcement in this raw conn)
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
            "created_at, updated_at) VALUES ('tom-agent', 'ToM Agent', 'mcp', 'active', "
            "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.commit()
        conn.close()

        result = mod.tool_agent_model(agent_id_target="tom-agent")
        assert result["ok"] is True
        assert result["agent_id"] == "tom-agent"
        assert "bdi_state" in result
        assert "active_beliefs" in result
        assert "open_conflicts" in result
        assert "knowledge_gaps" in result


# ---------------------------------------------------------------------------
# task_add
# ---------------------------------------------------------------------------

class TestTaskAdd:
    def test_add_basic_task(self):
        result = mod.tool_task_add(title="Implement feature X")
        assert result["ok"] is True
        assert isinstance(result["task_id"], int)
        assert result["task_id"] > 0

    def test_add_task_with_all_fields(self, isolated_db):
        # Register the agent first to satisfy the FK on assigned_agent_id
        mod.tool_agent_register(id="bot-1", name="Bot One")
        result = mod.tool_task_add(
            title="Fix bug Y",
            description="Detailed description",
            status="pending",
            priority="high",
            assign="bot-1",
            project="myproject",
            external_id="GH-42",
            external_system="github",
        )
        assert result["ok"] is True
        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (result["task_id"],)).fetchone()
        conn.close()
        assert row is not None
        assert row["external_id"] == "GH-42"

    def test_add_task_defaults(self, isolated_db):
        result = mod.tool_task_add(title="Default task")
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT status, priority FROM tasks WHERE id=?", (result["task_id"],)
        ).fetchone()
        conn.close()
        assert row[0] == "pending"
        assert row[1] == "medium"

    def test_add_task_empty_title_fails(self):
        result = mod.tool_task_add(title="")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# task_update
# ---------------------------------------------------------------------------

class TestTaskUpdate:
    def _create_task(self, title="Test task") -> int:
        result = mod.tool_task_add(title=title)
        return result["task_id"]

    def test_update_status(self, isolated_db):
        tid = self._create_task("Status task")
        result = mod.tool_task_update(id=tid, status="completed")
        assert result["ok"] is True
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "completed"

    def test_update_in_progress_claims(self, isolated_db):
        # Register the agent so the claimed_by FK is satisfied
        mod.tool_agent_register(id="claimer-bot", name="Claimer Bot")
        tid = self._create_task("Claim task")
        result = mod.tool_task_update(agent_id="claimer-bot", id=tid, status="in_progress")
        assert result["ok"] is True
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "claimer-bot"

    def test_update_in_progress_no_claim(self, isolated_db):
        # no_claim=True means claimed_by stays NULL — no agent registration needed
        tid = self._create_task("No claim task")
        mod.tool_task_update(agent_id="other-bot", id=tid, status="in_progress", no_claim=True)
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] is None

    def test_update_priority(self, isolated_db):
        tid = self._create_task("Priority task")
        result = mod.tool_task_update(id=tid, priority="critical")
        assert result["ok"] is True
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "critical"

    def test_update_no_fields_fails(self):
        tid = self._create_task("Empty update")
        result = mod.tool_task_update(id=tid)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# task_list
# ---------------------------------------------------------------------------

class TestTaskList:
    def test_list_all_tasks(self):
        mod.tool_task_add(title="Task A")
        mod.tool_task_add(title="Task B")
        result = mod.tool_task_list()
        assert result["ok"] is True
        assert len(result["tasks"]) >= 2

    def test_filter_by_status(self):
        mod.tool_task_add(title="Active task", status="in_progress")
        mod.tool_task_add(title="Pending task", status="pending")
        result = mod.tool_task_list(status="in_progress")
        assert result["ok"] is True
        assert all(t["status"] == "in_progress" for t in result["tasks"])

    def test_filter_by_project(self):
        mod.tool_task_add(title="Project task", project="alpha")
        mod.tool_task_add(title="Other task", project="beta")
        result = mod.tool_task_list(project="alpha")
        assert result["ok"] is True
        assert all(t["project"] == "alpha" for t in result["tasks"])

    def test_limit_applied(self):
        for i in range(5):
            mod.tool_task_add(title=f"Limit task {i}")
        result = mod.tool_task_list(limit=2)
        assert result["ok"] is True
        assert len(result["tasks"]) <= 2

    def test_priority_ordering(self):
        mod.tool_task_add(title="Low", priority="low")
        mod.tool_task_add(title="Critical", priority="critical")
        mod.tool_task_add(title="High", priority="high")
        result = mod.tool_task_list()
        assert result["ok"] is True
        priorities = [t["priority"] for t in result["tasks"]]
        # Critical should appear before high, high before low
        assert priorities.index("critical") < priorities.index("high")
        assert priorities.index("high") < priorities.index("low")


# ---------------------------------------------------------------------------
# context_add
# ---------------------------------------------------------------------------

class TestContextAdd:
    def test_add_basic_context(self):
        result = mod.tool_context_add(content="Some context text here")
        assert result["ok"] is True
        assert isinstance(result["context_id"], int)
        assert result["context_id"] > 0

    def test_add_context_with_metadata(self, isolated_db):
        result = mod.tool_context_add(
            content="Function that computes X",
            source_type="file",
            source_ref="/src/utils.py",
            chunk=3,
            summary="Utility functions",
            project="myproject",
            tags="python,utils",
            tokens=150,
        )
        assert result["ok"] is True
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT * FROM context WHERE id=?", (result["context_id"],)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_add_context_empty_content_fails(self):
        result = mod.tool_context_add(content="")
        assert result["ok"] is False

    def test_context_tags_stored_as_json(self, isolated_db):
        result = mod.tool_context_add(content="Tagged content", tags="foo,bar,baz")
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT tags FROM context WHERE id=?", (result["context_id"],)
        ).fetchone()
        conn.close()
        import json
        tags = json.loads(row[0])
        assert "foo" in tags
        assert "bar" in tags
        assert "baz" in tags


# ---------------------------------------------------------------------------
# context_search
# ---------------------------------------------------------------------------

class TestContextSearch:
    def test_search_finds_added_context(self):
        mod.tool_context_add(content="The quick brown fox jumps over the lazy dog")
        result = mod.tool_context_search(query="quick brown fox")
        assert result["ok"] is True
        assert isinstance(result["results"], list)
        assert len(result["results"]) > 0, "FTS search should return at least one result"

    def test_search_empty_query_fails(self):
        result = mod.tool_context_search(query="")
        assert result["ok"] is False

    def test_search_no_match_returns_empty_list(self):
        result = mod.tool_context_search(query="xyzzyplugplug123nonexistent")
        assert result["ok"] is True
        assert result["results"] == []

    def test_search_respects_limit(self):
        for i in range(5):
            mod.tool_context_add(content=f"Banana apple orange grape fruit item {i}")
        result = mod.tool_context_search(query="Banana apple orange grape fruit", limit=2)
        assert result["ok"] is True
        assert len(result["results"]) <= 2

    def test_search_fts_special_chars_sanitized(self):
        """Queries with FTS5 special characters should not raise an error."""
        result = mod.tool_context_search(query="hello & world | (test)")
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# TOOLS and DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(mod.TOOLS, list)
        assert all(isinstance(t, Tool) for t in mod.TOOLS)

    def test_tools_count(self):
        assert len(mod.TOOLS) == 9

    def test_dispatch_is_dict(self):
        assert isinstance(mod.DISPATCH, dict)

    def test_dispatch_has_all_tool_names(self):
        tool_names = {t.name for t in mod.TOOLS}
        assert tool_names == set(mod.DISPATCH.keys())

    def test_dispatch_all_callable(self):
        for name, fn in mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH['{name}'] is not callable"

    def test_all_tools_have_required_inputschema_fields(self):
        for tool in mod.TOOLS:
            schema = tool.inputSchema
            assert "type" in schema, f"{tool.name}: missing 'type' in inputSchema"
            assert "properties" in schema, f"{tool.name}: missing 'properties' in inputSchema"
