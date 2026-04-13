"""brainctl MCP tools — memory lifecycle reporting."""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


# ---------------------------------------------------------------------------
# lifecycle_summary — high-level memory lifecycle metrics
# ---------------------------------------------------------------------------

def _lifecycle_summary(agent_id: str, days: int = 30) -> dict:
    """Return high-level memory lifecycle metrics for an agent."""
    try:
        db = _db()

        # Total created in window
        created_row = db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM memories
            WHERE agent_id = ?
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchone()
        total_created = created_row["cnt"] if created_row else 0

        # Total retired in window
        retired_row = db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NOT NULL
              AND retired_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchone()
        total_retired = retired_row["cnt"] if retired_row else 0

        # Active count + avg confidence
        active_row = db.execute(
            """
            SELECT
              COUNT(*) AS cnt,
              AVG(confidence) AS avg_conf
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NULL
            """,
            (agent_id,),
        ).fetchone()
        active_count = active_row["cnt"] if active_row else 0
        avg_confidence_active = round(active_row["avg_conf"] or 0.0, 4) if active_row else 0.0

        # Avg confidence of retired memories
        retired_conf_row = db.execute(
            """
            SELECT AVG(confidence) AS avg_conf
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NOT NULL
            """,
            (agent_id,),
        ).fetchone()
        avg_confidence_retired = round(retired_conf_row["avg_conf"] or 0.0, 4) if retired_conf_row else 0.0

        # Survival rate: active / (active + all retired)
        all_retired_row = db.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE agent_id = ? AND retired_at IS NOT NULL",
            (agent_id,),
        ).fetchone()
        all_retired = all_retired_row["cnt"] if all_retired_row else 0
        total_denom = active_count + all_retired
        survival_rate = round(active_count / total_denom, 4) if total_denom > 0 else 1.0

        # Per-category breakdown
        cat_rows = db.execute(
            """
            SELECT
              category,
              COUNT(*) AS total,
              SUM(CASE WHEN retired_at IS NULL THEN 1 ELSE 0 END) AS active_cnt,
              SUM(CASE WHEN retired_at IS NOT NULL THEN 1 ELSE 0 END) AS retired_cnt
            FROM memories
            WHERE agent_id = ?
            GROUP BY category
            """,
            (agent_id,),
        ).fetchall()
        by_category: dict[str, dict] = {}
        for row in cat_rows:
            by_category[row["category"]] = {
                "created": row["total"],
                "active": row["active_cnt"],
                "retired": row["retired_cnt"],
            }

        # Decay candidates: active memories with confidence < 0.3
        decay_row = db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NULL
              AND confidence < 0.3
            """,
            (agent_id,),
        ).fetchone()
        decay_candidates = decay_row["cnt"] if decay_row else 0

        # Protected count: active memories with protected = 1
        protected_row = db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NULL
              AND protected = 1
            """,
            (agent_id,),
        ).fetchone()
        protected_count = protected_row["cnt"] if protected_row else 0

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "total_created": total_created,
            "total_retired": total_retired,
            "survival_rate": survival_rate,
            "avg_confidence_active": avg_confidence_active,
            "avg_confidence_retired": avg_confidence_retired,
            "by_category": by_category,
            "decay_candidates": decay_candidates,
            "protected_count": protected_count,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# write_gate_stats — W(m) worthiness gate statistics
# ---------------------------------------------------------------------------

def _write_gate_stats(agent_id: str, days: int = 30) -> dict:
    """Return statistics from the write gate, based on logged events and memory creation patterns."""
    try:
        db = _db()

        # Look for explicit write_gate_rejected events
        rejection_rows = db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM events
            WHERE agent_id = ?
              AND event_type = 'write_gate_rejected'
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchone()
        gate_events_found = rejection_rows["cnt"] if rejection_rows else 0

        # Memories created in the window = accepted writes
        accepted_row = db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM memories
            WHERE agent_id = ?
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchone()
        acceptance_estimate = accepted_row["cnt"] if accepted_row else 0

        # If we have no explicit gate events, compute best-effort rejection estimate
        if gate_events_found == 0:
            rejection_estimate = None
            notes = (
                "No 'write_gate_rejected' events found in the events table for this agent/window. "
                "Rejection rate cannot be computed. acceptance_estimate reflects memories actually created."
            )
        else:
            rejection_estimate = gate_events_found
            notes = (
                f"Found {gate_events_found} explicit write_gate_rejected event(s). "
                f"acceptance_estimate is the count of memories created in the same window."
            )

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "gate_events_found": gate_events_found,
            "rejection_estimate": rejection_estimate,
            "acceptance_estimate": acceptance_estimate,
            "notes": notes,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# decay_report — memories at risk of decay
