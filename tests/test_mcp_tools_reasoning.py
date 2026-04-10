"""Tests for mcp_tools_reasoning — reasoning & inference MCP tools."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_reasoning as _mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brain(tmp_path: Path) -> tuple[Brain, Path]:
    """Create a Brain-backed test DB and return (brain, db_path)."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    return brain, db_file


def _register_agent(db_path: Path, agent_id: str) -> None:
    """Ensure agent row exists so FK constraints on access_log pass."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES (?, ?, 'test', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()
    conn.close()


def _set_db(db_path: Path) -> None:
    """Point the module's DB_PATH at the test database."""
    _mod.DB_PATH = db_path
    # Also update _impl.DB_PATH so any internal helpers that reference it
    # (e.g. _try_get_db_with_vec, _embed_query_safe) use the test DB.
    # In CI vec is unavailable so these functions return None anyway, but
    # keeping it consistent avoids subtle failures if vec becomes available.
    import agentmemory._impl as _impl_mod
    _impl_mod.DB_PATH = db_path


# ---------------------------------------------------------------------------
# Tests — tool_reason
# ---------------------------------------------------------------------------

class TestToolReason:
    def test_returns_ok_structure(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        brain.remember("Python is a great language for data science", category="lesson", confidence=0.9)
        brain.remember("We use Python 3.12 for this project", category="project", confidence=1.0)
        _register_agent(db_path, "test-agent")
        _set_db(db_path)

        result = _mod.tool_reason(agent_id="test-agent", query="Python language")

        assert result["ok"] is True
        assert result["query"] == "Python language"
        assert result["tier"] == "L2-structural"
        assert "l1_memories" in result
        assert "l1_events" in result
        assert "l2_expansions" in result
        assert "provenance" in result
        assert "latency_ms" in result

    def test_finds_relevant_memories(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        brain.remember("Deploy to production using Docker containers", category="lesson", confidence=0.85)
        brain.remember("Database migrations run automatically on deploy", category="convention", confidence=0.9)
        _register_agent(db_path, "agent-alpha")
        _set_db(db_path)

        result = _mod.tool_reason(agent_id="agent-alpha", query="deploy production")

        assert result["ok"] is True
        assert result["provenance"]["l1_memory_count"] >= 1

    def test_empty_query_returns_error(self, tmp_path):
        _, db_path = _make_brain(tmp_path)
        _set_db(db_path)

        result = _mod.tool_reason(agent_id="test-agent", query="")
        assert result["ok"] is False
        assert "error" in result

    def test_no_memories_returns_empty_lists(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        _register_agent(db_path, "no-data-agent")
        _set_db(db_path)

        result = _mod.tool_reason(agent_id="no-data-agent", query="something obscure xyz")
        assert result["ok"] is True
        assert result["l1_memories"] == []
        assert result["l2_expansions"] == []


# ---------------------------------------------------------------------------
# Tests — tool_infer
# ---------------------------------------------------------------------------

class TestToolInfer:
    def test_returns_ok_with_inference(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        brain.remember("Security patches must be applied within 48 hours", category="convention", confidence=0.95)
        brain.remember("We had a security incident from unpatched CVE-2024-1234", category="lesson", confidence=0.9)
        _register_agent(db_path, "sec-agent")
        _set_db(db_path)

        result = _mod.tool_infer(agent_id="sec-agent", query="security patching policy")

        assert result["ok"] is True
        assert "inference" in result
        assert "tier" in result["inference"]
        assert "confidence" in result["inference"]
        assert "conclusion" in result["inference"]
        assert "evidence" in result
        assert "provenance" in result

    def test_inference_tier_present(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        brain.remember("Use feature flags for gradual rollouts", category="convention", confidence=0.8)
        _register_agent(db_path, "rollout-agent")
        _set_db(db_path)

        result = _mod.tool_infer(agent_id="rollout-agent", query="feature flags rollout")

        assert result["ok"] is True
        assert result["inference"]["tier"] in {"L1-gap", "L3-policy", "L3-inferential", "L3-weak"}

    def test_empty_query_returns_error(self, tmp_path):
        _, db_path = _make_brain(tmp_path)
        _set_db(db_path)

        result = _mod.tool_infer(agent_id="test-agent", query="")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Tests — tool_infer_pretask
# ---------------------------------------------------------------------------

class TestToolInferPretask:
    def test_detects_low_confidence_memories(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        # Manually insert a low-confidence memory (free_energy = (1-0.3)*0.9 = 0.63 > 0.15)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('unc-agent', 'unc-agent', 'test', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        # confidence=0.3, importance defaults to 0.5 via m.get("importance") or 0.5
        # free_energy = (1-0.3)*0.5 = 0.35 which exceeds the 0.15 threshold
        conn.execute(
            "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
            "VALUES ('unc-agent', 'lesson', 'global', 'database migration procedure unclear', 0.3, strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.commit()
        conn.close()
        _set_db(db_path)

        result = _mod.tool_infer_pretask(agent_id="unc-agent", task_desc="database migration")

        assert result["ok"] is True
        assert result["task_desc"] == "database migration"
        assert len(result["uncertainty_gaps"]) >= 1
        assert result["uncertainty_gaps"][0]["free_energy"] >= 0.15
        assert len(result["log_ids"]) >= 1

    def test_no_uncertainty_when_high_confidence(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        brain.remember("Deploy with zero downtime using blue-green strategy", category="lesson", confidence=0.99)
        _register_agent(db_path, "confident-agent")
        _set_db(db_path)

        result = _mod.tool_infer_pretask(agent_id="confident-agent", task_desc="deploy blue-green")

        assert result["ok"] is True
        # High confidence → free_energy = (1-0.99)*imp which is tiny, should be 0
        assert result["summary"]["total_gaps_found"] == 0

    def test_empty_task_desc_returns_error(self, tmp_path):
        _, db_path = _make_brain(tmp_path)
        _set_db(db_path)

        result = _mod.tool_infer_pretask(agent_id="test-agent", task_desc="")
        assert result["ok"] is False
        assert "error" in result

    def test_returns_summary_fields(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        _register_agent(db_path, "summary-agent")
        _set_db(db_path)

        result = _mod.tool_infer_pretask(agent_id="summary-agent", task_desc="deployment procedure")

        assert result["ok"] is True
        assert "summary" in result
        assert "total_gaps_found" in result["summary"]
        assert "max_free_energy" in result["summary"]
        assert "avg_free_energy" in result["summary"]
        assert "latency_ms" in result["summary"]


# ---------------------------------------------------------------------------
# Tests — tool_infer_gapfill
# ---------------------------------------------------------------------------

class TestToolInferGapfill:
    def _seed_uncertainty_log(self, db_path: Path, agent_id: str, task_desc: str) -> None:
        """Insert an open gap into agent_uncertainty_log for testing gapfill."""
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO agent_uncertainty_log (agent_id, task_desc, gap_topic, free_energy, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (agent_id, task_desc, "unclear deployment steps", 0.5)
        )
        conn.commit()
        conn.close()

    def test_resolves_open_gaps(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        _register_agent(db_path, "fill-agent")
        self._seed_uncertainty_log(db_path, "fill-agent", "kubernetes deployment steps")
        _set_db(db_path)

        result = _mod.tool_infer_gapfill(
            agent_id="fill-agent",
            task_desc="kubernetes deployment steps",
        )

        assert result["ok"] is True
        assert result["total_resolved"] >= 1
        assert len(result["resolved_gaps"]) >= 1
        assert result["memory_created"] is None  # no content passed

    def test_creates_memory_when_content_provided(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        _register_agent(db_path, "learn-agent")
        self._seed_uncertainty_log(db_path, "learn-agent", "API rate limiting strategy")
        _set_db(db_path)

        result = _mod.tool_infer_gapfill(
            agent_id="learn-agent",
            task_desc="API rate limiting strategy",
            content="Use exponential backoff with jitter; max 3 retries before circuit-break.",
        )

        assert result["ok"] is True
        assert result["memory_created"] is not None
        assert isinstance(result["memory_created"], int)
        assert result["total_resolved"] >= 1

        # Verify memory was actually written to DB
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT content, category FROM memories WHERE id=?", (result["memory_created"],)).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "lesson"
        assert "exponential backoff" in row[0]

    def test_empty_task_desc_returns_error(self, tmp_path):
        _, db_path = _make_brain(tmp_path)
        _set_db(db_path)

        result = _mod.tool_infer_gapfill(agent_id="test-agent", task_desc="")
        assert result["ok"] is False
        assert "error" in result

    def test_no_open_gaps_returns_ok_with_zero_resolved(self, tmp_path):
        brain, db_path = _make_brain(tmp_path)
        _register_agent(db_path, "empty-agent")
        _set_db(db_path)

        result = _mod.tool_infer_gapfill(
            agent_id="empty-agent",
            task_desc="no gaps exist for this unique task xyz789",
        )

        assert result["ok"] is True
        assert result["total_resolved"] == 0
        assert result["resolved_gaps"] == []


# ---------------------------------------------------------------------------
# Tests — TOOLS and DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list_of_tools(self):
        from mcp.types import Tool
        assert isinstance(_mod.TOOLS, list)
        assert len(_mod.TOOLS) == 4
        for tool in _mod.TOOLS:
            assert isinstance(tool, Tool)

    def test_tool_names(self):
        names = {t.name for t in _mod.TOOLS}
        assert names == {"reason", "infer", "infer_pretask", "infer_gapfill"}

    def test_dispatch_keys_match_tool_names(self):
        tool_names = {t.name for t in _mod.TOOLS}
        dispatch_keys = set(_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_dispatch_values_are_callable(self):
        for name, fn in _mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"

    def test_required_fields_in_schemas(self):
        for tool in _mod.TOOLS:
            schema = tool.inputSchema
            assert "required" in schema, f"Tool {tool.name!r} missing 'required' in schema"
            assert "properties" in schema, f"Tool {tool.name!r} missing 'properties' in schema"
