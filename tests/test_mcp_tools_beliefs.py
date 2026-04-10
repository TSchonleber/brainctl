"""Tests for mcp_tools_beliefs — belief system MCP tools."""
import sqlite3
import sys
import os
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_beliefs as beliefs_mod


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
        "created_at, updated_at) VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S','now'),"
        "strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id, "test", "active"),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def point_module_to_tmp(tmp_path, monkeypatch):
    """Redirect the module's DB_PATH to a fresh tmp database for each test."""
    db_file = _init_db(tmp_path)
    monkeypatch.setattr(beliefs_mod, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# belief_conflicts
# ---------------------------------------------------------------------------

class TestBeliefConflicts:
    def test_empty_returns_ok(self):
        result = beliefs_mod.tool_belief_conflicts()
        assert result["ok"] is True
        assert result["open_conflicts"] == 0
        assert result["conflicts"] == []

    def test_returns_inserted_conflict(self, monkeypatch):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "agent-a")
        _seed_agent(db_file, "agent-b")
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO belief_conflicts "
            "(topic, agent_a_id, agent_b_id, belief_a, belief_b, conflict_type, severity) "
            "VALUES (?,?,?,?,?,?,?)",
            ("topic:foo", "agent-a", "agent-b", "A says yes", "B says no", "factual", 0.8),
        )
        conn.commit()
        conn.close()

        result = beliefs_mod.tool_belief_conflicts()
        assert result["ok"] is True
        assert result["open_conflicts"] == 1
        assert result["conflicts"][0]["severity"] == 0.8

    def test_agent_filter(self, monkeypatch):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "alpha")
        _seed_agent(db_file, "beta")
        _seed_agent(db_file, "gamma")
        conn = sqlite3.connect(str(db_file))
        conn.executemany(
            "INSERT INTO belief_conflicts "
            "(topic, agent_a_id, agent_b_id, belief_a, belief_b, conflict_type, severity) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("topic:1", "alpha", "beta", "A", "B", "factual", 0.5),
                ("topic:2", "gamma", "beta", "C", "D", "factual", 0.4),
            ],
        )
        conn.commit()
        conn.close()

        result = beliefs_mod.tool_belief_conflicts(agent_id="alpha")
        assert result["ok"] is True
        assert result["open_conflicts"] == 1
        assert result["conflicts"][0]["agent_a_id"] == "alpha"

    def test_min_severity_filter(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "x")
        _seed_agent(db_file, "y")
        conn = sqlite3.connect(str(db_file))
        conn.executemany(
            "INSERT INTO belief_conflicts "
            "(topic, agent_a_id, agent_b_id, belief_a, belief_b, conflict_type, severity) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("t1", "x", "y", "a", "b", "factual", 0.9),
                ("t2", "x", "y", "c", "d", "factual", 0.1),
            ],
        )
        conn.commit()
        conn.close()

        result = beliefs_mod.tool_belief_conflicts(min_severity=0.5)
        assert result["ok"] is True
        assert result["open_conflicts"] == 1
        assert result["conflicts"][0]["severity"] == 0.9


# ---------------------------------------------------------------------------
# belief_set and belief_get
# ---------------------------------------------------------------------------

