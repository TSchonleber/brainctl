"""brainctl MCP tools — habenula (lateral habenula, anti-reward).

Phase 1 per docs/proposals/habenula.md. Records negative-RPE /
omission / aversive events as a dedicated channel separate from
bg_td_events. Phase 1 is inspection + writes only; Phase 3 will damp
bg_modulators.tonic_da from accumulated habenula activity.
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

VALID_EVENT_KINDS = {"omission", "aversive", "repeated_failure", "other"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        t for t in ("habenula_triggers", "habenula_firings", "habenula_state")
        if not _table_exists(conn, t)
    ]
    if missing:
        return ("habenula schema missing: " + ", ".join(missing)
                + ". Run `brainctl migrate` (migration 070).")
    return None


def tool_habenula_status(agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM habenula_state WHERE id = 1").fetchone()
        last_firings = _rows(conn.execute(
            """
            SELECT f.id, f.fired_at, f.agent_id, f.event_kind, f.signed_pe,
                   f.notes, t.name AS trigger_name
              FROM habenula_firings f
              LEFT JOIN habenula_triggers t ON t.id = f.trigger_id
             WHERE (? IS NULL OR f.agent_id = ?)
             ORDER BY f.id DESC LIMIT 5
            """, (agent_id, agent_id),
        ).fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(signed_pe), 0.0) AS mean_pe,
                   COALESCE(MIN(signed_pe), 0.0) AS worst_pe,
                   SUM(CASE WHEN event_kind='omission' THEN 1 ELSE 0 END) AS n_omission,
                   SUM(CASE WHEN event_kind='aversive' THEN 1 ELSE 0 END) AS n_aversive,
                   SUM(CASE WHEN event_kind='repeated_failure' THEN 1 ELSE 0 END) AS n_repeated
              FROM habenula_firings
             WHERE fired_at >= datetime('now', '-24 hours')
               AND (? IS NULL OR agent_id = ?)
            """, (agent_id, agent_id),
        ).fetchone()
        trigger_count = conn.execute(
            "SELECT COUNT(*) FROM habenula_triggers"
        ).fetchone()[0]
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_firings": last_firings,
        "aggregate_24h": dict(agg) if agg else {},
        "registered_triggers": trigger_count,
    }


