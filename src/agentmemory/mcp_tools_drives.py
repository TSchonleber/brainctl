"""brainctl MCP tools — homeostatic drives (hypothalamus + PAG).

Five canonical drives seeded by migration 062:
  consolidation_debt, staleness, belief_coverage, pii_pressure,
  entity_freshness. Each has set_point + sampler. Drives bias —
  never directly override — downstream gating.

`pii_pressure` is a SAFETY drive (PAG-style): hard threshold above
which exports/marketplace/public-scope writes should refuse.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH = get_db_path()


def _db(): return open_db(str(DB_PATH))
def _rows(r: Iterable[sqlite3.Row]): return [dict(x) for x in r]


def _sample_one(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """Sample a single drive by executing its sample_query and computing
    error + magnitude vs set_point. Built-in samplers override the
    sql query for the canonical drives."""
    defn = conn.execute(
        "SELECT name, set_point, hard_threshold, sample_query, is_safety_drive "
        "FROM drive_definitions WHERE name = ?",
        (name,),
    ).fetchone()
    if not defn:
        return None
    _name, set_point, hard_th, sql, is_safety = defn

    # Built-in samplers for the canonical drives
    try:
        if name == "consolidation_debt":
            v = conn.execute(
                "SELECT CAST(COUNT(*) AS REAL) / (SELECT MAX(1, COUNT(*)) FROM memories) "
                "FROM memories WHERE replay_priority < 1.0 AND "
                "(last_recalled_at IS NULL OR last_recalled_at < datetime('now','-24 hours'))"
            ).fetchone()[0]
        elif name == "staleness":
            row = conn.execute(
                "SELECT (julianday('now') - julianday(MAX(created_at))) * 24.0 "
                "FROM events WHERE created_at > datetime('now','-7 days')"
            ).fetchone()
            v = float(row[0]) if row and row[0] is not None else 0.0
        elif name == "belief_coverage":
            row = conn.execute(
                "SELECT 1.0 - CAST(SUM(CASE WHEN compiled_truth IS NOT NULL THEN 1 ELSE 0 END) AS REAL) "
                "/ MAX(1, COUNT(*)) FROM entities"
            ).fetchone()
            v = float(row[0]) if row and row[0] is not None else 0.0
        elif name == "pii_pressure":
            try:
                v = float(conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE category IN ('identity','user') "
                    "AND created_at > datetime('now','-1 hour')"
                ).fetchone()[0])
            except sqlite3.OperationalError:
                v = 0.0
        elif name == "entity_freshness":
            try:
                row = conn.execute(
                    "SELECT 1.0 - CAST(COUNT(DISTINCT e.id) AS REAL) / MAX(1, "
                    "(SELECT COUNT(*) FROM entities)) "
                    "FROM entities e WHERE EXISTS ("
                    "  SELECT 1 FROM events ev WHERE ev.event_type='observation' "
                    "  AND ev.created_at > datetime('now','-14 days')"
                    ")"
                ).fetchone()
                v = float(row[0]) if row and row[0] is not None else 0.0
            except sqlite3.OperationalError:
                v = 0.0
        else:
            v = float(conn.execute(sql).fetchone()[0])
    except Exception:
        v = 0.0

    error = float(v) - float(set_point)
    magnitude = min(1.0, abs(error) / max(0.001, abs(float(set_point) - 1.0) + 0.1))
    in_hard = 1 if (hard_th is not None and v >= float(hard_th)) else 0

    conn.execute(
        "INSERT INTO drive_current_state (name, current_level, error, magnitude, in_hard_state, sampled_at) "
        "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now')) "
        "ON CONFLICT(name) DO UPDATE SET "
        "current_level=excluded.current_level, error=excluded.error, "
        "magnitude=excluded.magnitude, in_hard_state=excluded.in_hard_state, "
        "sampled_at=excluded.sampled_at",
        (name, float(v), error, magnitude, in_hard),
    )
    conn.execute(
        "INSERT INTO drive_history (name, current_level, error, magnitude) VALUES (?, ?, ?, ?)",
        (name, float(v), error, magnitude),
    )
    return {"name": name, "current_level": float(v), "error": error,
            "magnitude": magnitude, "in_hard_state": bool(in_hard),
            "is_safety_drive": bool(is_safety)}


def tool_drive_sample(name: str | None = None, **kw: Any) -> dict[str, Any]:
    """Sample one drive or all of them. Writes drive_current_state + drive_history."""
    db = _db()
    try:
        if name:
            r = _sample_one(db, name)
            db.commit()
            return {"ok": True, "drive": r} if r else {"ok": False, "error": f"unknown drive {name}"}
        names = [r[0] for r in db.execute("SELECT name FROM drive_definitions").fetchall()]
        results = []
        for n in names:
            r = _sample_one(db, n)
            if r:
                results.append(r)
        db.commit()
        return {"ok": True, "drives": results}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_drive_status(name: str | None = None, include_history: bool = False, **kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        if name:
            row = db.execute(
                "SELECT d.name, d.set_point, d.hard_threshold, d.is_safety_drive, "
                "       d.recommended_mode, s.current_level, s.error, s.magnitude, "
                "       s.in_hard_state, s.sampled_at "
                "FROM drive_definitions d LEFT JOIN drive_current_state s ON s.name=d.name "
                "WHERE d.name=?", (name,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": f"unknown drive {name}"}
            out = {"ok": True, "drive": dict(row)}
            if include_history:
                hist = db.execute(
                    "SELECT current_level, error, magnitude, sampled_at FROM drive_history "
                    "WHERE name=? ORDER BY sampled_at DESC LIMIT 50", (name,),
                ).fetchall()
                out["history"] = _rows(hist)
            return out
        rows = db.execute(
            "SELECT d.name, d.set_point, d.is_safety_drive, d.recommended_mode, "
            "       s.current_level, s.error, s.magnitude, s.in_hard_state, s.sampled_at "
            "FROM drive_definitions d LEFT JOIN drive_current_state s ON s.name=d.name "
            "ORDER BY s.magnitude DESC NULLS LAST",
        ).fetchall()
        active_hard = [dict(r) for r in rows if r[7] == 1]
        return {"ok": True, "drives": _rows(rows), "active_hard_states": active_hard}
    finally:
        db.close()


def tool_drive_recommend_mode(agent_id: str | None = None, **kw: Any) -> dict[str, Any]:
    """Fold drives into a single mode recommendation. Highest-magnitude
    drive with a non-null recommended_mode wins. Returns hard_states too —
    callers should consult those for safety gating."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT d.name, d.recommended_mode, s.magnitude, s.in_hard_state, d.is_safety_drive "
            "FROM drive_definitions d JOIN drive_current_state s ON s.name=d.name "
            "ORDER BY s.magnitude DESC",
        ).fetchall()
        recommend, dominant = None, None
        for name, mode, mag, _hard, _safety in rows:
            if mode is not None:
                recommend = mode; dominant = name
                break
        hard_states = [
            {"name": r[0], "recommended_mode": r[1], "magnitude": float(r[2] or 0),
             "is_safety_drive": bool(r[4])}
            for r in rows if r[3] == 1
        ]
        return {"ok": True, "recommended_mode": recommend, "dominant_drive": dominant,
                "hard_states": hard_states}
    finally:
        db.close()