# ---------------------------------------------------------------------------

def _decay_report(
    agent_id: str,
    confidence_threshold: float = 0.3,
    days_inactive: int = 60,
    limit: int = 20,
) -> dict:
    """Return active memories at risk of decay: low confidence and/or long inactive."""
    try:
        db = _db()

        rows = db.execute(
            """
            SELECT
              id AS memory_id,
              SUBSTR(content, 1, 120) AS content_snippet,
              confidence,
              category,
              temporal_class,
              CAST(
                (julianday('now') - julianday(COALESCE(last_recalled_at, created_at)))
                AS INTEGER
              ) AS days_since_recalled
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NULL
              AND (
                confidence < ?
                OR CAST(
                     (julianday('now') - julianday(COALESCE(last_recalled_at, created_at)))
                   AS INTEGER) >= ?
              )
            ORDER BY confidence ASC, days_since_recalled DESC
            LIMIT ?
            """,
            (agent_id, confidence_threshold, days_inactive, limit),
        ).fetchall()

        at_risk = [
            {
                "memory_id": r["memory_id"],
                "content_snippet": r["content_snippet"],
                "confidence": r["confidence"],
                "category": r["category"],
                "days_since_recalled": r["days_since_recalled"],
                "temporal_class": r["temporal_class"],
            }
            for r in rows
        ]

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "confidence_threshold": confidence_threshold,
            "days_inactive": days_inactive,
            "at_risk": at_risk,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# consolidation_events — log of consolidation/maintenance events
# ---------------------------------------------------------------------------

_CONSOLIDATION_EVENT_TYPES = {
    "memory_promoted",
    "memory_retired",
    "memory_compressed",
    "cap_exceeded",
    "consolidation_sweep",
}