class TestBeliefSetGet:
    def test_belief_set_creates_belief(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "observer-1")
        _seed_agent(db_file, "target-1")

        result = beliefs_mod.tool_belief_set(
            observer="observer-1",
            target_agent="target-1",
            belief_type="role",
            content="This agent is a planner.",
        )
        assert result["ok"] is True
        assert result["action"] == "created"
        assert result["topic"] == "agent:target-1:role"

    def test_belief_set_updates_existing(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "obs")
        _seed_agent(db_file, "tgt")

        beliefs_mod.tool_belief_set(
            observer="obs", target_agent="tgt",
            belief_type="status", content="idle",
        )
        result = beliefs_mod.tool_belief_set(
            observer="obs", target_agent="tgt",
            belief_type="status", content="busy",
        )
        assert result["ok"] is True
        assert result["action"] == "updated"

    def test_belief_get_returns_created_belief(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "watcher")
        _seed_agent(db_file, "worker")

        beliefs_mod.tool_belief_set(
            observer="watcher", target_agent="worker",
            belief_type="capability", content="Can handle SQL tasks",
            confidence=0.85,
        )

        result = beliefs_mod.tool_belief_get(target_agent="worker")
        assert result["ok"] is True
        assert result["belief_count"] == 1
        b = result["beliefs"][0]
        assert b["observer"] == "watcher"
        assert b["belief_type"] == "capability"
        assert b["content"] == "Can handle SQL tasks"
        assert abs(b["confidence"] - 0.85) < 1e-6

    def test_belief_get_observer_filter(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "obs-a")
        _seed_agent(db_file, "obs-b")
        _seed_agent(db_file, "the-target")

        beliefs_mod.tool_belief_set(
            observer="obs-a", target_agent="the-target",
            belief_type="role", content="Planner",
        )
        beliefs_mod.tool_belief_set(
            observer="obs-b", target_agent="the-target",
            belief_type="role", content="Executor",
        )

        result_all = beliefs_mod.tool_belief_get(target_agent="the-target")
        assert result_all["belief_count"] == 2

        result_filtered = beliefs_mod.tool_belief_get(target_agent="the-target", observer="obs-a")
        assert result_filtered["belief_count"] == 1
        assert result_filtered["beliefs"][0]["observer"] == "obs-a"

    def test_belief_set_assumption_flag(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "guesser")
        _seed_agent(db_file, "unknown-agent")

        beliefs_mod.tool_belief_set(
            observer="guesser", target_agent="unknown-agent",
            belief_type="location", content="Probably in EU",
            assumption=True,
        )

        result = beliefs_mod.tool_belief_get(target_agent="unknown-agent")
        assert result["beliefs"][0]["is_assumption"] is True

    def test_belief_get_no_results(self):
        result = beliefs_mod.tool_belief_get(target_agent="nobody")
        assert result["ok"] is True
        assert result["belief_count"] == 0
        assert result["beliefs"] == []


# ---------------------------------------------------------------------------
# belief_seed
# ---------------------------------------------------------------------------

class TestBeliefSeed:
    def _insert_expertise(self, db_path: Path, agent_id: str, domain: str, strength: float):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR IGNORE INTO agent_expertise (agent_id, domain, strength) VALUES (?,?,?)",
            (agent_id, domain, strength),
        )
        conn.commit()
        conn.close()

    def test_seed_creates_capability_belief(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "expert-agent")
        _seed_agent(db_file, "cortex")
        self._insert_expertise(db_file, "expert-agent", "python", 0.9)

        result = beliefs_mod.tool_belief_seed(observer="cortex")
        assert result["ok"] is True
        assert result["created"] == 1
        assert result["updated"] == 0

        beliefs = beliefs_mod.tool_belief_get(target_agent="expert-agent")
        assert beliefs["belief_count"] == 1
        assert "python" in beliefs["beliefs"][0]["content"]

    def test_seed_dry_run_writes_nothing(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "dry-agent")
        _seed_agent(db_file, "cortex")
        self._insert_expertise(db_file, "dry-agent", "sql", 0.7)

        result = beliefs_mod.tool_belief_seed(observer="cortex", dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["created"] == 0
        assert result["updated"] == 0
        assert len(result["dry_run_items"]) == 1

        beliefs = beliefs_mod.tool_belief_get(target_agent="dry-agent")
        assert beliefs["belief_count"] == 0

    def test_seed_updates_existing_belief(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "skilled")
        _seed_agent(db_file, "cortex")
        self._insert_expertise(db_file, "skilled", "go", 0.8)

        beliefs_mod.tool_belief_seed(observer="cortex")
        # Add another domain so content changes
        self._insert_expertise(db_file, "skilled", "rust", 0.95)

        result = beliefs_mod.tool_belief_seed(observer="cortex")
        assert result["ok"] is True
        assert result["updated"] == 1
        assert result["created"] == 0

    def test_seed_min_strength_filter(self):
        db_file = beliefs_mod.DB_PATH
        _seed_agent(db_file, "weak-agent")
        _seed_agent(db_file, "cortex")
        self._insert_expertise(db_file, "weak-agent", "css", 0.1)

        result = beliefs_mod.tool_belief_seed(observer="cortex", min_strength=0.5)
        assert result["ok"] is True
        assert result["created"] == 0
        assert result["agents_processed"] == 0


# ---------------------------------------------------------------------------
# Module interface contract
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(beliefs_mod.TOOLS, list)
        assert all(isinstance(t, Tool) for t in beliefs_mod.TOOLS)

    def test_dispatch_keys_match_tool_names(self):
        tool_names = {t.name for t in beliefs_mod.TOOLS}
        dispatch_keys = set(beliefs_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_expected_tool_names_present(self):
        names = {t.name for t in beliefs_mod.TOOLS}
        expected = {
            "belief_conflicts", "collapse_log", "collapse_stats",
            "belief_set", "belief_get", "belief_seed",
        }
        assert expected == names

    def test_dispatch_callables(self):
        for name, fn in beliefs_mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"
