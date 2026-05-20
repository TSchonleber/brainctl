"""brainctl MCP tools — superior + inferior colliculi (orienting reflex).

Phase 1 per research-avenues memo Avenue 9. Pre-cortical orienting:
SC (visual / attention orienting) + IC (auditory / cross-modal). Phase
1 = manual orient events. Phase 2 wires into dispatch as early-fire.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_SUB_NUCLEI = {"sc", "ic"}
VALID_PATTERN_KINDS = {
    "novel_entity_shape", "unfamiliar_query_form", "unusual_content_type",
    "sudden_volume_change", "cross_modal_mismatch", "other",
}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("colliculi_state", "colliculi_trigger_patterns", "colliculi_orienting_events"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"colliculi schema missing: {t}. Run `brainctl migrate` (080)."
    return None


def tool_colliculi_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM colliculi_state WHERE id = 1").fetchone()
        patterns = _rows(conn.execute(
            "SELECT * FROM colliculi_trigger_patterns ORDER BY id"
        ).fetchall())
        last_5 = _rows(conn.execute(
            "SELECT * FROM colliculi_orienting_events ORDER BY id DESC LIMIT 5"
        ).fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(strength), 0.0) AS mean_strength,
                   SUM(CASE WHEN sub_nucleus='sc' THEN 1 ELSE 0 END) AS n_sc,
                   SUM(CASE WHEN sub_nucleus='ic' THEN 1 ELSE 0 END) AS n_ic,
                   SUM(aras_drive_fired) AS n_aras_drive_fired
              FROM colliculi_orienting_events
             WHERE oriented_at >= datetime('now', '-1 hour')
            """
        ).fetchone()
    return {
        "ok": True, "state": dict(state) if state else None,
        "patterns": patterns, "last_5_events": last_5,
        "aggregate_1h": dict(agg) if agg else {},
    }


def tool_colliculi_orient(
    sub_nucleus: str,
    pattern_name: str | None = None,
    strength: float | None = None,
    target_description: str | None = None,
    agent_id: str | None = None,
    aras_drive_fired: bool = False,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Record an orienting event. Provide pattern_name (uses its
    default_strength) OR strength explicitly."""
    if sub_nucleus not in VALID_SUB_NUCLEI:
        return {"error": f"invalid sub_nucleus {sub_nucleus!r}; expected sc or ic"}
    if pattern_name is None and strength is None:
        return {"error": "must pass pattern_name OR strength"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        pattern_id: int | None = None
        if pattern_name is not None:
            pat = conn.execute(
                "SELECT id, default_strength, sub_nucleus FROM colliculi_trigger_patterns WHERE name = ?",
                (pattern_name,),
            ).fetchone()
            if not pat:
                return {"error": f"pattern {pattern_name!r} not registered"}
            if pat["sub_nucleus"] != sub_nucleus:
                return {"error": f"pattern {pattern_name!r} belongs to {pat['sub_nucleus']!r}, not {sub_nucleus!r}"}
            pattern_id = int(pat["id"])
            if strength is None:
                strength = float(pat["default_strength"])
        if not 0.0 <= strength <= 1.0:
            return {"error": "strength must be in [0, 1]"}
        cur = conn.execute(
            """
            INSERT INTO colliculi_orienting_events
              (agent_id, sub_nucleus, pattern_id, strength, target_description, aras_drive_fired, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, sub_nucleus, pattern_id, float(strength), target_description,
             1 if aras_drive_fired else 0, notes),
        )
        event_id = cur.lastrowid
        conn.execute(
            """
            UPDATE colliculi_state SET
              total_orienting_events = total_orienting_events + 1,
              last_orient_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """
        )
        conn.commit()
    return {
        "ok": True, "event_id": event_id, "sub_nucleus": sub_nucleus,
        "strength": float(strength), "pattern_id": pattern_id,
    }


def tool_colliculi_register_pattern(
    name: str, sub_nucleus: str, pattern_kind: str,
    default_strength: float = 0.4, description: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if sub_nucleus not in VALID_SUB_NUCLEI:
        return {"error": f"invalid sub_nucleus; expected {sorted(VALID_SUB_NUCLEI)}"}
    if pattern_kind not in VALID_PATTERN_KINDS:
        return {"error": f"invalid pattern_kind; expected {sorted(VALID_PATTERN_KINDS)}"}
    if not 0.0 <= default_strength <= 1.0:
        return {"error": "default_strength must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            """
            INSERT INTO colliculi_trigger_patterns (name, sub_nucleus, pattern_kind, default_strength, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              default_strength = excluded.default_strength,
              description = COALESCE(excluded.description, colliculi_trigger_patterns.description)
            """,
            (name, sub_nucleus, pattern_kind, float(default_strength), description),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM colliculi_trigger_patterns WHERE name = ?", (name,)
        ).fetchone()
    return {"ok": True, "pattern": dict(row) if row else None}


def tool_colliculi_history(
    limit: int = 20, since: str | None = None,
    sub_nucleus: str | None = None, min_strength: float | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    if sub_nucleus is not None and sub_nucleus not in VALID_SUB_NUCLEI:
        return {"error": "invalid sub_nucleus"}
    if min_strength is not None and not 0.0 <= min_strength <= 1.0:
        return {"error": "min_strength must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses, params = [], []
        if since:
            clauses.append("oriented_at >= ?"); params.append(since)
        if sub_nucleus:
            clauses.append("sub_nucleus = ?"); params.append(sub_nucleus)
        if min_strength is not None:
            clauses.append("strength >= ?"); params.append(float(min_strength))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM colliculi_orienting_events {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {"ok": True, "history": _rows(rows)}


TOOLS: list[Tool] = [
    Tool(
        name="colliculi_status",
        description="SC + IC state + pattern catalog + last 5 events + 1h aggregate.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="colliculi_orient",
        description=(
            "Record one orienting event. sub_nucleus ∈ {sc, ic}. Pass pattern_name "
            "(uses its default_strength + validates sub_nucleus match) OR pass strength "
            "explicitly. Set aras_drive_fired=true if a downstream ARAS pulse was fired."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sub_nucleus": {"type": "string", "enum": sorted(VALID_SUB_NUCLEI)},
                "pattern_name": {"type": "string"},
                "strength": {"type": "number"},
                "target_description": {"type": "string"},
                "agent_id": {"type": "string"},
                "aras_drive_fired": {"type": "boolean", "default": False},
                "notes": {"type": "string"},
            },
            "required": ["sub_nucleus"],
        },
    ),
    Tool(
        name="colliculi_register_pattern",
        description="Idempotent UPSERT on colliculi_trigger_patterns. sub_nucleus + pattern_kind validated.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "sub_nucleus": {"type": "string", "enum": sorted(VALID_SUB_NUCLEI)},
                "pattern_kind": {"type": "string", "enum": sorted(VALID_PATTERN_KINDS)},
                "default_strength": {"type": "number", "default": 0.4},
                "description": {"type": "string"},
            },
            "required": ["name", "sub_nucleus", "pattern_kind"],
        },
    ),
    Tool(
        name="colliculi_history",
        description="Paginated orienting-event history. Filters: since, sub_nucleus, min_strength. limit clamped to [1, 200].",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "sub_nucleus": {"type": "string", "enum": sorted(VALID_SUB_NUCLEI)},
                "min_strength": {"type": "number"},
            },
        },
    ),
]


_COLL_TOOLS = {
    "colliculi_status": tool_colliculi_status,
    "colliculi_orient": tool_colliculi_orient,
    "colliculi_register_pattern": tool_colliculi_register_pattern,
    "colliculi_history": tool_colliculi_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _COLL_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
