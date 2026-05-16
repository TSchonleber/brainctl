"""brainctl MCP tools — prefrontal cortex sub-region slots.

Four named slots per agent — dlPFC (active task / WM), vmPFC
(outcome-utility model), OFC (realized-outcome log), frontopolar
(meta-monitor). This subsystem is mostly aggregation + naming;
the underlying machinery already exists scattered across
consolidation, trust, reflexion, and neuro modules.

Phase 1: read/write the named slots + status aggregator.
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
VALID_SLOTS = {"dlpfc", "vmpfc", "ofc", "frontopolar"}


def _db(): return open_db(str(DB_PATH))
def _rows(r: Iterable[sqlite3.Row]): return [dict(x) for x in r]


def tool_pfc_slot_set(
    agent_id: str, slot: str, content: dict[str, Any] | str,
    confidence: float = 0.5, **kw: Any,
) -> dict[str, Any]:
    """Set the content of a PFC slot for an agent. Content is JSON-serialized
    if a dict; treated as raw string otherwise."""
    if slot not in VALID_SLOTS:
        return {"ok": False, "error": f"slot must be one of {sorted(VALID_SLOTS)}"}
    body = json.dumps(content) if isinstance(content, dict) else str(content)
    db = _db()
    try:
        db.execute(
            "INSERT INTO pfc_slots (agent_id, slot, content, confidence) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(agent_id, slot) DO UPDATE SET "
            "content=excluded.content, confidence=excluded.confidence, "
            "last_updated=strftime('%Y-%m-%dT%H:%M:%S','now')",
            (agent_id, slot, body, max(0.0, min(1.0, float(confidence)))),
        )
        db.commit()
        return {"ok": True, "agent_id": agent_id, "slot": slot}
    finally:
        db.close()


def tool_pfc_slot_get(agent_id: str, slot: str | None = None, **kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        if slot:
            if slot not in VALID_SLOTS:
                return {"ok": False, "error": f"slot must be one of {sorted(VALID_SLOTS)}"}
            row = db.execute(
                "SELECT * FROM pfc_slots WHERE agent_id=? AND slot=?",
                (agent_id, slot),
            ).fetchone()
            if not row:
                return {"ok": True, "agent_id": agent_id, "slot": slot, "exists": False}
            d = dict(row)
            try:
                d["content_parsed"] = json.loads(d["content"])
            except Exception:
                d["content_parsed"] = d["content"]
            return {"ok": True, "exists": True, **d}
        rows = db.execute(
            "SELECT * FROM pfc_slots WHERE agent_id=? ORDER BY slot", (agent_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["content_parsed"] = json.loads(d["content"])
            except Exception:
                d["content_parsed"] = d["content"]
            out.append(d)
        return {"ok": True, "agent_id": agent_id, "slots": out}
    finally:
        db.close()


def tool_pfc_status(agent_id: str | None = None, **kw: Any) -> dict[str, Any]:
    """Cross-sub-region snapshot for an agent. Aggregates from existing
    machinery: dlPFC=active task, vmPFC=top-trusted memories + dopamine,
    OFC=last outcome rate, frontopolar=open gaps."""
    db = _db()
    try:
        if not agent_id:
            agg = db.execute(
                "SELECT slot, COUNT(*) AS n, ROUND(AVG(confidence), 4) AS mean_conf "
                "FROM pfc_slots GROUP BY slot ORDER BY slot"
            ).fetchall()
            return {"ok": True, "by_slot": _rows(agg),
                    "total": db.execute("SELECT COUNT(*) FROM pfc_slots").fetchone()[0]}

        # Aggregate per-agent — pull from existing tables where available
        slots = {r[0]: dict(r) for r in db.execute(
            "SELECT slot, content, confidence, last_updated FROM pfc_slots WHERE agent_id=?",
            (agent_id,),
        ).fetchall()}
        # dlPFC supplement: active task list (if tasks table exists)
        active_task_count = 0
        try:
            active_task_count = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='in_progress' AND "
                "(owner = ? OR assigned_to = ?)", (agent_id, agent_id),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        # vmPFC supplement: mean trust of recent memories
        try:
            mean_trust = db.execute(
                "SELECT ROUND(AVG(trust_score), 4) FROM memories "
                "WHERE agent_id=? AND created_at > datetime('now','-7 days')",
                (agent_id,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            mean_trust = None
        # frontopolar supplement: open uncertainty/gap count
        open_gaps = 0
        try:
            open_gaps = db.execute(
                "SELECT COUNT(*) FROM agent_uncertainty_log "
                "WHERE agent_id=? AND resolved_at IS NULL", (agent_id,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return {
            "ok": True, "agent_id": agent_id,
            "slots": slots,
            "dlpfc_supplement": {"active_tasks": active_task_count},
            "vmpfc_supplement": {"mean_trust_recent": mean_trust},
            "frontopolar_supplement": {"open_uncertainty_gaps": open_gaps},
        }
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(name="pfc_slot_set",
         description="Set a PFC sub-region slot (dlpfc/vmpfc/ofc/frontopolar) for an agent.",
         inputSchema={"type": "object", "properties": {
             "agent_id": {"type": "string"},
             "slot": {"type": "string", "enum": sorted(VALID_SLOTS)},
             "content": {},
             "confidence": {"type": "number", "default": 0.5},
         }, "required": ["agent_id", "slot", "content"]}),
    Tool(name="pfc_slot_get",
         description="Read a single slot or all slots for an agent.",
         inputSchema={"type": "object", "properties": {
             "agent_id": {"type": "string"},
             "slot": {"type": "string", "enum": sorted(VALID_SLOTS)},
         }, "required": ["agent_id"]}),
    Tool(name="pfc_status",
         description="Aggregated PFC snapshot: per-slot stats globally, or per-agent enriched "
                     "with supplemental signals from existing tables (active tasks, mean trust, "
                     "open uncertainty gaps).",
         inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}}),
]
_PFC_TOOLS = {"pfc_slot_set": tool_pfc_slot_set, "pfc_slot_get": tool_pfc_slot_get,
              "pfc_status": tool_pfc_status}
DISPATCH = {n: (lambda _f=f, **kw: _f(**kw)) for n, f in _PFC_TOOLS.items()}


def register_tools(): return TOOLS, DISPATCH
