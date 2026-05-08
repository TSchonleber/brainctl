"""brainctl MCP tools — cognitive profile + special-interest tagging + sensory affect.

Five MCP tools, all backed by the migration-052 schema additions:

  cognition_list           list built-in cognitive profiles
  cognition_show           show one profile's tunables
  cognition_set            set an agent's cognitive_profile column
  interest_add             tag an entity as a special interest (monotropism)
  interest_list            list tagged special interests
  affect_log_sensory       write an affect_log row carrying per-channel sensory_dimensions

These thin-wrap the same code paths the CLI uses (commands/cognition.py +
the cognitive_profile module). Keeping the MCP surface and CLI surface
behaviorally identical means autistic-profile tunables apply uniformly
whether the agent shells out or calls the tool.

See docs/AUTISTIC_BRAIN.md for the design and citations.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.types import Tool

from agentmemory.paths import get_db_path
from agentmemory.lib.mcp_helpers import open_db

DB_PATH: Path = get_db_path()


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def cognition_list() -> dict:
    from agentmemory.cognitive_profile import list_profiles
    return {"profiles": list_profiles()}


def cognition_show(name: str) -> dict:
    from agentmemory.cognitive_profile import get_profile, VALID_PROFILES
    if name not in VALID_PROFILES:
        return {
            "error": f"Unknown profile {name!r}",
            "valid": sorted(VALID_PROFILES),
        }
    return {"profile": get_profile(name)}


def cognition_set(agent_id: str, profile: str) -> dict:
    from agentmemory.cognitive_profile import set_agent_profile, VALID_PROFILES
    if profile not in VALID_PROFILES:
        return {
            "error": f"Unknown profile {profile!r}",
            "valid": sorted(VALID_PROFILES),
        }
    db = _db()
    try:
        prev = db.execute(
            "SELECT cognitive_profile FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if not prev:
            return {"error": f"Agent {agent_id!r} not found"}
        prev_name = prev[0] if isinstance(prev, tuple) else prev["cognitive_profile"]
        try:
            set_agent_profile(db, agent_id, profile)
        except ValueError as exc:
            return {"error": str(exc)}
        return {
            "agent_id": agent_id,
            "profile": profile,
            "previous_profile": prev_name,
            "ok": True,
        }
    finally:
        db.close()


def interest_add(entity: str, strength: float = 0.75) -> dict:
    """Mark an entity as a special interest. `entity` is id or unique name."""
    if not (0.0 <= strength <= 1.0):
        return {"error": f"strength must be in [0.0, 1.0], got {strength}"}
    db = _db()
    try:
        if entity.isdigit():
            row = db.execute(
                "SELECT id, name FROM entities WHERE id = ? AND retired_at IS NULL",
                (int(entity),),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT id, name FROM entities "
                "WHERE name = ? AND retired_at IS NULL "
                "ORDER BY updated_at DESC LIMIT 1",
                (entity,),
            ).fetchone()
        if not row:
            return {"error": f"Entity {entity!r} not found"}
        db.execute(
            "UPDATE entities SET special_interest = 1, interest_strength = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (strength, row["id"]),
        )
        db.commit()
        return {
            "ok": True,
            "entity_id": row["id"],
            "entity_name": row["name"],
            "special_interest": True,
            "interest_strength": strength,
        }
    finally:
        db.close()


def interest_list(scope: str | None = None, limit: int = 50) -> dict:
    db = _db()
    try:
        sql = (
            "SELECT id, name, entity_type, scope, interest_strength "
            "FROM entities "
            "WHERE special_interest = 1 AND retired_at IS NULL"
        )
        params: list = []
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        sql += " ORDER BY interest_strength DESC, updated_at DESC LIMIT ?"
        params.append(int(limit))
        rows = db.execute(sql, params).fetchall()
        return {
            "interests": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        db.close()


def affect_log_sensory(
    agent_id: str,
    sensory_dimensions: dict | None = None,
    valence: float = 0.0,
    arousal: float = 0.0,
    dominance: float = 0.0,
    affect_label: str | None = None,
    trigger: str | None = None,
) -> dict:
    """Write an affect_log row that carries per-channel sensory_dimensions.

    sensory_dimensions is a dict like
        {"auditory": 0.9, "visual": 0.4, "tactile": 0.7,
         "proprioceptive": 0.2, "interoceptive": 0.5}
    Each value should be in [0.0, 1.0]. Missing channels default to 0.

    `sensory_load` is computed as the max over channels — a single composite
    that the autistic profile's `sensory_overload_threshold` can be checked
    against without unpacking the JSON. When the threshold is crossed, an
    accompanying `sensory_overload` event is emitted.
    """
    from agentmemory.cognitive_profile import get_agent_profile

    channels = sensory_dimensions or {}
    # Coerce + clip
    norm: dict[str, float] = {}
    for k, v in channels.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        norm[k] = max(0.0, min(1.0, f))

    sensory_load = max(norm.values()) if norm else 0.0

    db = _db()
    try:
        profile = get_agent_profile(db, agent_id)
        threshold = profile.get("sensory_overload_threshold")

        now = _now_iso()
        cur = db.execute(
            "INSERT INTO affect_log "
            "(agent_id, valence, arousal, dominance, affect_label, "
            " trigger, source, metadata, created_at, "
            " sensory_load, sensory_dimensions) "
            "VALUES (?, ?, ?, ?, ?, ?, 'sensory', ?, ?, ?, ?)",
            (
                agent_id,
                float(valence), float(arousal), float(dominance),
                affect_label, trigger,
                json.dumps({"sensory": True, "channels": list(norm.keys())}),
                now,
                sensory_load,
                json.dumps(norm) if norm else None,
            ),
        )
        affect_id = cur.lastrowid

        overloaded = False
        event_id: int | None = None
        if threshold is not None and sensory_load > float(threshold):
            ev = db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
                "VALUES (?, 'sensory_overload', ?, ?, ?)",
                (
                    agent_id,
                    f"Sensory load {sensory_load:.2f} exceeded threshold {threshold:.2f}",
                    json.dumps({
                        "affect_log_id": affect_id,
                        "sensory_load": sensory_load,
                        "threshold": threshold,
                        "channels": norm,
                    }),
                    now,
                ),
            )
            event_id = ev.lastrowid
            overloaded = True

        db.commit()
        return {
            "ok": True,
            "affect_log_id": affect_id,
            "sensory_load": round(sensory_load, 4),
            "threshold": threshold,
            "overloaded": overloaded,
            "overload_event_id": event_id,
            "profile": profile.get("name"),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MCP Tool surface
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="cognition_list",
        description=(
            "List built-in cognitive profiles ('neurotypical', 'autistic'). "
            "Each profile retunes the W(m) gate, AGM threshold, Bayesian recall priors, "
            "and retrieval rerank without forking the codepaths."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="cognition_show",
        description=(
            "Show the full tunables dict for one cognitive profile. Useful for "
            "inspecting why an agent's gate behavior changed after `cognition_set`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Profile name"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="cognition_set",
        description=(
            "Set an agent's cognitive_profile. The autistic profile applies "
            "HIPPEA-grounded retuning (high prediction-error precision, weaker "
            "Bayesian priors, contradiction preservation, monotropic focus boost). "
            "See docs/AUTISTIC_BRAIN.md for the citations and parameter map."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "profile": {
                    "type": "string",
                    "enum": ["neurotypical", "autistic"],
                },
            },
            "required": ["agent_id", "profile"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="interest_add",
        description=(
            "Tag an entity as a special interest (monotropism). The interest "
            "gets a retention multiplier and a retrieval boost when the agent's "
            "cognitive_profile has monotropic_focus_boost > 1.0 (autistic = 2.5×). "
            "No effect under the neurotypical profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string",
                           "description": "Entity id (numeric) or unique name"},
                "strength": {"type": "number",
                             "description": "Interest strength 0.0–1.0",
                             "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["entity"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="interest_list",
        description="List entities tagged as special interests, ordered by strength.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Filter by scope"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="affect_log_sensory",
        description=(
            "Write an affect_log row carrying per-channel sensory_dimensions "
            "(auditory, visual, tactile, proprioceptive, interoceptive). The "
            "max channel value becomes sensory_load; if the agent's cognitive "
            "profile defines sensory_overload_threshold and sensory_load "
            "exceeds it, a sensory_overload event is also emitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "sensory_dimensions": {
                    "type": "object",
                    "description": (
                        "Per-channel load in [0.0, 1.0]. Recognized keys: "
                        "auditory, visual, tactile, proprioceptive, interoceptive."
                    ),
                    "additionalProperties": {"type": "number"},
                },
                "valence":    {"type": "number"},
                "arousal":    {"type": "number"},
                "dominance":  {"type": "number"},
                "affect_label": {"type": "string"},
                "trigger":      {"type": "string"},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    ),
]


# Dispatchers use the kwargs-style shape that _invoke_dispatch_fn expects
# (`fn(agent_id=..., **kw)`). For tools that don't take agent_id, the
# unused kwarg is silently absorbed.

def _disp_cognition_list(agent_id: str | None = None, **_: object) -> dict:
    return cognition_list()


def _disp_cognition_show(agent_id: str | None = None, name: str = "", **_: object) -> dict:
    return cognition_show(name=name)


def _disp_cognition_set(
    agent_id: str | None = None,
    profile: str = "",
    **kw: object,
) -> dict:
    # Allow either explicit agent_id in arguments or the harness-injected one.
    target = kw.get("agent_id") or agent_id  # type: ignore[assignment]
    if not target:
        return {"error": "agent_id required"}
    return cognition_set(agent_id=str(target), profile=profile)


def _disp_interest_add(
    agent_id: str | None = None,
    entity: str = "",
    strength: float = 0.75,
    **_: object,
) -> dict:
    return interest_add(entity=entity, strength=float(strength))


def _disp_interest_list(
    agent_id: str | None = None,
    scope: str | None = None,
    limit: int = 50,
    **_: object,
) -> dict:
    return interest_list(scope=scope, limit=int(limit))


def _disp_affect_log_sensory(
    agent_id: str | None = None,
    sensory_dimensions: dict | None = None,
    valence: float = 0.0,
    arousal: float = 0.0,
    dominance: float = 0.0,
    affect_label: str | None = None,
    trigger: str | None = None,
    **kw: object,
) -> dict:
    target = kw.get("agent_id") or agent_id  # type: ignore[assignment]
    if not target:
        return {"error": "agent_id required"}
    return affect_log_sensory(
        agent_id=str(target),
        sensory_dimensions=sensory_dimensions,
        valence=float(valence),
        arousal=float(arousal),
        dominance=float(dominance),
        affect_label=affect_label,
        trigger=trigger,
    )


DISPATCH: dict = {
    "cognition_list":     _disp_cognition_list,
    "cognition_show":     _disp_cognition_show,
    "cognition_set":      _disp_cognition_set,
    "interest_add":       _disp_interest_add,
    "interest_list":      _disp_interest_list,
    "affect_log_sensory": _disp_affect_log_sensory,
}
