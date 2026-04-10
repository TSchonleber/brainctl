"""Tests for mcp_tools_lifecycle — memory lifecycle reporting MCP tools."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentmemory.brain import Brain
import agentmemory.mcp_tools_lifecycle as lifecycle_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_db(tmp_path: Path) -> Path:
    """Create a fresh brain.db using Brain, return its path."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    return db_file


def _patch_db(monkeypatch, db_file: Path) -> None:
    """Point the lifecycle module at the test DB."""
    monkeypatch.setattr(lifecycle_mod, "DB_PATH", db_file)


def _insert_agent(conn, agent_id: str = "agent-a") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES (?, ?, 'test', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()


def _insert_memory(
    conn,
    agent_id: str = "agent-a",
    content: str = "test memory",
    category: str = "lesson",
    confidence: float = 0.8,
    recalled_count: int = 0,
    retired: bool = False,
    protected: int = 0,
    retraction_reason: str | None = None,
    last_recalled_at: str | None = None,
) -> int:
    retired_val = "strftime('%Y-%m-%dT%H:%M:%S','now')" if retired else "NULL"
    lr = f"'{last_recalled_at}'" if last_recalled_at else "NULL"
    cursor = conn.execute(
        f"""
        INSERT INTO memories
          (agent_id, content, category, scope, confidence, recalled_count, protected,
           retraction_reason, last_recalled_at, retired_at, created_at, updated_at)
        VALUES
          (?, ?, ?, 'agent', ?, ?, ?, ?, {lr}, {retired_val},
           strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))
        """,
        (agent_id, content, category, confidence, recalled_count, protected, retraction_reason),
    )
    conn.commit()
    return cursor.lastrowid


def _insert_event(
    conn,
    agent_id: str = "agent-a",
    event_type: str = "memory_retired",
    summary: str = "event summary",
) -> None:
    conn.execute(
        "INSERT INTO events (agent_id, event_type, summary, created_at) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, event_type, summary),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# lifecycle_summary
# ---------------------------------------------------------------------------

class TestLifecycleSummary:
    def test_empty_db_returns_ok(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = lifecycle_mod._lifecycle_summary("no-such-agent")

        assert result["ok"] is True
        assert result["total_created"] == 0
        assert result["total_retired"] == 0
        assert result["survival_rate"] == 1.0
        assert result["decay_candidates"] == 0
        assert result["protected_count"] == 0
        assert isinstance(result["by_category"], dict)

    def test_counts_active_and_retired_memories(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_memory(conn, "agent-a", "memory 1", "lesson", 0.9, retired=False)
        _insert_memory(conn, "agent-a", "memory 2", "lesson", 0.9, retired=False)
        _insert_memory(conn, "agent-a", "memory 3", "lesson", 0.5, retired=True)
        conn.close()

        result = lifecycle_mod._lifecycle_summary("agent-a", days=30)

        assert result["ok"] is True
        assert result["total_created"] == 3   # 2 active + 1 retired in window
        assert result["total_retired"] == 1
        # 2 active / (2 active + 1 retired)
        assert round(result["survival_rate"], 2) == round(2 / 3, 2)

    def test_by_category_breakdown(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_memory(conn, "agent-a", "a", "lesson", 0.8)
        _insert_memory(conn, "agent-a", "b", "decision", 0.7)
        _insert_memory(conn, "agent-a", "c", "lesson", 0.4, retired=True)
        conn.close()

        result = lifecycle_mod._lifecycle_summary("agent-a", days=30)

        assert result["ok"] is True
        cats = result["by_category"]
        assert "lesson" in cats
        assert cats["lesson"]["active"] == 1
        assert cats["lesson"]["retired"] == 1
        assert "decision" in cats
        assert cats["decision"]["active"] == 1

    def test_decay_candidates_and_protected(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_memory(conn, "agent-a", "low conf", "lesson", 0.1, protected=0)
        _insert_memory(conn, "agent-a", "ok conf", "lesson", 0.9, protected=1)
        conn.close()

        result = lifecycle_mod._lifecycle_summary("agent-a", days=30)

        assert result["ok"] is True
        assert result["decay_candidates"] == 1
        assert result["protected_count"] == 1

    def test_dispatch_lifecycle_summary(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = lifecycle_mod.DISPATCH["lifecycle_summary"]
        result = fn({"agent_id": "nobody", "days": 7})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# write_gate_stats
# ---------------------------------------------------------------------------

class TestWriteGateStats:
    def test_empty_db_no_events(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = lifecycle_mod._write_gate_stats("agent-x", days=30)

        assert result["ok"] is True
        assert result["gate_events_found"] == 0
        assert result["rejection_estimate"] is None
        assert "notes" in result

    def test_rejection_events_counted(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_event(conn, "agent-a", "write_gate_rejected", "gate rejected memory X")
        _insert_event(conn, "agent-a", "write_gate_rejected", "gate rejected memory Y")
        _insert_memory(conn, "agent-a", "accepted memory", "lesson", 0.9)
        conn.close()

        result = lifecycle_mod._write_gate_stats("agent-a", days=30)

        assert result["ok"] is True
        assert result["gate_events_found"] == 2
        assert result["rejection_estimate"] == 2
        assert result["acceptance_estimate"] == 1

    def test_dispatch_write_gate_stats(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = lifecycle_mod.DISPATCH["write_gate_stats"]
        result = fn({"agent_id": "nobody"})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# decay_report
# ---------------------------------------------------------------------------

class TestDecayReport:
    def test_empty_db_returns_ok(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = lifecycle_mod._decay_report("no-agent")

        assert result["ok"] is True
        assert result["at_risk"] == []

    def test_low_confidence_memory_flagged(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_memory(conn, "agent-a", "shaky memory", "lesson", 0.1)
        _insert_memory(conn, "agent-a", "solid memory", "lesson", 0.95)
        conn.close()

        result = lifecycle_mod._decay_report("agent-a", confidence_threshold=0.3)

        assert result["ok"] is True
        ids = [r["memory_id"] for r in result["at_risk"]]
        # Only the low-confidence one should appear
        confidences = [r["confidence"] for r in result["at_risk"]]
        assert all(c < 0.3 for c in confidences)
        assert len(result["at_risk"]) == 1

    def test_at_risk_entry_has_required_fields(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_memory(conn, "agent-a", "shaky memory", "lesson", 0.1)
        conn.close()

        result = lifecycle_mod._decay_report("agent-a", confidence_threshold=0.5)

        assert result["ok"] is True
        assert len(result["at_risk"]) >= 1
        entry = result["at_risk"][0]
        for field in ("memory_id", "content_snippet", "confidence", "category",
                      "days_since_recalled", "temporal_class"):
            assert field in entry, f"Missing field: {field}"

    def test_dispatch_decay_report(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = lifecycle_mod.DISPATCH["decay_report"]
        result = fn({"agent_id": "nobody", "confidence_threshold": 0.5, "days_inactive": 30, "limit": 5})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# consolidation_events
# ---------------------------------------------------------------------------

class TestConsolidationEvents:
    def test_empty_db_returns_ok(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = lifecycle_mod._consolidation_events("no-agent")

        assert result["ok"] is True
        assert result["events"] == []
        assert result["by_type"] == {}

    def test_consolidation_events_filtered_by_type(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_event(conn, "agent-a", "memory_promoted", "promoted memory X")
        _insert_event(conn, "agent-a", "memory_retired", "retired memory Y")
        _insert_event(conn, "agent-a", "observation", "should not appear")
        conn.close()

        result = lifecycle_mod._consolidation_events("agent-a", days=30)

        assert result["ok"] is True
        # The "observation" event should NOT appear
        event_types = {e["event_type"] for e in result["events"]}
        assert "observation" not in event_types
        assert "memory_promoted" in event_types or "memory_retired" in event_types

    def test_by_type_counts(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_event(conn, "agent-a", "memory_promoted", "promote 1")
        _insert_event(conn, "agent-a", "memory_promoted", "promote 2")
        _insert_event(conn, "agent-a", "cap_exceeded", "cap hit")
        conn.close()

        result = lifecycle_mod._consolidation_events("agent-a", days=30)

        assert result["ok"] is True
        assert result["by_type"].get("memory_promoted") == 2
        assert result["by_type"].get("cap_exceeded") == 1

    def test_dispatch_consolidation_events(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = lifecycle_mod.DISPATCH["consolidation_events"]
        result = fn({"agent_id": "nobody", "days": 14, "limit": 10})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# retirement_analysis
# ---------------------------------------------------------------------------

class TestRetirementAnalysis:
    def test_empty_db_returns_ok(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = lifecycle_mod._retirement_analysis("no-agent")

        assert result["ok"] is True
        assert result["total_retired"] == 0
        assert result["by_category"] == {}
        assert result["low_confidence_retirements"] == 0
        assert result["manual_retirements"] == 0

    def test_retired_memories_categorised(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        _insert_memory(conn, "agent-a", "old lesson", "lesson", 0.2, retired=True)
        _insert_memory(conn, "agent-a", "old decision", "decision", 0.5, retired=True)
        _insert_memory(conn, "agent-a", "active", "lesson", 0.9, retired=False)
        conn.close()

        result = lifecycle_mod._retirement_analysis("agent-a", days=30)

        assert result["ok"] is True
        assert result["total_retired"] == 2
        cats = result["by_category"]
        assert "lesson" in cats
        assert cats["lesson"]["count"] == 1
        assert "decision" in cats

    def test_low_confidence_and_manual_retirements(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        _insert_agent(conn, "agent-a")
        # Low-confidence retirement
        _insert_memory(conn, "agent-a", "very uncertain", "lesson", 0.1, retired=True)
        # Manual retirement with explicit reason
        _insert_memory(conn, "agent-a", "manually removed", "lesson", 0.7, retired=True,
                       retraction_reason="user explicitly removed")
        conn.close()

        result = lifecycle_mod._retirement_analysis("agent-a", days=30)

        assert result["ok"] is True
        assert result["low_confidence_retirements"] == 1
        assert result["manual_retirements"] == 1

    def test_dispatch_retirement_analysis(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = lifecycle_mod.DISPATCH["retirement_analysis"]
        result = fn({"agent_id": "nobody"})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_tools_list_has_five_entries(self):
        assert len(lifecycle_mod.TOOLS) == 5

    def test_tool_names_match_dispatch_keys(self):
        tool_names = {t.name for t in lifecycle_mod.TOOLS}
        dispatch_keys = set(lifecycle_mod.DISPATCH.keys())
        expected = {
            "lifecycle_summary",
            "write_gate_stats",
            "decay_report",
            "consolidation_events",
            "retirement_analysis",
        }
        assert tool_names == dispatch_keys == expected

    def test_all_dispatch_values_are_callable(self):
        for name, fn in lifecycle_mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"

    def test_all_tools_have_input_schema(self):
        for tool in lifecycle_mod.TOOLS:
            assert tool.inputSchema is not None, f"Tool {tool.name!r} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"

    def test_agent_id_required_in_all_tools(self):
        """Every tool in this module requires agent_id."""
        for tool in lifecycle_mod.TOOLS:
            assert "agent_id" in tool.inputSchema.get("required", []), (
                f"Tool {tool.name!r} should require agent_id"
            )
