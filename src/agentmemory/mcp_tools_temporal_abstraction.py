"""brainctl MCP tools — temporal abstraction hierarchy (issue #20).

The biological brain operates at multiple time scales: seconds (events), hours
(sessions), days (projects), months (strategic arcs). This module provides
zoom-level navigation across that hierarchy.

temporal_level values on memories:
  moment   — raw episodic event (default)
  session  — 2–8h cluster summary
  day      — daily summary of session summaries
  week     — weekly summary of day summaries
  month    — monthly summary
  quarter  — quarterly strategic arc

abstract_summarize: creates a summary memory at the requested level by
    clustering constituent memories and extracting salient content (extractive,
    no LLM required — LLM-based synthesis can be layered on top later).
zoom_out: given a memory, return the hierarchy *above* it (parent summaries).
zoom_in:  given a summary memory, return its constituent memories.
temporal_map: show the full temporal hierarchy tree for a project or agent.
"""
from __future__ import annotations

import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

_TEMPORAL_LEVELS = ("moment", "session", "day", "week", "month", "quarter")
_LEVEL_WINDOW = {
    "session": timedelta(hours=8),
    "day": timedelta(hours=24),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "quarter": timedelta(days=91),
}
_CHILD_LEVEL = {
    "session": "moment",
    "day": "session",
    "week": "day",
    "month": "week",
    "quarter": "month",
}

_now = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_temporal_level_col(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "temporal_level" not in cols:
        conn.execute(
            "ALTER TABLE memories ADD COLUMN temporal_level TEXT NOT NULL DEFAULT 'moment' "
            "CHECK(temporal_level IN ('moment','session','day','week','month','quarter'))"
        )
        conn.commit()


def _extract_summary(memories: list[dict], max_chars: int = 400) -> str:
    """Extractive summary: first sentence of each unique content block, up to max_chars."""
    seen: set[str] = set()
    parts: list[str] = []
    total = 0
    for m in sorted(memories, key=lambda x: x.get("confidence", 0.0), reverse=True):
        text = (m.get("content") or "").strip()
        first = text.split(".")[0].strip()
        if not first or first in seen:
            continue
        seen.add(first)
        parts.append(first)
        total += len(first)
        if total >= max_chars:
            break
    return "; ".join(parts) if parts else "No content"


# ---------------------------------------------------------------------------
# abstract_summarize
# ---------------------------------------------------------------------------

def tool_abstract_summarize(
    agent_id: str = "mcp-client",
    level: str = "session",
    anchor_time: str | None = None,
    project: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    dry_run: bool = False,
    **kw,
) -> dict:
    """Create a summary memory at the requested temporal level.

    Clusters constituent memories within the time window for that level,
    then writes a new memory with temporal_level=<level> and memory_type='semantic'.
    Use anchor_time (ISO datetime) to pick which window to summarize; defaults
    to the most recent window with activity.
    """
    if level not in _LEVEL_WINDOW:
        return {"ok": False, "error": f"level must be one of {list(_LEVEL_WINDOW)}"}

    db = _db()
    _ensure_temporal_level_col(db)
    try:
        window = _LEVEL_WINDOW[level]
        child_level = _CHILD_LEVEL[level]

        if anchor_time:
            try:
                anchor_dt = datetime.fromisoformat(anchor_time).replace(tzinfo=timezone.utc)
            except ValueError:
                return {"ok": False, "error": f"anchor_time invalid ISO datetime: {anchor_time}"}
        else:
            # Find the most recent child-level memory to anchor on
            q = "SELECT created_at FROM memories WHERE retired_at IS NULL AND agent_id = ? AND temporal_level = ?"
            params = [agent_id, child_level]
            if project:
                q += " AND content LIKE ?"
                params.append(f"%{project}%")
            q += " ORDER BY created_at DESC LIMIT 1"
            row = db.execute(q, params).fetchone()
            if not row:
                return {"ok": False, "error": f"No {child_level}-level memories found to summarize"}
            anchor_dt = datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)

        window_start = (anchor_dt - window).strftime("%Y-%m-%dT%H:%M:%S")
        window_end = anchor_dt.strftime("%Y-%m-%dT%H:%M:%S")

        # Fetch child memories in the window
        conditions = [
            "retired_at IS NULL",
            "agent_id = ?",
            "temporal_level = ?",
            "created_at BETWEEN ? AND ?",
        ]
        params = [agent_id, child_level, window_start, window_end]
        if project:
            conditions.append("content LIKE ?")
            params.append(f"%{project}%")
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if category:
            conditions.append("category = ?")
            params.append(category)

        rows = db.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(conditions)} ORDER BY created_at ASC",
            params,
        ).fetchall()
        children = [dict(r) for r in rows]

        if not children:
            return {"ok": False, "error": f"No {child_level}-level memories in window {window_start}–{window_end}"}

        summary_text = _extract_summary(children)
        derived_from = [c["id"] for c in children]
        top_categories = Counter(c.get("category") for c in children if c.get("category"))
        summary_category = top_categories.most_common(1)[0][0] if top_categories else "convention"

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "level": level,
                "window_start": window_start,
                "window_end": window_end,
                "constituent_count": len(children),
                "summary_preview": summary_text[:200],
                "derived_from_ids": derived_from,
            }

        # Write the summary memory
        now = _now()
        db.execute(
            "INSERT INTO memories (agent_id, category, content, confidence, scope, "
            "memory_type, temporal_level, created_at) VALUES (?, ?, ?, ?, ?, 'semantic', ?, ?)",
            (agent_id, summary_category, summary_text,
             min(1.0, sum(c.get("confidence", 0.5) for c in children) / len(children) + 0.1),
             scope or "global", level, now),
        )
        db.commit()
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "ok": True,
            "memory_id": new_id,
            "level": level,
            "window_start": window_start,
            "window_end": window_end,
            "constituent_count": len(children),
            "derived_from_ids": derived_from,
            "summary": summary_text,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# zoom_out