def tool_habenula_fire(
    trigger_name: str | None = None,
    signed_pe: float | None = None,
    event_kind: str | None = None,
    agent_id: str | None = None,
    context_hash: str | None = None,
    source_event_id: int | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Record one habenula firing (negative event).

    Either pass `trigger_name` (uses its default_pe and event_kind)
    OR pass both `signed_pe` and `event_kind` explicitly.
    """
    if trigger_name is None and (signed_pe is None or event_kind is None):
        return {"error": "must pass trigger_name OR (signed_pe + event_kind)"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        trigger_id: int | None = None
        if trigger_name is not None:
            trig = conn.execute(
                "SELECT id, default_pe, event_kind FROM habenula_triggers WHERE name = ?",
                (trigger_name,),
            ).fetchone()
            if not trig:
                return {"error": f"trigger {trigger_name!r} not registered"}
            trigger_id = int(trig["id"])
            if signed_pe is None:
                signed_pe = float(trig["default_pe"])
            if event_kind is None:
                event_kind = trig["event_kind"]
        if event_kind not in VALID_EVENT_KINDS:
            return {"error": f"invalid event_kind {event_kind!r}"}
        if signed_pe > 0.0:
            return {"error": "signed_pe must be <= 0 (habenula codes negative PE)"}
        cur = conn.execute(
            """
            INSERT INTO habenula_firings
              (agent_id, trigger_id, event_kind, signed_pe, context_hash, source_event_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, trigger_id, event_kind, float(signed_pe), context_hash, source_event_id, notes),
        )
        firing_id = cur.lastrowid
        # Update state: EWMA on tonic_activity, instantaneous phasic burst,
        # increment 24h counter (rough — Phase 3 will use a proper sliding window).
        state = conn.execute("SELECT * FROM habenula_state WHERE id = 1").fetchone()
        old_tonic = float(state["tonic_activity"]) if state else 0.0
        magnitude = abs(float(signed_pe))
        new_tonic = max(0.0, min(1.0, 0.9 * old_tonic + 0.1 * magnitude))
        new_phasic = max(0.0, min(1.0, magnitude))
        # Phase 1 suggested damp is purely informational; Phase 3 will read it.
        new_damp = max(0.0, min(0.5, 0.5 * new_tonic + 0.3 * new_phasic))
        conn.execute(
            """
            UPDATE habenula_state SET
              tonic_activity = ?,
              phasic_burst = ?,
              rolling_disappointment_24h = rolling_disappointment_24h + 1,
              last_firing_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              suggested_da_damp = ?,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (new_tonic, new_phasic, new_damp),
        )
        conn.commit()
    return {
        "ok": True, "firing_id": firing_id, "trigger_name": trigger_name,
        "event_kind": event_kind, "signed_pe": float(signed_pe),
        "new_tonic_activity": new_tonic,
        "new_phasic_burst": new_phasic,
        "suggested_da_damp": new_damp,
    }


def tool_habenula_register_trigger(
    name: str, event_kind: str,
    default_pe: float = -0.1, description: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if event_kind not in VALID_EVENT_KINDS:
        return {"error": f"invalid event_kind {event_kind!r}"}
    if default_pe > 0.0:
        return {"error": "default_pe must be <= 0"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            """
            INSERT INTO habenula_triggers (name, event_kind, default_pe, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              event_kind = excluded.event_kind,
              default_pe = excluded.default_pe,
              description = COALESCE(excluded.description, habenula_triggers.description)
            """,
            (name, event_kind, float(default_pe), description),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM habenula_triggers WHERE name = ?", (name,)
        ).fetchone()
    return {"ok": True, "trigger": dict(row) if row else None}


def tool_habenula_history(
    limit: int = 20, since: str | None = None,
    agent_id: str | None = None, event_kind: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses, params = [], []
        if since:
            clauses.append("fired_at >= ?"); params.append(since)
        if agent_id:
            clauses.append("agent_id = ?"); params.append(agent_id)
        if event_kind:
            clauses.append("event_kind = ?"); params.append(event_kind)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT id, fired_at, agent_id, trigger_id, event_kind, signed_pe,
                   context_hash, source_event_id, notes
              FROM habenula_firings
              {where}
             ORDER BY id DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return {"ok": True, "history": _rows(rows)}


def tool_habenula_reset(agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    """Admin-mode reset of habenula state. Use sparingly — clears the
    accumulated disengagement signal. Returns the prior state for audit.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        prior = conn.execute("SELECT * FROM habenula_state WHERE id = 1").fetchone()
        conn.execute(
            """
            UPDATE habenula_state SET
              tonic_activity = 0.0,
              phasic_burst = 0.0,
              rolling_disappointment_24h = 0,
              suggested_da_damp = 0.0,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """
        )
        conn.commit()
    return {"ok": True, "reset_for_agent": agent_id, "prior_state": dict(prior) if prior else None}


TOOLS: list[Tool] = [
    Tool(
        name="habenula_status",
        description="Habenula Phase 1 inspection. Current state + last 5 firings + 24h aggregate.",
        inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}},
    ),
    Tool(
        name="habenula_register_trigger",
        description="Idempotent UPSERT on habenula_triggers. event_kind ∈ {omission, aversive, repeated_failure, other}. default_pe ≤ 0.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "event_kind": {"type": "string", "enum": sorted(VALID_EVENT_KINDS)},
                "default_pe": {"type": "number", "default": -0.1},
                "description": {"type": "string"},
            },
            "required": ["name", "event_kind"],
        },
    ),
    Tool(
        name="habenula_fire",
        description=(
            "Record a negative-PE event. Pass trigger_name (uses default_pe + event_kind) "
            "OR pass signed_pe + event_kind explicitly. signed_pe must be ≤ 0. Updates "
            "habenula_state tonic/phasic + 24h counter + suggested_da_damp."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "trigger_name": {"type": "string"},
                "signed_pe": {"type": "number"},
                "event_kind": {"type": "string", "enum": sorted(VALID_EVENT_KINDS)},
                "agent_id": {"type": "string"},
                "context_hash": {"type": "string"},
                "source_event_id": {"type": "integer"},
                "notes": {"type": "string"},
            },
        },
    ),
    Tool(
        name="habenula_history",
        description="Paginated firings. Filters: since, agent_id, event_kind. limit clamped to [1, 200].",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "agent_id": {"type": "string"},
                "event_kind": {"type": "string", "enum": sorted(VALID_EVENT_KINDS)},
            },
        },
    ),
    Tool(
        name="habenula_reset",
        description="Admin reset of habenula_state (clears tonic/phasic/counter/damp). Returns prior state for audit.",
        inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}},
    ),
]


_HABENULA_TOOLS = {
    "habenula_status": tool_habenula_status,
    "habenula_register_trigger": tool_habenula_register_trigger,
    "habenula_fire": tool_habenula_fire,
    "habenula_history": tool_habenula_history,
    "habenula_reset": tool_habenula_reset,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _HABENULA_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
