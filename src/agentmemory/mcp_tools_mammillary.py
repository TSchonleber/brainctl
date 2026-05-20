"""brainctl MCP tools — mammillary bodies + Papez circuit.

Phase 1: log episodic-memory transits through the Papez loop
(hippocampus → MB → ATN → cingulate → hippocampus). Phase 2 will
auto-log on consolidation_run. Phase 3 lets Papez-completed memories
surface with higher confidence (proxy for "consolidated to
declarative").
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

VALID_DIRECTIONS = {
    "hippocampus_to_atn", "atn_to_cingulate",
    "cingulate_to_hippocampus", "full_loop",
}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("mammillary_state", "mammillary_transit_log"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"mammillary schema missing: {t}. Run `brainctl migrate` (081)."
    return None


def tool_mammillary_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM mammillary_state WHERE id = 1").fetchone()
        last_5 = _rows(conn.execute(
            "SELECT * FROM mammillary_transit_log ORDER BY id DESC LIMIT 5"
        ).fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN direction='full_loop' THEN 1 ELSE 0 END) AS n_full,
                   SUM(CASE WHEN direction='hippocampus_to_atn' THEN 1 ELSE 0 END) AS n_h2a,
                   COUNT(DISTINCT memory_id) AS unique_memories
              FROM mammillary_transit_log
             WHERE transited_at >= datetime('now', '-24 hours')
            """
        ).fetchone()
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_transits": last_5,
        "aggregate_24h": dict(agg) if agg else {},
    }


def tool_mammillary_log_transit(
    memory_id: int, direction: str,
    transit_strength: float = 1.0,
    agent_id: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if direction not in VALID_DIRECTIONS:
        return {"error": f"invalid direction {direction!r}; expected {sorted(VALID_DIRECTIONS)}"}
    if not 0.0 <= transit_strength <= 1.0:
        return {"error": "transit_strength must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        cur = conn.execute(
            """
            INSERT INTO mammillary_transit_log
              (memory_id, agent_id, direction, transit_strength, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(memory_id), agent_id, direction, float(transit_strength), notes),
        )
        transit_id = cur.lastrowid
        conn.execute(
            """
            UPDATE mammillary_state SET
              total_transits = total_transits + 1,
              transits_24h = transits_24h + 1,
              last_transit_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """
        )
        conn.commit()
    return {
        "ok": True, "transit_id": transit_id,
        "memory_id": int(memory_id), "direction": direction,
        "transit_strength": float(transit_strength),
    }


def tool_mammillary_memory_history(memory_id: int, limit: int = 20, **_kw: Any) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        rows = conn.execute(
            """
            SELECT * FROM mammillary_transit_log
             WHERE memory_id = ? ORDER BY id DESC LIMIT ?
            """,
            (int(memory_id), limit),
        ).fetchall()
        full_loops = conn.execute(
            "SELECT COUNT(*) FROM mammillary_transit_log WHERE memory_id = ? AND direction = 'full_loop'",
            (int(memory_id),),
        ).fetchone()[0]
    return {
        "ok": True, "memory_id": int(memory_id),
        "transits": _rows(rows),
        "full_loop_count": full_loops,
        "consolidated": full_loops > 0,
    }


def tool_mammillary_reset_24h(**_kw: Any) -> dict[str, Any]:
    """Reset transits_24h. Phase 1 manual; Phase 2 daemon will
    auto-roll this on a 24h schedule."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        prior = conn.execute("SELECT transits_24h FROM mammillary_state WHERE id = 1").fetchone()
        conn.execute(
            "UPDATE mammillary_state SET transits_24h = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = 1"
        )
        conn.commit()
    return {"ok": True, "prior_24h_count": int(prior["transits_24h"]) if prior else 0}


TOOLS: list[Tool] = [
    Tool(
        name="mammillary_status",
        description="Mammillary + Papez state + last 5 transits + 24h aggregate.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="mammillary_log_transit",
        description=(
            "Log one Papez-circuit transit for an episodic memory. direction ∈ "
            "{hippocampus_to_atn, atn_to_cingulate, cingulate_to_hippocampus, full_loop}. "
            "full_loop = single call completes one full Papez cycle (use when a "
            "consolidation_run pass fully processes a memory)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "direction": {"type": "string", "enum": sorted(VALID_DIRECTIONS)},
                "transit_strength": {"type": "number", "default": 1.0},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["memory_id", "direction"],
        },
    ),
    Tool(
        name="mammillary_memory_history",
        description=(
            "Per-memory transit history with full_loop count + consolidated flag (true "
            "iff ≥1 full_loop transit recorded)."
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
        name="mammillary_reset_24h",
        description="Reset transits_24h counter. Phase 1 manual; Phase 2 daemon-driven.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


_MAMM_TOOLS = {
    "mammillary_status": tool_mammillary_status,
    "mammillary_log_transit": tool_mammillary_log_transit,
    "mammillary_memory_history": tool_mammillary_memory_history,
    "mammillary_reset_24h": tool_mammillary_reset_24h,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _MAMM_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
