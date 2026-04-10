"""brainctl MCP tools — world model."""
from __future__ import annotations
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# World-model table helpers
# ---------------------------------------------------------------------------

def _ensure_world_model_tables(db: sqlite3.Connection) -> None:
    """Create OWM tables if not present (idempotent)."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_capabilities (
            agent_id        TEXT NOT NULL,
            capability      TEXT NOT NULL,
            skill_level     REAL NOT NULL DEFAULT 0.5,
            task_count      INTEGER NOT NULL DEFAULT 0,
            avg_events      REAL,
            block_rate      REAL DEFAULT 0.0,
            last_active     TEXT,
            updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            PRIMARY KEY (agent_id, capability)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_caps_agent ON agent_capabilities(agent_id);
        CREATE INDEX IF NOT EXISTS idx_agent_caps_cap ON agent_capabilities(capability);
        CREATE TABLE IF NOT EXISTS world_model_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_type    TEXT NOT NULL,
            subject_id       TEXT,
            subject_type     TEXT,
            predicted_state  TEXT,
            actual_state     TEXT,
            prediction_error REAL,
            author_agent_id  TEXT,
            created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            resolved_at      TEXT
        );
    """)
    db.commit()


def _world_rebuild_caps_for_agent(db: sqlite3.Connection, agent_id: str) -> int:
    """Derive agent_capabilities from event + expertise history for one agent."""
    cap_map = {
        "memory": "memory_ops", "memories": "memory_ops", "agentmemory": "memory_ops",
        "distilled": "memory_ops", "promoted": "memory_ops", "retired": "memory_ops",
        "sql": "db_schema", "schema": "db_schema", "migration": "db_schema",
        "sqlite": "db_schema", "database": "db_schema",
        "research": "research", "analysis": "research", "intelligence": "research",
        "synthesis": "research", "brief": "research",
        "temporal": "temporal_reasoning", "epoch": "temporal_reasoning",
        "causal": "temporal_reasoning", "timeline": "temporal_reasoning",
        "policy": "policy_engine", "decision": "policy_engine",
        "governance": "policy_engine",
        "agent": "agent_coordination", "agents": "agent_coordination",
        "coordination": "agent_coordination", "handoff": "agent_coordination",
        "product": "product_domain",
        "heartbeat": "agent_ops", "framework": "agent_ops",
        "task": "agent_ops", "issues": "agent_ops",
        "embedding": "vector_ops", "vec": "vector_ops", "vsearch": "vector_ops",
    }

    try:
        exp_rows = db.execute(
            "SELECT domain, strength, evidence_count FROM agent_expertise WHERE agent_id=?",
            (agent_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return 0

    cap_accum: dict[str, dict] = {}
    stopwords = {"and", "the", "for", "with", "from", "this", "that", "are", "was",
                 "has", "have", "been", "will", "would", "could", "should", "result"}
    for row in exp_rows:
        domain = row["domain"].lower()
        cap = cap_map.get(domain)
        if not cap:
            if len(domain) >= 4 and domain not in stopwords:
                cap = domain[:30]
            else:
                continue
        if cap not in cap_accum:
            cap_accum[cap] = {"total_strength": 0.0, "count": 0, "evidence": 0}
        cap_accum[cap]["total_strength"] += row["strength"]
        cap_accum[cap]["count"] += 1
        cap_accum[cap]["evidence"] += row["evidence_count"]

    if not cap_accum:
        return 0

    ev_rows = db.execute(
        """SELECT project,
                  COUNT(*) as total,
                  SUM(CASE WHEN event_type IN ('error','warning') THEN 1 ELSE 0 END) as bad,
                  MAX(created_at) as last_active
           FROM events WHERE agent_id=? AND project IS NOT NULL AND project != ''
           GROUP BY project""",
        (agent_id,)
    ).fetchall()
    total_ev = sum(r["total"] for r in ev_rows)
    bad_ev = sum(r["bad"] for r in ev_rows)
    block_rate = (bad_ev / total_ev) if total_ev > 0 else 0.0
    last_active = max((r["last_active"] for r in ev_rows), default=None)

    now_str = _now_ts()
    written = 0
    for cap, data in cap_accum.items():
        avg_str = data["total_strength"] / data["count"] if data["count"] else 0.5
        db.execute(
            """INSERT OR REPLACE INTO agent_capabilities
               (agent_id, capability, skill_level, task_count, avg_events, block_rate, last_active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, cap, round(min(avg_str, 1.0), 4), data["evidence"],
             None, round(block_rate, 4), last_active, now_str)
        )
        written += 1

    db.commit()
    return written


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_world_rebuild_caps(agent_id: str = "mcp-client", agent: str = None) -> dict:
    """Rebuild agent_capabilities from event + expertise history."""
    db = _db()
    _ensure_world_model_tables(db)

    if agent:
        agent_ids = [agent]
    else:
        agent_ids = [r["id"] for r in
            db.execute("SELECT id FROM agents WHERE status='active'").fetchall()]

    results = []
    for aid in agent_ids:
        n = _world_rebuild_caps_for_agent(db, aid)
        results.append({"agent_id": aid, "capabilities_written": n})

    return {"ok": True, "agents_processed": len(results), "results": results}


def tool_world_agent(agent_id: str = "mcp-client", agent: str = None, limit: int = 20) -> dict:
    """Show world model capability profile for an agent."""
    if not agent:
        return {"ok": False, "error": "agent parameter is required"}

    db = _db()
    _ensure_world_model_tables(db)

    agent_row = db.execute(
        "SELECT id, display_name, agent_type, status FROM agents WHERE id=?",
        (agent,)
    ).fetchone()
    if not agent_row:
        return {"ok": False, "error": f"agent '{agent}' not found"}

    cap_rows = db.execute(
        """SELECT capability, skill_level, task_count, block_rate, last_active
           FROM agent_capabilities WHERE agent_id=?
           ORDER BY skill_level DESC LIMIT ?""",
        (agent, limit)
    ).fetchall()

    ev_summary = db.execute(
        "SELECT COUNT(*) as total, MAX(created_at) as last_event FROM events WHERE agent_id=?",
        (agent,)
    ).fetchone()

    return {
        "ok": True,
        "agent_id": agent,
        "display_name": agent_row["display_name"],
        "status": agent_row["status"],
        "total_events": ev_summary["total"] if ev_summary else 0,
        "last_event": ev_summary["last_event"] if ev_summary else None,
        "capabilities": rows_to_list(cap_rows),
    }


def tool_world_project(agent_id: str = "mcp-client", project: str = None, days: int = 14) -> dict:
    """Show project dynamics — velocity, agent activity, event breakdown."""
    if not project:
        return {"ok": False, "error": "project parameter is required"}

    db = _db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    ev_rows = db.execute(
        """SELECT agent_id, event_type, importance, summary, created_at
           FROM events WHERE project LIKE ? AND created_at >= ?
           ORDER BY created_at DESC""",
        (f"%{project}%", cutoff)
    ).fetchall()

    if not ev_rows:
        return {
            "ok": True,
            "project": project,
            "window_days": days,
            "total_events": 0,
            "message": f"No events found for project matching '{project}' in last {days} days.",
        }

    agent_set: dict[str, int] = {}
    type_counts: Counter = Counter()
    daily_counts: Counter = Counter()
    total_importance = 0.0
    for r in ev_rows:
        agent_set[r["agent_id"]] = agent_set.get(r["agent_id"], 0) + 1
        type_counts[r["event_type"]] += 1
        daily_counts[(r["created_at"] or "")[:10]] += 1
        total_importance += r["importance"] or 0.5

    total = len(ev_rows)
    velocity = total / days
    avg_importance = total_importance / total if total else 0.0
    error_count = type_counts.get("error", 0) + type_counts.get("warning", 0)
    block_rate = error_count / total if total else 0.0
    active_agents = sorted(agent_set.items(), key=lambda x: -x[1])

    return {
        "ok": True,
        "project": project,
        "window_days": days,
        "total_events": total,
        "velocity_per_day": round(velocity, 2),
        "avg_importance": round(avg_importance, 3),
        "error_block_rate": round(block_rate, 3),
        "event_type_counts": dict(type_counts.most_common()),
        "active_agents": [{"agent_id": a, "event_count": c} for a, c in active_agents],
        "daily_activity": dict(sorted(daily_counts.items())),
    }


def tool_world_status(agent_id: str = "mcp-client", days: int = 7) -> dict:
    """Generate compressed org snapshot — the core World Model output."""
    db = _db()
    _ensure_world_model_tables(db)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    agent_activity = db.execute(
        """SELECT agent_id, COUNT(*) as event_count, MAX(created_at) as last_active
           FROM events WHERE created_at >= ?
           GROUP BY agent_id ORDER BY event_count DESC""",
        (cutoff,)
    ).fetchall()

    project_activity = db.execute(
        """SELECT project, COUNT(*) as events,
                  SUM(CASE WHEN event_type IN ('error','warning') THEN 1 ELSE 0 END) as errors,
                  COUNT(DISTINCT agent_id) as agent_count,
                  MAX(created_at) as last_active
           FROM events WHERE project IS NOT NULL AND project != '' AND created_at >= ?
           GROUP BY project ORDER BY events DESC""",
        (cutoff,)
    ).fetchall()

    top_caps = db.execute(
        """SELECT capability,
                  COUNT(DISTINCT agent_id) as agent_count,
                  AVG(skill_level) as avg_skill,
                  SUM(task_count) as total_tasks
           FROM agent_capabilities
           GROUP BY capability
           ORDER BY total_tasks DESC, avg_skill DESC
           LIMIT 10"""
    ).fetchall()

    gaps = db.execute(
        """SELECT capability, COUNT(DISTINCT agent_id) as agent_count, AVG(skill_level) as avg_skill
           FROM agent_capabilities
           GROUP BY capability
           HAVING agent_count <= 1 AND avg_skill < 0.4
           ORDER BY avg_skill ASC
           LIMIT 8"""
    ).fetchall()

    mem_stats = db.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN retired_at IS NULL THEN 1 ELSE 0 END) as active,
                  AVG(CASE WHEN retired_at IS NULL THEN confidence ELSE NULL END) as avg_confidence
           FROM memories"""
    ).fetchone()

    try:
        nm = db.execute("SELECT org_state FROM neuromodulation_state WHERE id=1").fetchone()
        org_state = nm["org_state"] if nm else "normal"
    except Exception:
        org_state = "unknown"

    highlights = db.execute(
        """SELECT agent_id, event_type, summary, project, importance, created_at
           FROM events WHERE created_at >= ? AND importance >= 0.7
           ORDER BY importance DESC, created_at DESC LIMIT 8""",
        (cutoff,)
    ).fetchall()

    return {
        "ok": True,
        "snapshot_at": _now(),
        "window_days": days,
        "org_state": org_state,
        "active_agents": rows_to_list(agent_activity),
        "project_dynamics": rows_to_list(project_activity),
        "capability_hotspots": rows_to_list(top_caps),
        "capability_gaps": rows_to_list(gaps),
        "memory_health": row_to_dict(mem_stats),
        "highlights": rows_to_list(highlights),
    }


def _ensure_agent(db, agent_id: str) -> None:
    """Auto-register agent_id to satisfy FK constraints."""
    try:
        now = _now_ts()
        db.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES (?, ?, 'api', 'active', ?, ?)",
            (agent_id, agent_id, now, now),
        )
    except Exception:
        pass


def tool_world_predict(agent_id: str = "mcp-client", subject: str = None,
                       subject_type: str = "task", predicted: str = None) -> dict:
    """Log a world model prediction for later calibration."""
    if not subject:
        return {"ok": False, "error": "subject parameter is required"}
    if not predicted:
        return {"ok": False, "error": "predicted parameter is required"}

    db = _db()
    _ensure_world_model_tables(db)
    _ensure_agent(db, agent_id)

    row_id = db.execute(
        """INSERT INTO world_model_snapshots
           (snapshot_type, subject_id, subject_type, predicted_state, author_agent_id)
           VALUES ('prediction', ?, ?, ?, ?)""",
        (subject, subject_type or "task", predicted, agent_id)
    ).lastrowid
    db.commit()
    return {"ok": True, "snapshot_id": row_id, "subject": subject}


def tool_world_resolve(agent_id: str = "mcp-client", snapshot_id: int = None,
                       actual: str = None, error: float = None) -> dict:
    """Resolve a world model prediction with actual outcome."""
    if snapshot_id is None:
        return {"ok": False, "error": "snapshot_id parameter is required"}
    if actual is None:
        return {"ok": False, "error": "actual parameter is required"}

    db = _db()
    _ensure_world_model_tables(db)

    now_str = _now_ts()
    db.execute(
        "UPDATE world_model_snapshots SET actual_state=?, prediction_error=?, resolved_at=? WHERE id=?",
        (actual, error, now_str, snapshot_id)
    )
    db.commit()

    changes = db.execute("SELECT changes()").fetchone()[0]
    if changes == 0:
        return {"ok": False, "error": f"snapshot {snapshot_id} not found"}

    return {"ok": True, "snapshot_id": snapshot_id, "resolved_at": now_str}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="world_rebuild_caps",
        description=(
            "Rebuild agent_capabilities table from event and expertise history. "
            "Run this to refresh the capability data used by world_agent and world_status. "
            "Processes all active agents, or a single agent if specified."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent ID to rebuild caps for. If omitted, rebuilds all active agents.",
                },
            },
        },
    ),
    Tool(
        name="world_agent",
        description=(
            "Show the world model capability profile for a specific agent. "
            "Returns skill levels, task counts, block rates, and event summary. "
            "Run world_rebuild_caps first if capabilities are empty."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent ID to profile.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max capabilities to return (default 20).",
                    "default": 20,
                },
            },
            "required": ["agent"],
        },
    ),
    Tool(
        name="world_project",
        description=(
            "Show project dynamics: velocity, agent activity breakdown, and event type counts "
            "for a given project over a configurable time window."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project name (partial match supported).",
                },
                "days": {
                    "type": "integer",
                    "description": "Time window in days (default 14).",
                    "default": 14,
                },
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="world_status",
        description=(
            "Generate a compressed organizational world model snapshot. "
            "Returns active agents, project dynamics, capability hotspots and gaps, "
            "memory health, and high-importance highlights for a recent time window."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Look-back window in days (default 7).",
                    "default": 7,
                },
            },
        },
    ),
    Tool(
        name="world_predict",
        description=(
            "Log a world model prediction for later calibration. "
            "Records a predicted outcome for a subject (task, agent, project) that can be "
            "resolved later with world_resolve."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Subject ID or label being predicted about.",
                },
                "subject_type": {
                    "type": "string",
                    "description": "Type of subject (task, agent, project, etc.).",
                    "default": "task",
                },
                "predicted": {
                    "type": "string",
                    "description": "Predicted state or outcome.",
                },
            },
            "required": ["subject", "predicted"],
        },
    ),
    Tool(
        name="world_resolve",
        description=(
            "Resolve a world model prediction with the actual outcome. "
            "Links back to a snapshot_id returned by world_predict."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "snapshot_id": {
                    "type": "integer",
                    "description": "ID of the prediction snapshot to resolve.",
                },
                "actual": {
                    "type": "string",
                    "description": "Actual observed outcome.",
                },
                "error": {
                    "type": "number",
                    "description": "Numeric prediction error, if applicable (optional).",
                },
            },
            "required": ["snapshot_id", "actual"],
        },
    ),
]

DISPATCH: dict = {
    "world_rebuild_caps": tool_world_rebuild_caps,
    "world_agent": tool_world_agent,
    "world_project": tool_world_project,
    "world_status": tool_world_status,
    "world_predict": tool_world_predict,
    "world_resolve": tool_world_resolve,
}
