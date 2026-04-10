"""brainctl MCP tools — allostatic scheduling (issue #9).

Allostasis: the brain anticipates future metabolic demand and prepares in advance,
rather than reacting after need is observed. Applied to memory consolidation:

If the system predicts you'll need certain memories soon (based on access patterns,
project activity, recurring temporal cycles), it should boost their replay_priority
now — so consolidation happens before demand, not after.

Three heuristic signal sources are used (no LLM or learned model required):
  temporal_pattern  — memories recalled on a given weekday/hour cycle are
                      predicted for the next occurrence
  project_activity  — memories in the most recently active project/category
                      are predicted to be needed again within 24h
  access_recency    — memories accessed in the last N hours but not yet
                      consolidated (replay_priority < threshold) get a forecast

Tools:
  consolidation_schedule — predict next N memories likely to be needed soon
  allostatic_prime       — boost predicted memories' replay_priority
  demand_forecast        — show the forecast table for an agent
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

_now = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
_FORECAST_HORIZON_H = 24   # predict demand within this many hours


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_forecasts_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS consolidation_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            predicted_demand_at TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
            signal_source TEXT NOT NULL,
            fulfilled_at TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_forecasts_agent ON consolidation_forecasts(agent_id, predicted_demand_at);
        CREATE INDEX IF NOT EXISTS idx_forecasts_memory ON consolidation_forecasts(memory_id);
        CREATE INDEX IF NOT EXISTS idx_forecasts_fulfilled ON consolidation_forecasts(fulfilled_at);
    """)
    conn.commit()


