"""brainctl MCP tools — ARAS (ascending reticular activating system).

Phase 1 of the ARAS subsystem per docs/proposals/aras.md. ARAS sits
above LC and NB as the global arousal / sleep-wake broadcast — it
gates whether the rest of the neuromod surface is responsive at all.

Phase 1 is inspection + idempotent writes only. No behavior change
to retrieval, LC, NB, or any existing subsystem.
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

VALID_MODES = {
    "nrem_sleep", "rem_sleep", "drowsy",
    "awake_relaxed", "awake_focused", "hyperalert",
}
VALID_TRIGGER_KINDS = {
    "novelty", "threat", "explicit_alert",
    "consolidation_signal", "idle_decay", "other",
}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        t for t in ("aras_state", "aras_transitions", "aras_triggers")
        if not _table_exists(conn, t)
    ]
    if missing:
        return (
            "ARAS schema missing: " + ", ".join(missing)
            + ". Run `brainctl migrate` (migration 069) and retry."
        )
    return None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# --------------------------------------------------------------------- tools


def tool_aras_status(agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute(
            "SELECT * FROM aras_state WHERE id = 1"
        ).fetchone()
        recent_transitions = _rows(conn.execute(
            """
            SELECT id, transitioned_at, agent_id, from_mode, to_mode,
                   reason, arousal_before, arousal_after
              FROM aras_transitions
              WHERE (? IS NULL OR agent_id = ?)
             ORDER BY id DESC LIMIT 5
            """,
            (agent_id, agent_id),
        ).fetchall())
        trigger_count = conn.execute(
            "SELECT COUNT(*) FROM aras_triggers"
        ).fetchone()[0]
        last_24h = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN to_mode IN ('hyperalert','awake_focused') THEN 1 ELSE 0 END) AS heightened,
                   SUM(CASE WHEN to_mode IN ('drowsy','nrem_sleep','rem_sleep') THEN 1 ELSE 0 END) AS lowered
              FROM aras_transitions
             WHERE transitioned_at >= datetime('now', '-24 hours')
               AND (? IS NULL OR agent_id = ?)
            """,
            (agent_id, agent_id),
        ).fetchone()
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_transitions": recent_transitions,
        "registered_triggers": trigger_count,
        "transitions_24h": dict(last_24h) if last_24h else {},
    }


