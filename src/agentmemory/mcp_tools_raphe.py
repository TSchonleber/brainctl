"""brainctl MCP tools — raphe nuclei (serotonin source).

Phase 1 codifies the serotonin source as a first-class structure.
Completes the neuromod-source trio: LC (NE, PR #121), VTA/SNc (DA,
PR #130), Raphe (5-HT, this PR).

Biology: dorsal raphe + median raphe nuclei produce most CNS 5-HT.
DRN modulates patience / time horizon / cost-of-waiting; MRN modulates
mood persistence and hippocampal contextual stability.

Phase 1 = inspection + manual firings. Phase 3 wires
raphe.time_horizon into BG eligibility-trace decay.
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

VALID_SUBTYPES = {"drn", "mrn"}
VALID_TRIGGER_KINDS = {
    "patience_required", "sustained_effort", "long_horizon_plan",
    "mood_stabilization", "manual", "other",
}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("raphe_state", "raphe_firings", "raphe_subtype_catalog"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"raphe schema missing: {t}. Run `brainctl migrate` (077)."
    return None


def tool_raphe_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM raphe_state WHERE id = 1").fetchone()
        last_5 = _rows(conn.execute(
            "SELECT * FROM raphe_firings ORDER BY id DESC LIMIT 5"
        ).fetchall())
        subtypes = _rows(conn.execute("SELECT * FROM raphe_subtype_catalog ORDER BY id").fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(magnitude), 0.0) AS mean_mag,
                   SUM(CASE WHEN subtype='drn' THEN 1 ELSE 0 END) AS n_drn,
                   SUM(CASE WHEN subtype='mrn' THEN 1 ELSE 0 END) AS n_mrn
              FROM raphe_firings
             WHERE fired_at >= datetime('now', '-24 hours')
            """
        ).fetchone()
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "subtype_catalog": subtypes,
        "last_5_firings": last_5,
        "aggregate_24h": dict(agg) if agg else {},
    }


def tool_raphe_fire(
    subtype: str, magnitude: float,
    trigger_kind: str | None = None, agent_id: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if subtype not in VALID_SUBTYPES:
        return {"error": f"invalid subtype {subtype!r}; expected drn or mrn"}
    if not 0.0 <= magnitude <= 1.0:
        return {"error": "magnitude must be in [0, 1]"}
    if trigger_kind is not None and trigger_kind not in VALID_TRIGGER_KINDS:
        return {"error": f"invalid trigger_kind {trigger_kind!r}; expected one of {sorted(VALID_TRIGGER_KINDS)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        cur = conn.execute(
            """
            INSERT INTO raphe_firings (agent_id, subtype, magnitude, trigger_kind, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (agent_id, subtype, float(magnitude), trigger_kind, notes),
        )
        firing_id = cur.lastrowid
        conn.execute(
            """
            UPDATE raphe_state SET
              phasic_burst = ?,
              last_phasic_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              total_firings = total_firings + 1,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (float(magnitude),),
        )
        conn.commit()
    return {
        "ok": True, "firing_id": firing_id,
        "subtype": subtype, "magnitude": float(magnitude),
    }


def tool_raphe_set_state(
    tonic_5ht: float | None = None,
    time_horizon_seconds: int | None = None,
    mood_baseline: float | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Update raphe tonic state. Phase 3 will automate this from
    sustained-firing aggregates + downstream feedback."""
    if tonic_5ht is not None and not 0.0 <= tonic_5ht <= 1.0:
        return {"error": "tonic_5ht must be in [0, 1]"}
    if time_horizon_seconds is not None and time_horizon_seconds <= 0:
        return {"error": "time_horizon_seconds must be > 0"}
    if mood_baseline is not None and not -1.0 <= mood_baseline <= 1.0:
        return {"error": "mood_baseline must be in [-1, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        updates, params = [], []
        if tonic_5ht is not None:
            updates.append("tonic_5ht = ?"); params.append(float(tonic_5ht))
        if time_horizon_seconds is not None:
            updates.append("time_horizon_seconds = ?"); params.append(int(time_horizon_seconds))
        if mood_baseline is not None:
            updates.append("mood_baseline = ?"); params.append(float(mood_baseline))
        if not updates:
            return {"error": "no fields to update"}
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        conn.execute(
            f"UPDATE raphe_state SET {', '.join(updates)} WHERE id = 1",
            tuple(params),
        )
        conn.commit()
        state = conn.execute("SELECT * FROM raphe_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None}


def tool_raphe_history(
    limit: int = 20, since: str | None = None,
    subtype: str | None = None, trigger_kind: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    if subtype is not None and subtype not in VALID_SUBTYPES:
        return {"error": "invalid subtype"}
    if trigger_kind is not None and trigger_kind not in VALID_TRIGGER_KINDS:
        return {"error": "invalid trigger_kind"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses, params = [], []
        if since:
            clauses.append("fired_at >= ?"); params.append(since)
        if subtype:
            clauses.append("subtype = ?"); params.append(subtype)
        if trigger_kind:
            clauses.append("trigger_kind = ?"); params.append(trigger_kind)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM raphe_firings {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {"ok": True, "history": _rows(rows)}


TOOLS: list[Tool] = [
    Tool(
        name="raphe_status",
        description="Raphe state + DRN/MRN catalog + last 5 firings + 24h aggregate.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="raphe_fire",
        description=(
            "Record a phasic 5-HT firing. subtype ∈ {drn, mrn}. magnitude in [0, 1]. "
            "Optional trigger_kind ∈ {patience_required, sustained_effort, "
            "long_horizon_plan, mood_stabilization, manual, other}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "subtype": {"type": "string", "enum": sorted(VALID_SUBTYPES)},
                "magnitude": {"type": "number"},
                "trigger_kind": {"type": "string", "enum": sorted(VALID_TRIGGER_KINDS)},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["subtype", "magnitude"],
        },
    ),
    Tool(
        name="raphe_set_state",
        description=(
            "Manually update tonic_5ht, time_horizon_seconds, and/or mood_baseline. "
            "Phase 3 will automate from sustained firings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tonic_5ht": {"type": "number"},
                "time_horizon_seconds": {"type": "integer"},
                "mood_baseline": {"type": "number"},
            },
        },
    ),
    Tool(
        name="raphe_history",
        description="Paginated firing history. Filters: since, subtype, trigger_kind. limit clamped to [1, 200].",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "subtype": {"type": "string", "enum": sorted(VALID_SUBTYPES)},
                "trigger_kind": {"type": "string", "enum": sorted(VALID_TRIGGER_KINDS)},
            },
        },
    ),
]


_RAPHE_TOOLS = {
    "raphe_status": tool_raphe_status,
    "raphe_fire": tool_raphe_fire,
    "raphe_set_state": tool_raphe_set_state,
    "raphe_history": tool_raphe_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _RAPHE_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
