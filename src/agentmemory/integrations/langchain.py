"""LangChain integration for brainctl.

Provides BaseChatMessageHistory backed by brain.db, plus direct Brain access
for knowledge operations (entities, decisions, triggers) that go beyond chat.

Install: pip install brainctl langchain-core

Usage with LangGraph / LCEL:

    from agentmemory.integrations.langchain import BrainctlChatMessageHistory
    from langchain_core.runnables.history import RunnableWithMessageHistory

    def get_session_history(session_id: str):
        return BrainctlChatMessageHistory(session_id=session_id)

    chain_with_history = RunnableWithMessageHistory(
        runnable=my_chain,
        get_session_history=get_session_history,
    )

Direct Brain access (for knowledge beyond chat):

    history = BrainctlChatMessageHistory(session_id="sess-1")
    history.brain.remember("API rate-limits at 100/15s", category="integration")
    history.brain.entity("RateLimitAPI", "service")
    results = history.brain.search("rate limit")
"""
from __future__ import annotations

import json
from typing import List, Optional, Sequence

try:
    from langchain_core.chat_history import BaseChatMessageHistory
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        SystemMessage,
        messages_from_dict,
        message_to_dict,
    )
except ImportError as e:
    raise ImportError(
        "langchain-core is required for the LangChain integration. "
        "Install it with: pip install langchain-core"
    ) from e

from agentmemory.brain import Brain


class BrainctlChatMessageHistory(BaseChatMessageHistory):
    """Chat message history backed by brainctl's brain.db.

    Messages are stored as events in the events table with type 'chat_message'.
    The full message (role + content) is preserved in the event's metadata field
    so round-tripping is lossless.

    The Brain instance is exposed as `self.brain` for knowledge operations
    that go beyond chat history (entities, decisions, triggers, search).

    Args:
        session_id: Unique session identifier. Used to scope messages.
        db_path: Path to brain.db. Defaults to ~/agentmemory/db/brain.db.
        agent_id: Agent attribution for writes. Defaults to "langchain".
    """

    def __init__(
        self,
        session_id: str,
        db_path: Optional[str] = None,
        agent_id: str = "langchain",
    ) -> None:
        self.session_id = session_id
        self.brain = Brain(db_path=db_path, agent_id=agent_id)

    @property
    def messages(self) -> List[BaseMessage]:
        """Retrieve all messages for this session, ordered chronologically."""
        db = self.brain._db()
        rows = db.execute(
            "SELECT metadata FROM events "
            "WHERE agent_id = ? AND session_id = ? AND event_type = 'chat_message' "
            "ORDER BY created_at ASC",
            (self.brain.agent_id, self.session_id),
        ).fetchall()
        db.close()

        msgs: List[BaseMessage] = []
        for row in rows:
            try:
                data = json.loads(row["metadata"])
                msg_type = data.get("type", "human")
                content = data.get("content", "")
                if msg_type == "ai":
                    msgs.append(AIMessage(content=content))
                elif msg_type == "system":
                    msgs.append(SystemMessage(content=content))
                else:
                    msgs.append(HumanMessage(content=content))
            except (json.JSONDecodeError, TypeError):
                continue
        return msgs

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Persist messages to brain.db as events."""
        db = self.brain._db()
        from agentmemory.brain import _now_ts
        now = _now_ts()
        for msg in messages:
            msg_type = "human"
            if isinstance(msg, AIMessage):
                msg_type = "ai"
            elif isinstance(msg, SystemMessage):
                msg_type = "system"

            metadata = json.dumps({
                "type": msg_type,
                "content": msg.content if isinstance(msg.content, str) else str(msg.content),
            })
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, session_id, created_at) "
                "VALUES (?, 'chat_message', ?, ?, ?, ?)",
                (
                    self.brain.agent_id,
                    f"[{msg_type}] {str(msg.content)[:100]}",
                    metadata,
                    self.session_id,
                    now,
                ),
            )
        db.commit()
        db.close()

    def clear(self) -> None:
        """Remove all messages for this session."""
        db = self.brain._db()
        db.execute(
            "DELETE FROM events "
            "WHERE agent_id = ? AND session_id = ? AND event_type = 'chat_message'",
            (self.brain.agent_id, self.session_id),
        )
        db.commit()
        db.close()
