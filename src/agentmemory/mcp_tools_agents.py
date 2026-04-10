"""brainctl MCP tools — agent management, tasks & context."""
from __future__ import annotations
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# Beliefs older than this (hours) are considered stale
_STALE_HOURS = 24

# FTS5 special characters that cause sqlite3.OperationalError when unescaped.
_FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^]')


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _log_access(conn, agent_id, action, target_table=None, target_id=None,
                query=None, result_count=None):
    conn.execute(
        "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count) "
        "VALUES (?,?,?,?,?,?)",
        (agent_id, action, target_table, target_id, query, result_count)
    )


def _safe_fts(query: str) -> str:
    """Sanitize query for FTS5 — strip special chars, return '' if nothing remains."""
    cleaned = _FTS5_SPECIAL.sub(" ", query or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _tom_tables_exist(db) -> bool:
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    return "agent_beliefs" in tables


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_agent_register(agent_id: str = "mcp-client", *, id: str, name: str,
                        type: str = "mcp", adapter_info: str = None, **kw) -> dict:
    """Register (or update) an agent in the agents table."""
    if not id or not id.strip():
        return {"ok": False, "error": "id must not be empty"}
    if not name or not name.strip():
        return {"ok": False, "error": "name must not be empty"}
    db = _db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO agents "
            "(id, display_name, agent_type, adapter_info, status, last_seen_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
            "strftime('%Y-%m-%dT%H:%M:%S', 'now'))",
            (id.strip(), name.strip(), type, adapter_info)
        )
        db.commit()
        return {"ok": True, "agent_id": id.strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_agent_list(agent_id: str = "mcp-client", **kw) -> dict:
    """List all registered agents ordered by creation time."""
    db = _db()
    try:
        rows = db.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        return {"ok": True, "agents": _rows_to_list(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_agent_ping(agent_id: str = "mcp-client", *, agent: str, **kw) -> dict:
    """Update last_seen_at for the given agent (heartbeat / liveness ping)."""
    if not agent or not agent.strip():
        return {"ok": False, "error": "agent must not be empty"}
    db = _db()
    try:
        db.execute(
            "UPDATE agents SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
            (agent.strip(),)
        )
        db.commit()
        return {"ok": True, "agent": agent.strip(), "pinged_at": _now()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_agent_model(agent_id: str = "mcp-client", *, agent_id_target: str, **kw) -> dict:
    """Return full mental model for an agent: BDI state, beliefs, conflicts, knowledge gaps.

    Requires Theory of Mind tables (migration 012_theory_of_mind.sql).
    """
    db = _db()
    try:
        if not _tom_tables_exist(db):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. "
                         "Apply migration 012_theory_of_mind.sql.",
            }

        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        agent_row = db.execute(
            "SELECT id, display_name, status FROM agents WHERE id=?",
            (agent_id_target,)
        ).fetchone()
        if not agent_row:
            return {"ok": False, "error": f"Agent '{agent_id_target}' not found."}

        bdi = db.execute(
            "SELECT * FROM agent_bdi_state WHERE agent_id=?", (agent_id_target,)
        ).fetchone()

        beliefs = db.execute(
            "SELECT topic, belief_content, confidence, is_assumption, last_updated_at "
            "FROM agent_beliefs WHERE agent_id=? AND invalidated_at IS NULL "
            "ORDER BY last_updated_at DESC",
            (agent_id_target,)
        ).fetchall()

        conflicts = db.execute(
            "SELECT id, topic, conflict_type, severity, belief_a, belief_b, "
            "agent_b_id, requires_supervisor_intervention "
            "FROM belief_conflicts "
            "WHERE (agent_a_id=? OR agent_b_id=?) AND resolved_at IS NULL "
            "ORDER BY severity DESC",
            (agent_id_target, agent_id_target)
        ).fetchall()

        perspective = db.execute(
            "SELECT observer_agent_id, topic, knowledge_gap, confusion_risk "
            "FROM agent_perspective_models "
            "WHERE subject_agent_id=? AND knowledge_gap IS NOT NULL "
            "ORDER BY confusion_risk DESC LIMIT 10",
            (agent_id_target,)
        ).fetchall()

        return {
            "ok": True,
            "agent_id": agent_id_target,
            "display_name": agent_row["display_name"],
            "bdi_state": _row_to_dict(bdi),
            "active_beliefs": _rows_to_list(beliefs),
            "open_conflicts": _rows_to_list(conflicts),
            "knowledge_gaps": _rows_to_list(perspective),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_task_add(agent_id: str = "mcp-client", *, title: str,
                  description: str = None, status: str = "pending",
                  priority: str = "medium", assign: str = None,
                  project: str = None, external_id: str = None,
                  external_system: str = None, metadata: str = None, **kw) -> dict:
    """Add a new task to the tasks table."""
    if not title or not title.strip():
        return {"ok": False, "error": "title must not be empty"}
    db = _db()
    try:
        cursor = db.execute(
            "INSERT INTO tasks (external_id, external_system, title, description, status, "
            "priority, assigned_agent_id, project, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (external_id, external_system, title.strip(), description,
             status or "pending", priority or "medium",
             assign, project, metadata)
        )
        task_id = cursor.lastrowid
        _log_access(db, agent_id, "write", "tasks", task_id)
        db.commit()
        return {"ok": True, "task_id": task_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_task_update(agent_id: str = "mcp-client", *, id: int,
                     status: str = None, assign: str = None,
                     priority: str = None, no_claim: bool = False, **kw) -> dict:
    """Update status, assignment, or priority on an existing task."""
    if not id:
        return {"ok": False, "error": "id must be provided"}
    sets = []
    params = []
    if status:
        sets.append("status = ?")
        params.append(status)
        if status == "completed":
            sets.append("completed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        if status == "in_progress" and not no_claim:
            sets.append("claimed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
            sets.append("claimed_by = ?")
            params.append(agent_id)
    if assign:
        sets.append("assigned_agent_id = ?")
        params.append(assign)
    if priority:
        sets.append("priority = ?")
        params.append(priority)
    if not sets:
        return {"ok": False, "error": "No fields to update. Provide status, assign, or priority."}
    sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
    params.append(id)
    db = _db()
    try:
        db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
        _log_access(db, agent_id, "write", "tasks", id)
        db.commit()
        return {"ok": True, "task_id": id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_task_list(agent_id: str = "mcp-client", *, status: str = None,
                   agent: str = None, project: str = None, limit: int = None, **kw) -> dict:
    """List tasks with optional filters on status, assigned agent, and project."""
    sql = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if agent:
        sql += " AND assigned_agent_id = ?"
        params.append(agent)
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += (" ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END, created_at")
    if limit:
        sql += f" LIMIT {int(limit)}"
    db = _db()
    try:
        rows = db.execute(sql, params).fetchall()
        return {"ok": True, "tasks": _rows_to_list(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_context_add(agent_id: str = "mcp-client", *, content: str,
                     source_type: str = "text", source_ref: str = None,
                     chunk: int = 0, summary: str = None,
                     project: str = None, tags: str = None, tokens: int = None, **kw) -> dict:
    """Add a context chunk to the context table."""
    if not content or not content.strip():
        return {"ok": False, "error": "content must not be empty"}
    tags_json = json.dumps(tags.split(",")) if tags else None
    db = _db()
    try:
        cursor = db.execute(
            "INSERT INTO context (source_type, source_ref, chunk_index, content, summary, "
            "project, tags, token_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_type, source_ref or "", chunk or 0, content.strip(),
             summary, project, tags_json, tokens)
        )
        ctx_id = cursor.lastrowid
        _log_access(db, agent_id, "write", "context", ctx_id)
        db.commit()
        return {"ok": True, "context_id": ctx_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_context_search(agent_id: str = "mcp-client", *, query: str,
                        limit: int = 20, **kw) -> dict:
    """Search context chunks using FTS5 full-text search."""
    if not query or not query.strip():
        return {"ok": False, "error": "query must not be empty"}
    fts_query = _safe_fts(query)
    db = _db()
    try:
        if not fts_query:
            results = []
        else:
            rows = db.execute(
                "SELECT c.* FROM context c JOIN context_fts f ON c.id = f.rowid "
                "WHERE context_fts MATCH ? AND c.stale_at IS NULL "
                "ORDER BY rank LIMIT ?",
                (fts_query, limit or 20)
            ).fetchall()
            results = _rows_to_list(rows)
        _log_access(db, agent_id, "search", "context", query=query, result_count=len(results))
        db.commit()
        return {"ok": True, "results": results}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="agent_register",
        description=(
            "Register or update an agent in brain.db. Use this when an agent starts up "
            "or changes its configuration. Agents must be registered before they can "
            "be referenced in tasks, events, or other records."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique agent identifier"},
                "name": {"type": "string", "description": "Human-readable display name"},
                "type": {"type": "string", "description": "Agent type (e.g. 'mcp', 'cli', 'service')", "default": "mcp"},
                "adapter_info": {"type": "string", "description": "Optional adapter/connection metadata"},
            },
            "required": ["id", "name"],
        },
    ),
    Tool(
        name="agent_list",
        description="List all registered agents in brain.db ordered by creation time.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="agent_ping",
        description=(
            "Update the last_seen_at timestamp for an agent. Use as a heartbeat / "
            "liveness signal so the system knows the agent is still active."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID to ping"},
            },
            "required": ["agent"],
        },
    ),
    Tool(
        name="agent_model",
        description=(
            "Return the full Theory-of-Mind mental model for an agent: BDI state summary, "
            "active beliefs, open belief conflicts, and knowledge gaps observed by peers. "
            "Requires Theory of Mind migration (012_theory_of_mind.sql) to have been applied."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id_target": {"type": "string", "description": "Agent ID whose model to retrieve"},
            },
            "required": ["agent_id_target"],
        },
    ),
    Tool(
        name="task_add",
        description=(
            "Add a new task to brain.db. Tasks track work items across agents, with "
            "status, priority, and optional external system references."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title"},
                "description": {"type": "string", "description": "Longer task description"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "cancelled"], "default": "pending"},
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"], "default": "medium"},
                "assign": {"type": "string", "description": "Agent ID to assign the task to"},
                "project": {"type": "string", "description": "Project name for grouping"},
                "external_id": {"type": "string", "description": "ID in an external system (e.g. GitHub issue number)"},
                "external_system": {"type": "string", "description": "Name of the external system (e.g. 'github', 'linear')"},
                "metadata": {"type": "string", "description": "JSON metadata string"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="task_update",
        description=(
            "Update an existing task's status, assignment, or priority. "
            "When status is set to 'in_progress', the calling agent is automatically "
            "recorded as the claimer unless no_claim=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Task ID to update"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "cancelled"]},
                "assign": {"type": "string", "description": "New assigned agent ID"},
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "no_claim": {"type": "boolean", "description": "If true, do not auto-claim when setting in_progress", "default": False},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="task_list",
        description="List tasks with optional filters. Results are ordered by priority then creation time.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "cancelled"]},
                "agent": {"type": "string", "description": "Filter by assigned agent ID"},
                "project": {"type": "string", "description": "Filter by project name"},
                "limit": {"type": "integer", "description": "Max results to return", "default": 50},
            },
        },
    ),
    Tool(
        name="context_add",
        description=(
            "Store a context chunk (file section, conversation excerpt, etc.) in brain.db. "
            "Context is indexed with FTS5 for fast retrieval via context_search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Text content of the chunk"},
                "source_type": {"type": "string", "description": "Source type (e.g. 'file', 'conversation', 'text')", "default": "text"},
                "source_ref": {"type": "string", "description": "Source reference (e.g. file path or URL)"},
                "chunk": {"type": "integer", "description": "Chunk index within the source", "default": 0},
                "summary": {"type": "string", "description": "Short summary of the chunk"},
                "project": {"type": "string", "description": "Project name for grouping"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
                "tokens": {"type": "integer", "description": "Approximate token count"},
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="context_search",
        description="Search context chunks in brain.db using FTS5 full-text search.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text search query"},
                "limit": {"type": "integer", "description": "Max results to return", "default": 20},
            },
            "required": ["query"],
        },
    ),
]

DISPATCH: dict = {
    "agent_register": tool_agent_register,
    "agent_list": tool_agent_list,
    "agent_ping": tool_agent_ping,
    "agent_model": tool_agent_model,
    "task_add": tool_task_add,
    "task_update": tool_task_update,
    "task_list": tool_task_list,
    "context_add": tool_context_add,
    "context_search": tool_context_search,
}
