"""brainctl MCP tools — VTA/SNc dopamine source.

Phase 1 per research-avenues memo Avenue 7. Codifies the dopamine
source as a first-class structure with its own firing log and state,
rather than just a derived quantity in bg_td_events + bg_modulators.

Pairs with Habenula (PR #124): habenula's suggested_da_damp feeds VTA
tonic in Phase 3.
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

VALID_SOURCE_KINDS = {"bg_td_positive", "novelty", "reward_received", "explicit_motivation", "other"}
VALID_PATHWAYS = {"mesolimbic", "mesocortical", "nigrostriatal", "broadcast", "other"}
VALID_PATHOLOGY = {"none", "low_da", "high_da"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("vta_state", "vta_firings", "vta_pathway_links"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"VTA/SNc schema missing: {t}. Run `brainctl migrate` (075)."
    return None


def tool_vta_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM vta_state WHERE id = 1").fetchone()
        last_5 = _rows(conn.execute(
            "SELECT * FROM vta_firings ORDER BY id DESC LIMIT 5"
        ).fetchall())
        agg_24h = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(burst_magnitude), 0.0) AS mean_mag,
                   COALESCE(MAX(burst_magnitude), 0.0) AS peak_mag,
                   SUM(CASE WHEN source_kind='bg_td_positive' THEN 1 ELSE 0 END) AS n_td,
                   SUM(CASE WHEN source_kind='novelty' THEN 1 ELSE 0 END) AS n_novelty,
                   SUM(CASE WHEN source_kind='reward_received' THEN 1 ELSE 0 END) AS n_reward
              FROM vta_firings
             WHERE fired_at >= datetime('now', '-24 hours')
            """
        ).fetchone()
        pathway_dist = _rows(conn.execute(
            """
            SELECT target_pathway, COUNT(*) AS n
              FROM vta_firings
             WHERE fired_at >= datetime('now', '-24 hours')
             GROUP BY target_pathway
            """
        ).fetchall())
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_firings": last_5,
        "aggregate_24h": dict(agg_24h) if agg_24h else {},
        "pathway_distribution_24h": pathway_dist,
    }


