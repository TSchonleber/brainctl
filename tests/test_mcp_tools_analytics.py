"""Tests for mcp_tools_analytics — access analytics & retrieval effectiveness."""
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
import agentmemory.mcp_tools_analytics as mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_db(tmp_path: Path) -> Path:
    """Create a fresh brain.db and point the module at it."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    mod.DB_PATH = db_file
    return db_file


def _ensure_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO agents "
        "(id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES (?,?,?,'active',strftime('%Y-%m-%dT%H:%M:%S','now'),strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id, "test"),
    )


def _insert_memory(db_file: Path, agent_id: str, content: str,
                   category: str = "project",
                   recalled_count: int = 0,
                   days_ago: int = 0) -> int:
    conn = sqlite3.connect(str(db_file))
    _ensure_agent(conn, agent_id)
    if days_ago:
        ts_expr = f"strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-{days_ago} days'))"
    else:
        ts_expr = "strftime('%Y-%m-%dT%H:%M:%S','now')"
    cur = conn.execute(
        f"INSERT INTO memories (agent_id, category, content, confidence, recalled_count, "
        f"created_at, updated_at) VALUES (?, ?, ?, 0.9, ?, {ts_expr}, {ts_expr})",
        (agent_id, category, content, recalled_count),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def _insert_access_log(db_file: Path, agent_id: str, action: str,
                       target_table: str | None = None,
                       target_id: int | None = None,
                       query: str | None = None,
                       result_count: int | None = None,
                       days_ago: int = 0) -> int:
    """Insert an access_log row. Uses actual column names: action, target_table, target_id, query."""
    conn = sqlite3.connect(str(db_file))
    _ensure_agent(conn, agent_id)
    if days_ago:
        ts_expr = f"strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-{days_ago} days'))"
    else:
        ts_expr = "strftime('%Y-%m-%dT%H:%M:%S','now')"
    cur = conn.execute(
        f"INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count, created_at) "
        f"VALUES (?,?,?,?,?,?,{ts_expr})",
        (agent_id, action, target_table, target_id, query, result_count),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def _insert_push_event(db_file: Path, agent_id: str, memory_ids: list[int],
                       recalled_at_push: dict | None = None,
                       days_ago: int = 0) -> int:
    """Insert a push_delivered event with proper metadata JSON."""
    conn = sqlite3.connect(str(db_file))
    _ensure_agent(conn, agent_id)
    if recalled_at_push is None:
        recalled_at_push = {mid: 0 for mid in memory_ids}
    push_id = "test-push-1"
    meta = json.dumps({
        "push_id": push_id,
        "task_desc": "test task",
        "memory_ids": memory_ids,
        "recalled_at_push": recalled_at_push,
        "top_k": len(memory_ids),
        "hybrid": False,
    })
    if days_ago:
        ts_expr = f"strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-{days_ago} days'))"
    else:
        ts_expr = "strftime('%Y-%m-%dT%H:%M:%S','now')"
    cur = conn.execute(
        f"INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at) "
        f"VALUES (?, 'push_delivered', ?, ?, 0.2, {ts_expr})",
        (agent_id, f"push:{push_id} delivered {len(memory_ids)} memories", meta),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


# ---------------------------------------------------------------------------
# Module interface
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list(self, tmp_path):
        assert isinstance(mod.TOOLS, list)
        assert len(mod.TOOLS) == 6

    def test_dispatch_is_dict(self, tmp_path):
        assert isinstance(mod.DISPATCH, dict)
        assert set(mod.DISPATCH.keys()) == {
            "hot_memories",
            "cold_memories",
            "search_patterns",
            "retrieval_effectiveness",
            "agent_activity",
            "memory_utility_rate",
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
# hot_memories
# ---------------------------------------------------------------------------

class TestHotMemories:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_hot_memories(agent_id="test-agent")
        assert result["ok"] is True
        assert result["memories"] == []

    def test_counts_reads_per_memory(self, tmp_path):
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "test-agent", "important fact")
        _insert_access_log(db_file, "test-agent", "read", "memories", mid)
        _insert_access_log(db_file, "test-agent", "read", "memories", mid)
        _insert_access_log(db_file, "test-agent", "read", "memories", mid)

        result = mod.tool_hot_memories(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert len(result["memories"]) == 1
        entry = result["memories"][0]
        assert entry["memory_id"] == mid
        assert entry["recall_count"] == 3
        assert "important fact" in entry["content_snippet"]

    def test_ranks_by_recall_count_descending(self, tmp_path):
        db_file = _setup_db(tmp_path)
        m1 = _insert_memory(db_file, "test-agent", "low usage")
        m2 = _insert_memory(db_file, "test-agent", "high usage")
        # m2 read 5 times, m1 read once
        for _ in range(5):
            _insert_access_log(db_file, "test-agent", "read", "memories", m2)
        _insert_access_log(db_file, "test-agent", "read", "memories", m1)

        result = mod.tool_hot_memories(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["memories"][0]["memory_id"] == m2
        assert result["memories"][1]["memory_id"] == m1

    def test_ignores_old_reads(self, tmp_path):
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "test-agent", "old memory")
        _insert_access_log(db_file, "test-agent", "read", "memories", mid, days_ago=100)

        result = mod.tool_hot_memories(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["memories"] == []

    def test_limit_respected(self, tmp_path):
        db_file = _setup_db(tmp_path)
        for i in range(5):
            mid = _insert_memory(db_file, "test-agent", f"memory {i}")
            _insert_access_log(db_file, "test-agent", "read", "memories", mid)

        result = mod.tool_hot_memories(agent_id="test-agent", days=30, limit=3)
        assert result["ok"] is True
        assert len(result["memories"]) == 3

    def test_ignores_other_agents(self, tmp_path):
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "agent-b", "other agent memory")
        _insert_access_log(db_file, "agent-b", "read", "memories", mid)

        result = mod.tool_hot_memories(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["memories"] == []


# ---------------------------------------------------------------------------
# cold_memories
# ---------------------------------------------------------------------------

class TestColdMemories:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_cold_memories(agent_id="test-agent")
        assert result["ok"] is True
        assert result["memories"] == []

    def test_finds_never_recalled_old_memory(self, tmp_path):
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "test-agent", "forgotten fact",
                              recalled_count=0, days_ago=100)

        result = mod.tool_cold_memories(agent_id="test-agent", older_than_days=90)
        assert result["ok"] is True
        assert len(result["memories"]) == 1
        assert result["memories"][0]["memory_id"] == mid

    def test_excludes_recalled_memories(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_memory(db_file, "test-agent", "used fact",
                       recalled_count=5, days_ago=100)

        result = mod.tool_cold_memories(agent_id="test-agent", older_than_days=90)
        assert result["ok"] is True
        assert result["memories"] == []

    def test_excludes_recent_memories(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_memory(db_file, "test-agent", "brand new", recalled_count=0, days_ago=1)

        result = mod.tool_cold_memories(agent_id="test-agent", older_than_days=90)
        assert result["ok"] is True
        assert result["memories"] == []

    def test_limit_respected(self, tmp_path):
        db_file = _setup_db(tmp_path)
        for i in range(5):
            _insert_memory(db_file, "test-agent", f"cold {i}", recalled_count=0, days_ago=100)

        result = mod.tool_cold_memories(agent_id="test-agent", older_than_days=90, limit=3)
        assert result["ok"] is True
        assert len(result["memories"]) == 3


# ---------------------------------------------------------------------------
# search_patterns
# ---------------------------------------------------------------------------

class TestSearchPatterns:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_search_patterns(agent_id="test-agent")
        assert result["ok"] is True
        assert result["terms"] == []

    def test_counts_query_terms(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "search", query="python database migration")
        _insert_access_log(db_file, "test-agent", "search", query="python testing framework")
        _insert_access_log(db_file, "test-agent", "search", query="database schema design")

        result = mod.tool_search_patterns(agent_id="test-agent", days=7)
        assert result["ok"] is True
        terms = {t["term"]: t["frequency"] for t in result["terms"]}
        assert terms.get("python") == 2
        assert terms.get("database") == 2
        assert terms.get("migration") == 1

    def test_ignores_old_searches(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "search", query="ancient query", days_ago=30)

        result = mod.tool_search_patterns(agent_id="test-agent", days=7)
        assert result["ok"] is True
        assert result["terms"] == []

    def test_ranked_by_frequency(self, tmp_path):
        db_file = _setup_db(tmp_path)
        for _ in range(5):
            _insert_access_log(db_file, "test-agent", "search", query="postgres")
        _insert_access_log(db_file, "test-agent", "search", query="redis")

        result = mod.tool_search_patterns(agent_id="test-agent", days=7)
        assert result["ok"] is True
        assert result["terms"][0]["term"] == "postgres"
        assert result["terms"][0]["frequency"] == 5

    def test_stopwords_excluded(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "search", query="the best way to do this")

        result = mod.tool_search_patterns(agent_id="test-agent", days=7)
        terms = {t["term"] for t in result["terms"]}
        # "the", "to", "this" are stopwords / too short
        assert "the" not in terms
        assert "to" not in terms

    def test_limit_respected(self, tmp_path):
        db_file = _setup_db(tmp_path)
        for word in ["alpha", "beta", "gamma", "delta", "epsilon"]:
            _insert_access_log(db_file, "test-agent", "search", query=word)

        result = mod.tool_search_patterns(agent_id="test-agent", days=7, limit=3)
        assert result["ok"] is True
        assert len(result["terms"]) <= 3


# ---------------------------------------------------------------------------
# retrieval_effectiveness
# ---------------------------------------------------------------------------

class TestRetrievalEffectiveness:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_retrieval_effectiveness(agent_id="test-agent")
        assert result["ok"] is True
        assert result["total_searches"] == 0
        assert result["effectiveness_rate"] == 0.0

    def test_counts_searches_with_results(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "search", query="hit", result_count=5)
        _insert_access_log(db_file, "test-agent", "search", query="hit again", result_count=2)
        _insert_access_log(db_file, "test-agent", "search", query="miss", result_count=0)

        result = mod.tool_retrieval_effectiveness(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["total_searches"] == 3
        assert result["searches_with_results"] == 2
        assert abs(result["effectiveness_rate"] - round(2 / 3, 4)) < 1e-4

    def test_avg_result_count(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "search", result_count=4)
        _insert_access_log(db_file, "test-agent", "search", result_count=6)

        result = mod.tool_retrieval_effectiveness(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert abs(result["avg_result_count"] - 5.0) < 1e-2

    def test_ignores_old_searches(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "search", result_count=10, days_ago=60)

        result = mod.tool_retrieval_effectiveness(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["total_searches"] == 0

    def test_non_search_actions_excluded(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "test-agent", "read", result_count=3)
        _insert_access_log(db_file, "test-agent", "write", result_count=1)

        result = mod.tool_retrieval_effectiveness(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["total_searches"] == 0


# ---------------------------------------------------------------------------
# agent_activity
# ---------------------------------------------------------------------------

class TestAgentActivity:
    def test_returns_ok_on_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_agent_activity(days=7)
        assert result["ok"] is True
        assert result["agents"] == []

    def test_groups_by_agent_and_action(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "agent-a", "read")
        _insert_access_log(db_file, "agent-a", "read")
        _insert_access_log(db_file, "agent-a", "search")
        _insert_access_log(db_file, "agent-b", "write")

        result = mod.tool_agent_activity(days=7)
        assert result["ok"] is True
        agents = {a["agent_id"]: a for a in result["agents"]}
        assert "agent-a" in agents
        assert "agent-b" in agents
        assert agents["agent-a"]["operations"]["read"] == 2
        assert agents["agent-a"]["operations"]["search"] == 1
        assert agents["agent-b"]["operations"]["write"] == 1

    def test_sorted_by_total_descending(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "low-agent", "read")
        for _ in range(5):
            _insert_access_log(db_file, "high-agent", "read")

        result = mod.tool_agent_activity(days=7)
        assert result["ok"] is True
        assert result["agents"][0]["agent_id"] == "high-agent"

    def test_ignored_old_activity(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "old-agent", "read", days_ago=30)

        result = mod.tool_agent_activity(days=7)
        assert result["ok"] is True
        assert result["agents"] == []

    def test_all_tracked_actions_present(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_access_log(db_file, "agent-a", "read")

        result = mod.tool_agent_activity(days=7)
        assert result["ok"] is True
        ops = result["agents"][0]["operations"]
        for action in ("read", "write", "search", "push", "promote", "retire"):
            assert action in ops


# ---------------------------------------------------------------------------
# memory_utility_rate
# ---------------------------------------------------------------------------

class TestMemoryUtilityRate:
    def test_returns_ok_on_no_pushes(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_memory_utility_rate(agent_id="test-agent")
        assert result["ok"] is True
        assert result["pushes_tracked"] == 0
        assert result["memories_pushed"] == 0
        assert result["utility_rate"] == 0.0

    def test_detects_recalled_memories(self, tmp_path):
        db_file = _setup_db(tmp_path)
        m1 = _insert_memory(db_file, "test-agent", "pushed memory 1", recalled_count=3)
        m2 = _insert_memory(db_file, "test-agent", "pushed memory 2", recalled_count=0)
        # Snapshot: m1 had 0 recalls at push time, m2 had 0
        _insert_push_event(db_file, "test-agent", [m1, m2],
                           recalled_at_push={str(m1): 0, str(m2): 0})

        result = mod.tool_memory_utility_rate(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["pushes_tracked"] == 1
        assert result["memories_pushed"] == 2
        assert result["memories_recalled"] == 1  # only m1 had recalled_count increase
        assert abs(result["utility_rate"] - 0.5) < 1e-4

    def test_utility_rate_all_recalled(self, tmp_path):
        db_file = _setup_db(tmp_path)
        m1 = _insert_memory(db_file, "test-agent", "mem A", recalled_count=2)
        m2 = _insert_memory(db_file, "test-agent", "mem B", recalled_count=1)
        _insert_push_event(db_file, "test-agent", [m1, m2],
                           recalled_at_push={str(m1): 0, str(m2): 0})

        result = mod.tool_memory_utility_rate(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["utility_rate"] == 1.0

    def test_ignores_old_push_events(self, tmp_path):
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "test-agent", "old push mem", recalled_count=5)
        _insert_push_event(db_file, "test-agent", [mid], days_ago=60)

        result = mod.tool_memory_utility_rate(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["pushes_tracked"] == 0
        assert result["utility_rate"] == 0.0

    def test_ignores_other_agents_push(self, tmp_path):
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "agent-b", "other agent mem", recalled_count=5)
        _insert_push_event(db_file, "agent-b", [mid])

        result = mod.tool_memory_utility_rate(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["pushes_tracked"] == 0

    def test_snapshot_baseline_respected(self, tmp_path):
        """Memory with recalled_count == recalled_at_push should NOT count as recalled."""
        db_file = _setup_db(tmp_path)
        mid = _insert_memory(db_file, "test-agent", "stable memory", recalled_count=3)
        # Snapshot also shows 3 — no new recalls since push
        _insert_push_event(db_file, "test-agent", [mid],
                           recalled_at_push={str(mid): 3})

        result = mod.tool_memory_utility_rate(agent_id="test-agent", days=30)
        assert result["ok"] is True
        assert result["memories_recalled"] == 0
        assert result["utility_rate"] == 0.0


# ---------------------------------------------------------------------------
# _tokenize helper
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic_tokenization(self):
        tokens = mod._tokenize("python database migration")
        assert "python" in tokens
        assert "database" in tokens
        assert "migration" in tokens

    def test_stopwords_removed(self):
        tokens = mod._tokenize("the quick fox")
        assert "the" not in tokens

    def test_short_tokens_removed(self):
        tokens = mod._tokenize("a go do run")
        assert "a" not in tokens
        assert "go" not in tokens

    def test_case_insensitive(self):
        tokens = mod._tokenize("Python DATABASE")
        assert "python" in tokens
        assert "database" in tokens

    def test_empty_string(self):
        assert mod._tokenize("") == []

    def test_none_input(self):
        assert mod._tokenize(None) == []