def tool_aras_register_trigger(
    name: str, trigger_kind: str,
    default_arousal_delta: float = 0.05,
    default_target_mode: str | None = None,
    description: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if trigger_kind not in VALID_TRIGGER_KINDS:
        return {"error": f"invalid trigger_kind {trigger_kind!r}; expected one of {sorted(VALID_TRIGGER_KINDS)}"}
    if not -1.0 <= default_arousal_delta <= 1.0:
        return {"error": "default_arousal_delta must be in [-1, 1]"}
    if default_target_mode is not None and default_target_mode not in VALID_MODES:
        return {"error": f"invalid default_target_mode {default_target_mode!r}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            """
            INSERT INTO aras_triggers (name, trigger_kind, default_arousal_delta, default_target_mode, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              trigger_kind = excluded.trigger_kind,
              default_arousal_delta = excluded.default_arousal_delta,
              default_target_mode = excluded.default_target_mode,
              description = COALESCE(excluded.description, aras_triggers.description)
            """,
            (name, trigger_kind, float(default_arousal_delta), default_target_mode, description),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM aras_triggers WHERE name = ?", (name,)
        ).fetchone()
    return {"ok": True, "trigger": dict(row) if row else None}


def tool_aras_transition(
    to_mode: str,
    reason: str | None = None,
    agent_id: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if to_mode not in VALID_MODES:
        return {"error": f"invalid to_mode {to_mode!r}; expected one of {sorted(VALID_MODES)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM aras_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "aras_state seed row missing"}
        from_mode = state["sleep_wake_mode"]
        arousal_before = float(state["arousal_level"])
        # Derive next arousal as a soft pull toward a mode-typical level.
        mode_target = {
            "nrem_sleep": 0.05, "rem_sleep": 0.20, "drowsy": 0.30,
            "awake_relaxed": 0.50, "awake_focused": 0.75, "hyperalert": 0.95,
        }[to_mode]
        arousal_after = _clamp(0.5 * arousal_before + 0.5 * mode_target)
        cur = conn.execute(
            """
            INSERT INTO aras_transitions
              (agent_id, from_mode, to_mode, reason, arousal_before, arousal_after, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, from_mode, to_mode, reason, arousal_before, arousal_after, notes),
        )
        transition_id = cur.lastrowid
        conn.execute(
            """
            UPDATE aras_state SET
              sleep_wake_mode = ?,
              arousal_level = ?,
              last_transition_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (to_mode, arousal_after),
        )
        conn.commit()
    return {
        "ok": True, "transition_id": transition_id,
        "from_mode": from_mode, "to_mode": to_mode,
        "arousal_before": arousal_before, "arousal_after": arousal_after,
    }


def tool_aras_drive(
    trigger_name: str,
    magnitude: float = 1.0,
    agent_id: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Apply a phasic arousal pulse via a registered trigger.

    `magnitude` ∈ [0, 1] scales the trigger's default_arousal_delta.
    If the resulting arousal crosses a mode boundary, automatically
    fires aras_transition to the trigger's default_target_mode.
    """
    if not 0.0 <= magnitude <= 1.0:
        return {"error": "magnitude must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        trig = conn.execute(
            "SELECT id, default_arousal_delta, default_target_mode FROM aras_triggers WHERE name = ?",
            (trigger_name,),
        ).fetchone()
        if not trig:
            return {"error": f"trigger {trigger_name!r} not registered"}
        state = conn.execute("SELECT * FROM aras_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "aras_state seed row missing"}
        delta = float(trig["default_arousal_delta"]) * float(magnitude)
        new_phasic = _clamp(float(state["phasic_alertness"]) + abs(delta))
        new_arousal = _clamp(float(state["arousal_level"]) + delta)
        conn.execute(
            """
            UPDATE aras_state SET
              arousal_level = ?,
              phasic_alertness = ?,
              last_drive_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (new_arousal, new_phasic),
        )
        transition_id = None
        if trig["default_target_mode"] and trig["default_target_mode"] != state["sleep_wake_mode"]:
            # Phasic alertness above 0.7 + a target_mode different from current → auto-transition
            if new_phasic >= 0.7 or abs(delta) >= 0.2:
                cur = conn.execute(
                    """
                    INSERT INTO aras_transitions
                      (agent_id, from_mode, to_mode, reason, trigger_id, arousal_before, arousal_after)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (agent_id, state["sleep_wake_mode"], trig["default_target_mode"],
                     f"auto-fire from drive '{trigger_name}'", trig["id"],
                     float(state["arousal_level"]), new_arousal),
                )
                transition_id = cur.lastrowid
                conn.execute(
                    "UPDATE aras_state SET sleep_wake_mode = ?, last_transition_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = 1",
                    (trig["default_target_mode"],),
                )
        conn.commit()
    return {
        "ok": True, "trigger_name": trigger_name,
        "arousal_delta_applied": delta,
        "new_arousal_level": new_arousal,
        "new_phasic_alertness": new_phasic,
        "auto_transition_id": transition_id,
    }


def tool_aras_history(
    limit: int = 20, since: str | None = None,
    agent_id: str | None = None,
    from_mode: str | None = None, to_mode: str | None = None,
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
            clauses.append("transitioned_at >= ?"); params.append(since)
        if agent_id:
            clauses.append("agent_id = ?"); params.append(agent_id)
        if from_mode:
            clauses.append("from_mode = ?"); params.append(from_mode)
        if to_mode:
            clauses.append("to_mode = ?"); params.append(to_mode)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT id, transitioned_at, agent_id, from_mode, to_mode,
                   reason, trigger_id, arousal_before, arousal_after, notes
              FROM aras_transitions
              {where}
             ORDER BY id DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return {"ok": True, "history": _rows(rows)}


# --------------------------------------------------------------------- registration

TOOLS: list[Tool] = [
    Tool(
        name="aras_status",
        description=(
            "ARAS Phase 1 inspection. Returns current aras_state (sleep_wake_mode, "
            "arousal_level, tonic_drive, phasic_alertness) plus last 5 transitions "
            "and a 24h transition summary."
        ),
        inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}},
    ),
    Tool(
        name="aras_register_trigger",
        description=(
            "Idempotent UPSERT on aras_triggers. trigger_kind ∈ {novelty, threat, "
            "explicit_alert, consolidation_signal, idle_decay, other}. "
            "default_arousal_delta in [-1, 1]; default_target_mode optional."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "trigger_kind": {"type": "string", "enum": sorted(VALID_TRIGGER_KINDS)},
                "default_arousal_delta": {"type": "number", "default": 0.05},
                "default_target_mode": {"type": "string", "enum": sorted(VALID_MODES)},
                "description": {"type": "string"},
            },
            "required": ["name", "trigger_kind"],
        },
    ),
    Tool(
        name="aras_transition",
        description=(
            "Explicit mode change. Writes aras_transitions row + updates "
            "aras_state.sleep_wake_mode + arousal_level (soft-pull to mode-typical). "
            "to_mode ∈ {nrem_sleep, rem_sleep, drowsy, awake_relaxed, awake_focused, "
            "hyperalert}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "to_mode": {"type": "string", "enum": sorted(VALID_MODES)},
                "reason": {"type": "string"},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["to_mode"],
        },
    ),
    Tool(
        name="aras_drive",
        description=(
            "Phasic arousal pulse via a registered trigger. magnitude ∈ [0, 1] scales "
            "trigger's default_arousal_delta. Auto-fires aras_transition to the trigger's "
            "default_target_mode when phasic_alertness ≥ 0.7 or |delta| ≥ 0.2."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "trigger_name": {"type": "string"},
                "magnitude": {"type": "number", "default": 1.0},
                "agent_id": {"type": "string"},
            },
            "required": ["trigger_name"],
        },
    ),
    Tool(
        name="aras_history",
        description=(
            "Paginated ARAS transition history with filters: since (ISO timestamp), "
            "agent_id, from_mode, to_mode. limit clamped to [1, 200]."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "agent_id": {"type": "string"},
                "from_mode": {"type": "string", "enum": sorted(VALID_MODES)},
                "to_mode": {"type": "string", "enum": sorted(VALID_MODES)},
            },
        },
    ),
]


_ARAS_TOOLS = {
    "aras_status": tool_aras_status,
    "aras_register_trigger": tool_aras_register_trigger,
    "aras_transition": tool_aras_transition,
    "aras_drive": tool_aras_drive,
    "aras_history": tool_aras_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _ARAS_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
