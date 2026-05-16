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


def _cascade_to_thalamus(
    tonic_da: float | None,
    lc_ne: float | None,
    set_by: str | None,
) -> dict[str, Any] | None:
    """Mirror BG modulator changes into thalamic_mode where biologically
    plausible. Tonic DA biases the exploit/explore axis (=> wake_focused vs
    wake_exploratory); LC-NE drives thalamic norepinephrine and overall
    arousal; serotonin has no direct thalamic dial (skip).

    Never raises. Returns the thalamus update result on success, None if
    the cascade did not fire (no relevant inputs / schema not applied).
    """
    if tonic_da is None and lc_ne is None:
        return None
    try:
        # Resolve the canonical thalamic mode if tonic DA was supplied
        mode: str | None = None
        if tonic_da is not None:
            if tonic_da >= 0.7:
                mode = "wake_focused"
            elif tonic_da <= 0.3:
                mode = "wake_exploratory"
            # else: mid range — leave the existing mode alone
        # Build kwargs for thalamus_mode_set
        thal_kwargs: dict[str, Any] = {"set_by": f"bg_cascade:{set_by or 'unknown'}"}
        if lc_ne is not None:
            thal_kwargs["norepinephrine"] = _clamp(lc_ne, default=0.5)
            # Arousal blends both BG dials
            if tonic_da is not None:
                thal_kwargs["arousal"] = _clamp((float(tonic_da) + float(lc_ne)) / 2.0, default=0.5)
            else:
                thal_kwargs["arousal"] = _clamp(lc_ne, default=0.5)
        elif tonic_da is not None:
            thal_kwargs["arousal"] = _clamp(tonic_da, default=0.5)

        if mode is None:
            # No mode flip but dial-only update — read current mode and reuse.
            db = _db()
            try:
                row = db.execute("SELECT mode FROM thalamic_mode WHERE id=1").fetchone()
                mode = row[0] if row else "wake_focused"
            finally:
                db.close()
        thal_kwargs["mode"] = mode

        from agentmemory.mcp_tools_thalamus import tool_thalamus_mode_set
        return tool_thalamus_mode_set(**thal_kwargs)
    except Exception:
        return None


