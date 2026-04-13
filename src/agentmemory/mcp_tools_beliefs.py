"""brainctl MCP tools — belief system."""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

from agentmemory.lib.mcp_helpers import now_iso, open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


_now = now_iso


def _tom_tables_exist(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    return "agent_beliefs" in tables


def _log_access(conn, agent_id, action, target_table=None, target_id=None, query=None):
    """Best-effort access log write — silently ignored if table is missing."""
    try:
        conn.execute(
            "INSERT INTO access_log "
            "(agent_id, action, target_table, target_id, query, result_count, tokens_consumed) "
            "VALUES (?,?,?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query, None, None),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_belief_conflicts(
    agent_id: str | None = None,
    topic: str | None = None,
    min_severity: float = 0.0,
    limit: int = 50,
) -> dict:
    """List open belief conflicts sorted by severity."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        q = (
            "SELECT bc.id, bc.topic, bc.agent_a_id, bc.agent_b_id, "
            "bc.belief_a, bc.belief_b, bc.conflict_type, bc.severity, "
            "bc.detected_at, bc.requires_supervisor_intervention "
            "FROM belief_conflicts bc "
            "WHERE bc.resolved_at IS NULL AND bc.severity >= ?"
        )
        params: list[Any] = [min_severity]

        if agent_id:
            q += " AND (bc.agent_a_id=? OR bc.agent_b_id=?)"
            params += [agent_id, agent_id]
        if topic:
            q += " AND bc.topic LIKE ?"
            params.append(f"%{topic}%")

        q += " ORDER BY bc.severity DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(q, params).fetchall()
        conflicts = [dict(r) for r in rows]

        return {"ok": True, "open_conflicts": len(conflicts), "conflicts": conflicts}
    finally:
        conn.close()


def tool_collapse_log(
    belief_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
) -> dict:
    """List collapse events from belief_collapse_events."""
    try:
        import sys
        sys.path.insert(0, str(Path.home() / "agentmemory"))
        from collapse_mechanics import list_collapse_events
    except ImportError as e:
        return {"ok": False, "error": f"collapse_mechanics import failed: {e}"}

    try:
        events = list_collapse_events(
            belief_id=belief_id,
            agent_id=agent_id,
            limit=limit,
        )
        return {"ok": True, "count": len(events), "events": events}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_collapse_stats() -> dict:
    """Show aggregate statistics for belief collapses."""
    try:
        import sys
        sys.path.insert(0, str(Path.home() / "agentmemory"))
        from collapse_mechanics import collapse_stats
    except ImportError as e:
        return {"ok": False, "error": f"collapse_mechanics import failed: {e}"}

    try:
        stats = collapse_stats()
        return {"ok": True, **stats}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_belief_set(
    observer: str,
    target_agent: str,
    belief_type: str,
    content: str,
    confidence: float = 1.0,
    assumption: bool = False,
) -> dict:
    """Write a belief about a target agent. observer is the believing agent."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        is_assumption = 1 if assumption else 0
        topic = f"agent:{target_agent}:{belief_type}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        existing = conn.execute(
            "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?",
            (observer, topic),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE agent_beliefs SET
                   belief_content=?, confidence=?, is_assumption=?,
                   last_updated_at=?, invalidated_at=NULL, invalidation_reason=NULL, updated_at=?
                   WHERE agent_id=? AND topic=?""",
                (content, confidence, is_assumption, now, now, observer, topic),
            )
            action = "updated"
        else:
            conn.execute(
                """INSERT INTO agent_beliefs
                   (agent_id, topic, belief_content, confidence, is_assumption,
                    last_updated_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (observer, topic, content, confidence, is_assumption, now, now, now),
            )
            action = "created"

        conn.commit()
        _log_access(conn, observer, f"belief_{action}", "agent_beliefs", None, topic)
        conn.commit()

        return {
            "ok": True,
            "action": action,
            "observer": observer,
            "target": target_agent,
            "belief_type": belief_type,
            "topic": topic,
        }
    finally:
        conn.close()


def tool_belief_get(
    target_agent: str,
    observer: str | None = None,
) -> dict:
    """Retrieve all active beliefs about a target agent held by any observer."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        pattern = f"agent:{target_agent}:%"
        query = (
            "SELECT agent_id, topic, belief_content, confidence, is_assumption, last_updated_at "
            "FROM agent_beliefs "
            "WHERE topic LIKE ? AND invalidated_at IS NULL"
        )
        params: list[Any] = [pattern]

        if observer:
            query += " AND agent_id=?"
            params.append(observer)

        query += " ORDER BY last_updated_at DESC"
        rows = conn.execute(query, params).fetchall()

        beliefs = [
            {
                "observer": r["agent_id"],
                "topic": r["topic"],
                "belief_type": r["topic"].split(":")[-1] if r["topic"] else "",
                "content": r["belief_content"],
                "confidence": r["confidence"],
                "is_assumption": bool(r["is_assumption"]),
                "last_updated_at": r["last_updated_at"],
            }
            for r in rows
        ]

        return {"ok": True, "target": target_agent, "belief_count": len(beliefs), "beliefs": beliefs}
    finally:
        conn.close()


def tool_belief_seed(
    observer: str = "cortex",
    min_strength: float = 0.3,
    dry_run: bool = False,
) -> dict:
    """Seed capability beliefs from agent_expertise entries."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        # Check agent_expertise table exists
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "agent_expertise" not in tables:
            return {"ok": False, "error": "agent_expertise table not found."}

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        agents_q = conn.execute(
            "SELECT DISTINCT agent_id FROM agent_expertise WHERE strength >= ?",
            (min_strength,),
        ).fetchall()
        target_agents = [r["agent_id"] for r in agents_q]

        created = 0
        updated = 0
        dry_run_items: list[dict] = []

        for agent_id in target_agents:
            domains = conn.execute(
                "SELECT domain, strength FROM agent_expertise "
                "WHERE agent_id=? AND strength>=? ORDER BY strength DESC LIMIT 10",
                (agent_id, min_strength),
            ).fetchall()

            if not domains:
                continue

            top_domains = [f"{r['domain']} ({r['strength']:.2f})" for r in domains[:5]]
            content = "Capable in: " + ", ".join(top_domains)
            topic = f"agent:{agent_id}:capability"
            confidence = min(1.0, max(r["strength"] for r in domains))

            if dry_run:
                dry_run_items.append({"topic": topic, "content": content[:100]})
                continue

            existing = conn.execute(
                "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?",
                (observer, topic),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE agent_beliefs SET
                       belief_content=?, confidence=?, is_assumption=0,
                       last_updated_at=?, invalidated_at=NULL, invalidation_reason=NULL, updated_at=?
                       WHERE agent_id=? AND topic=?""",
                    (content, confidence, now, now, observer, topic),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO agent_beliefs
                       (agent_id, topic, belief_content, confidence, is_assumption,
                        last_updated_at, created_at, updated_at)
                       VALUES (?,?,?,?,0,?,?,?)""",
                    (observer, topic, content, confidence, now, now, now),
                )
                created += 1

        if not dry_run:
            conn.commit()
            _log_access(
                conn, observer, "belief_seed", "agent_beliefs", None,
                f"seeded {created + updated} beliefs",
            )
            conn.commit()

        return {
            "ok": True,
            "created": created,
            "updated": updated,
            "dry_run": dry_run,
            "agents_processed": len(target_agents),
            **({"dry_run_items": dry_run_items} if dry_run else {}),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="belief_conflicts",
        description=(
            "List open belief conflicts sorted by severity. "
            "Optionally filter by agent_id (agent A or B), topic substring, "
            "and minimum severity threshold."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id":     {"type": "string",  "description": "Filter conflicts involving this agent"},
                "topic":        {"type": "string",  "description": "Filter by topic substring"},
                "min_severity": {"type": "number",  "description": "Minimum severity (0.0–1.0)", "default": 0.0},
                "limit":        {"type": "integer", "description": "Max rows to return", "default": 50},
            },
        },
    ),
    Tool(
        name="collapse_log",
        description=(
            "List collapse events from belief_collapse_events. "
            "Optionally filter by belief_id or agent_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "belief_id": {"type": "string",  "description": "Filter by belief UUID"},
                "agent_id":  {"type": "string",  "description": "Filter by agent ID"},
                "limit":     {"type": "integer", "description": "Max events to return", "default": 50},
            },
        },
    ),
    Tool(
        name="collapse_stats",
        description=(
            "Show aggregate statistics for belief collapses: total collapses, "
            "last-7-day counts, average collapse probability, and per-trigger-type breakdown."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="belief_set",
        description=(
            "Write (create or update) a typed belief held by an observer agent about a target agent. "
            "Topic is derived automatically as 'agent:<target>:<belief_type>'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "observer":     {"type": "string",  "description": "Agent ID holding the belief"},
                "target_agent": {"type": "string",  "description": "Agent being modelled"},
                "belief_type":  {"type": "string",  "description": "Belief category, e.g. 'capability', 'role', 'status'"},
                "content":      {"type": "string",  "description": "Belief content text"},
                "confidence":   {"type": "number",  "description": "Confidence score 0.0–1.0", "default": 1.0},
                "assumption":   {"type": "boolean", "description": "Mark as unverified assumption", "default": False},
            },
            "required": ["observer", "target_agent", "belief_type", "content"],
        },
    ),
    Tool(
        name="belief_get",
        description=(
            "Retrieve all active beliefs about a target agent held by any (or a specific) observer agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_agent": {"type": "string", "description": "Agent whose beliefs are queried"},
                "observer":     {"type": "string", "description": "Filter by this observer agent (optional)"},
            },
            "required": ["target_agent"],
        },
    ),
    Tool(
        name="belief_seed",
        description=(
            "Seed capability beliefs from agent_expertise entries. "
            "For each agent with expertise above min_strength, creates or updates "
            "an 'agent:<id>:capability' belief held by the observer agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "observer":     {"type": "string",  "description": "Agent that will hold the seeded beliefs", "default": "cortex"},
                "min_strength": {"type": "number",  "description": "Minimum expertise strength to include (default: 0.3)", "default": 0.3},
                "dry_run":      {"type": "boolean", "description": "Preview without writing changes", "default": False},
            },
        },
    ),
]

DISPATCH: dict = {
    "belief_conflicts": lambda agent_id=None, **kw: tool_belief_conflicts(**kw),
    "collapse_log":     lambda agent_id=None, **kw: tool_collapse_log(**kw),
    "collapse_stats":   lambda agent_id=None, **kw: tool_collapse_stats(**kw),
    "belief_set":       lambda agent_id=None, **kw: tool_belief_set(**kw),
    "belief_get":       lambda agent_id=None, **kw: tool_belief_get(**kw),
    "belief_seed":      lambda agent_id=None, **kw: tool_belief_seed(**kw),
}
