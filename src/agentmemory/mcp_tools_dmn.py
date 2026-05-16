"""brainctl MCP tools — Default Mode Network (DMN).

Offline simulation / counterfactual rollout subsystem. Speculative
memories live in dmn_speculative_memories — quarantined from default
retrieval. They only graduate to `memories` when validated against
real events. Phase 1: schema + 4 tools (simulate, validate, list,
schedule status). Phase 2: wire into scheduler / dream_cycle.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH = get_db_path()
VALID_SEED = {"entity", "memory", "event"}
VALID_STATE = {"pending", "corroborated", "falsified", "expired"}


def _db(): return open_db(str(DB_PATH))
def _rows(r: Iterable[sqlite3.Row]): return [dict(x) for x in r]


def tool_dmn_simulate(
    agent_id: str,
    seed_type: str = "entity",
    seed_id: int | None = None,
    scope: str | None = None,
    scenario: str | None = None,
    plausibility: float = 0.5,
    novelty: float = 0.5,
    utility: float = 0.5,
    speculative_content: str | None = None,
    triggered_by: str = "manual",
    **kw: Any,
) -> dict[str, Any]:
    """Record a DMN simulation + (optional) quarantined speculative memory.

    The actual LLM-conditioned rollout is the caller's responsibility;
    this tool persists the result. Composite score = mean(plausibility,
    novelty, utility) — used by `dmn_speculative_list` ranking.
    """
    if seed_type not in VALID_SEED:
        return {"ok": False, "error": f"seed_type must be one of {sorted(VALID_SEED)}"}
    if seed_id is None:
        return {"ok": False, "error": "seed_id is required"}
    if not scenario:
        return {"ok": False, "error": "scenario is required"}
    p = max(0.0, min(1.0, float(plausibility)))
    n = max(0.0, min(1.0, float(novelty)))
    u = max(0.0, min(1.0, float(utility)))
    composite = (p + n + u) / 3.0

    db = _db()
    try:
        cur = db.execute(
            """
            INSERT INTO dmn_simulations
              (agent_id, seed_type, seed_id, scope, scenario,
               plausibility, novelty, utility, composite_score, triggered_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, seed_type, int(seed_id), scope, scenario,
             p, n, u, composite, triggered_by),
        )
        sim_id = cur.lastrowid
        spec_id = None
        if speculative_content:
            sc = db.execute(
                """
                INSERT INTO dmn_speculative_memories
                  (simulation_id, agent_id, content, scope,
                   confidence, expires_at)
                VALUES (?, ?, ?, ?, ?,
                        strftime('%Y-%m-%dT%H:%M:%S','now','+30 days'))
                """,
                (sim_id, agent_id, speculative_content, scope, 0.3),
            )
            spec_id = sc.lastrowid
        db.commit()
        return {"ok": True, "simulation_id": sim_id,
                "speculative_memory_id": spec_id, "composite_score": composite}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_dmn_validate(
    speculative_id: int,
    outcome: str,
    validated_against_event_id: int | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Flip a speculative memory's state. If 'corroborated', it becomes a
    candidate for promotion to the live `memories` table (Phase 2 wiring)."""
    if outcome not in VALID_STATE:
        return {"ok": False, "error": f"outcome must be one of {sorted(VALID_STATE)}"}
    db = _db()
    try:
        cur = db.execute(
            """
            UPDATE dmn_speculative_memories
            SET validation_state = ?, validated_against_event_id = ?
            WHERE id = ?
            """,
            (outcome, validated_against_event_id, int(speculative_id)),
        )
        db.commit()
        return {"ok": True, "speculative_id": int(speculative_id),
                "validation_state": outcome, "rows_updated": cur.rowcount}
    finally:
        db.close()


def tool_dmn_speculative_list(
    agent_id: str | None = None,
    validation_state: str = "pending",
    scope: str | None = None,
    limit: int = 20,
    **kw: Any,
) -> dict[str, Any]:
    """Read-only inspection of quarantined speculative memories — the only
    way agents see DMN output."""
    if validation_state not in VALID_STATE:
        return {"ok": False, "error": f"validation_state must be one of {sorted(VALID_STATE)}"}
    db = _db()
    try:
        where = ["validation_state = ?"]
        params: list[Any] = [validation_state]
        if agent_id:
            where.append("agent_id = ?"); params.append(agent_id)
        if scope:
            where.append("scope = ?"); params.append(scope)
        params.append(max(1, min(int(limit), 100)))
        rows = db.execute(
            f"SELECT * FROM dmn_speculative_memories WHERE "
            f"{' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",  # nosec B608
            params,
        ).fetchall()
        return {"ok": True, "validation_state": validation_state, "items": _rows(rows)}
    finally:
        db.close()


def tool_dmn_schedule_status(agent_id: str | None = None, **kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        if agent_id:
            row = db.execute(
                "SELECT * FROM dmn_schedule WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if not row:
                return {"ok": True, "agent_id": agent_id, "scheduled": False}
            return {"ok": True, "scheduled": True, **dict(row)}
        rows = db.execute("SELECT * FROM dmn_schedule").fetchall()
        # Plus aggregate sim + speculative counts
        total_sims = db.execute("SELECT COUNT(*) FROM dmn_simulations").fetchone()[0]
        spec_by_state = db.execute(
            "SELECT validation_state, COUNT(*) FROM dmn_speculative_memories GROUP BY validation_state"
        ).fetchall()
        return {"ok": True, "schedules": _rows(rows),
                "total_simulations": total_sims,
                "speculative_by_state": [{"state": s, "n": n} for s, n in spec_by_state]}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(name="dmn_simulate",
         description="Record a DMN simulation + optional quarantined speculative memory. "
                     "Caller supplies the LLM-conditioned scenario + scoring.",
         inputSchema={"type": "object", "properties": {
             "agent_id": {"type": "string"},
             "seed_type": {"type": "string", "enum": sorted(VALID_SEED)},
             "seed_id": {"type": "integer"},
             "scope": {"type": "string"},
             "scenario": {"type": "string"},
             "plausibility": {"type": "number", "default": 0.5},
             "novelty": {"type": "number", "default": 0.5},
             "utility": {"type": "number", "default": 0.5},
             "speculative_content": {"type": "string"},
             "triggered_by": {"type": "string", "default": "manual"},
         }, "required": ["agent_id", "seed_id", "scenario"]}),
    Tool(name="dmn_validate",
         description="Flip a speculative memory's state (corroborated / falsified / expired).",
         inputSchema={"type": "object", "properties": {
             "speculative_id": {"type": "integer"},
             "outcome": {"type": "string", "enum": sorted(VALID_STATE)},
             "validated_against_event_id": {"type": "integer"},
         }, "required": ["speculative_id", "outcome"]}),
    Tool(name="dmn_speculative_list",
         description="List quarantined speculative memories. Filter by validation_state, agent, scope.",
         inputSchema={"type": "object", "properties": {
             "agent_id": {"type": "string"},
             "validation_state": {"type": "string", "enum": sorted(VALID_STATE), "default": "pending"},
             "scope": {"type": "string"},
             "limit": {"type": "integer", "default": 20},
         }}),
    Tool(name="dmn_schedule_status",
         description="Inspect DMN schedule rows + aggregate counts.",
         inputSchema={"type": "object", "properties": {
             "agent_id": {"type": "string"},
         }}),
]
_DMN_TOOLS = {"dmn_simulate": tool_dmn_simulate, "dmn_validate": tool_dmn_validate,
              "dmn_speculative_list": tool_dmn_speculative_list,
              "dmn_schedule_status": tool_dmn_schedule_status}
DISPATCH = {n: (lambda _f=f, **kw: _f(**kw)) for n, f in _DMN_TOOLS.items()}


def register_tools(): return TOOLS, DISPATCH