def tool_drive_register(
    name: str, description: str, set_point: float,
    sample_query: str = "SELECT 0.0",
    hard_threshold: float | None = None,
    recommended_mode: str | None = None,
    is_safety_drive: bool = False,
    **kw: Any,
) -> dict[str, Any]:
    """Register a new drive definition. Idempotent."""
    db = _db()
    try:
        db.execute(
            "INSERT INTO drive_definitions (name, description, set_point, hard_threshold, "
            "sample_query, recommended_mode, is_safety_drive) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
            "set_point=excluded.set_point, hard_threshold=excluded.hard_threshold, "
            "sample_query=excluded.sample_query, recommended_mode=excluded.recommended_mode, "
            "is_safety_drive=excluded.is_safety_drive",
            (name, description, float(set_point), hard_threshold, sample_query,
             recommended_mode, 1 if is_safety_drive else 0),
        )
        db.commit()
        return {"ok": True, "name": name}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(name="drive_sample",
         description="Sample one drive (or all of them). Writes drive_current_state + history.",
         inputSchema={"type": "object", "properties": {"name": {"type": "string"}}}),
    Tool(name="drive_status",
         description="Inspect drive current state, set-points, hard states, optional history.",
         inputSchema={"type": "object", "properties": {
             "name": {"type": "string"},
             "include_history": {"type": "boolean", "default": False},
         }}),
    Tool(name="drive_recommend_mode",
         description="Fold drives into a single neuromodulation mode recommendation + "
                     "list of active hard states (for safety gating).",
         inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}}),
    Tool(name="drive_register",
         description="Register/update a drive definition. Idempotent.",
         inputSchema={"type": "object", "properties": {
             "name": {"type": "string"}, "description": {"type": "string"},
             "set_point": {"type": "number"},
             "sample_query": {"type": "string", "default": "SELECT 0.0"},
             "hard_threshold": {"type": "number"},
             "recommended_mode": {"type": "string"},
             "is_safety_drive": {"type": "boolean", "default": False},
         }, "required": ["name", "description", "set_point"]}),
]
_DRIVE_TOOLS = {"drive_sample": tool_drive_sample, "drive_status": tool_drive_status,
                "drive_recommend_mode": tool_drive_recommend_mode,
                "drive_register": tool_drive_register}
DISPATCH = {n: (lambda _f=f, **kw: _f(**kw)) for n, f in _DRIVE_TOOLS.items()}


def register_tools(): return TOOLS, DISPATCH
