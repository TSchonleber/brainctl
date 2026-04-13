"""brainctl MCP tools — reasoning & inference."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import now_iso, open_db, rows_to_list

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# Import shared helpers from _impl rather than duplicating them
from agentmemory._impl import (
    _reason_l1_search,
    _reason_l2_expand,
    _reason_l3_infer,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AIL_FREE_ENERGY_THRESHOLD = 0.15  # (1-confidence)*importance must exceed this to flag a gap

_FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^]')


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


_now = now_iso
_rows_to_list = rows_to_list


def _sanitize_fts_query(query: str) -> str:
    """Remove FTS5 special characters to prevent syntax errors."""
    cleaned = _FTS5_SPECIAL.sub(" ", query or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _log_access(conn, agent_id, action, target_table=None, target_id=None, query=None, result_count=None):
    try:
        conn.execute(
            "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count) "
            "VALUES (?,?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query, result_count),
        )
    except Exception:
        pass  # access_log missing in minimal schemas — not fatal


def _ensure_agent(conn, agent_id: str) -> None:
    """Auto-register agent if missing to avoid FK violations."""
    if not agent_id:
        return
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES (?, ?, 'mcp', 'active', ?, ?)",
            (agent_id, agent_id, _now(), _now()),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_reason(agent_id: str = "unknown", query: str = "", limit: int = 10, hops: int = 2) -> dict:
    """L1+L2: hybrid search + structural graph expansion."""
    if not query:
        return {"ok": False, "error": "query is required"}
    try:
        t0 = time.monotonic()
        conn = _db()
        _ensure_agent(conn, agent_id)

        l1_memories, l1_events = _reason_l1_search(conn, query, limit=limit)
        l2_expanded, _ = _reason_l2_expand(conn, l1_memories, l1_events, hops=hops, top_k=15)

        latency_ms = round((time.monotonic() - t0) * 1000)
        _log_access(conn, agent_id, "reason", query=query, result_count=len(l1_memories) + len(l2_expanded))
        conn.commit()
        conn.close()

        return {
            "ok": True,
            "query": query,
            "tier": "L2-structural",
            "l1_memories": l1_memories,
            "l1_events": l1_events,
            "l2_expansions": l2_expanded,
            "provenance": {
                "l1_memory_count": len(l1_memories),
                "l1_event_count": len(l1_events),
                "l2_expansion_count": len(l2_expanded),
            },
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_infer(
    agent_id: str = "unknown",
    query: str = "",
    limit: int = 10,
    hops: int = 2,
    min_confidence: float = 0.0,
) -> dict:
    """L1+L2+L3: full neuro-symbolic inference."""
    if not query:
        return {"ok": False, "error": "query is required"}
    try:
        t0 = time.monotonic()
        conn = _db()
        _ensure_agent(conn, agent_id)

        l1_memories, l1_events = _reason_l1_search(conn, query, limit=limit)
        l2_expanded, _ = _reason_l2_expand(conn, l1_memories, l1_events, hops=hops, top_k=15)
        inference, all_evidence, matched_policies, rules_evaluated = _reason_l3_infer(
            conn, query, l1_memories, l2_expanded, agent_id=agent_id, min_confidence=min_confidence
        )

        latency_ms = round((time.monotonic() - t0) * 1000)
        _log_access(conn, agent_id, "infer", query=query, result_count=len(all_evidence))
        conn.commit()
        conn.close()

        return {
            "ok": True,
            "query": query,
            "inference": inference,
            "evidence": all_evidence[:10],
            "matched_policies": matched_policies,
            "provenance": {
                "l1_results": len(l1_memories) + len(l1_events),
                "l2_expansions": len(l2_expanded),
                "policy_rules_evaluated": rules_evaluated,
                "policy_rules_triggered": len(matched_policies),
            },
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_infer_pretask(
    agent_id: str = "unknown",
    task_desc: str = "",
    limit: int = 10,
) -> dict:
    """Pre-task uncertainty scan: query low-confidence memories, log gaps, report free energy."""
    if not task_desc:
        return {"ok": False, "error": "task_desc is required"}
    try:
        t0 = time.monotonic()
        conn = _db()
        _ensure_agent(conn, agent_id)

        fts_q = _sanitize_fts_query(task_desc)

        # Check for existing knowledge gaps matching this task
        gap_hits = []
        try:
            first_word = fts_q.split()[0] if fts_q.split() else ""
            if first_word:
                gap_hits = _rows_to_list(conn.execute(
                    "SELECT * FROM knowledge_gaps WHERE domain LIKE ? OR description LIKE ? "
                    "ORDER BY importance DESC LIMIT ?",
                    (f"%{first_word}%", f"%{task_desc[:80]}%", limit)
                ).fetchall())
        except Exception:
            gap_hits = []

        # Find low-confidence memories matching this task
        memories = []
        if fts_q:
            try:
                mem_rows = conn.execute(
                    "SELECT m.* FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.retired_at IS NULL AND m.confidence < 0.7 "
                    "ORDER BY rank LIMIT ?",
                    (fts_q, limit * 3)
                ).fetchall()
                memories = _rows_to_list(mem_rows)
            except Exception:
                memories = []

        # Compute free energy per memory and filter by threshold
        uncertainty_gaps = []
        for m in memories:
            conf = m.get("confidence") or 1.0
            imp = m.get("importance") or 0.5
            fe = round((1.0 - conf) * imp, 4)
            if fe >= _AIL_FREE_ENERGY_THRESHOLD:
                uncertainty_gaps.append({
                    "memory_id": m["id"],
                    "topic": (m.get("content") or "")[:120].replace("\n", " "),
                    "confidence": conf,
                    "importance": imp,
                    "free_energy": fe,
                    "scope": m.get("scope"),
                    "category": m.get("category"),
                })
        uncertainty_gaps.sort(key=lambda g: -g["free_energy"])
        uncertainty_gaps = uncertainty_gaps[:limit]

        # Log each gap to agent_uncertainty_log
        now = _now()
        log_ids = []
        for gap in uncertainty_gaps:
            try:
                cur = conn.execute(
                    "INSERT INTO agent_uncertainty_log (agent_id, task_desc, gap_topic, free_energy, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (agent_id, task_desc[:500], gap["topic"][:200], gap["free_energy"], now)
                )
                log_ids.append(cur.lastrowid)
            except Exception:
                pass

        latency_ms = round((time.monotonic() - t0) * 1000)
        _log_access(conn, agent_id, "infer-pretask", "memories", query=task_desc[:200], result_count=len(uncertainty_gaps))
        conn.commit()
        conn.close()

        return {
            "ok": True,
            "task_desc": task_desc,
            "agent_id": agent_id,
            "uncertainty_gaps": uncertainty_gaps,
            "knowledge_gaps_matched": len(gap_hits),
            "log_ids": log_ids,
            "summary": {
                "total_gaps_found": len(uncertainty_gaps),
                "max_free_energy": uncertainty_gaps[0]["free_energy"] if uncertainty_gaps else 0.0,
                "avg_free_energy": round(
                    sum(g["free_energy"] for g in uncertainty_gaps) / len(uncertainty_gaps), 4
                ) if uncertainty_gaps else 0.0,
                "latency_ms": latency_ms,
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_infer_gapfill(
    agent_id: str = "unknown",
    task_desc: str = "",
    content: str | None = None,
) -> dict:
    """Gap fill after task: resolve open uncertainty log entries, optionally create memory."""
    if not task_desc:
        return {"ok": False, "error": "task_desc is required"}
    try:
        conn = _db()
        _ensure_agent(conn, agent_id)
        now = _now()

        # Find open gaps matching this task
        open_gaps = conn.execute(
            "SELECT * FROM agent_uncertainty_log WHERE agent_id = ? AND resolved_at IS NULL "
            "AND task_desc LIKE ? ORDER BY free_energy DESC LIMIT 20",
            (agent_id, f"%{task_desc[:50]}%")
        ).fetchall()

        if not open_gaps:
            open_gaps = conn.execute(
                "SELECT * FROM agent_uncertainty_log WHERE agent_id = ? AND resolved_at IS NULL "
                "AND created_at > datetime('now', '-24 hours') ORDER BY free_energy DESC LIMIT 10",
                (agent_id,)
            ).fetchall()

        # Optionally create a lesson memory from the content
        memory_id = None
        if content:
            try:
                cur = conn.execute(
                    "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
                    "VALUES (?, 'lesson', 'global', ?, 0.80, ?, ?)",
                    (agent_id, content[:2000], now, now)
                )
                memory_id = cur.lastrowid
            except Exception:
                pass

        # Mark matched gaps as resolved
        resolved_ids = []
        for row in open_gaps:
            try:
                conn.execute(
                    "UPDATE agent_uncertainty_log SET resolved_at = ?, resolved_by = ? WHERE id = ?",
                    (now, memory_id, row["id"])
                )
                resolved_ids.append(row["id"])
            except Exception:
                pass

        _log_access(conn, agent_id, "infer-gapfill", "agent_uncertainty_log",
                    query=task_desc[:200], result_count=len(resolved_ids))
        conn.commit()
        conn.close()

        return {
            "ok": True,
            "task_desc": task_desc,
            "agent_id": agent_id,
            "resolved_gaps": resolved_ids,
            "memory_created": memory_id,
            "total_resolved": len(resolved_ids),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="reason",
        description=(
            "L1+L2 hybrid reasoning: FTS keyword search over memories and events (L1), "
            "then structural graph expansion via knowledge_edges (L2). "
            "Returns direct evidence plus graph-connected nodes with provenance chains."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier", "default": "unknown"},
                "query": {"type": "string", "description": "Reasoning query"},
                "limit": {"type": "integer", "description": "Max L1 results per source", "default": 10},
                "hops": {"type": "integer", "description": "Graph expansion hop depth", "default": 2},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="infer",
        description=(
            "L1+L2+L3 full neuro-symbolic inference: hybrid search (L1), graph expansion (L2), "
            "plus policy rule evaluation and confidence chaining over the evidence (L3). "
            "Returns a conclusion with confidence tier, evidence, and matched policy rules."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier", "default": "unknown"},
                "query": {"type": "string", "description": "Inference query"},
                "limit": {"type": "integer", "description": "Max L1 results per source", "default": 10},
                "hops": {"type": "integer", "description": "Graph expansion hop depth", "default": 2},
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum effective confidence for policy rules (0.0–1.0)",
                    "default": 0.0,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="infer_pretask",
        description=(
            "Pre-task uncertainty scan (Active Inference Layer). "
            "Finds low-confidence memories matching the task description, computes free energy "
            "(=(1-confidence)*importance), logs high-uncertainty gaps to agent_uncertainty_log, "
            "and returns a ranked list of knowledge gaps to fill before starting the task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier", "default": "unknown"},
                "task_desc": {"type": "string", "description": "Description of the upcoming task"},
                "limit": {"type": "integer", "description": "Max gaps to return", "default": 10},
            },
            "required": ["task_desc"],
        },
    ),
    Tool(
        name="infer_gapfill",
        description=(
            "Post-task gap fill (Active Inference Layer). "
            "Resolves open uncertainty log entries created by infer_pretask for the matching task. "
            "Optionally records what was learned as a new 'lesson' memory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier", "default": "unknown"},
                "task_desc": {
                    "type": "string",
                    "description": "Task description used to match open gaps (same as infer_pretask)",
                },
                "content": {
                    "type": "string",
                    "description": "Optional: what was learned; stored as a lesson memory",
                },
            },
            "required": ["task_desc"],
        },
    ),
]

DISPATCH: dict[str, Any] = {
    "reason": tool_reason,
    "infer": tool_infer,
    "infer_pretask": tool_infer_pretask,
    "infer_gapfill": tool_infer_gapfill,
}
