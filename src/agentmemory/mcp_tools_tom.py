"""brainctl MCP tools — Theory of Mind."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

_STALE_HOURS = 24  # beliefs older than this are considered stale


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_plain() -> str:
    """Return now as naive-looking ISO string matching _impl.py's format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _tom_tables_exist(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    return "agent_beliefs" in tables


def _require_tom(conn: sqlite3.Connection) -> bool:
    """Return True if ToM tables exist, False otherwise."""
    return _tom_tables_exist(conn)


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _log_access(conn, agent_id, action, target_table=None, target_id=None, query=None):
    try:
        conn.execute(
            "INSERT INTO access_log (agent_id, action, target_table, target_id, query) "
            "VALUES (?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query),
        )
    except Exception:
        pass


def _tom_compute_bdi(conn: sqlite3.Connection, agent_id: str) -> dict:
    """Compute BDI snapshot components for an agent. Returns dict for upsert."""
    now_iso = _now()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    active_beliefs = conn.execute(
        "SELECT id, topic, belief_content, confidence, is_assumption, last_updated_at "
        "FROM agent_beliefs WHERE agent_id=? AND invalidated_at IS NULL",
        (agent_id,),
    ).fetchall()

    active_count = len(active_beliefs)
    stale_count = sum(
        1 for b in active_beliefs if (b["last_updated_at"] or "") < stale_cutoff
    )
    assumption_count = sum(1 for b in active_beliefs if b["is_assumption"])
    conflict_count = conn.execute(
        "SELECT count(*) as cnt FROM belief_conflicts "
        "WHERE (agent_a_id=? OR agent_b_id=?) AND resolved_at IS NULL",
        (agent_id, agent_id),
    ).fetchone()["cnt"]
    key_topics = [b["topic"] for b in active_beliefs[:10]]

    beliefs_summary = json.dumps({
        "active_belief_count": active_count,
        "stale_belief_count": stale_count,
        "assumption_count": assumption_count,
        "conflict_count": conflict_count,
        "key_topics": key_topics,
    })

    task_rows = conn.execute(
        "SELECT id, external_id, title, priority, status FROM tasks "
        "WHERE assigned_agent_id=? AND status IN ('pending','in_progress') "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END LIMIT 20",
        (agent_id,),
    ).fetchall()
    primary = task_rows[0] if task_rows else None
    desires_summary = json.dumps({
        "active_task_count": len(task_rows),
        "primary_goal": primary["title"] if primary else None,
        "priority": primary["priority"] if primary else None,
        "task_ids": [(r["external_id"] or str(r["id"])) for r in task_rows],
    })

    inprog = [r for r in task_rows if r["status"] == "in_progress"]
    recent_events = conn.execute(
        "SELECT summary FROM events WHERE agent_id=? ORDER BY created_at DESC LIMIT 5",
        (agent_id,),
    ).fetchall()
    intentions_summary = json.dumps({
        "in_progress_tasks": [(r["external_id"] or str(r["id"])) for r in inprog],
        "committed_actions": [r["summary"][:80] for r in recent_events],
    })

    if task_rows:
        covered = 0
        for t in task_rows:
            topic_key = f"task:{t['external_id'] or t['id']}:status"
            hit = conn.execute(
                "SELECT 1 FROM agent_beliefs WHERE agent_id=? AND topic=? AND invalidated_at IS NULL",
                (agent_id, topic_key),
            ).fetchone()
            if hit:
                covered += 1
        knowledge_coverage_score = covered / len(task_rows)
    else:
        knowledge_coverage_score = 1.0

    belief_staleness_score = (stale_count / active_count) if active_count > 0 else 0.0

    cr_row = conn.execute(
        "SELECT MAX(confusion_risk) as max_cr FROM agent_perspective_models "
        "WHERE subject_agent_id=?",
        (agent_id,),
    ).fetchone()
    confusion_risk_score = (
        cr_row["max_cr"] if cr_row and cr_row["max_cr"] is not None else 0.0
    )

    return {
        "agent_id": agent_id,
        "beliefs_summary": beliefs_summary,
        "beliefs_last_updated_at": now_iso,
        "desires_summary": desires_summary,
        "desires_last_updated_at": now_iso,
        "intentions_summary": intentions_summary,
        "intentions_last_updated_at": now_iso,
        "knowledge_coverage_score": round(knowledge_coverage_score, 4),
        "belief_staleness_score": round(belief_staleness_score, 4),
        "confusion_risk_score": round(confusion_risk_score, 4),
        "last_full_assessment_at": now_iso,
        "updated_at": now_iso,
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_tom_update(agent_id: str = "mcp-client", target_agent_id: str = None, **_) -> dict:
    """Refresh BDI state snapshot for one or all active agents."""
    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql."}

    agent_ids = [target_agent_id] if target_agent_id else [
        r["id"] for r in conn.execute("SELECT id FROM agents WHERE status='active'").fetchall()
    ]

    results = []
    for aid in agent_ids:
        bdi = _tom_compute_bdi(conn, aid)
        conn.execute(
            """INSERT INTO agent_bdi_state
               (agent_id, beliefs_summary, beliefs_last_updated_at,
                desires_summary, desires_last_updated_at,
                intentions_summary, intentions_last_updated_at,
                knowledge_coverage_score, belief_staleness_score,
                confusion_risk_score, last_full_assessment_at, updated_at)
               VALUES (:agent_id, :beliefs_summary, :beliefs_last_updated_at,
                       :desires_summary, :desires_last_updated_at,
                       :intentions_summary, :intentions_last_updated_at,
                       :knowledge_coverage_score, :belief_staleness_score,
                       :confusion_risk_score, :last_full_assessment_at, :updated_at)
               ON CONFLICT(agent_id) DO UPDATE SET
                 beliefs_summary=excluded.beliefs_summary,
                 beliefs_last_updated_at=excluded.beliefs_last_updated_at,
                 desires_summary=excluded.desires_summary,
                 desires_last_updated_at=excluded.desires_last_updated_at,
                 intentions_summary=excluded.intentions_summary,
                 intentions_last_updated_at=excluded.intentions_last_updated_at,
                 knowledge_coverage_score=excluded.knowledge_coverage_score,
                 belief_staleness_score=excluded.belief_staleness_score,
                 confusion_risk_score=excluded.confusion_risk_score,
                 last_full_assessment_at=excluded.last_full_assessment_at,
                 updated_at=excluded.updated_at""",
            bdi,
        )
        conn.commit()
        results.append(bdi)

    conn.close()
    return {"ok": True, "agents_updated": len(results), "results": results}


def tool_tom_belief_set(
    agent_id: str = "mcp-client",
    target_agent_id: str = "",
    topic: str = "",
    content: str = "",
    assumption: bool = False,
    confidence: float = 1.0,
    **_,
) -> dict:
    """Record or update a belief for an agent."""
    if not target_agent_id:
        return {"ok": False, "error": "target_agent_id is required"}
    if not topic:
        return {"ok": False, "error": "topic is required"}
    if not content:
        return {"ok": False, "error": "content is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    is_assumption = 1 if assumption else 0
    now = _now_plain()

    existing = conn.execute(
        "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?",
        (target_agent_id, topic),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE agent_beliefs SET
               belief_content=?, confidence=?, is_assumption=?,
               last_updated_at=?, invalidated_at=NULL, invalidation_reason=NULL, updated_at=?
               WHERE agent_id=? AND topic=?""",
            (content, confidence, is_assumption, now, now, target_agent_id, topic),
        )
        action = "updated"
    else:
        conn.execute(
            """INSERT INTO agent_beliefs
               (agent_id, topic, belief_content, confidence, is_assumption,
                last_updated_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (target_agent_id, topic, content, confidence, is_assumption, now, now, now),
        )
        action = "created"

    _log_access(conn, target_agent_id, f"belief_{action}", "agent_beliefs", None, topic)
    conn.commit()
    conn.close()
    return {"ok": True, "action": action, "agent_id": target_agent_id, "topic": topic}


def tool_tom_belief_invalidate(
    agent_id: str = "mcp-client",
    target_agent_id: str = "",
    topic: str = "",
    reason: str = "",
    **_,
) -> dict:
    """Mark a belief as invalid and create a conflict record."""
    if not target_agent_id:
        return {"ok": False, "error": "target_agent_id is required"}
    if not topic:
        return {"ok": False, "error": "topic is required"}
    if not reason:
        return {"ok": False, "error": "reason is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    now = _now_plain()
    row = conn.execute(
        "SELECT id, belief_content FROM agent_beliefs "
        "WHERE agent_id=? AND topic=? AND invalidated_at IS NULL",
        (target_agent_id, topic),
    ).fetchone()
    if not row:
        conn.close()
        return {
            "ok": False,
            "error": f"No active belief for agent '{target_agent_id}' on topic '{topic}'",
        }

    conn.execute(
        "UPDATE agent_beliefs SET invalidated_at=?, invalidation_reason=?, updated_at=? "
        "WHERE agent_id=? AND topic=?",
        (now, reason, now, target_agent_id, topic),
    )

    existing_conflict = conn.execute(
        "SELECT id FROM belief_conflicts WHERE agent_a_id=? AND topic=? AND resolved_at IS NULL",
        (target_agent_id, topic),
    ).fetchone()
    if not existing_conflict:
        conn.execute(
            """INSERT INTO belief_conflicts
               (topic, agent_a_id, agent_b_id, belief_a, belief_b,
                conflict_type, severity, detected_at, requires_supervisor_intervention)
               VALUES (?,?,NULL,?,?,?,?,?,?)""",
            (
                topic, target_agent_id, row["belief_content"],
                f"Invalidated: {reason}", "staleness", 0.6, now, 1,
            ),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "agent_id": target_agent_id, "topic": topic, "reason": reason}


def tool_tom_conflicts_list(
    agent_id: str = "mcp-client",
    filter_agent: str = None,
    topic: str = None,
    min_severity: float = 0.0,
    limit: int = 50,
    **_,
) -> dict:
    """List open belief conflicts sorted by severity."""
    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    q = (
        "SELECT bc.id, bc.topic, bc.agent_a_id, bc.agent_b_id, "
        "bc.belief_a, bc.belief_b, bc.conflict_type, bc.severity, "
        "bc.detected_at, bc.requires_supervisor_intervention "
        "FROM belief_conflicts bc "
        "WHERE bc.resolved_at IS NULL AND bc.severity >= ?"
    )
    params: list = [min_severity]

    if filter_agent:
        q += " AND (bc.agent_a_id=? OR bc.agent_b_id=?)"
        params += [filter_agent, filter_agent]
    if topic:
        q += " AND bc.topic LIKE ?"
        params.append(f"%{topic}%")

    q += " ORDER BY bc.severity DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"ok": True, "open_conflicts": len(rows), "conflicts": _rows_to_list(rows)}


def tool_tom_conflicts_resolve(
    agent_id: str = "mcp-client",
    conflict_id: int = None,
    resolution: str = "",
    **_,
) -> dict:
    """Mark a conflict as resolved."""
    if conflict_id is None:
        return {"ok": False, "error": "conflict_id is required"}
    if not resolution:
        return {"ok": False, "error": "resolution is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    now = _now_plain()
    row = conn.execute(
        "SELECT id, topic FROM belief_conflicts WHERE id=?", (conflict_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": f"Conflict #{conflict_id} not found."}

    conn.execute(
        "UPDATE belief_conflicts SET resolved_at=?, resolution=? WHERE id=?",
        (now, resolution, conflict_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "conflict_id": conflict_id, "topic": row["topic"], "resolved_at": now}


def tool_tom_perspective_set(
    agent_id: str = "mcp-client",
    observer: str = "",
    subject: str = "",
    topic: str = "",
    belief: str = "",
    gap: str = None,
    confusion: float = 0.0,
    **_,
) -> dict:
    """Update observer's perspective model of subject on a topic."""
    if not observer:
        return {"ok": False, "error": "observer is required"}
    if not subject:
        return {"ok": False, "error": "subject is required"}
    if not topic:
        return {"ok": False, "error": "topic is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    now = _now_plain()
    existing = conn.execute(
        "SELECT id FROM agent_perspective_models "
        "WHERE observer_agent_id=? AND subject_agent_id=? AND topic=?",
        (observer, subject, topic),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE agent_perspective_models SET
               estimated_belief=?, knowledge_gap=?, confusion_risk=?, last_updated_at=?
               WHERE observer_agent_id=? AND subject_agent_id=? AND topic=?""",
            (belief or None, gap, confusion, now, observer, subject, topic),
        )
        action = "updated"
    else:
        conn.execute(
            """INSERT INTO agent_perspective_models
               (observer_agent_id, subject_agent_id, topic, estimated_belief,
                knowledge_gap, confusion_risk, last_updated_at, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (observer, subject, topic, belief or None, gap, confusion, now, now),
        )
        action = "created"

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "action": action,
        "observer": observer,
        "subject": subject,
        "topic": topic,
        "confusion_risk": confusion,
    }


def tool_tom_perspective_get(
    agent_id: str = "mcp-client",
    observer: str = "",
    subject: str = "",
    **_,
) -> dict:
    """Print all perspective model entries for an observer->subject pair."""
    if not observer:
        return {"ok": False, "error": "observer is required"}
    if not subject:
        return {"ok": False, "error": "subject is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    rows = conn.execute(
        """SELECT topic, estimated_belief, knowledge_gap, confusion_risk,
                  estimated_confidence, last_updated_at
           FROM agent_perspective_models
           WHERE observer_agent_id=? AND subject_agent_id=?
           ORDER BY confusion_risk DESC""",
        (observer, subject),
    ).fetchall()
    conn.close()
    return {
        "ok": True,
        "observer": observer,
        "subject": subject,
        "perspective_models": _rows_to_list(rows),
    }


def tool_tom_gap_scan(
    agent_id: str = "mcp-client",
    target_agent_id: str = "",
    **_,
) -> dict:
    """Scan agent's active tasks vs beliefs — emit gap report."""
    if not target_agent_id:
        return {"ok": False, "error": "target_agent_id is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    tasks = conn.execute(
        "SELECT id, external_id, title, description, priority FROM tasks "
        "WHERE assigned_agent_id=? AND status IN ('pending','in_progress') "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END",
        (target_agent_id,),
    ).fetchall()

    if not tasks:
        conn.close()
        return {
            "ok": True,
            "agent_id": target_agent_id,
            "message": f"No active tasks for {target_agent_id}. Nothing to scan.",
            "gaps": [],
            "missing": 0,
            "stale": 0,
        }

    beliefs = conn.execute(
        "SELECT topic, last_updated_at, confidence FROM agent_beliefs "
        "WHERE agent_id=? AND invalidated_at IS NULL",
        (target_agent_id,),
    ).fetchall()
    belief_map = {b["topic"]: b for b in beliefs}

    rows_out = []
    for t in tasks:
        topic_key = f"task:{t['external_id'] or t['id']}:status"
        b = belief_map.get(topic_key)
        if b is None:
            status = "MISSING"
            staleness = "--"
            confusion = 1.0
        elif b["last_updated_at"] and b["last_updated_at"] < stale_cutoff:
            status = "STALE"
            staleness = b["last_updated_at"][:10]
            confusion = 0.6
        else:
            status = "CURRENT"
            staleness = "recent"
            confusion = 0.1
        rows_out.append({
            "topic": topic_key,
            "task_title": t["title"],
            "status": status,
            "staleness": staleness,
            "confusion_risk": confusion,
        })

    conn.close()
    missing = sum(1 for r in rows_out if r["status"] == "MISSING")
    stale = sum(1 for r in rows_out if r["status"] == "STALE")
    return {
        "ok": True,
        "agent_id": target_agent_id,
        "gaps": rows_out,
        "missing": missing,
        "stale": stale,
    }


def tool_tom_inject(
    agent_id: str = "mcp-client",
    target_agent_id: str = "",
    topic: str = "",
    content: str = None,
    observer: str = None,
    **_,
) -> dict:
    """Write a gap-filling memory scoped to agent, update perspective model."""
    if not target_agent_id:
        return {"ok": False, "error": "target_agent_id is required"}
    if not topic:
        return {"ok": False, "error": "topic is required"}

    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    observer_id = observer or target_agent_id
    now = _now_plain()

    if not content:
        pm = conn.execute(
            "SELECT knowledge_gap FROM agent_perspective_models "
            "WHERE subject_agent_id=? AND topic=? ORDER BY last_updated_at DESC LIMIT 1",
            (target_agent_id, topic),
        ).fetchone()
        if pm and pm["knowledge_gap"]:
            content = pm["knowledge_gap"]
        else:
            conn.close()
            return {
                "ok": False,
                "error": "No content provided and no knowledge gap in perspective model.",
            }

    scope = f"agent:{target_agent_id}"
    row = conn.execute(
        """INSERT INTO memories
           (agent_id, content, category, scope, confidence, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           RETURNING id""",
        (
            observer_id,
            f"[ToM inject -> {target_agent_id}] Topic: {topic}\n{content}",
            "environment", scope, 0.7, now, now,
        ),
    ).fetchone()
    memory_id = row["id"] if row else None

    conn.execute(
        """INSERT INTO agent_beliefs
           (agent_id, topic, belief_content, confidence, is_assumption,
            source_memory_id, last_updated_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id, topic) DO UPDATE SET
             belief_content=excluded.belief_content,
             confidence=0.9,
             source_memory_id=excluded.source_memory_id,
             last_updated_at=excluded.last_updated_at,
             invalidated_at=NULL,
             updated_at=excluded.updated_at""",
        (target_agent_id, topic, content, 0.9, 0, memory_id, now, now, now),
    )

    old_cr_row = conn.execute(
        "SELECT confusion_risk FROM agent_perspective_models "
        "WHERE observer_agent_id=? AND subject_agent_id=? AND topic=?",
        (observer_id, target_agent_id, topic),
    ).fetchone()
    old_cr_val = old_cr_row["confusion_risk"] if old_cr_row else None
    new_cr = 0.1

    conn.execute(
        """INSERT INTO agent_perspective_models
           (observer_agent_id, subject_agent_id, topic, estimated_belief,
            knowledge_gap, confusion_risk, last_updated_at, created_at)
           VALUES (?,?,?,?,NULL,?,?,?)
           ON CONFLICT(observer_agent_id, subject_agent_id, topic) DO UPDATE SET
             estimated_belief=excluded.estimated_belief,
             knowledge_gap=NULL,
             confusion_risk=?,
             last_updated_at=excluded.last_updated_at""",
        (observer_id, target_agent_id, topic, content, new_cr, now, now, new_cr),
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "memory_id": memory_id,
        "agent_id": target_agent_id,
        "topic": topic,
        "confusion_risk_before": old_cr_val,
        "confusion_risk_after": new_cr,
    }


def tool_tom_status(
    agent_id: str = "mcp-client",
    target_agent_id: str = None,
    **_,
) -> dict:
    """Print BDI health summary — all agents ranked by confusion_risk."""
    conn = _db()
    if not _require_tom(conn):
        return {"ok": False, "error": "Theory of Mind tables not found."}

    if target_agent_id:
        rows = conn.execute(
            """SELECT b.agent_id, a.display_name,
                      b.knowledge_coverage_score, b.belief_staleness_score,
                      b.confusion_risk_score, b.last_full_assessment_at
               FROM agent_bdi_state b JOIN agents a ON a.id = b.agent_id
               WHERE b.agent_id=?""",
            (target_agent_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT b.agent_id, a.display_name,
                      b.knowledge_coverage_score, b.belief_staleness_score,
                      b.confusion_risk_score, b.last_full_assessment_at
               FROM agent_bdi_state b JOIN agents a ON a.id = b.agent_id
               ORDER BY b.confusion_risk_score DESC"""
        ).fetchall()

    conn.close()
    return {"ok": True, "agents": _rows_to_list(rows)}


# ---------------------------------------------------------------------------
# TOOLS and DISPATCH exports
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="tom_update",
        description=(
            "Refresh the BDI (Belief-Desire-Intention) state snapshot for one or all "
            "active agents. Computes belief counts, staleness, task coverage, and "
            "confusion risk scores and upserts them into agent_bdi_state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "target_agent_id": {
                    "type": "string",
                    "description": "Agent to update. Omit to update all active agents.",
                },
            },
        },
    ),
    Tool(
        name="tom_belief_set",
        description=(
            "Record or update a belief for an agent. Creates the belief if it doesn't "
            "exist; re-activates and updates it if it was previously invalidated."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "target_agent_id": {"type": "string", "description": "Agent whose belief to set"},
                "topic": {"type": "string", "description": "Belief topic key"},
                "content": {"type": "string", "description": "Belief content"},
                "assumption": {"type": "boolean", "default": False, "description": "Mark as assumption"},
                "confidence": {"type": "number", "default": 1.0, "description": "Confidence 0.0-1.0"},
            },
            "required": ["target_agent_id", "topic", "content"],
        },
    ),
    Tool(
        name="tom_belief_invalidate",
        description=(
            "Mark an active belief as invalid and automatically create a belief conflict "
            "record. Use when you discover an agent is operating on stale or wrong information."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "target_agent_id": {"type": "string", "description": "Agent whose belief to invalidate"},
                "topic": {"type": "string", "description": "Belief topic key"},
                "reason": {"type": "string", "description": "Reason for invalidation"},
            },
            "required": ["target_agent_id", "topic", "reason"],
        },
    ),
    Tool(
        name="tom_conflicts_list",
        description=(
            "List open (unresolved) belief conflicts sorted by severity descending. "
            "Optionally filter by agent or topic substring."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "filter_agent": {"type": "string", "description": "Filter by agent ID (A or B side)"},
                "topic": {"type": "string", "description": "Filter by topic substring"},
                "min_severity": {"type": "number", "default": 0.0, "description": "Minimum severity 0.0-1.0"},
                "limit": {"type": "integer", "default": 50, "description": "Max results"},
            },
        },
    ),
    Tool(
        name="tom_conflicts_resolve",
        description="Mark a belief conflict as resolved with a resolution description.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "conflict_id": {"type": "integer", "description": "Conflict record ID"},
                "resolution": {"type": "string", "description": "Resolution description"},
            },
            "required": ["conflict_id", "resolution"],
        },
    ),
    Tool(
        name="tom_perspective_set",
        description=(
            "Update an observer agent's perspective model of a subject agent on a given "
            "topic. Records estimated belief, knowledge gaps, and confusion risk."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "observer": {"type": "string", "description": "Observer agent ID"},
                "subject": {"type": "string", "description": "Subject agent ID"},
                "topic": {"type": "string", "description": "Topic"},
                "belief": {"type": "string", "description": "Estimated belief content"},
                "gap": {"type": "string", "description": "Knowledge gap description"},
                "confusion": {"type": "number", "default": 0.0, "description": "Confusion risk 0.0-1.0"},
            },
            "required": ["observer", "subject", "topic"],
        },
    ),
    Tool(
        name="tom_perspective_get",
        description=(
            "Retrieve all perspective model entries for an observer->subject pair, "
            "sorted by confusion risk descending."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "observer": {"type": "string", "description": "Observer agent ID"},
                "subject": {"type": "string", "description": "Subject agent ID"},
            },
            "required": ["observer", "subject"],
        },
    ),
    Tool(
        name="tom_gap_scan",
        description=(
            "Scan an agent's active tasks against its beliefs to identify knowledge gaps. "
            "Returns MISSING (no belief), STALE (outdated belief), or CURRENT for each task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "target_agent_id": {"type": "string", "description": "Agent to scan"},
            },
            "required": ["target_agent_id"],
        },
    ),
    Tool(
        name="tom_inject",
        description=(
            "Write a gap-filling memory scoped to an agent and update the perspective model. "
            "Lowers confusion risk for the observer->subject->topic relationship to 0.1. "
            "If content is omitted, uses the knowledge_gap from the perspective model."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "target_agent_id": {"type": "string", "description": "Agent to inject belief into"},
                "topic": {"type": "string", "description": "Topic key"},
                "content": {"type": "string", "description": "Content to inject (optional if gap exists in perspective model)"},
                "observer": {"type": "string", "description": "Observer agent ID (defaults to target_agent_id)"},
            },
            "required": ["target_agent_id", "topic"],
        },
    ),
    Tool(
        name="tom_status",
        description=(
            "Show BDI health summary for all agents (or one specific agent), ranked by "
            "confusion risk descending. Requires tom_update to have been run first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "target_agent_id": {"type": "string", "description": "Specific agent to show (omit for all)"},
            },
        },
    ),
]

DISPATCH: dict = {
    "tom_update": tool_tom_update,
    "tom_belief_set": tool_tom_belief_set,
    "tom_belief_invalidate": tool_tom_belief_invalidate,
    "tom_conflicts_list": tool_tom_conflicts_list,
    "tom_conflicts_resolve": tool_tom_conflicts_resolve,
    "tom_perspective_set": tool_tom_perspective_set,
    "tom_perspective_get": tool_tom_perspective_get,
    "tom_gap_scan": tool_tom_gap_scan,
    "tom_inject": tool_tom_inject,
    "tom_status": tool_tom_status,
}
