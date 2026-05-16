"""brainctl MCP tools — basal ganglia inspection and action catalog.

Phase 1 of the BG subsystem per docs/proposals/basal_ganglia.md. Read-only
plus minimal idempotent writes for catalog setup; no behavior change to
existing brainctl tools yet. The TD-error broadcast bus and eligibility
trace updates live as tables; wiring them into actual outcome flows is
Phase 2.

The BG pairs with the thalamus subsystem — call order is:
  agent request → BG (action selection, learned weights) →
  thalamus (typed routing, gating) → substrate
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

VALID_LOOPS = {"motor", "oculomotor", "dlpfc", "lofc", "acc"}
VALID_HOLD_REASONS = {"conflict", "surprise", "explicit_stop"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows_to_list(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        table
        for table in (
            "bg_actions",
            "bg_striatal_weights",
            "bg_eligibility_traces",
            "bg_td_events",
            "bg_holds",
            "bg_modulators",
            "bg_chunks",
        )
        if not _table_exists(conn, table)
    ]
    if missing:
        return "basal ganglia schema missing tables: " + ", ".join(missing)
    return None


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def tool_bg_status(
    loop: str | None = None,
    agent_id: str | None = None,
    top_n: int = 10,
    **kw: Any,
) -> dict[str, Any]:
    """Return BG snapshot: modulator dials, top actions per loop, recent TD
    events, active holds, eligibility-trace stats.
    """
    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}

        top_n = max(1, min(int(top_n or 10), 50))

        mod_row = db.execute("SELECT * FROM bg_modulators WHERE id=1").fetchone()
        modulators = dict(mod_row) if mod_row else {
            "id": 1,
            "tonic_da": 0.5,
            "lc_ne": 0.5,
            "serotonin": 0.5,
            "set_by": None,
            "updated_at": None,
        }

        loop_filter_sql = "WHERE loop = ?" if loop else ""
        loop_params: list[Any] = [loop] if loop else []
        if loop and loop not in VALID_LOOPS:
            return {"ok": False, "error": f"loop must be one of {sorted(VALID_LOOPS)}"}

        actions_by_loop = db.execute(
            f"""
            SELECT loop, COUNT(*) AS n
            FROM bg_actions
            {loop_filter_sql}
            GROUP BY loop
            ORDER BY loop
            """,  # nosec B608
            loop_params,
        ).fetchall()

        # Top actions ranked by net signal (w_go − w_nogo), summed across contexts
        top_actions = db.execute(
            f"""
            SELECT a.loop, a.action_key, a.description,
                   COUNT(w.context_hash) AS context_count,
                   ROUND(COALESCE(SUM(w.w_go), 0.0), 4) AS sum_w_go,
                   ROUND(COALESCE(SUM(w.w_nogo), 0.0), 4) AS sum_w_nogo,
                   ROUND(COALESCE(SUM(w.w_go - w.w_nogo), 0.0), 4) AS net_signal
            FROM bg_actions a
            LEFT JOIN bg_striatal_weights w ON w.action_id = a.id
            {loop_filter_sql.replace('loop', 'a.loop') if loop else ''}
            GROUP BY a.id
            ORDER BY net_signal DESC, a.action_key ASC
            LIMIT ?
            """,  # nosec B608
            loop_params + [top_n],
        ).fetchall()

        td_where = []
        td_params: list[Any] = []
        if agent_id:
            td_where.append("agent_id = ?")
            td_params.append(agent_id)
        td_where_sql = ("WHERE " + " AND ".join(td_where)) if td_where else ""
        recent_td = db.execute(
            f"""
            SELECT id, task_id, agent_id, utility, v_current, v_next,
                   gamma, delta, source, fired_at, consumed_count
            FROM bg_td_events
            {td_where_sql}
            ORDER BY fired_at DESC, id DESC
            LIMIT ?
            """,  # nosec B608
            td_params + [top_n],
        ).fetchall()

        active_holds = db.execute(
            f"""
            SELECT id, loop, reason, trigger_score_gap, ticks, fired_at
            FROM bg_holds
            WHERE released_at IS NULL
            {('AND loop = ?' if loop else '')}
            ORDER BY fired_at DESC
            LIMIT ?
            """,  # nosec B608
            loop_params + [top_n],
        ).fetchall()

        trace_stats = db.execute(
            """
            SELECT COUNT(*) AS active_traces,
                   ROUND(COALESCE(AVG(trace_strength), 0.0), 4) AS mean_strength,
                   ROUND(COALESCE(MAX(trace_strength), 0.0), 4) AS max_strength
            FROM bg_eligibility_traces
            WHERE expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now')
            """
        ).fetchone()

        return {
            "ok": True,
            "loop_filter": loop,
            "agent_filter": agent_id,
            "modulators": modulators,
            "actions_by_loop": _rows_to_list(actions_by_loop),
            "top_actions": _rows_to_list(top_actions),
            "recent_td_events": _rows_to_list(recent_td),
            "active_holds": _rows_to_list(active_holds),
            "trace_stats": dict(trace_stats) if trace_stats else {},
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_bg_action_register(
    loop: str,
    action_key: str,
    description: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Idempotent UPSERT into bg_actions. Returns the row (created or existing).
    """
    if loop not in VALID_LOOPS:
        return {"ok": False, "error": f"loop must be one of {sorted(VALID_LOOPS)}"}
    if not action_key or not isinstance(action_key, str):
        return {"ok": False, "error": "action_key is required"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        db.execute(
            """
            INSERT INTO bg_actions (loop, action_key, description)
            VALUES (?, ?, ?)
            ON CONFLICT(loop, action_key) DO UPDATE SET
                description = COALESCE(excluded.description, bg_actions.description)
            """,
            (loop, action_key, description),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM bg_actions WHERE loop = ? AND action_key = ?",
            (loop, action_key),
        ).fetchone()
        return {"ok": True, "action": dict(row) if row else None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_bg_modulator_set(
    tonic_da: float | None = None,
    lc_ne: float | None = None,
    serotonin: float | None = None,
    set_by: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Update the three independent neuromodulator dials.

    tonic_da   = policy vigor / search breadth (exploit vs explore)
    lc_ne      = arousal / surprise gain (broaden eligibility under high)
    serotonin  = time horizon / γ scaling (myopic vs patient)
    """
    if tonic_da is None and lc_ne is None and serotonin is None:
        return {"ok": False, "error": "at least one of tonic_da/lc_ne/serotonin is required"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        sets: list[str] = []
        params: list[Any] = []
        if tonic_da is not None:
            sets.append("tonic_da = ?")
            params.append(_clamp(tonic_da, default=0.5))
        if lc_ne is not None:
            sets.append("lc_ne = ?")
            params.append(_clamp(lc_ne, default=0.5))
        if serotonin is not None:
            sets.append("serotonin = ?")
            params.append(_clamp(serotonin, default=0.5))
        if set_by is not None:
            sets.append("set_by = ?")
            params.append(str(set_by))
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        sql = f"UPDATE bg_modulators SET {', '.join(sets)} WHERE id = 1"  # nosec B608
        db.execute(sql, params)
        db.commit()
        row = db.execute("SELECT * FROM bg_modulators WHERE id = 1").fetchone()
        return {"ok": True, "modulators": dict(row) if row else None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(
        name="bg_status",
        description=(
            "Inspect the basal ganglia subsystem: modulator dials (tonic DA / LC-NE / "
            "5-HT), per-loop action counts, top actions by net Go−NoGo signal, recent "
            "TD-error events, active holds, and eligibility-trace stats."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loop": {"type": "string", "enum": sorted(VALID_LOOPS), "description": "Optional loop filter"},
                "agent_id": {"type": "string", "description": "Optional agent filter for TD events"},
                "top_n": {"type": "integer", "default": 10},
            },
        },
    ),
    Tool(
        name="bg_action_register",
        description=(
            "Register a candidate action in the BG catalog. Idempotent UPSERT on "
            "(loop, action_key). loop ∈ {motor, oculomotor, dlpfc, lofc, acc} mirrors "
            "the five parallel cortico-BG-thalamo-cortical loops."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loop": {"type": "string", "enum": sorted(VALID_LOOPS)},
                "action_key": {"type": "string", "description": "Stable identifier, e.g. 'tool:memory_search'"},
                "description": {"type": "string"},
            },
            "required": ["loop", "action_key"],
        },
    ),
    Tool(
        name="bg_modulator_set",
        description=(
            "Update the three independent BG neuromodulator dials (tonic_da, lc_ne, "
            "serotonin). All clamped to [0, 1]. These are policy / arousal / horizon "
            "knobs, NOT one temperature scalar."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tonic_da": {"type": "number", "description": "0=explore, 1=exploit"},
                "lc_ne": {"type": "number", "description": "0=low arousal, 1=high arousal/surprise gain"},
                "serotonin": {"type": "number", "description": "0=myopic, 1=patient"},
                "set_by": {"type": "string"},
            },
        },
    ),
]

_BG_TOOLS = {
    "bg_status": tool_bg_status,
    "bg_action_register": tool_bg_action_register,
    "bg_modulator_set": tool_bg_modulator_set,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _BG_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    """Return tool descriptors and dispatch map for mcp_server integration."""
    return TOOLS, DISPATCH
