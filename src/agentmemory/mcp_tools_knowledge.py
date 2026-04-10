"""brainctl MCP tools — knowledge index & synthesis."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


# ---------------------------------------------------------------------------
# Category / event-type mapping (mirrors _impl.py)
# ---------------------------------------------------------------------------

_EVENT_TYPE_TO_CATEGORY = {
    "result": "project",
    "decision": "decision",
    "observation": "environment",
    "error": "lesson",
    "handoff": "project",
    "session_end": "project",
    "consolidation_cycle": "project",
    "coherence_check": "project",
    "warning": "environment",
    "task_update": "project",
    "cadence_updated": "environment",
    "push_delivered": "project",
    "health_alert": "environment",
    "reflexion_propagation": "lesson",
}

_CATEGORY_KEYWORDS = [
    ("decision",    ["decided", "chose", "option", "tradeoff", "approved", "rejected",
                     "selected", "architecture", "design choice", "will use", "going with"]),
    ("lesson",      ["lesson:", "lesson —", "learned:", "never run", "always ", "mistake",
                     "bug:", "failure:", "incident:", "root cause", "postmortem",
                     "regression", "gotcha", "footgun", "caution:"]),
    ("identity",    ["i am ", "my role", "my name", "agent id", "i report to",
                     "my capabilities", "identity:", "persona:", "i own "]),
    ("environment", ["schema", "database", "db path", "cron", "infrastructure",
                     "endpoint", "api key", "config", "env var", "port ", "url:",
                     "installed", "deployed", "server", "tooling", "pipeline"]),
    ("project",     ["milestone", "shipped", "released", "completed", "done:",
                     "sprint", "wave ", "cos-", "issue", "heartbeat", "task",
                     "implemented", "delivered", "closed", "fixed"]),
]


def _infer_category_from_content(content: str) -> str:
    if not content:
        return "project"
    lower = content.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category
    return "project"


# ---------------------------------------------------------------------------
# Tool: knowledge_index
# ---------------------------------------------------------------------------

def tool_knowledge_index(
    agent_id: str = "mcp-client",
    category: str | None = None,
    scope: str | None = None,
) -> dict:
    """Generate a browsable JSON catalog of all knowledge in the brain.

    Mirrors cmd_index (line ~8706 of _impl.py), always returning JSON
    (the MCP transport is structured; markdown output is dropped).
    """
    try:
        conn = _db()

        where_clauses = ["retired_at IS NULL"]
        params: list[Any] = []
        if category:
            where_clauses.append("category = ?")
            params.append(category)
        if scope:
            where_clauses.append("scope = ?")
            params.append(scope)

        where = " AND ".join(where_clauses)
        memories = conn.execute(
            f"SELECT id, category, scope, content, confidence, recalled_count, "
            f"file_path, file_line, created_at, agent_id "
            f"FROM memories WHERE {where} ORDER BY category, confidence DESC",
            params,
        ).fetchall()

        entities = conn.execute(
            "SELECT id, name, entity_type, created_at FROM entities "
            "WHERE retired_at IS NULL ORDER BY entity_type, name"
        ).fetchall()

        decisions = conn.execute(
            "SELECT id, title, rationale, agent_id, created_at FROM decisions "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()

        memories_by_category: dict[str, list] = {}
        for m in memories:
            cat = m["category"]
            if cat not in memories_by_category:
                memories_by_category[cat] = []
            entry: dict[str, Any] = {
                "id": m["id"],
                "content": m["content"][:200],
                "confidence": m["confidence"],
                "recalled": m["recalled_count"],
                "scope": m["scope"],
                "agent": m["agent_id"],
                "created": m["created_at"],
            }
            if m["file_path"]:
                entry["file"] = m["file_path"]
                if m["file_line"]:
                    entry["line"] = m["file_line"]
            memories_by_category[cat].append(entry)

        entities_by_type: dict[str, list] = {}
        for e in entities:
            etype = e["entity_type"]
            if etype not in entities_by_type:
                entities_by_type[etype] = []
            entities_by_type[etype].append({
                "id": e["id"],
                "name": e["name"],
                "created": e["created_at"],
            })

        decisions_list = [
            {
                "id": d["id"],
                "title": d["title"][:200],
                "rationale": (d["rationale"] or "")[:100],
                "agent": d["agent_id"],
                "created": d["created_at"],
            }
            for d in decisions
        ]

        conn.close()
        return {
            "ok": True,
            "generated_at": _now(),
            "memories_by_category": memories_by_category,
            "entities_by_type": entities_by_type,
            "decisions": decisions_list,
            "stats": {
                "total_memories": len(memories),
                "total_entities": len(entities),
                "total_decisions": len(decisions),
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: knowledge_report
# ---------------------------------------------------------------------------

def tool_knowledge_report(
    agent_id: str = "mcp-client",
    topic: str | None = None,
    agent_filter: str | None = None,
    entity: str | None = None,
    limit: int = 20,
) -> dict:
    """Compile brain knowledge into a structured report.

    Mirrors cmd_report (line ~5777 of _impl.py), returning structured JSON
    instead of markdown text.
    """
    try:
        conn = _db()
        limit = limit or 20

        # --- Stats overview ---
        stats: dict[str, Any] = {}
        for tbl in ["memories", "events", "entities", "decisions", "knowledge_edges"]:
            try:
                stats[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                stats[tbl] = 0
        active_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]
        stats["active_memories"] = active_memories

        # --- Entity focus ---
        entity_section: dict | None = None
        if entity:
            entity_section = _report_entity_json(conn, entity, limit)

        # --- Memories ---
        mem_sql = (
            "SELECT id, category, content, confidence, created_at "
            "FROM memories WHERE retired_at IS NULL"
        )
        mem_params: list[Any] = []
        if topic:
            mem_sql += " AND (content LIKE ? OR category LIKE ?)"
            mem_params.extend([f"%{topic}%", f"%{topic}%"])
        if agent_filter:
            mem_sql += " AND agent_id = ?"
            mem_params.append(agent_filter)
        mem_sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        mem_params.append(limit)
        mem_rows = conn.execute(mem_sql, mem_params).fetchall()

        memories_by_category: dict[str, list] = {}
        for m in mem_rows:
            cat = m["category"] or "general"
            memories_by_category.setdefault(cat, []).append({
                "id": m["id"],
                "content": m["content"][:200],
                "confidence": m["confidence"],
                "created_at": m["created_at"],
            })

        # --- Entities ---
        ent_sql = (
            "SELECT id, name, entity_type, observations, confidence "
            "FROM entities WHERE retired_at IS NULL"
        )
        ent_params: list[Any] = []
        if topic:
            ent_sql += " AND (name LIKE ? OR observations LIKE ?)"
            ent_params.extend([f"%{topic}%", f"%{topic}%"])
        ent_sql += " ORDER BY confidence DESC LIMIT ?"
        ent_params.append(limit)
        ent_rows = conn.execute(ent_sql, ent_params).fetchall()

        entities_list = []
        ent_ids = []
        for e in ent_rows:
            obs: list = []
            try:
                obs = json.loads(e["observations"] or "[]")
            except Exception:
                pass
            entities_list.append({
                "id": e["id"],
                "name": e["name"],
                "entity_type": e["entity_type"],
                "confidence": e["confidence"],
                "observations": obs[:3],
            })
            ent_ids.append(e["id"])

        # Relations between listed entities
        relations: list[dict] = []
        if ent_ids:
            ph = ",".join("?" * len(ent_ids))
            edges = conn.execute(
                f"SELECT ke.relation_type, es.name as src, et.name as tgt "
                f"FROM knowledge_edges ke "
                f"JOIN entities es ON ke.source_id = es.id AND ke.source_table = 'entities' "
                f"JOIN entities et ON ke.target_id = et.id AND ke.target_table = 'entities' "
                f"WHERE ke.source_id IN ({ph}) OR ke.target_id IN ({ph})",
                ent_ids + ent_ids,
            ).fetchall()
            seen: set[str] = set()
            for edge in edges:
                key = f"{edge['src']}-{edge['relation_type']}-{edge['tgt']}"
                if key not in seen:
                    relations.append({
                        "src": edge["src"],
                        "relation": edge["relation_type"],
                        "tgt": edge["tgt"],
                    })
                    seen.add(key)

        # --- Recent Decisions ---
        dec_sql = "SELECT title, rationale, project, created_at FROM decisions"
        dec_params: list[Any] = []
        if topic:
            dec_sql += " WHERE title LIKE ? OR rationale LIKE ?"
            dec_params.extend([f"%{topic}%", f"%{topic}%"])
        dec_sql += " ORDER BY created_at DESC LIMIT ?"
        dec_params.append(min(limit, 10))
        dec_rows = conn.execute(dec_sql, dec_params).fetchall()

        decisions_list = [
            {
                "title": d["title"],
                "rationale": (d["rationale"] or "")[:200],
                "project": d["project"],
                "created_at": d["created_at"],
            }
            for d in dec_rows
        ]

        # --- Recent Events ---
        ev_sql = "SELECT event_type, summary, project, created_at FROM events"
        ev_params: list[Any] = []
        if topic:
            ev_sql += " WHERE summary LIKE ?"
            ev_params.append(f"%{topic}%")
        if agent_filter:
            ev_sql += (" AND" if topic else " WHERE") + " agent_id = ?"
            ev_params.append(agent_filter)
        ev_sql += " ORDER BY created_at DESC LIMIT ?"
        ev_params.append(min(limit, 15))
        ev_rows = conn.execute(ev_sql, ev_params).fetchall()

        events_list = [
            {
                "event_type": e["event_type"],
                "summary": (e["summary"] or "")[:150],
                "project": e["project"],
                "created_at": e["created_at"],
            }
            for e in ev_rows
        ]

        # --- Affect State ---
        affect_state: list[dict] = []
        try:
            aff_rows = conn.execute("""
                SELECT a.agent_id, a.valence, a.arousal, a.dominance,
                       a.affect_label, a.functional_state, a.safety_flag
                FROM affect_log a INNER JOIN (
                    SELECT agent_id, MAX(id) as max_id FROM affect_log GROUP BY agent_id
                ) latest ON a.id = latest.max_id
                ORDER BY a.created_at DESC LIMIT 10
            """).fetchall()
            for a in aff_rows:
                affect_state.append({
                    "agent_id": a["agent_id"],
                    "valence": a["valence"],
                    "arousal": a["arousal"],
                    "dominance": a["dominance"],
                    "affect_label": a["affect_label"],
                    "functional_state": a["functional_state"],
                    "safety_flag": a["safety_flag"],
                })
        except Exception:
            pass

        conn.close()
        return {
            "ok": True,
            "generated_at": _now(),
            "filters": {
                "topic": topic,
                "agent": agent_filter,
                "entity": entity,
            },
            "stats": stats,
            "entity_focus": entity_section,
            "memories_by_category": memories_by_category,
            "entities": entities_list,
            "entity_relations": relations,
            "decisions": decisions_list,
            "recent_events": events_list,
            "affect_state": affect_state,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _report_entity_json(conn: sqlite3.Connection, name: str, limit: int) -> dict | None:
    """Return a dict with entity focus data (mirrors _report_entity in _impl.py)."""
    row = conn.execute(
        "SELECT * FROM entities WHERE name LIKE ? AND retired_at IS NULL LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    if not row:
        return {"error": f"Entity '{name}' not found"}

    ent = dict(row)
    obs: list = []
    try:
        obs = json.loads(ent.get("observations") or "[]")
    except Exception:
        pass

    out_edges = conn.execute(
        "SELECT ke.relation_type, et.name, et.entity_type FROM knowledge_edges ke "
        "JOIN entities et ON ke.target_id = et.id AND ke.target_table = 'entities' "
        "WHERE ke.source_table = 'entities' AND ke.source_id = ? LIMIT ?",
        (ent["id"], limit),
    ).fetchall()

    in_edges = conn.execute(
        "SELECT ke.relation_type, es.name, es.entity_type FROM knowledge_edges ke "
        "JOIN entities es ON ke.source_id = es.id AND ke.source_table = 'entities' "
        "WHERE ke.target_table = 'entities' AND ke.target_id = ? LIMIT ?",
        (ent["id"], limit),
    ).fetchall()

    related_mems = conn.execute(
        "SELECT content, confidence, created_at FROM memories "
        "WHERE retired_at IS NULL AND content LIKE ? ORDER BY confidence DESC LIMIT ?",
        (f"%{ent['name']}%", limit),
    ).fetchall()

    return {
        "id": ent["id"],
        "name": ent["name"],
        "entity_type": ent["entity_type"],
        "confidence": ent["confidence"],
        "created_at": ent["created_at"],
        "observations": obs,
        "outgoing_relations": [
            {"relation": e["relation_type"], "target": e["name"], "target_type": e["entity_type"]}
            for e in out_edges
        ],
        "incoming_relations": [
            {"source": e["name"], "source_type": e["entity_type"], "relation": e["relation_type"]}
            for e in in_edges
        ],
        "related_memories": [
            {"content": m["content"][:200], "confidence": m["confidence"], "created_at": m["created_at"]}
            for m in related_mems
        ],
    }


# ---------------------------------------------------------------------------
# Tool: distill
# ---------------------------------------------------------------------------

def tool_distill(
    agent_id: str = "mcp-client",
    threshold: float = 0.7,
    limit: int = 50,
    dry_run: bool = False,
    since: str | None = None,
    filter_agent: str | None = None,
    event_types: str | None = None,
) -> dict:
    """Batch-promote high-importance events to durable memories.

    Mirrors cmd_distill (line ~9424 of _impl.py).
    """
    try:
        conn = _db()

        promoted_ids: set[int] = set()
        for row in conn.execute(
            "SELECT source_event_id FROM memories WHERE source_event_id IS NOT NULL"
        ):
            promoted_ids.add(row[0])

        valid_agents = {r[0] for r in conn.execute("SELECT id FROM agents")}

        skip_types = {"memory_promoted", "memory_retired", "session_start"}
        event_type_list = (
            [t.strip() for t in event_types.split(",")]
            if event_types
            else None
        )

        sql = """
            SELECT id, agent_id, event_type, summary, detail, importance, project, created_at
            FROM events
            WHERE importance >= ?
            AND event_type NOT IN ({skip})
        """.format(skip=",".join(f"'{t}'" for t in skip_types))
        params: list[Any] = [threshold]

        if event_type_list:
            placeholders = ",".join("?" for _ in event_type_list)
            sql += f" AND event_type IN ({placeholders}) "
            params.extend(event_type_list)

        if since:
            sql += " AND created_at >= ? "
            params.append(since)

        if filter_agent:
            sql += " AND agent_id = ? "
            params.append(filter_agent)

        sql += " ORDER BY importance DESC, created_at DESC"

        candidates = conn.execute(sql, params).fetchall()

        to_promote = []
        skipped_orphans = 0
        for ev in candidates:
            if ev["id"] not in promoted_ids:
                if ev["agent_id"] not in valid_agents:
                    skipped_orphans += 1
                    continue
                to_promote.append(ev)
            if len(to_promote) >= limit:
                break

        if dry_run:
            results = []
            for ev in to_promote:
                results.append({
                    "event_id": ev["id"],
                    "agent_id": ev["agent_id"],
                    "event_type": ev["event_type"],
                    "importance": ev["importance"],
                    "summary": ev["summary"][:120],
                    "would_promote_as": _EVENT_TYPE_TO_CATEGORY.get(
                        ev["event_type"],
                        _infer_category_from_content(ev["summary"]),
                    ),
                })
            conn.close()
            return {
                "ok": True,
                "dry_run": True,
                "threshold": threshold,
                "candidates_found": len(to_promote),
                "total_events_above_threshold": len(candidates),
                "already_promoted_skipped": len(candidates) - len(to_promote) - skipped_orphans,
                "orphan_agents_skipped": skipped_orphans,
                "promotions": results,
            }

        promoted = []
        for ev in to_promote:
            category = _EVENT_TYPE_TO_CATEGORY.get(
                ev["event_type"],
                _infer_category_from_content(ev["summary"]),
            )
            scope = f"project:{ev['project']}" if ev["project"] else "global"

            cursor = conn.execute(
                "INSERT INTO memories (agent_id, category, scope, content, confidence, "
                "source_event_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ev["agent_id"], category, scope, ev["summary"],
                    min(ev["importance"], 0.95), ev["id"], _now(), _now(),
                ),
            )
            memory_id = cursor.lastrowid

            conn.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, importance, created_at) "
                "VALUES (?, 'memory_promoted', ?, ?, 0.3, ?)",
                (
                    ev["agent_id"],
                    f"Distilled event #{ev['id']} (importance={ev['importance']}) to memory #{memory_id}",
                    json.dumps({"event_id": ev["id"], "memory_id": memory_id, "source": "distill"}),
                    _now(),
                ),
            )

            promoted.append({"event_id": ev["id"], "memory_id": memory_id, "category": category})

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "dry_run": False,
            "threshold": threshold,
            "promoted_count": len(promoted),
            "promotions": promoted,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: promote
# ---------------------------------------------------------------------------

def tool_promote(
    agent_id: str = "mcp-client",
    event_id: int = 0,
    category: str | None = None,
    scope: str | None = None,
    content: str | None = None,
    confidence: float | None = None,
    tags: str | None = None,
) -> dict:
    """Elevate a single event into a durable memory.

    Mirrors cmd_promote (line ~9303 of _impl.py).
    """
    try:
        conn = _db()
        event = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if not event:
            conn.close()
            return {"ok": False, "error": f"Event {event_id} not found"}

        tags_json = json.dumps(tags.split(",")) if tags else None
        effective_content = content or event["summary"]
        effective_category = (
            category
            or _EVENT_TYPE_TO_CATEGORY.get(
                event["event_type"],
                _infer_category_from_content(event["summary"]),
            )
        )
        effective_confidence = confidence if confidence is not None else 0.9
        effective_scope = scope or "global"

        cursor = conn.execute(
            "INSERT INTO memories (agent_id, category, scope, content, confidence, "
            "source_event_id, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["agent_id"],
                effective_category,
                effective_scope,
                effective_content,
                effective_confidence,
                event_id,
                tags_json,
                _now(),
                _now(),
            ),
        )
        memory_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
            "VALUES (?, 'memory_promoted', ?, ?, ?)",
            (
                event["agent_id"],
                f"Promoted event #{event_id} to memory #{memory_id}",
                json.dumps({"event_id": event_id, "memory_id": memory_id}),
                _now(),
            ),
        )

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "memory_id": memory_id,
            "from_event": event_id,
            "category": effective_category,
            "scope": effective_scope,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: dreams
# ---------------------------------------------------------------------------

def tool_dreams(
    agent_id: str = "mcp-client",
    status: str = "incubating",
    limit: int = 20,
) -> dict:
    """Show recent dream hypotheses from the incubation queue.

    Mirrors cmd_dreams (line ~9577 of _impl.py).
    """
    try:
        conn = _db()

        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dream_hypotheses'"
        ).fetchone()
        if not tbl:
            conn.close()
            return {
                "ok": True,
                "hypotheses": [],
                "message": "dream_hypotheses table not found — run a consolidation cycle first",
            }

        rows = conn.execute(
            """
            SELECT dh.id, dh.memory_a_id, dh.memory_b_id, dh.hypothesis_memory_id,
                   dh.similarity, dh.status, dh.created_at, dh.promoted_at, dh.retired_at,
                   dh.retirement_reason,
                   m.content  AS hypothesis_text,
                   ma.scope   AS scope_a,   mb.scope   AS scope_b,
                   ma.content AS content_a, mb.content AS content_b
            FROM dream_hypotheses dh
            LEFT JOIN memories m  ON m.id  = dh.hypothesis_memory_id
            LEFT JOIN memories ma ON ma.id = dh.memory_a_id
            LEFT JOIN memories mb ON mb.id = dh.memory_b_id
            WHERE dh.status = ?
            ORDER BY dh.created_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()

        hypotheses = [
            {
                "id": row["id"],
                "memory_a": {
                    "id": row["memory_a_id"],
                    "scope": row["scope_a"],
                    "snippet": (row["content_a"] or "")[:80],
                },
                "memory_b": {
                    "id": row["memory_b_id"],
                    "scope": row["scope_b"],
                    "snippet": (row["content_b"] or "")[:80],
                },
                "hypothesis_memory_id": row["hypothesis_memory_id"],
                "hypothesis": (row["hypothesis_text"] or "")[:200],
                "similarity": row["similarity"],
                "status": row["status"],
                "created_at": row["created_at"],
                "promoted_at": row["promoted_at"],
                "retired_at": row["retired_at"],
            }
            for row in rows
        ]

        conn.close()
        return {
            "ok": True,
            "status": status,
            "count": len(hypotheses),
            "hypotheses": hypotheses,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="knowledge_index",
        description=(
            "Generate a browsable JSON catalog of all knowledge in brain.db — "
            "memories grouped by category, entities by type, and recent decisions. "
            "Inspired by Karpathy's LLM Wiki pattern: a snapshot that lets agents "
            "quickly orient, see what's known, and identify gaps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "category": {
                    "type": "string",
                    "description": "Filter memories by category (e.g. 'lesson', 'decision')",
                },
                "scope": {
                    "type": "string",
                    "description": "Filter memories by scope (e.g. 'global', 'project:myproject')",
                },
            },
        },
    ),
    Tool(
        name="knowledge_report",
        description=(
            "Compile a structured JSON report of brain knowledge — memories by category, "
            "entities with relations, recent decisions, events, and affect state. "
            "Supports optional topic, agent, and entity-focus filters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "topic": {
                    "type": "string",
                    "description": "Optional keyword filter applied across memories, entities, decisions",
                },
                "agent_filter": {
                    "type": "string",
                    "description": "Restrict results to a specific agent ID",
                },
                "entity": {
                    "type": "string",
                    "description": "Focus the report on a specific entity (name or partial match)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items per section (default 20)",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="distill",
        description=(
            "Batch-promote high-importance events that have not yet been promoted to "
            "durable memories. Use dry_run=true to preview what would be promoted. "
            "Threshold controls minimum importance score (0.0–1.0)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "threshold": {
                    "type": "number",
                    "description": "Minimum importance score to consider (default 0.7)",
                    "default": 0.7,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of promotions in one call (default 50)",
                    "default": 50,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview only — do not write to DB",
                    "default": False,
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 datetime; only consider events after this time",
                },
                "filter_agent": {
                    "type": "string",
                    "description": "Restrict to events from a specific agent ID",
                },
                "event_types": {
                    "type": "string",
                    "description": "Comma-separated list of event types to include",
                },
            },
        },
    ),
    Tool(
        name="promote",
        description=(
            "Elevate a single event into a durable memory. "
            "Optionally override category, scope, content, confidence, and tags."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "event_id": {
                    "type": "integer",
                    "description": "ID of the event to promote",
                },
                "category": {
                    "type": "string",
                    "description": "Override inferred memory category",
                },
                "scope": {
                    "type": "string",
                    "description": "Override scope (default: 'global')",
                },
                "content": {
                    "type": "string",
                    "description": "Override memory content (default: event summary)",
                },
                "confidence": {
                    "type": "number",
                    "description": "Override confidence score 0.0–1.0 (default: 0.9)",
                },
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags",
                },
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="dreams",
        description=(
            "Show dream hypotheses from the incubation queue — bisociation candidates "
            "generated during consolidation cycles. Useful for discovering unexpected "
            "cross-domain connections between memories."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "status": {
                    "type": "string",
                    "description": "Filter by status: 'incubating', 'promoted', 'retired' (default: 'incubating')",
                    "default": "incubating",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of hypotheses to return (default 20)",
                    "default": 20,
                },
            },
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch table (tool name -> callable)
# ---------------------------------------------------------------------------

DISPATCH: dict = {
    "knowledge_index": tool_knowledge_index,
    "knowledge_report": tool_knowledge_report,
    "distill": tool_distill,
    "promote": tool_promote,
    "dreams": tool_dreams,
}
