"""brainctl MCP tools — multi-DB federation (read-only union queries)."""
from __future__ import annotations

from typing import Any

from mcp.types import Tool

from agentmemory.federation import (
    federated_entity_search,
    federated_memory_search,
    federated_search,
    federated_stats,
)

# ---------------------------------------------------------------------------
# Tool implementations (thin wrappers that parse kwargs)
# ---------------------------------------------------------------------------


def _call_federated_search(**kwargs: Any) -> dict:
    query = kwargs.get("query", "")
    tables = kwargs.get("tables") or None
    limit = int(kwargs.get("limit", 20))
    agent_id = kwargs.get("agent_id") or None
    return federated_search(query=query, tables=tables, limit=limit, agent_id=agent_id)


def _call_federated_stats(**kwargs: Any) -> dict:
    return federated_stats()


def _call_federated_memory_search(**kwargs: Any) -> dict:
    query = kwargs.get("query", "")
    limit = int(kwargs.get("limit", 20))
    category = kwargs.get("category") or None
    return federated_memory_search(query=query, limit=limit, category=category)


def _call_federated_entity_search(**kwargs: Any) -> dict:
    name = kwargs.get("name", "")
    entity_type = kwargs.get("entity_type") or None
    return federated_entity_search(name=name, entity_type=entity_type)


# ---------------------------------------------------------------------------
# MCP tool declarations
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="federated_search",
        description=(
            "Search memories, events, and entities across all federated brain.db files "
            "simultaneously in read-only union mode. "
            "Configure additional DBs via the BRAIN_DB_FEDERATION environment variable "
            "(colon-separated list of paths)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for across all federated databases.",
                },
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Which tables to search. Defaults to ['memories', 'events', 'entities']."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of total results to return (default: 20).",
                    "default": 20,
                },
                "agent_id": {
                    "type": "string",
                    "description": "If set, filter results to this agent ID only.",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="federated_stats",
        description=(
            "Return aggregate statistics across all federated brain.db files: "
            "memory count, event count, entity count, and agent count per DB, "
            "plus fleet-wide totals."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="federated_memory_search",
        description=(
            "FTS5 full-text memory search across all federated brain.db files. "
            "Results include a source_db field indicating which database each memory came from."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Full-text search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 20).",
                    "default": 20,
                },
                "category": {
                    "type": "string",
                    "description": "Optional memory category filter (e.g. 'lesson', 'convention').",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="federated_entity_search",
        description=(
            "Entity name lookup across all federated brain.db files. "
            "Results include a source_db field indicating which database each entity came from."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Entity name (or partial name) to search for.",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Optional entity type filter (e.g. 'person', 'project').",
                },
            },
            "required": ["name"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch table (maps tool name -> function)
# ---------------------------------------------------------------------------

DISPATCH: dict[str, Any] = {
    "federated_search": _call_federated_search,
    "federated_stats": _call_federated_stats,
    "federated_memory_search": _call_federated_memory_search,
    "federated_entity_search": _call_federated_entity_search,
}
