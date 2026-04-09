"""Tests for MCP tool input validation."""

import os
import tempfile
import pytest

from agentmemory.brain import Brain


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Ensure BRAIN_DB doesn't leak across tests."""
    db_path = str(tmp_path / "test_brain.db")
    monkeypatch.setenv("BRAIN_DB", db_path)
    Brain(db_path, agent_id="test-agent")
    # Reload mcp_server DB_PATH
    import agentmemory.mcp_server as ms
    from pathlib import Path
    ms.DB_PATH = Path(db_path)


def _init():
    pass  # handled by fixture


class TestMemoryValidation:
    def test_invalid_category_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_memory_add
        result = tool_memory_add(agent_id="test", content="test", category="invalid_category")
        assert result.get("ok") is False
        assert "category" in result.get("error", "").lower()

    def test_confidence_out_of_range_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_memory_add
        result = tool_memory_add(agent_id="test", content="test", category="lesson", confidence=5.0)
        assert result.get("ok") is False
        assert "confidence" in result.get("error", "").lower()

    def test_negative_confidence_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_memory_add
        result = tool_memory_add(agent_id="test", content="test", category="lesson", confidence=-0.5)
        assert result.get("ok") is False

    def test_invalid_scope_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_memory_add
        result = tool_memory_add(agent_id="test", content="test", category="lesson", scope="bad_scope")
        assert result.get("ok") is False
        assert "scope" in result.get("error", "").lower()

    def test_invalid_memory_type_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_memory_add
        result = tool_memory_add(agent_id="test", content="test", category="lesson", memory_type="dream")
        assert result.get("ok") is False

    def test_valid_memory_accepted(self):
        _init()
        from agentmemory.mcp_server import tool_memory_add
        result = tool_memory_add(agent_id="test", content="valid memory", category="lesson", force=True)
        assert result.get("ok") is True


class TestEventValidation:
    def test_invalid_event_type_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_event_add
        result = tool_event_add(agent_id="test", summary="test", event_type="bogus")
        assert result.get("ok") is False
        assert "event_type" in result.get("error", "").lower()

    def test_importance_out_of_range_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_event_add
        result = tool_event_add(agent_id="test", summary="test", event_type="observation", importance=2.0)
        assert result.get("ok") is False


class TestEntityValidation:
    def test_invalid_entity_type_rejected(self):
        _init()
        from agentmemory.mcp_server import tool_entity_create
        result = tool_entity_create(agent_id="test", name="Bob", entity_type="alien")
        assert result.get("ok") is False
        assert "entity_type" in result.get("error", "").lower()