def _predict_memories(db: sqlite3.Connection, agent_id: str, limit: int) -> list[dict]:
    """Produce candidate forecasts using three heuristic signals.

    Returns list of {memory_id, signal_source, confidence, predicted_demand_at}.
    Deduplicates by memory_id, keeping highest confidence.
    """
    now_dt = datetime.now(timezone.utc)
    horizon = (now_dt + timedelta(hours=_FORECAST_HORIZON_H)).strftime("%Y-%m-%dT%H:%M:%S")
    now_s = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    candidates: dict[int, dict] = {}

    def _add(mid, source, conf):
        if mid not in candidates or conf > candidates[mid]["confidence"]:
            candidates[mid] = {
                "memory_id": mid,
                "signal_source": source,
                "confidence": round(conf, 3),
                "predicted_demand_at": horizon,
            }

    # Signal 1: project_activity — most recently active project's memories
    # Find the most accessed category in the last 24h
    cutoff_24h = (now_dt - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    recent_cats = db.execute(
        "SELECT m.category, COUNT(*) as cnt FROM access_log a "
        "JOIN memories m ON m.id = a.target_id "
        "WHERE a.agent_id = ? AND a.created_at >= ? AND a.target_table = 'memories' "
        "GROUP BY m.category ORDER BY cnt DESC LIMIT 1",
        (agent_id, cutoff_24h),
    ).fetchone()
    if recent_cats:
        hot_category = recent_cats["category"]
        rows = db.execute(
            "SELECT id, replay_priority FROM memories "
            "WHERE agent_id = ? AND category = ? AND retired_at IS NULL "
            "AND replay_priority < 3.0 ORDER BY replay_priority ASC LIMIT ?",
            (agent_id, hot_category, limit),
        ).fetchall()
        for r in rows:
            _add(r["id"], "project_activity", 0.65)

    # Signal 2: access_recency — recently accessed but low-priority memories
    rows = db.execute(
        "SELECT DISTINCT m.id, m.replay_priority FROM access_log a "
        "JOIN memories m ON m.id = a.target_id "
        "WHERE a.agent_id = ? AND a.created_at >= ? AND a.target_table = 'memories' "
        "AND m.retired_at IS NULL AND m.replay_priority < 2.0 "
        "ORDER BY a.created_at DESC LIMIT ?",
        (agent_id, cutoff_24h, limit),
    ).fetchall()
    for r in rows:
        _add(r["id"], "access_recency", 0.55)

    # Signal 3: temporal_pattern — memories with high ripple_tags (frequently recalled)
    # that haven't been touched recently → likely due for recall
    cutoff_week = (now_dt - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.execute(
        "SELECT m.id, m.ripple_tags FROM memories m "
        "WHERE m.agent_id = ? AND m.retired_at IS NULL AND m.ripple_tags >= 2 "
        "AND (m.id NOT IN ("
        "  SELECT DISTINCT target_id FROM access_log "
        "  WHERE agent_id = ? AND target_table = 'memories' AND created_at >= ?"
        ")) ORDER BY m.ripple_tags DESC LIMIT ?",
        (agent_id, agent_id, cutoff_week, limit),
    ).fetchall()
    for r in rows:
        _add(r["id"], "temporal_pattern", min(0.9, 0.5 + r["ripple_tags"] * 0.1))

    return sorted(candidates.values(), key=lambda x: -x["confidence"])[:limit]


# ---------------------------------------------------------------------------
# consolidation_schedule
# ---------------------------------------------------------------------------

def tool_consolidation_schedule(
    agent_id: str = "mcp-client",
    limit: int = 10,
    horizon_hours: int = 24,
    dry_run: bool = False,
    **kw,
) -> dict:
    """Predict the next N memories likely to be needed soon and store forecasts.

    Uses three heuristic signals: project_activity (hot category in last 24h),
    access_recency (recently accessed low-priority memories), and temporal_pattern
    (high-ripple memories not accessed in the last week).

    Forecasts are written to consolidation_forecasts. Use allostatic_prime to then
    boost their replay_priority. Use dry_run=true to preview without writing.
    """
    db = _db()
    _ensure_forecasts_table(db)
    try:
        candidates = _predict_memories(db, agent_id, limit)
        if not candidates:
            return {"ok": True, "forecast_count": 0, "forecasts": [],
                    "note": "No candidates found — insufficient access history"}
        if dry_run:
            return {"ok": True, "dry_run": True, "forecast_count": len(candidates),
                    "forecasts": candidates}
        horizon = (datetime.now(timezone.utc) + timedelta(hours=horizon_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        written = 0
        for c in candidates:
            # Skip if already forecast
            existing = db.execute(
                "SELECT id FROM consolidation_forecasts WHERE memory_id = ? AND agent_id = ? "
                "AND fulfilled_at IS NULL",
                (c["memory_id"], agent_id),
            ).fetchone()
            if existing:
                continue
            db.execute(
                "INSERT INTO consolidation_forecasts (memory_id, agent_id, predicted_demand_at, "
                "confidence, signal_source) VALUES (?, ?, ?, ?, ?)",
                (c["memory_id"], agent_id, horizon, c["confidence"], c["signal_source"]),
            )
            written += 1
        db.commit()
        return {"ok": True, "forecast_count": written, "forecasts": candidates}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# allostatic_prime
# ---------------------------------------------------------------------------

def tool_allostatic_prime(
    agent_id: str = "mcp-client",
    boost_delta: float = 1.5,
    limit: int = 10,
    **kw,
) -> dict:
    """Boost replay_priority for all pending (unfulfilled) forecasts.

    Implements the allostatic prime: preload predicted memories into fast-access
    by raising replay_priority so they're at the top of the consolidation queue
    when demand materializes.
    """
    if boost_delta <= 0:
        return {"ok": False, "error": "boost_delta must be positive"}
    db = _db()
    _ensure_forecasts_table(db)
    try:
        rows = db.execute(
            "SELECT DISTINCT memory_id FROM consolidation_forecasts "
            "WHERE agent_id = ? AND fulfilled_at IS NULL "
            "ORDER BY confidence DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        if not rows:
            return {"ok": True, "primed": 0, "note": "No pending forecasts — run consolidation_schedule first"}
        boosted = 0
        for r in rows:
            db.execute(
                "UPDATE memories SET replay_priority = MIN(10.0, replay_priority + ?) WHERE id = ?",
                (boost_delta, r["memory_id"]),
            )
            boosted += 1
        db.commit()
        return {"ok": True, "primed": boosted, "boost_delta": boost_delta}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# demand_forecast
# ---------------------------------------------------------------------------

def tool_demand_forecast(
    agent_id: str = "mcp-client",
    include_fulfilled: bool = False,
    limit: int = 20,
    **kw,
) -> dict:
    """Show the access-pattern model's predictions for an agent.

    Lists pending forecasts with their signal_source and confidence, showing what
    the system predicts will be needed and why.
    """
    db = _db()
    _ensure_forecasts_table(db)
    try:
        conditions = ["f.agent_id = ?"]
        params: list = [agent_id]
        if not include_fulfilled:
            conditions.append("f.fulfilled_at IS NULL")
        rows = db.execute(
            f"""SELECT f.id, f.memory_id, f.predicted_demand_at, f.confidence, f.signal_source,
                       f.fulfilled_at, f.created_at, m.content, m.category
                FROM consolidation_forecasts f
                JOIN memories m ON m.id = f.memory_id
                WHERE {' AND '.join(conditions)}
                ORDER BY f.confidence DESC, f.predicted_demand_at ASC
                LIMIT ?""",
            params + [limit],
        ).fetchall()
        items = [dict(r) for r in rows]
        # Summary stats
        all_rows = db.execute(
            "SELECT signal_source, COUNT(*) as cnt FROM consolidation_forecasts "
            "WHERE agent_id = ? AND fulfilled_at IS NULL GROUP BY signal_source",
            (agent_id,),
        ).fetchall()
        signal_breakdown = {r["signal_source"]: r["cnt"] for r in all_rows}
        return {
            "ok": True,
            "pending_count": sum(signal_breakdown.values()),
            "signal_breakdown": signal_breakdown,
            "forecasts": items,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TOOLS list and DISPATCH
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="consolidation_schedule",
        description=(
            "Predict the next N memories likely to be needed soon and store forecasts. "
            "Uses three heuristic signals: project_activity (hot category in last 24h), "
            "access_recency (recently accessed low-priority memories), and temporal_pattern "
            "(high-ripple memories not accessed in the last week). "
            "Use allostatic_prime to boost replay_priority for the forecasted memories. "
            "dry_run=true previews without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "Max forecasts to generate"},
                "horizon_hours": {"type": "integer", "default": 24, "description": "Hours ahead to predict demand"},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    ),
    Tool(
        name="allostatic_prime",
        description=(
            "Boost replay_priority for all pending (unfulfilled) forecasts. "
            "Preloads predicted memories into the consolidation queue before demand arrives. "
            "Run after consolidation_schedule."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "boost_delta": {"type": "number", "default": 1.5, "description": "Amount to boost replay_priority (capped at 10.0)"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    ),
    Tool(
        name="demand_forecast",
        description=(
            "Show all pending (and optionally fulfilled) consolidation forecasts for an agent. "
            "Displays signal_source and confidence so you can understand why each memory is predicted. "
            "Include fulfilled=true to see historical forecasts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_fulfilled": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
]

DISPATCH: dict = {
    "consolidation_schedule": lambda **kw: tool_consolidation_schedule(**kw),
    "allostatic_prime": lambda **kw: tool_allostatic_prime(**kw),
    "demand_forecast": lambda **kw: tool_demand_forecast(**kw),
}
