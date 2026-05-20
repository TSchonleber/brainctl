"""brainctl MCP tools — sleep architecture state machine.

Phase 1 per research/autonomous-research-avenues-2026-05-20.md Avenue 1.
Codifies the 5 sleep stages (awake / NREM1 / NREM2 / NREM3-SWS / REM)
as a first-class state machine. Phase 1 = inspection + manual stage
transitions; Phase 2 will auto-progress through ultradian cycles when
ARAS sleep_wake_mode flips; Phase 3 will stage-gate consolidation ops.
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

VALID_STAGES = {"awake", "nrem1", "nrem2", "nrem3_sws", "rem"}

# Canonical NREM1 → NREM2 → NREM3 → NREM2 → REM ultradian cycle.
# Returning to awake from any stage is always permitted.
_CANONICAL_NEXT_STAGE = {
    "awake": "nrem1",
    "nrem1": "nrem2",
    "nrem2": "nrem3_sws",
    "nrem3_sws": "rem",
    "rem": "nrem2",      # back into NREM2 starts the next ultradian cycle
}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("sleep_stage_catalog", "sleep_cycle_state", "sleep_cycle_transitions"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return (f"sleep architecture schema missing: {t} not found. "
                    "Run `brainctl migrate` (migration 074).")
    return None


def tool_sleep_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM sleep_cycle_state WHERE id = 1").fetchone()
        catalog = _rows(conn.execute(
            "SELECT * FROM sleep_stage_catalog ORDER BY id"
        ).fetchall())
        last_transitions = _rows(conn.execute(
            "SELECT * FROM sleep_cycle_transitions ORDER BY id DESC LIMIT 5"
        ).fetchall())
        # Elapsed in current stage
        elapsed_row = conn.execute(
            "SELECT (julianday('now') * 86400 - julianday(?) * 86400) AS elapsed_s",
            (state["stage_entered_at"],),
        ).fetchone()
        elapsed = float(elapsed_row[0]) if elapsed_row and elapsed_row[0] is not None else 0.0
        current_stage_meta = next(
            (c for c in catalog if c["stage"] == state["current_stage"]), None,
        )
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "stage_catalog": catalog,
        "last_5_transitions": last_transitions,
        "current_stage_elapsed_seconds": elapsed,
        "current_stage_meta": current_stage_meta,
    }


def tool_sleep_transition(
    to_stage: str,
    reason: str | None = None,
    triggered_by: str = "manual",
    agent_id: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Move to a specific sleep stage. Records the transition + updates
    cycle bookkeeping. Setting to_stage='awake' from any stage is
    always permitted. From 'rem' to 'nrem2' increments cycle_number."""
    if to_stage not in VALID_STAGES:
        return {"error": f"invalid to_stage {to_stage!r}; expected one of {sorted(VALID_STAGES)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM sleep_cycle_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "sleep_cycle_state seed row missing"}
        from_stage = state["current_stage"]
        if from_stage == to_stage:
            return {"ok": True, "no_op": True, "current_stage": to_stage}
        # Compute duration in from_stage
        elapsed_row = conn.execute(
            "SELECT (julianday('now') * 86400 - julianday(?) * 86400) AS elapsed_s",
            (state["stage_entered_at"],),
        ).fetchone()
        dur = int(elapsed_row[0]) if elapsed_row and elapsed_row[0] is not None else 0
        cycle_number = int(state["cycle_number"])
        cycle_started_at = state["cycle_started_at"]
        # Cycle bookkeeping
        if from_stage == "awake" and to_stage == "nrem1":
            # Starting a new sleep period
            cycle_number = max(1, cycle_number + 1) if state["cycle_started_at"] is None else cycle_number + 1
            cycle_started_at = "datetime('now')"
        elif from_stage == "rem" and to_stage == "nrem2":
            # Completing an ultradian cycle, starting the next
            cycle_number += 1
        # Insert transition row
        cur = conn.execute(
            """
            INSERT INTO sleep_cycle_transitions
              (agent_id, from_stage, to_stage, cycle_number,
               duration_in_from_stage_seconds, reason, triggered_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, from_stage, to_stage, cycle_number, dur, reason, triggered_by),
        )
        transition_id = cur.lastrowid
        # Bookkeeping: total_*_seconds
        total_sleep_inc = dur if from_stage != "awake" else 0
        total_rem_inc = dur if from_stage == "rem" else 0
        total_sws_inc = dur if from_stage == "nrem3_sws" else 0
        if cycle_started_at == "datetime('now')":
            conn.execute(
                """
                UPDATE sleep_cycle_state SET
                  current_stage = ?, cycle_number = ?,
                  stage_entered_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                  cycle_started_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                  total_sleep_seconds = total_sleep_seconds + ?,
                  total_rem_seconds = total_rem_seconds + ?,
                  total_sws_seconds = total_sws_seconds + ?,
                  updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                 WHERE id = 1
                """,
                (to_stage, cycle_number, total_sleep_inc, total_rem_inc, total_sws_inc),
            )
        else:
            conn.execute(
                """
                UPDATE sleep_cycle_state SET
                  current_stage = ?, cycle_number = ?,
                  stage_entered_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                  total_sleep_seconds = total_sleep_seconds + ?,
                  total_rem_seconds = total_rem_seconds + ?,
                  total_sws_seconds = total_sws_seconds + ?,
                  updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                 WHERE id = 1
                """,
                (to_stage, cycle_number, total_sleep_inc, total_rem_inc, total_sws_inc),
            )
        conn.commit()
    return {
        "ok": True, "transition_id": transition_id,
        "from_stage": from_stage, "to_stage": to_stage,
        "cycle_number": cycle_number,
        "duration_in_from_stage_seconds": dur,
    }


def tool_sleep_advance(reason: str | None = None, agent_id: str | None = None,
                      **_kw: Any) -> dict[str, Any]:
    """Advance one step along the canonical ultradian cycle.
    awake → nrem1 → nrem2 → nrem3_sws → rem → nrem2 → ..."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM sleep_cycle_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "sleep_cycle_state seed row missing"}
        next_stage = _CANONICAL_NEXT_STAGE.get(state["current_stage"])
        if not next_stage:
            return {"error": f"no canonical next stage from {state['current_stage']!r}"}
    return tool_sleep_transition(
        to_stage=next_stage,
        reason=reason or "canonical_advance",
        triggered_by="manual",
        agent_id=agent_id,
    )


def tool_sleep_operation_permitted(operation: str, **_kw: Any) -> dict[str, Any]:
    """Check whether `operation` is permitted in the current sleep stage.

    Looks up the current stage's permitted_operations CSV and matches
    case-insensitively. Returns {permitted: bool, current_stage,
    permitted_operations}.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT current_stage FROM sleep_cycle_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "sleep_cycle_state seed row missing"}
        catalog_row = conn.execute(
            "SELECT permitted_operations FROM sleep_stage_catalog WHERE stage = ?",
            (state["current_stage"],),
        ).fetchone()
    ops_csv = (catalog_row["permitted_operations"] or "").lower() if catalog_row else ""
    ops = {o.strip() for o in ops_csv.split(",") if o.strip()}
    permitted = "all" in ops or operation.lower() in ops
    return {
        "ok": True, "operation": operation, "current_stage": state["current_stage"],
        "permitted": permitted, "permitted_operations": sorted(ops),
    }


def tool_sleep_history(limit: int = 50, since: str | None = None,
                       to_stage: str | None = None, **_kw: Any) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses, params = [], []
        if since:
            clauses.append("transitioned_at >= ?"); params.append(since)
        if to_stage:
            if to_stage not in VALID_STAGES:
                return {"error": f"invalid to_stage {to_stage!r}"}
            clauses.append("to_stage = ?"); params.append(to_stage)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM sleep_cycle_transitions {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {"ok": True, "transitions": _rows(rows)}


TOOLS: list[Tool] = [
    Tool(
        name="sleep_status",
        description=(
            "Sleep architecture inspection. Current stage + cycle_number + elapsed in "
            "stage + last 5 transitions + full catalog of permitted operations per stage."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="sleep_transition",
        description=(
            "Move to a specific sleep stage. to_stage ∈ {awake, nrem1, nrem2, nrem3_sws, "
            "rem}. Updates cycle bookkeeping (entering nrem1 from awake = new cycle; "
            "rem → nrem2 = next ultradian cycle)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "to_stage": {"type": "string", "enum": sorted(VALID_STAGES)},
                "reason": {"type": "string"},
                "triggered_by": {"type": "string", "default": "manual"},
                "agent_id": {"type": "string"},
            },
            "required": ["to_stage"],
        },
    ),
    Tool(
        name="sleep_advance",
        description=(
            "Advance one step along the canonical ultradian cycle: "
            "awake → nrem1 → nrem2 → nrem3_sws → rem → nrem2 → ... Convenience wrapper "
            "around sleep_transition."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "agent_id": {"type": "string"},
            },
        },
    ),
    Tool(
        name="sleep_operation_permitted",
        description=(
            "Check whether a named operation is permitted in the current sleep stage. "
            "Looks up the catalog's permitted_operations CSV. 'all' permits everything. "
            "Used by consolidation/dream/replay code to stage-gate their work."
        ),
        inputSchema={
            "type": "object",
            "properties": {"operation": {"type": "string"}},
            "required": ["operation"],
        },
    ),
    Tool(
        name="sleep_history",
        description="Paginated stage-transition history. Filters: since, to_stage. limit clamped to [1, 500].",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
                "since": {"type": "string"},
                "to_stage": {"type": "string", "enum": sorted(VALID_STAGES)},
            },
        },
    ),
]


_SLEEP_TOOLS = {
    "sleep_status": tool_sleep_status,
    "sleep_transition": tool_sleep_transition,
    "sleep_advance": tool_sleep_advance,
    "sleep_operation_permitted": tool_sleep_operation_permitted,
    "sleep_history": tool_sleep_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _SLEEP_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
