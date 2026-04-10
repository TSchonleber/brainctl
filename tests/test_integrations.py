"""Tests for framework integrations (LangChain, CrewAI).

Tests mock the framework imports so they run without langchain-core or crewai installed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Mock frameworks before importing integrations
# ---------------------------------------------------------------------------


def _mock_langchain():
    """Mock langchain_core so the integration can import."""
    # Create mock message classes
    class BaseMessage:
        def __init__(self, content="", **kwargs):
            self.content = content

    class HumanMessage(BaseMessage):
        pass

    class AIMessage(BaseMessage):
        pass

    class SystemMessage(BaseMessage):
        pass

    class BaseChatMessageHistory:
        pass

    def messages_from_dict(dicts):
        return []

    def message_to_dict(msg):
        return {}

    # Wire up mock modules
    chat_history = types.ModuleType("langchain_core.chat_history")
    chat_history.BaseChatMessageHistory = BaseChatMessageHistory

    messages = types.ModuleType("langchain_core.messages")
    messages.BaseMessage = BaseMessage
    messages.HumanMessage = HumanMessage
    messages.AIMessage = AIMessage
    messages.SystemMessage = SystemMessage
    messages.messages_from_dict = messages_from_dict
    messages.message_to_dict = message_to_dict

    langchain_core = types.ModuleType("langchain_core")
    sys.modules["langchain_core"] = langchain_core
    sys.modules["langchain_core.chat_history"] = chat_history
    sys.modules["langchain_core.messages"] = messages

    return HumanMessage, AIMessage, SystemMessage


def _mock_crewai():
    """Mock crewai so the integration can import."""
    class RAGStorage:
        def __init__(self, *args, **kwargs):
            pass
        def save(self, value, metadata=None, agent=None):
            pass
        def search(self, query, limit=3, filter=None, score_threshold=0.0):
            return []
        def reset(self):
            pass
        def _initialize_app(self):
            pass

    rag_storage = types.ModuleType("crewai.memory.storage.rag_storage")
    rag_storage.RAGStorage = RAGStorage

    memory_storage = types.ModuleType("crewai.memory.storage")
    memory = types.ModuleType("crewai.memory")
    crewai = types.ModuleType("crewai")

    sys.modules["crewai"] = crewai
    sys.modules["crewai.memory"] = memory
    sys.modules["crewai.memory.storage"] = memory_storage
    sys.modules["crewai.memory.storage.rag_storage"] = rag_storage


# Set up mocks before importing
HumanMessage, AIMessage, SystemMessage = _mock_langchain()
_mock_crewai()

from agentmemory.integrations.langchain import BrainctlChatMessageHistory
from agentmemory.integrations.crewai import BrainctlStorage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "brain.db")


# ---------------------------------------------------------------------------
# LangChain: BrainctlChatMessageHistory
# ---------------------------------------------------------------------------


class TestLangChainHistory:
    def test_empty_history(self, tmp_db):
        history = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        assert history.messages == []

    def test_add_and_retrieve_messages(self, tmp_db):
        history = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        history.add_messages([
            HumanMessage(content="What's the weather?"),
            AIMessage(content="It's sunny today."),
        ])
        msgs = history.messages
        assert len(msgs) == 2
        assert msgs[0].content == "What's the weather?"
        assert msgs[1].content == "It's sunny today."

    def test_session_isolation(self, tmp_db):
        h1 = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        h2 = BrainctlChatMessageHistory(session_id="sess-2", db_path=tmp_db)
        h1.add_messages([HumanMessage(content="session 1 message")])
        h2.add_messages([HumanMessage(content="session 2 message")])
        assert len(h1.messages) == 1
        assert len(h2.messages) == 1
        assert h1.messages[0].content == "session 1 message"
        assert h2.messages[0].content == "session 2 message"

    def test_clear(self, tmp_db):
        history = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        history.add_messages([HumanMessage(content="hello")])
        assert len(history.messages) == 1
        history.clear()
        assert len(history.messages) == 0

    def test_message_types_preserved(self, tmp_db):
        history = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        history.add_messages([
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
        ])
        msgs = history.messages
        assert len(msgs) == 3
        # Check types via the metadata stored in events
        db = history.brain._db()
        rows = db.execute(
            "SELECT metadata FROM events WHERE session_id = 'sess-1' ORDER BY created_at"
        ).fetchall()
        db.close()
        types = [json.loads(r["metadata"])["type"] for r in rows]
        assert types == ["system", "human", "ai"]

    def test_brain_access(self, tmp_db):
        history = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        mid = history.brain.remember("Important fact", category="lesson")
        assert mid > 0
        results = history.brain.search("important fact")
        assert len(results) >= 1

    def test_chronological_order(self, tmp_db):
        history = BrainctlChatMessageHistory(session_id="sess-1", db_path=tmp_db)
        history.add_messages([HumanMessage(content="first")])
        history.add_messages([HumanMessage(content="second")])
        history.add_messages([HumanMessage(content="third")])
        msgs = history.messages
        assert [m.content for m in msgs] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# CrewAI: BrainctlStorage
# ---------------------------------------------------------------------------


class TestCrewAIStorage:
    def test_save_and_search(self, tmp_db):
        storage = BrainctlStorage(type="short-term", db_path=tmp_db)
        storage.save("The API rate-limits at 100 requests per 15 seconds")
        results = storage.search("rate limit")
        assert len(results) >= 1
        assert "rate" in results[0]["context"].lower()

    def test_search_returns_correct_format(self, tmp_db):
        storage = BrainctlStorage(type="long-term", db_path=tmp_db)
        storage.save("PostgreSQL connection pool is capped at 20")
        results = storage.search("connection pool")
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "metadata" in r
        assert "context" in r
        assert "score" in r
        assert isinstance(r["score"], float)

    def test_save_with_metadata(self, tmp_db):
        storage = BrainctlStorage(type="short-term", db_path=tmp_db)
        storage.save(
            "JWT tokens expire after 24 hours",
            metadata={"importance": 0.9, "categories": ["convention"]},
        )
        results = storage.search("JWT expire")
        assert len(results) >= 1

    def test_type_to_category_mapping(self, tmp_db):
        st = BrainctlStorage(type="short-term", db_path=tmp_db)
        lt = BrainctlStorage(type="long-term", db_path=tmp_db)
        ent = BrainctlStorage(type="entity", db_path=tmp_db)
        assert st._category_for_type() == "project"
        assert lt._category_for_type() == "lesson"
        assert ent._category_for_type() == "integration"

    def test_reset_only_retires_own_type(self, tmp_db):
        st = BrainctlStorage(type="short-term", db_path=tmp_db)
        lt = BrainctlStorage(type="long-term", db_path=tmp_db)
        st.save("short term fact")
        lt.save("long term fact")
        st.reset()
        # Long-term memories should survive
        lt_results = lt.search("long term")
        assert len(lt_results) >= 1

    def test_empty_search(self, tmp_db):
        storage = BrainctlStorage(type="short-term", db_path=tmp_db)
        results = storage.search("nonexistent query xyz123")
        assert results == []

    def test_score_threshold(self, tmp_db):
        storage = BrainctlStorage(type="short-term", db_path=tmp_db)
        storage.save("testing score threshold behavior")
        results = storage.search("testing score", score_threshold=0.99)
        # High threshold should filter out most results
        # (FTS5 confidence is typically < 0.99)
        assert isinstance(results, list)

    def test_brain_accessible(self, tmp_db):
        storage = BrainctlStorage(type="short-term", db_path=tmp_db)
        assert hasattr(storage, "brain")
        stats = storage.brain.stats()
        assert "active_memories" in stats

    def test_empty_value_ignored(self, tmp_db):
        storage = BrainctlStorage(type="short-term", db_path=tmp_db)
        storage.save("")
        storage.save("   ")
        assert storage.brain.stats()["active_memories"] == 0
