"""brainctl MCP tools — replay priority and reconsolidation window.

Neuroscience grounding:
- Sharp Wave Ripples (SWR) dynamically tag memories for consolidation based on salience,
  not static importance. replay_priority accumulates through use.
- Memory reconsolidation (Nader 2000, Ecker 2015): retrieval opens a ~20-min lability
  window during which the memory can be updated without creating a new trace.
  High prediction error at retrieval = strong reconsolidation trigger.
- Agent-scoped lability: only the agent that opened the window can reconsolidate,
  preventing race conditions in concurrent multi-agent access.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

_LABILITY_MINUTES = 20
_LABILITY_THRESHOLD = 0.35   # cosine distance above which lability opens
_HIGH_SALIENCE_THRESHOLD = 0.8  # salience score above which ripple_tags increments


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _is_labile(row: dict, agent_id: str) -> tuple[bool, str]:
    """Return (is_labile, reason) for a memory row."""
    labile_until = row.get("labile_until")
    labile_agent = row.get("labile_agent_id")

    if not labile_until:
        return False, "no lability window open"

    try:
        exp = datetime.fromisoformat(labile_until.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now > exp:
            return False, "lability window expired"
    except Exception:
        return False, "invalid labile_until timestamp"

    if labile_agent and labile_agent != agent_id:
        return False, f"lability opened by different agent ({labile_agent})"

    return True, "lability window active"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_replay_boost(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    delta: float = 0.5,
    scope: str | None = None,
    **kw,
) -> dict:
    """Manually boost the replay_priority of one or more memories.

    Use this to manually flag memories for priority consolidation — analogous to
    tagging a hippocampal trace for next-cycle Sharp Wave Ripple replay.

    Args:
        memory_id: Specific memory ID to boost (mutually exclusive with scope).
        delta: Amount to add to replay_priority (clamped to [0.01, 5.0]).
        scope: Boost all active memories in this scope (e.g. 'project:foo').
    """
    if memory_id is None and scope is None:
        return {"ok": False, "error": "Provide memory_id or scope"}
    if memory_id is not None and scope is not None:
        return {"ok": False, "error": "Provide memory_id or scope, not both"}

    delta = max(0.01, min(5.0, float(delta)))

    try:
        db = _db()
        if memory_id is not None:
            row = db.execute(
                "SELECT id, replay_priority, retired_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": f"Memory {memory_id} not found"}
            if row["retired_at"]:
                return {"ok": False, "error": f"Memory {memory_id} is retired"}
            db.execute(
                "UPDATE memories SET replay_priority = MIN(10.0, replay_priority + ?) WHERE id = ?",
                (delta, memory_id),
            )
            new_priority = min(10.0, (row["replay_priority"] or 0.0) + delta)
            db.commit()
            return {
                "ok": True,
                "memory_id": memory_id,
                "delta": delta,
                "new_replay_priority": round(new_priority, 4),
            }
        else:
            result = db.execute(
                "UPDATE memories SET replay_priority = MIN(10.0, replay_priority + ?) "
                "WHERE retired_at IS NULL AND scope = ?",
                (delta, scope),
            )
            affected = db.execute("SELECT changes()").fetchone()[0]
            db.commit()
            return {
                "ok": True,
                "scope": scope,
                "delta": delta,
                "affected": affected,
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_replay_queue(
    agent_id: str = "mcp-client",
    limit: int = 20,
    min_priority: float = 0.1,
    scope: str | None = None,
    **kw,
) -> dict:
    """Return memories sorted by replay_priority for consolidation scheduling.

    Returns candidates the consolidation scheduler should process next, ordered
    by accumulated replay priority (highest first). Mirrors the hippocampal
    scheduler's SWR-driven replay queue.

    Args:
        limit: Max memories to return (default 20, max 100).
        min_priority: Exclude memories below this threshold (default 0.1).
        scope: Filter to a specific scope (e.g. 'project:foo').
    """
    limit = max(1, min(100, int(limit)))
    min_priority = float(min_priority)

    try:
        db = _db()
        where_parts = ["retired_at IS NULL", "replay_priority >= ?"]
        params: list = [min_priority]
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        where = " AND ".join(where_parts)

        rows = db.execute(
            f"SELECT id, content, category, scope, confidence, "
            f"replay_priority, ripple_tags, recalled_count, created_at, last_recalled_at "
            f"FROM memories WHERE {where} "
            f"ORDER BY replay_priority DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["content_preview"] = (d.get("content") or "")[:100]
            items.append(d)

        return {
            "ok": True,
            "count": len(items),
            "min_priority": min_priority,
            "scope": scope,
            "queue": items,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_reconsolidation_check(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    **kw,
) -> dict:
    """Check whether a memory is currently in its reconsolidation (lability) window.

    Retrieval opens a lability window when the retrieval context diverges significantly
    from the stored memory content (prediction error > threshold). During this window,
    the memory can be updated via reconsolidate() without creating a new trace.

    Returns lability status, time remaining, and the prediction error that opened it.
    """
    if memory_id is None:
        return {"ok": False, "error": "memory_id is required"}

    try:
        db = _db()
        row = db.execute(
            "SELECT id, content, labile_until, labile_agent_id, retrieval_prediction_error, "
            "recalled_count, confidence FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found"}

        d = dict(row)
        labile, reason = _is_labile(d, agent_id)

        seconds_remaining = 0
        if labile and d.get("labile_until"):
            try:
                exp = datetime.fromisoformat(d["labile_until"].replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                seconds_remaining = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass

        return {
            "ok": True,
            "memory_id": memory_id,
            "labile": labile,
            "reason": reason,
            "labile_until": d.get("labile_until"),
            "labile_agent_id": d.get("labile_agent_id"),
            "retrieval_prediction_error": d.get("retrieval_prediction_error"),
            "seconds_remaining": seconds_remaining,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_reconsolidate(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    new_content: str | None = None,
    merge_mode: str = "replace",
    **kw,
) -> dict:
    """Update a memory during its reconsolidation (lability) window.

    During the lability window opened by high-prediction-error retrieval, the memory
    can be updated without creating a new trace — it merges the new information into
    the existing memory, just as biological reconsolidation incorporates retrieved-plus-
    updated content back into long-term storage.

    Only the agent that opened the lability window can reconsolidate.
    After reconsolidation the window is closed (labile_until set to NULL).

    Args:
        memory_id: ID of the memory to update.
        new_content: Updated memory content.
        merge_mode: 'replace' (overwrite content) or 'append' (append to existing).
    """
    if memory_id is None:
        return {"ok": False, "error": "memory_id is required"}
    if not new_content or not new_content.strip():
        return {"ok": False, "error": "new_content is required"}
    if merge_mode not in ("replace", "append"):
        return {"ok": False, "error": "merge_mode must be 'replace' or 'append'"}

    try:
        db = _db()
        row = db.execute(
            "SELECT id, content, labile_until, labile_agent_id, retrieval_prediction_error, "
            "retired_at FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found"}
        if row["retired_at"]:
            return {"ok": False, "error": f"Memory {memory_id} is retired"}

        d = dict(row)
        labile, reason = _is_labile(d, agent_id)
        if not labile:
            return {
                "ok": False,
                "error": f"Memory {memory_id} is not labile: {reason}",
                "labile_until": d.get("labile_until"),
                "hint": "Reconsolidation requires an open lability window. Retrieve the memory first.",
            }

        old_content = d["content"] or ""
        if merge_mode == "append":
            merged_content = f"{old_content}\n\n[Reconsolidated {_now()}] {new_content.strip()}"
        else:
            merged_content = new_content.strip()

        now = _now_sql()
        db.execute(
            "UPDATE memories SET content = ?, updated_at = ?, "
            "labile_until = NULL, labile_agent_id = NULL, retrieval_prediction_error = NULL "
            "WHERE id = ?",
            (merged_content, now, memory_id),
        )
        db.commit()

        return {
            "ok": True,
            "memory_id": memory_id,
            "merge_mode": merge_mode,
            "old_content_preview": old_content[:100],
            "new_content_preview": merged_content[:100],
            "prediction_error_was": d.get("retrieval_prediction_error"),
            "lability_closed": True,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_consolidation_run(
    agent_id: str = "mcp-client",
    limit: int = 50,
    min_priority: float = 0.1,
    scope: str | None = None,
    promote_threshold_ripple: int = 3,
    promote_threshold_confidence: float = 0.7,
    run_causal_mining: bool = True,
    **kw,
) -> dict:
    """Run a consolidation pass over the replay queue.

    Implements the SWR-driven offline consolidation cycle:
    1. Fetch top-N memories by replay_priority (the accumulated SWR tag score).
    2. Promote episodic→semantic for memories that have sufficient ripple endorsement
       and confidence (analogous to cortical transfer during slow-wave sleep).
    3. Optionally mine causal chains in the events table (writes direct_cause /
       transitive_cause edges to knowledge_edges).
    4. Zero out replay_priority for all processed memories, resetting the queue.

    Returns stats: processed, promoted, causal edges created/updated.

    Args:
        limit: Max memories to process per pass (default 50, max 500).
        min_priority: Ignore memories below this replay_priority (default 0.1).
        scope: Limit pass to a specific scope (e.g. 'project:foo').
        promote_threshold_ripple: Min ripple_tags for episodic→semantic promotion (default 3).
        promote_threshold_confidence: Min confidence for promotion (default 0.7).
        run_causal_mining: Whether to run mine_causal_chains after the memory pass (default True).
    """
    limit = max(1, min(500, int(limit)))
    min_priority = float(min_priority)
    promote_threshold_ripple = max(1, int(promote_threshold_ripple))
    promote_threshold_confidence = max(0.0, min(1.0, float(promote_threshold_confidence)))

    try:
        db = _db()

        # Step 1: Fetch replay queue
        where_parts = ["retired_at IS NULL", "replay_priority >= ?"]
        params: list = [min_priority]
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        where = " AND ".join(where_parts)

        rows = db.execute(
            f"SELECT id, memory_type, ripple_tags, confidence, replay_priority "
            f"FROM memories WHERE {where} ORDER BY replay_priority DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        processed_ids = [r["id"] for r in rows]

        # Step 2: Promote episodic → semantic
        promotion_ids: list[int] = []
        for r in rows:
            if (
                r["memory_type"] == "episodic"
                and (r["ripple_tags"] or 0) >= promote_threshold_ripple
                and (r["confidence"] or 0.0) >= promote_threshold_confidence
            ):
                promotion_ids.append(r["id"])

        if promotion_ids:
            now = _now_sql()
            for mid in promotion_ids:
                db.execute(
                    "UPDATE memories SET memory_type = 'semantic', updated_at = ? WHERE id = ?",
                    (now, mid),
                )

        # Step 3: Zero out replay_priority for all processed memories
        if processed_ids:
            placeholders = ",".join("?" * len(processed_ids))
            db.execute(
                f"UPDATE memories SET replay_priority = 0.0 WHERE id IN ({placeholders})",
                processed_ids,
            )

        db.commit()

        # Step 4: Mine causal chains
        causal_stats: dict = {"events_scanned": 0, "edges_created": 0, "edges_updated": 0}
        if run_causal_mining:
            from .hippocampus import mine_causal_chains
            causal_stats = mine_causal_chains(db)
            db.commit()

        db.close()

        return {
            "ok": True,
            "processed": len(processed_ids),
            "promoted_to_semantic": len(promotion_ids),
            "promotion_ids": promotion_ids[:20],
            "causal_mining": causal_stats,
            "scope": scope,
            "min_priority": min_priority,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_consolidation_stats(
    agent_id: str = "mcp-client",
    scope: str | None = None,
    **kw,
) -> dict:
    """Return consolidation health stats: replay queue depth, labile memories, ripple events.

    Args:
        scope: Limit stats to a specific scope (e.g. 'project:foo').
    """
    try:
        db = _db()
        where_active = "retired_at IS NULL"
        params_base: list = []
        if scope:
            where_active += " AND scope = ?"
            params_base = [scope]

        total = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active}", params_base
        ).fetchone()[0]

        queued = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active} AND replay_priority >= 0.1",
            params_base,
        ).fetchone()[0]

        high_priority = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active} AND replay_priority >= 2.0",
            params_base,
        ).fetchone()[0]

        avg_priority = db.execute(
            f"SELECT AVG(replay_priority) FROM memories WHERE {where_active}", params_base
        ).fetchone()[0] or 0.0

        total_ripple = db.execute(
            f"SELECT SUM(ripple_tags) FROM memories WHERE {where_active}", params_base
        ).fetchone()[0] or 0

        now_sql = _now_sql()
        labile_count = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active} "
            f"AND labile_until IS NOT NULL AND labile_until > ?",
            params_base + [now_sql],
        ).fetchone()[0]

        return {
            "ok": True,
            "scope": scope,
            "total_active_memories": total,
            "queued_for_replay": queued,
            "high_priority_replay": high_priority,
            "avg_replay_priority": round(avg_priority, 4),
            "total_ripple_tags": total_ripple,
            "currently_labile": labile_count,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="replay_boost",
        description=(
            "Manually boost the replay_priority of a memory or all memories in a scope. "
            "Analogous to manually tagging a hippocampal trace for Sharp Wave Ripple replay. "
            "Higher replay_priority = earlier processing in the consolidation queue."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "memory_id": {
                    "type": "integer",
                    "description": "Specific memory ID to boost (mutually exclusive with scope)",
                },
                "delta": {
                    "type": "number",
                    "description": "Amount to add to replay_priority (clamped to [0.01, 5.0], default 0.5)",
                    "default": 0.5,
                },
                "scope": {
                    "type": "string",
                    "description": "Boost all active memories in this scope (e.g. 'project:foo')",
                },
            },
        },
    ),
    Tool(
        name="replay_queue",
        description=(
            "Return memories sorted by replay_priority for consolidation scheduling. "
            "Highest replay_priority = most urgent consolidation candidates. "
            "Mirrors the hippocampal SWR-driven replay queue. "
            "Use this before a consolidation pass to pick what to process."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "limit": {
                    "type": "integer",
                    "description": "Max memories to return (default 20, max 100)",
                    "default": 20,
                },
                "min_priority": {
                    "type": "number",
                    "description": "Exclude memories below this replay_priority (default 0.1)",
                    "default": 0.1,
                },
                "scope": {
                    "type": "string",
                    "description": "Filter to a specific scope (e.g. 'project:foo')",
                },
            },
        },
    ),
    Tool(
        name="reconsolidation_check",
        description=(
            "Check whether a memory is currently in its reconsolidation (lability) window. "
            "A lability window opens automatically when vsearch retrieves a memory with "
            "high prediction error (cosine distance > 0.35 from query). "
            "During the window, reconsolidate() can update the memory without creating a new trace."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "memory_id": {
                    "type": "integer",
                    "description": "Memory ID to check",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="reconsolidate",
        description=(
            "Update a memory during its active reconsolidation (lability) window. "
            "Only the agent that triggered lability (via vsearch) can reconsolidate. "
            "Closes the lability window after updating. "
            "merge_mode='replace' overwrites content; 'append' adds to existing. "
            "Returns error if the window is closed or belongs to a different agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "memory_id": {
                    "type": "integer",
                    "description": "ID of the labile memory to update",
                },
                "new_content": {
                    "type": "string",
                    "description": "Updated memory content",
                },
                "merge_mode": {
                    "type": "string",
                    "enum": ["replace", "append"],
                    "description": "How to merge: 'replace' overwrites, 'append' adds below existing",
                    "default": "replace",
                },
            },
            "required": ["memory_id", "new_content"],
        },
    ),
    Tool(
        name="consolidation_run",
        description=(
            "Run a consolidation pass over the replay queue. "
            "Fetches top-N memories by replay_priority, promotes eligible episodic→semantic "
            "(ripple_tags >= threshold and confidence >= threshold), mines causal chains in "
            "the events table, then zeros out processed replay_priority scores. "
            "Implements the SWR-driven offline consolidation cycle (cortical transfer analogue)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "limit": {
                    "type": "integer",
                    "description": "Max memories to process per pass (default 50, max 500)",
                    "default": 50,
                },
                "min_priority": {
                    "type": "number",
                    "description": "Ignore memories below this replay_priority (default 0.1)",
                    "default": 0.1,
                },
                "scope": {
                    "type": "string",
                    "description": "Limit pass to a specific scope (e.g. 'project:foo')",
                },
                "promote_threshold_ripple": {
                    "type": "integer",
                    "description": "Min ripple_tags for episodic→semantic promotion (default 3)",
                    "default": 3,
                },
                "promote_threshold_confidence": {
                    "type": "number",
                    "description": "Min confidence for episodic→semantic promotion (default 0.7)",
                    "default": 0.7,
                },
                "run_causal_mining": {
                    "type": "boolean",
                    "description": "Run causal chain mining pass after memory processing (default true)",
                    "default": True,
                },
            },
        },
    ),
    Tool(
        name="consolidation_stats",
        description=(
            "Return consolidation health: replay queue depth, labile memories, ripple event totals. "
            "Use this to monitor the state of memory consolidation for an agent or scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "scope": {
                    "type": "string",
                    "description": "Limit stats to this scope (optional)",
                },
            },
        },
    ),
]

DISPATCH: dict = {
    "consolidation_run":       lambda agent_id=None, **kw: tool_consolidation_run(agent_id=agent_id or "mcp-client", **kw),
    "replay_boost":           lambda agent_id=None, **kw: tool_replay_boost(agent_id=agent_id or "mcp-client", **kw),
    "replay_queue":           lambda agent_id=None, **kw: tool_replay_queue(agent_id=agent_id or "mcp-client", **kw),
    "reconsolidation_check":  lambda agent_id=None, **kw: tool_reconsolidation_check(agent_id=agent_id or "mcp-client", **kw),
    "reconsolidate":          lambda agent_id=None, **kw: tool_reconsolidate(agent_id=agent_id or "mcp-client", **kw),
    "consolidation_stats":    lambda agent_id=None, **kw: tool_consolidation_stats(agent_id=agent_id or "mcp-client", **kw),
}