def tool_vta_fire(
    burst_magnitude: float, source_kind: str,
    target_pathway: str | None = None, agent_id: str | None = None,
    source_event_id: int | None = None, notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Record one phasic dopamine burst from VTA/SNc.

    Updates `vta_state.burst_budget` (depletes by 0.1 × magnitude;
    clamped at 0) and `phasic_burst` (set to current magnitude).
    Does NOT update `bg_modulators.tonic_da` in Phase 1; Phase 3 will.
    """
    if not 0.0 <= burst_magnitude <= 1.0:
        return {"error": "burst_magnitude must be in [0, 1]"}
    if source_kind not in VALID_SOURCE_KINDS:
        return {"error": f"invalid source_kind {source_kind!r}; expected {sorted(VALID_SOURCE_KINDS)}"}
    if target_pathway is not None and target_pathway not in VALID_PATHWAYS:
        return {"error": f"invalid target_pathway {target_pathway!r}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        cur = conn.execute(
            """
            INSERT INTO vta_firings
              (agent_id, burst_magnitude, source_kind, source_event_id, target_pathway, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent_id, float(burst_magnitude), source_kind, source_event_id, target_pathway, notes),
        )
        firing_id = cur.lastrowid
        state = conn.execute("SELECT * FROM vta_state WHERE id = 1").fetchone()
        new_budget = max(0.0, float(state["burst_budget"]) - 0.1 * float(burst_magnitude))
        conn.execute(
            """
            UPDATE vta_state SET
              phasic_burst = ?,
              burst_budget = ?,
              last_phasic_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              total_firings = total_firings + 1,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (float(burst_magnitude), new_budget),
        )
        conn.commit()
    return {
        "ok": True, "firing_id": firing_id,
        "burst_magnitude": float(burst_magnitude),
        "new_burst_budget": new_budget,
    }


def tool_vta_set_tonic(
    tonic_da: float | None = None,
    pathology_flag: str | None = None,
    reason: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Update VTA tonic state. Phase 1: manual adjustment for
    diagnostics + testing. Phase 3 lets Habenula's suggested_da_damp
    + ARAS arousal modulate it automatically."""
    if tonic_da is not None and not 0.0 <= tonic_da <= 1.0:
        return {"error": "tonic_da must be in [0, 1]"}
    if pathology_flag is not None and pathology_flag not in VALID_PATHOLOGY:
        return {"error": f"invalid pathology_flag; expected {sorted(VALID_PATHOLOGY)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        updates, params = [], []
        if tonic_da is not None:
            updates.append("tonic_da = ?"); params.append(float(tonic_da))
        if pathology_flag is not None:
            updates.append("pathology_flag = ?"); params.append(pathology_flag)
        if not updates:
            return {"error": "no fields to update"}
        updates.append("last_tonic_update_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        conn.execute(
            f"UPDATE vta_state SET {', '.join(updates)} WHERE id = 1",
            tuple(params),
        )
        conn.commit()
        state = conn.execute("SELECT * FROM vta_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None, "reason": reason}


def tool_vta_pathways(pathway: str | None = None, **_kw: Any) -> dict[str, Any]:
    """Catalog of VTA → target-subsystem links by pathway."""
    if pathway is not None and pathway not in VALID_PATHWAYS:
        return {"error": f"invalid pathway"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        if pathway:
            rows = conn.execute(
                "SELECT * FROM vta_pathway_links WHERE pathway = ?", (pathway,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vta_pathway_links ORDER BY pathway, target_subsystem"
            ).fetchall()
    return {"ok": True, "pathway_links": _rows(rows)}


def tool_vta_history(
    limit: int = 20, since: str | None = None,
    source_kind: str | None = None, target_pathway: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    if source_kind is not None and source_kind not in VALID_SOURCE_KINDS:
        return {"error": "invalid source_kind"}
    if target_pathway is not None and target_pathway not in VALID_PATHWAYS:
        return {"error": "invalid target_pathway"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses, params = [], []
        if since:
            clauses.append("fired_at >= ?"); params.append(since)
        if source_kind:
            clauses.append("source_kind = ?"); params.append(source_kind)
        if target_pathway:
            clauses.append("target_pathway = ?"); params.append(target_pathway)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM vta_firings {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {"ok": True, "history": _rows(rows)}


TOOLS: list[Tool] = [
    Tool(
        name="vta_status",
        description="VTA/SNc state + last 5 firings + 24h aggregate + pathway distribution.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="vta_fire",
        description=(
            "Record one phasic dopamine burst. burst_magnitude in [0,1]. source_kind ∈ "
            "{bg_td_positive, novelty, reward_received, explicit_motivation, other}. "
            "Optional target_pathway ∈ {mesolimbic, mesocortical, nigrostriatal, broadcast}. "
            "Depletes burst_budget by 0.1×magnitude."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "burst_magnitude": {"type": "number"},
                "source_kind": {"type": "string", "enum": sorted(VALID_SOURCE_KINDS)},
                "target_pathway": {"type": "string", "enum": sorted(VALID_PATHWAYS)},
                "agent_id": {"type": "string"},
                "source_event_id": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["burst_magnitude", "source_kind"],
        },
    ),
    Tool(
        name="vta_set_tonic",
        description="Manually set tonic_da and/or pathology_flag. Phase 3 will automate from Habenula + ARAS.",
        inputSchema={
            "type": "object",
            "properties": {
                "tonic_da": {"type": "number"},
                "pathology_flag": {"type": "string", "enum": sorted(VALID_PATHOLOGY)},
                "reason": {"type": "string"},
            },
        },
    ),
    Tool(
        name="vta_pathways",
        description=(
            "Catalog of VTA → target-subsystem links. Without filter: all 4 pathways "
            "(mesolimbic, mesocortical, nigrostriatal, broadcast). With pathway filter: "
            "just that pathway's links."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pathway": {"type": "string", "enum": sorted(VALID_PATHWAYS)},
            },
        },
    ),
    Tool(
        name="vta_history",
        description="Paginated firing history. Filters: since, source_kind, target_pathway. limit clamped to [1, 200].",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "source_kind": {"type": "string", "enum": sorted(VALID_SOURCE_KINDS)},
                "target_pathway": {"type": "string", "enum": sorted(VALID_PATHWAYS)},
            },
        },
    ),
]


_VTA_TOOLS = {
    "vta_status": tool_vta_status,
    "vta_fire": tool_vta_fire,
    "vta_set_tonic": tool_vta_set_tonic,
    "vta_pathways": tool_vta_pathways,
    "vta_history": tool_vta_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _VTA_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