def tool_bg_modulator_set(
    tonic_da: float | None = None,
    lc_ne: float | None = None,
    serotonin: float | None = None,
    set_by: str | None = None,
    cascade_to_thalamus: bool = True,
    **kw: Any,
) -> dict[str, Any]:
    """Update the three independent neuromodulator dials.

    tonic_da   = policy vigor / search breadth (exploit vs explore)
    lc_ne      = arousal / surprise gain (broaden eligibility under high)
    serotonin  = time horizon / γ scaling (myopic vs patient)

    When `cascade_to_thalamus` is True (default), biologically plausible
    knock-on effects propagate to thalamic_mode: tonic_da ≥ 0.7 selects
    wake_focused, ≤ 0.3 selects wake_exploratory; LC-NE cascades to
    thalamic norepinephrine; arousal is the blended mean. Serotonin has
    no direct thalamic analog. The cascade is silent on failure.
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
        response: dict[str, Any] = {"ok": True, "modulators": dict(row) if row else None}

        if cascade_to_thalamus:
            cascade = _cascade_to_thalamus(tonic_da=tonic_da, lc_ne=lc_ne, set_by=set_by)
            if cascade is not None:
                response["thalamus_cascade"] = cascade

        return response
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_bg_sweep_traces(**kw: Any) -> dict[str, Any]:
    """Phase 3 maintenance: prune expired or weak eligibility traces.

    Safe to call periodically (e.g., from cron or after large outcome
    cycles). Returns counts of removed and remaining traces.
    """
    from agentmemory.bg_shadow import sweep_eligibility_traces
    return sweep_eligibility_traces()


def tool_bg_weights_show(
    action_key: str | None = None,
    loop: str | None = None,
    top_n: int = 20,
    **kw: Any,
) -> dict[str, Any]:
    """Phase 3 inspection: show striatal weights (Go / NoGo / distributional
    value) per (action, context). Filter by action_key and/or loop.
    """
    if loop and loop not in VALID_LOOPS:
        return {"ok": False, "error": f"loop must be one of {sorted(VALID_LOOPS)}"}
    try:
        top_n_int = max(1, min(int(top_n), 100))
    except (TypeError, ValueError):
        return {"ok": False, "error": "top_n must be an integer"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        clauses = []
        params: list[Any] = []
        if action_key:
            clauses.append("a.action_key = ?")
            params.append(action_key if action_key.startswith("tool:") else f"tool:{action_key}")
        if loop:
            clauses.append("a.loop = ?")
            params.append(loop)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            f"""
            SELECT a.loop, a.action_key, w.context_hash, w.w_go, w.w_nogo,
                   ROUND(w.w_go - w.w_nogo, 4) AS net_signal,
                   w.v_q10, w.v_q30, w.v_q50, w.v_q70, w.v_q90,
                   w.n_updates, w.last_updated
            FROM bg_striatal_weights w
            JOIN bg_actions a ON a.id = w.action_id
            {where_sql}
            ORDER BY ABS(w.w_go - w.w_nogo) DESC, w.n_updates DESC
            LIMIT ?
            """,  # nosec B608
            params + [top_n_int],
        ).fetchall()
        return {
            "ok": True,
            "action_filter": action_key,
            "loop_filter": loop,
            "weight_count": len(rows),
            "weights": _rows_to_list(rows),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_bg_td_emit(
    task_id: str | None = None,
    agent_id: str | None = None,
    outcome: str | None = None,
    utility: float | None = None,
    v_current: float = 0.0,
    v_next: float = 0.0,
    gamma: float = 0.95,
    source: str = "manual",
    **kw: Any,
) -> dict[str, Any]:
    """Compute δ = utility(outcome) + γ·V(s') − V(s) and broadcast onto
    bg_td_events. Phase 2: explicit broadcast; eligibility-trace updates are
    deferred to Phase 3.
    """
    from agentmemory.bg_shadow import broadcast_td_error
    row = broadcast_td_error(
        task_id=task_id,
        agent_id=agent_id,
        outcome=outcome,
        utility=utility,
        v_current=v_current,
        v_next=v_next,
        gamma=gamma,
        source=source,
    )
    if row is None:
        return {"ok": False, "error": "td broadcast failed (missing schema or invalid input)"}
    return {"ok": True, **row}


def tool_bg_shadow_stats(
    days: int = 7,
    loop: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Summarize Phase 2 shadow-mode dispatch decisions: by-decision and
    by-action breakdown plus divergence rate (non-approve fraction).
    """
    try:
        days_int = max(1, int(days))
    except (TypeError, ValueError):
        return {"ok": False, "error": "days must be an integer"}
    if loop and loop not in VALID_LOOPS:
        return {"ok": False, "error": f"loop must be one of {sorted(VALID_LOOPS)}"}

    db = _db()
    try:
        if not _table_exists(db, "bg_shadow_decisions"):
            return {"ok": False, "error": "bg_shadow_decisions table missing (apply migration 055)"}

        where = [f"decision_at >= datetime('now', '-{days_int} days')"]
        params: list[Any] = []
        if loop:
            where.append("loop = ?")
            params.append(loop)
        where_sql = "WHERE " + " AND ".join(where)

        total = db.execute(
            f"SELECT COUNT(*) FROM bg_shadow_decisions {where_sql}",  # nosec B608
            params,
        ).fetchone()[0]
        by_decision = db.execute(
            f"""
            SELECT decision, COUNT(*) AS n
            FROM bg_shadow_decisions
            {where_sql}
            GROUP BY decision
            ORDER BY n DESC
            """,  # nosec B608
            params,
        ).fetchall()
        by_action = db.execute(
            f"""
            SELECT action_key, loop, COUNT(*) AS n,
                   ROUND(AVG(net_signal), 4) AS mean_net
            FROM bg_shadow_decisions
            {where_sql}
            GROUP BY action_key, loop
            ORDER BY n DESC
            LIMIT 20
            """,  # nosec B608
            params,
        ).fetchall()
        recent_blocks = db.execute(
            f"""
            SELECT decision_at, action_key, loop, net_signal, reason
            FROM bg_shadow_decisions
            {where_sql} AND decision != 'approve'
            ORDER BY decision_at DESC
            LIMIT 20
            """,  # nosec B608
            params,
        ).fetchall()

        approve_count = next((r["n"] for r in by_decision if r["decision"] == "approve"), 0)
        return {
            "ok": True,
            "window_days": days_int,
            "loop_filter": loop,
            "total_decisions": total,
            "by_decision": _rows_to_list(by_decision),
            "by_action": _rows_to_list(by_action),
            "divergence_rate": round(1 - (approve_count / total), 4) if total > 0 else 0.0,
            "recent_non_approve": _rows_to_list(recent_blocks),
        }
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
        name="bg_td_emit",
        description=(
            "Phase 2 broadcast: compute δ = utility(outcome) + γ·V(s') − V(s) and insert "
            "a row onto the bg_td_events bus. Outcome maps to utility via a fixed table "
            "(success=1.0, failure=-1.0, partial=0.3, etc.); pass `utility` directly to "
            "override. v_current and v_next default to 0 in Phase 2 (state-value learning "
            "is Phase 3)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "outcome": {"type": "string", "description": "e.g. 'success', 'failure', 'partial', 'blocked'"},
                "utility": {"type": "number", "description": "Override outcome-to-utility mapping"},
                "v_current": {"type": "number", "default": 0.0},
                "v_next": {"type": "number", "default": 0.0},
                "gamma": {"type": "number", "default": 0.95},
                "source": {"type": "string", "default": "manual"},
            },
        },
    ),
    Tool(
        name="bg_shadow_stats",
        description=(
            "Phase 2 observability. Summarize shadow-mode dispatch decisions: counts by "
            "decision, top actions by hit count + mean net signal, recent non-approve "
            "examples, divergence rate (fraction non-approve)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7},
                "loop": {"type": "string", "enum": sorted(VALID_LOOPS)},
            },
        },
    ),
    Tool(
        name="bg_sweep_traces",
        description=(
            "Phase 3 maintenance: prune eligibility traces below strength 0.05 or past "
            "their TTL (1h default). Safe to call from cron or after large outcome cycles."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="bg_weights_show",
        description=(
            "Phase 3 inspection: show striatal weights (Go / NoGo / distributional value) "
            "per (action, context). Filter by action_key or loop. Sorted by |net signal|."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action_key": {"type": "string"},
                "loop": {"type": "string", "enum": sorted(VALID_LOOPS)},
                "top_n": {"type": "integer", "default": 20},
            },
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
    "bg_td_emit": tool_bg_td_emit,
    "bg_shadow_stats": tool_bg_shadow_stats,
    "bg_sweep_traces": tool_bg_sweep_traces,
    "bg_weights_show": tool_bg_weights_show,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _BG_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    """Return tool descriptors and dispatch map for mcp_server integration."""
    return TOOLS, DISPATCH
