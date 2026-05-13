"""brainctl MCP tools — procedural memory system."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory import procedural
from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()


def _db():
    conn = open_db(str(DB_PATH))
    procedural.ensure_procedure_schema(conn)
    return conn


def tool_procedure_add(
    agent_id: str = "mcp-client",
    goal: str = "",
    title: str | None = None,
    description: str | None = None,
    procedure_kind: str = "workflow",
    task_family: str | None = None,
    scope: str = "global",
    category: str = "convention",
    confidence: float = 0.9,
    steps: list[str] | None = None,
    trigger_conditions: list[str] | None = None,
    preconditions: list[str] | None = None,
    tools: list[str] | None = None,
    failure_modes: list[str] | None = None,
    rollback_steps: list[str] | None = None,
    success_criteria: list[str] | None = None,
    expected_outcomes: list[str] | None = None,
    status: str = "active",
    **_kw: Any,
) -> dict[str, Any]:
    if not goal:
        return {"ok": False, "error": "goal is required"}
    db = _db()
    try:
        payload = {
            "title": title,
            "goal": goal,
            "description": description or "",
            "procedure_kind": procedure_kind,
            "task_family": task_family,
            "steps_json": [{"action": step} for step in (steps or [])],
            "trigger_conditions": trigger_conditions or [],
            "preconditions": preconditions or [],
            "tools_json": tools or [],
            "failure_modes_json": failure_modes or [],
            "rollback_steps_json": rollback_steps or [],
            "success_criteria_json": success_criteria or [],
            "expected_outcomes": expected_outcomes or [],
            "applicability_scope": scope,
            "status": status,
        }
        result = procedural.create_procedure(
            db,
            agent_id=agent_id,
            payload=payload,
            category=category,
            scope=scope,
            confidence=confidence,
        )
        db.commit()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_procedure_get(procedure_id: int, **_kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        return {"ok": True, **procedural.get_procedure(db, procedure_id, include_sources=True)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_procedure_list(status: str = "all", scope: str | None = None, limit: int = 50, **_kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        items = procedural.list_procedures(db, status=status, scope=scope, limit=limit)
        return {"ok": True, "procedures": items, "count": len(items)}
    finally:
        db.close()


def tool_procedure_search(query: str, limit: int = 10, scope: str | None = None, status: str = "all", debug: bool = False, **_kw: Any) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required"}
    db = _db()
    try:
        return procedural.search_procedures(db, query, limit=limit, scope=scope, status=status, debug=debug)
    finally:
        db.close()


def tool_procedure_update(procedure_id: int, **changes: Any) -> dict[str, Any]:
    db = _db()
    try:
        normalized = dict(changes)
        if normalized.get("steps") is not None:
            normalized["steps_json"] = [{"action": step} for step in normalized.pop("steps") or []]
        if normalized.get("tools") is not None:
            normalized["tools_json"] = normalized.pop("tools")
        if normalized.get("trigger_conditions") is not None:
            normalized["trigger_conditions"] = normalized["trigger_conditions"]
        result = procedural.update_procedure(db, procedure_id, normalized)
        db.commit()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_procedure_feedback(
    procedure_id: int,
    agent_id: str = "mcp-client",
    success: bool = True,
    usefulness_score: float | None = None,
    outcome_summary: str | None = None,
    errors_seen: str | None = None,
    validated: bool = False,
    task_signature: str | None = None,
    input_summary: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    db = _db()
    try:
        result = procedural.record_feedback(
            db,
            procedure_id=procedure_id,
            agent_id=agent_id,
            success=success,
            usefulness_score=usefulness_score,
            outcome_summary=outcome_summary,
            errors_seen=errors_seen,
            validated=validated,
            task_signature=task_signature,
            input_summary=input_summary,
        )
        db.commit()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_procedure_backfill(agent_id: str = "mcp-client", scope: str | None = None, limit: int = 100, dry_run: bool = False, **_kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        result = procedural.backfill_procedures(
            db,
            agent_id=agent_id,
            scope=scope,
            limit=limit,
            dry_run=dry_run,
        )
        if not dry_run:
            db.commit()
        return result
    finally:
        db.close()


def tool_procedure_stats(**_kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        return procedural.procedure_stats(db)
    finally:
        db.close()


TOOLS = [
    Tool(
        name="procedure_add",
        description="Create a canonical structured procedure with ordered steps and provenance.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "goal": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "procedure_kind": {"type": "string"},
                "task_family": {"type": "string"},
                "scope": {"type": "string", "default": "global"},
                "category": {"type": "string", "default": "convention"},
                "confidence": {"type": "number", "default": 0.9},
                "steps": {"type": "array", "items": {"type": "string"}},
                "trigger_conditions": {"type": "array", "items": {"type": "string"}},
                "preconditions": {"type": "array", "items": {"type": "string"}},
                "tools": {"type": "array", "items": {"type": "string"}},
                "failure_modes": {"type": "array", "items": {"type": "string"}},
                "rollback_steps": {"type": "array", "items": {"type": "string"}},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "expected_outcomes": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "default": "active"},
            },
            "required": ["goal"],
        },
    ),
    Tool(
        name="procedure_get",
        description="Get a procedure by id.",
        inputSchema={"type": "object", "properties": {"procedure_id": {"type": "integer"}}, "required": ["procedure_id"]},
    ),
    Tool(
        name="procedure_list",
        description="List procedures with optional scope/status filters.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "all"},
                "scope": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="procedure_search",
        description="Search structured procedural memories.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "scope": {"type": "string"},
                "status": {"type": "string", "default": "all"},
                "debug": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="procedure_update",
        description="Update a procedure.",
        inputSchema={
            "type": "object",
            "properties": {
                "procedure_id": {"type": "integer"},
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "description": {"type": "string"},
                "procedure_kind": {"type": "string"},
                "task_family": {"type": "string"},
                "status": {"type": "string"},
                "scope": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
                "tools": {"type": "array", "items": {"type": "string"}},
                "trigger_conditions": {"type": "array", "items": {"type": "string"}},
                "preconditions": {"type": "array", "items": {"type": "string"}},
                "failure_modes_json": {"type": "array", "items": {"type": "string"}},
                "rollback_steps_json": {"type": "array", "items": {"type": "string"}},
                "success_criteria_json": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["procedure_id"],
        },
    ),
    Tool(
        name="procedure_feedback",
        description="Record procedural execution feedback and validation outcome.",
        inputSchema={
            "type": "object",
            "properties": {
                "procedure_id": {"type": "integer"},
                "agent_id": {"type": "string"},
                "success": {"type": "boolean", "default": True},
                "usefulness_score": {"type": "number"},
                "outcome_summary": {"type": "string"},
                "errors_seen": {"type": "string"},
                "validated": {"type": "boolean", "default": False},
                "task_signature": {"type": "string"},
                "input_summary": {"type": "string"},
            },
            "required": ["procedure_id"],
        },
    ),
    Tool(
        name="procedure_backfill",
        description="Backfill or synthesize procedures from existing memories, events, and decisions.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "scope": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    ),
    Tool(
        name="procedure_stats",
        description="Show procedure counts and candidate promotion stats.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


DISPATCH = {
    "procedure_add": tool_procedure_add,
    "procedure_get": tool_procedure_get,
    "procedure_list": tool_procedure_list,
    "procedure_search": tool_procedure_search,
    "procedure_update": tool_procedure_update,
    "procedure_feedback": tool_procedure_feedback,
    "procedure_backfill": tool_procedure_backfill,
    "procedure_stats": tool_procedure_stats,
}