# ---------------------------------------------------------------------------

def tool_zoom_out(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    **kw,
) -> dict:
    """Given a memory, return all parent summaries in the hierarchy above it.

    Walks from the memory's created_at timestamp upward through session→day→week→month→quarter,
    returning the nearest summary at each level that covers the memory's timepoint.
    """
    if memory_id is None:
        return {"ok": False, "error": "memory_id required"}
    db = _db()
    _ensure_temporal_level_col(db)
    try:
        row = db.execute(
            "SELECT id, content, category, created_at, temporal_level, agent_id "
            "FROM memories WHERE id = ? AND retired_at IS NULL",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"memory {memory_id} not found"}
        mem = dict(row)
        created_at = mem["created_at"]

        hierarchy = []
        current_level_idx = _TEMPORAL_LEVELS.index(mem["temporal_level"])

        for level in _TEMPORAL_LEVELS[current_level_idx + 1:]:
            if level not in _LEVEL_WINDOW:
                continue
            window = _LEVEL_WINDOW[level]
            try:
                mem_dt = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
            except ValueError:
                break
            window_start = (mem_dt - window).strftime("%Y-%m-%dT%H:%M:%S")
            window_end = (mem_dt + window).strftime("%Y-%m-%dT%H:%M:%S")
            parent = db.execute(
                "SELECT id, content, category, created_at, temporal_level, confidence "
                "FROM memories WHERE temporal_level = ? AND agent_id = ? "
                "AND created_at BETWEEN ? AND ? AND retired_at IS NULL "
                "ORDER BY ABS(julianday(created_at) - julianday(?)) ASC LIMIT 1",
                (level, mem.get("agent_id", agent_id), window_start, window_end, created_at),
            ).fetchone()
            if parent:
                hierarchy.append(dict(parent))

        return {
            "ok": True,
            "memory_id": memory_id,
            "current_level": mem["temporal_level"],
            "hierarchy_above": hierarchy,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# zoom_in
# ---------------------------------------------------------------------------

def tool_zoom_in(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    limit: int = 20,
    **kw,
) -> dict:
    """Given a summary memory, return constituent memories at the next level down.

    Since we don't store explicit parent-child links, finds memories at the
    child level whose created_at falls within the summary's temporal window.
    """
    if memory_id is None:
        return {"ok": False, "error": "memory_id required"}
    db = _db()
    _ensure_temporal_level_col(db)
    try:
        row = db.execute(
            "SELECT id, content, category, created_at, temporal_level, agent_id "
            "FROM memories WHERE id = ? AND retired_at IS NULL",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"memory {memory_id} not found"}
        mem = dict(row)
        level = mem["temporal_level"]
        if level == "moment":
            return {"ok": True, "memory_id": memory_id, "level": level,
                    "children": [], "note": "moment is the finest level — no children"}
        if level not in _LEVEL_WINDOW:
            return {"ok": False, "error": f"Unknown level: {level}"}

        child_level = _CHILD_LEVEL[level]
        window = _LEVEL_WINDOW[level]
        try:
            mem_dt = datetime.fromisoformat(mem["created_at"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return {"ok": False, "error": "Invalid created_at on summary memory"}
        window_start = (mem_dt - window).strftime("%Y-%m-%dT%H:%M:%S")
        window_end = mem_dt.strftime("%Y-%m-%dT%H:%M:%S")

        children = db.execute(
            "SELECT id, content, category, created_at, temporal_level, confidence "
            "FROM memories WHERE temporal_level = ? AND agent_id = ? "
            "AND created_at BETWEEN ? AND ? AND retired_at IS NULL "
            "ORDER BY created_at ASC LIMIT ?",
            (child_level, mem.get("agent_id", agent_id), window_start, window_end, limit),
        ).fetchall()
        return {
            "ok": True,
            "memory_id": memory_id,
            "level": level,
            "child_level": child_level,
            "window_start": window_start,
            "window_end": window_end,
            "children": [dict(c) for c in children],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# temporal_map
# ---------------------------------------------------------------------------

def tool_temporal_map(
    agent_id: str = "mcp-client",
    project: str | None = None,
    scope: str | None = None,
    **kw,
) -> dict:
    """Show a count summary of the temporal hierarchy for an agent or project.

    Returns the number of memories at each temporal_level for the given agent/scope,
    giving a bird's-eye view of how much content exists at each zoom level.
    """
    db = _db()
    _ensure_temporal_level_col(db)
    try:
        conditions = ["retired_at IS NULL", "agent_id = ?"]
        params: list = [agent_id]
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if project:
            conditions.append("content LIKE ?")
            params.append(f"%{project}%")

        rows = db.execute(
            f"SELECT temporal_level, COUNT(*) as count, "
            f"MIN(created_at) as earliest, MAX(created_at) as latest "
            f"FROM memories WHERE {' AND '.join(conditions)} "
            f"GROUP BY temporal_level",
            params,
        ).fetchall()

        level_map = {r["temporal_level"]: dict(r) for r in rows}
        # Fill in zeros for missing levels
        full_map = []
        for level in _TEMPORAL_LEVELS:
            if level in level_map:
                full_map.append(level_map[level])
            else:
                full_map.append({"temporal_level": level, "count": 0,
                                  "earliest": None, "latest": None})

        total = sum(r["count"] for r in full_map)
        return {
            "ok": True,
            "agent_id": agent_id,
            "total_memories": total,
            "levels": full_map,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TOOLS list and DISPATCH
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="abstract_summarize",
        description=(
            "Create a summary memory at a higher temporal abstraction level (session/day/week/month/quarter). "
            "Clusters constituent memories within the time window and extracts salient content. "
            "Use anchor_time to pick which window to summarize; defaults to the most recent active window. "
            "Use dry_run=true to preview without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["session", "day", "week", "month", "quarter"],
                    "description": "Abstraction level to produce",
                },
                "anchor_time": {"type": "string", "description": "ISO datetime to anchor the window (defaults to most recent activity)"},
                "project": {"type": "string", "description": "Filter constituent memories to those containing this project name"},
                "scope": {"type": "string"},
                "category": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["level"],
        },
    ),
    Tool(
        name="zoom_out",
        description=(
            "Given a memory_id, return the temporal hierarchy above it — the nearest session, day, week, "
            "month, and quarter summaries that cover this memory's timepoint."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="zoom_in",
        description=(
            "Given a summary memory, return its constituent memories at the next level down. "
            "E.g., zoom_in on a week summary returns its day summaries; zoom_in on a day returns sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="temporal_map",
        description=(
            "Show a count breakdown of memories at each temporal level (moment→quarter) for an agent. "
            "Gives a bird's-eye view of how much content exists at each zoom level. "
            "Filter by scope or project name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
            },
        },
    ),
]

DISPATCH: dict = {
    "abstract_summarize": lambda **kw: tool_abstract_summarize(**kw),
    "zoom_out": lambda **kw: tool_zoom_out(**kw),
    "zoom_in": lambda **kw: tool_zoom_in(**kw),
    "temporal_map": lambda **kw: tool_temporal_map(**kw),
}
