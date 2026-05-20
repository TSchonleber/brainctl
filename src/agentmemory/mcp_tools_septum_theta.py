"""brainctl MCP tools — medial septum + theta rhythm.

Phase 1 per research-avenues memo Avenue 8. Hippocampal theta
pacemaker (4-8 Hz). Phase 1 = manual ticking + queries; Phase 2 daemon
auto-ticks; Phase 3 phase-locks memory_search to current theta bin.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_OPERATIONS = {"write", "recall", "reconsolidate"}
TWO_PI = 2.0 * math.pi


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("septum_state", "septum_ticks", "septum_phase_locked_memories"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"septum schema missing: {t}. Run `brainctl migrate` (076)."
    return None


def _phase_to_bin(phase: float) -> int:
    """Map theta_phase ∈ [0, 2π) to 8-bin index (45° each)."""
    p = phase % TWO_PI
    return int(p / (TWO_PI / 8)) % 8


def tool_septum_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM septum_state WHERE id = 1").fetchone()
        last_5 = _rows(conn.execute(
            "SELECT * FROM septum_ticks ORDER BY id DESC LIMIT 5"
        ).fetchall())
        bin_dist = _rows(conn.execute(
            """
            SELECT theta_bin, COUNT(*) AS n
              FROM septum_phase_locked_memories
             WHERE locked_at >= datetime('now', '-1 hour')
             GROUP BY theta_bin ORDER BY theta_bin
            """
        ).fetchall())
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_ticks": last_5,
        "phase_locks_by_bin_1h": bin_dist,
    }


def tool_septum_tick(triggered_by: str = "manual", **_kw: Any) -> dict[str, Any]:
    """Advance one tick of the theta rhythm. Each tick advances phase
    by 2π/8 (one bin). After 8 ticks a full cycle completes."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM septum_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "septum_state seed row missing"}
        # Advance phase by 2π/8 = π/4
        new_phase = (float(state["theta_phase"]) + (TWO_PI / 8.0)) % TWO_PI
        new_bin = _phase_to_bin(new_phase)
        new_cycle = int(state["cycle_count"])
        if new_bin < state["theta_bin"]:
            # Wrapped around — completed a cycle
            new_cycle += 1
        conn.execute(
            """
            UPDATE septum_state SET
              theta_phase = ?, theta_bin = ?, cycle_count = ?,
              last_tick_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (new_phase, new_bin, new_cycle),
        )
        cur = conn.execute(
            """
            INSERT INTO septum_ticks (cycle_count, theta_bin, triggered_by)
            VALUES (?, ?, ?)
            """,
            (new_cycle, new_bin, triggered_by),
        )
        tick_id = cur.lastrowid
        conn.commit()
    return {
        "ok": True, "tick_id": tick_id,
        "theta_phase": new_phase, "theta_bin": new_bin,
        "cycle_count": new_cycle,
    }


def tool_septum_phase_lock(
    memory_id: int, operation: str = "write",
    **_kw: Any,
) -> dict[str, Any]:
    """Record that a memory operation occurred at the current theta bin.
    operation ∈ {write, recall, reconsolidate}."""
    if operation not in VALID_OPERATIONS:
        return {"error": f"invalid operation {operation!r}; expected {sorted(VALID_OPERATIONS)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT theta_bin, cycle_count FROM septum_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "septum_state seed row missing"}
        cur = conn.execute(
            """
            INSERT INTO septum_phase_locked_memories
              (memory_id, theta_bin, cycle_count, operation)
            VALUES (?, ?, ?, ?)
            """,
            (int(memory_id), int(state["theta_bin"]), int(state["cycle_count"]), operation),
        )
        conn.commit()
    return {
        "ok": True, "lock_id": cur.lastrowid,
        "memory_id": int(memory_id), "theta_bin": int(state["theta_bin"]),
        "cycle_count": int(state["cycle_count"]),
    }


def tool_septum_query_bin(theta_bin: int, limit: int = 50, **_kw: Any) -> dict[str, Any]:
    """Return memory_ids phase-locked to a specific theta bin."""
    if not 0 <= theta_bin <= 7:
        return {"error": "theta_bin must be in [0, 7]"}
    limit = max(1, min(int(limit), 500))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        rows = conn.execute(
            """
            SELECT id, memory_id, locked_at, theta_bin, cycle_count, operation
              FROM septum_phase_locked_memories
             WHERE theta_bin = ?
             ORDER BY id DESC LIMIT ?
            """,
            (theta_bin, limit),
        ).fetchall()
    return {"ok": True, "theta_bin": theta_bin, "memories": _rows(rows)}


def tool_septum_set_frequency(theta_frequency_hz: float, **_kw: Any) -> dict[str, Any]:
    """Update theta_frequency_hz. Valid range [4.0, 8.0] (biological theta band)."""
    if not 4.0 <= theta_frequency_hz <= 8.0:
        return {"error": "theta_frequency_hz must be in [4.0, 8.0] (biological theta band)"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            """
            UPDATE septum_state SET
              theta_frequency_hz = ?,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (float(theta_frequency_hz),),
        )
        conn.commit()
        state = conn.execute("SELECT * FROM septum_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None}


TOOLS: list[Tool] = [
    Tool(
        name="septum_status",
        description="Septum + theta state: frequency, current phase + bin (0-7), cycle count, last 5 ticks, 1h phase-lock distribution.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="septum_tick",
        description=(
            "Advance one theta tick (one 8th of a cycle, 45°). Wraps to new cycle every 8 ticks. "
            "Phase 1 is manual; Phase 2 daemon will auto-tick at theta_frequency_hz cadence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "triggered_by": {"type": "string", "default": "manual"},
            },
        },
    ),
    Tool(
        name="septum_phase_lock",
        description=(
            "Stamp a memory operation with the current theta_bin + cycle. operation ∈ "
            "{write, recall, reconsolidate}. Used by Phase 3 phase-locked memory_search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "operation": {"type": "string", "enum": sorted(VALID_OPERATIONS), "default": "write"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="septum_query_bin",
        description="List memory_ids phase-locked to a specific theta_bin (0-7). limit clamped to [1, 500].",
        inputSchema={
            "type": "object",
            "properties": {
                "theta_bin": {"type": "integer", "minimum": 0, "maximum": 7},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["theta_bin"],
        },
    ),
    Tool(
        name="septum_set_frequency",
        description="Update theta_frequency_hz. Valid range [4.0, 8.0] (biological theta band).",
        inputSchema={
            "type": "object",
            "properties": {"theta_frequency_hz": {"type": "number"}},
            "required": ["theta_frequency_hz"],
        },
    ),
]


_SEPTUM_TOOLS = {
    "septum_status": tool_septum_status,
    "septum_tick": tool_septum_tick,
    "septum_phase_lock": tool_septum_phase_lock,
    "septum_query_bin": tool_septum_query_bin,
    "septum_set_frequency": tool_septum_set_frequency,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _SEPTUM_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