def _consolidation_events(agent_id: str, days: int = 30, limit: int = 50) -> dict:
    """Return consolidation and maintenance events for an agent."""
    try:
        db = _db()

        placeholders = ",".join("?" for _ in _CONSOLIDATION_EVENT_TYPES)
        rows = db.execute(
            f"""
            SELECT id, event_type, summary, created_at
            FROM events
            WHERE agent_id = ?
              AND event_type IN ({placeholders})
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (agent_id, *_CONSOLIDATION_EVENT_TYPES, f"-{days} days", limit),
        ).fetchall()

        events_list = [
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "summary": r["summary"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

        # Aggregate counts by event_type
        by_type: dict[str, int] = {}
        for ev in events_list:
            et = ev["event_type"]
            by_type[et] = by_type.get(et, 0) + 1

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "events": events_list,
            "by_type": by_type,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# retirement_analysis — why memories get retired
# ---------------------------------------------------------------------------

def _retirement_analysis(agent_id: str, days: int = 30) -> dict:
    """Analyse retired memories: group by category and confidence at retirement."""
    try:
        db = _db()

        # Retired in window
        retired_rows = db.execute(
            """
            SELECT
              category,
              confidence,
              retraction_reason
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NOT NULL
              AND retired_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchall()

        total_retired = len(retired_rows)

        by_category: dict[str, dict] = {}
        low_confidence_retirements = 0
        manual_retirements = 0

        for row in retired_rows:
            cat = row["category"] or "unknown"
            conf = row["confidence"] if row["confidence"] is not None else 0.0
            reason = row["retraction_reason"]

            if cat not in by_category:
                by_category[cat] = {"count": 0, "avg_confidence": 0.0, "_conf_sum": 0.0}
            by_category[cat]["count"] += 1
            by_category[cat]["_conf_sum"] += conf

            if conf < 0.3:
                low_confidence_retirements += 1

            # Consider a retirement "manual" if there's an explicit retraction_reason
            if reason:
                manual_retirements += 1

        # Finalise averages and drop internal accumulator
        for cat, data in by_category.items():
            n = data["count"]
            data["avg_confidence"] = round(data["_conf_sum"] / n, 4) if n > 0 else 0.0
            del data["_conf_sum"]

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "total_retired": total_retired,
            "by_category": by_category,
            "low_confidence_retirements": low_confidence_retirements,
            "manual_retirements": manual_retirements,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP dispatch helpers
# ---------------------------------------------------------------------------

def _call_lifecycle_summary(args: dict) -> dict:
    agent_id = args.get("agent_id", "")
    days = int(args.get("days", 30))
    return _lifecycle_summary(agent_id=agent_id, days=days)


def _call_write_gate_stats(args: dict) -> dict:
    agent_id = args.get("agent_id", "")
    days = int(args.get("days", 30))
    return _write_gate_stats(agent_id=agent_id, days=days)


def _call_decay_report(args: dict) -> dict:
    agent_id = args.get("agent_id", "")
    confidence_threshold = float(args.get("confidence_threshold", 0.3))
    days_inactive = int(args.get("days_inactive", 60))
    limit = int(args.get("limit", 20))
    return _decay_report(
        agent_id=agent_id,
        confidence_threshold=confidence_threshold,
        days_inactive=days_inactive,
        limit=limit,
    )


def _call_consolidation_events(args: dict) -> dict:
    agent_id = args.get("agent_id", "")
    days = int(args.get("days", 30))
    limit = int(args.get("limit", 50))
    return _consolidation_events(agent_id=agent_id, days=days, limit=limit)


def _call_retirement_analysis(args: dict) -> dict:
    agent_id = args.get("agent_id", "")
    days = int(args.get("days", 30))
    return _retirement_analysis(agent_id=agent_id, days=days)


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="lifecycle_summary",
        description=(
            "High-level memory lifecycle metrics for an agent: total created/retired, survival rate, "
            "confidence averages for active vs retired memories, per-category breakdown, "
            "count of decay candidates (confidence < 0.3), and protected memory count."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent whose memory lifecycle to summarise.",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30).",
                    "default": 30,
                },
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="write_gate_stats",
        description=(
            "Statistics from the W(m) worthiness gate. Queries 'write_gate_rejected' events; "
            "if none are logged, returns a best-effort estimate based on memory creation counts. "
            "Use this to understand what fraction of candidate memories were accepted or rejected."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent to report gate statistics for.",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30).",
                    "default": 30,
                },
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="decay_report",
        description=(
            "Memories at risk of decay: active memories with confidence below threshold "
            "or that have not been recalled within days_inactive days. "
            "Returns a ranked list with content snippet, confidence, category, temporal class, "
            "and days since last recall."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent whose memories to inspect.",
                },
                "confidence_threshold": {
                    "type": "number",
                    "description": "Confidence below this is considered at-risk (default: 0.3).",
                    "default": 0.3,
                },
                "days_inactive": {
                    "type": "integer",
                    "description": "Memories not recalled within this many days are at-risk (default: 60).",
                    "default": 60,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of at-risk memories to return (default: 20).",
                    "default": 20,
                },
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="consolidation_events",
        description=(
            "Log of consolidation and maintenance events for an agent: memory_promoted, "
            "memory_retired, memory_compressed, cap_exceeded, consolidation_sweep. "
            "Returns events ordered newest-first, plus a by-type count summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent whose consolidation events to fetch.",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30).",
                    "default": 30,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return (default: 50).",
                    "default": 50,
                },
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="retirement_analysis",
        description=(
            "Analysis of why memories get retired for an agent: total retired in window, "
            "breakdown by category with average confidence at retirement, count of "
            "low-confidence retirements (confidence < 0.3), and count of manual retirements "
            "(those with an explicit retraction_reason)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent whose retired memories to analyse.",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30).",
                    "default": 30,
                },
            },
            "required": ["agent_id"],
        },
    ),
]

DISPATCH: dict = {
    "lifecycle_summary": _call_lifecycle_summary,
    "write_gate_stats": _call_write_gate_stats,
    "decay_report": _call_decay_report,
    "consolidation_events": _call_consolidation_events,
    "retirement_analysis": _call_retirement_analysis,
}
