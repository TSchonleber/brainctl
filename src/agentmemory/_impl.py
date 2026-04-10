"""Full brainctl implementation — imported by command modules."""
"""
brainctl v3 — Unified agent memory CLI

The single interface for agent memory across runtimes and frameworks
to read, write, search, and maintain the shared memory spine.

Database: $BRAIN_DB or $BRAINCTL_HOME/db/brain.db (default: ~/agentmemory/db/brain.db)
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import hashlib
import math
import shutil
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

from agentmemory.paths import get_backups_dir, get_blobs_dir, get_brain_home, get_db_path

logger = logging.getLogger(__name__)

# Adaptive salience routing
try:
    sys.path.insert(0, str(Path.home() / "agentmemory" / "bin"))
    import salience_routing as _sal
    _SAL_AVAILABLE = True
except Exception:
    _SAL_AVAILABLE = False

# Query intent classification
try:
    from intent_classifier import classify_intent as _classify_intent
    _INTENT_AVAILABLE = True
except Exception:
    _INTENT_AVAILABLE = False

# Built-in lightweight intent classifier fallback
class _BuiltinIntentResult:
    """Lightweight intent classification result used when intent_classifier module is unavailable."""
    __slots__ = ("intent", "confidence", "matched_rule", "format_hint", "tables")
    def __init__(self, intent, confidence, matched_rule, format_hint, tables):
        self.intent = intent
        self.confidence = confidence
        self.matched_rule = matched_rule
        self.format_hint = format_hint
        self.tables = tables

def _builtin_classify_intent(query):
    """Rule-based intent classifier — inline fallback for."""
    q = query.lower()
    if any(w in q for w in ['who ', 'person', 'agent', 'team', 'assigned']):
        return _BuiltinIntentResult('entity_lookup', 0.8, 'keyword:entity',
                                     'Show entity details with relations',
                                     ['memories', 'events', 'context'])
    if any(w in q for w in ['what happened', 'when did', 'history', 'timeline', 'log']):
        return _BuiltinIntentResult('event_lookup', 0.8, 'keyword:event',
                                     'Show events in chronological order',
                                     ['events', 'memories', 'context'])
    if any(w in q for w in ['how to', 'how do', 'procedure', 'steps', 'guide']):
        return _BuiltinIntentResult('procedural', 0.7, 'keyword:procedural',
                                     'Show step-by-step instructions',
                                     ['memories', 'context', 'events'])
    if any(w in q for w in ['why ', 'decision', 'rationale', 'reason']):
        return _BuiltinIntentResult('decision_lookup', 0.8, 'keyword:decision',
                                     'Show decisions with rationale',
                                     ['memories', 'events', 'context'])
    if any(w in q for w in ['related', 'connected', 'depends', 'link']):
        return _BuiltinIntentResult('graph_traversal', 0.7, 'keyword:graph',
                                     'Show connected nodes and edges',
                                     ['memories', 'events', 'context'])
    return _BuiltinIntentResult('general', 0.5, 'default',
                                 'Standard search results',
                                 ['memories', 'events', 'context'])

# Quantum amplitude scorer
try:
    sys.path.insert(0, str(Path.home() / "bin" / "lib"))
    from quantum_retrieval import quantum_rerank as _quantum_rerank, update_phase_after_recall as _quantum_phase_update
    _QUANTUM_AVAILABLE = True
except Exception:
    _QUANTUM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = get_db_path()
BLOBS_DIR = get_blobs_dir()
BACKUPS_DIR = get_backups_dir()
VERSION = "1.0.1"

VALID_MEMORY_CATEGORIES = {
    "identity", "user", "environment", "convention",
    "project", "decision", "lesson", "preference", "integration"
}

VALID_EVENT_TYPES = {
    "observation", "result", "decision", "error", "handoff",
    "task_update", "artifact", "session_start", "session_end",
    "memory_promoted", "memory_retired", "warning", "stale_context"
}

VALID_TASK_STATUSES = {"pending", "in_progress", "blocked", "completed", "cancelled"}
VALID_PRIORITIES = {"critical", "high", "medium", "low"}

# FTS5 special characters that cause sqlite3.OperationalError when unescaped.
# Strip them before passing any user query to a MATCH clause.
_FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^]')


def _sanitize_fts_query(query: str) -> str:
    """Remove FTS5 special characters to prevent syntax errors.

    Strips: . & | * \" ( ) - @ ^
    Then collapses extra whitespace.  Returns an empty string if nothing
    remains so callers can skip the MATCH clause gracefully.
    """
    cleaned = _FTS5_SPECIAL.sub(" ", query or "")
    return re.sub(r"\s+", " ", cleaned).strip()

# Temporal recency decay constants (lambda) — configurable per scope
# half-life: global ~70d, project ~23d, agent ~14d
RECENCY_LAMBDA = {
    "global": 0.01,
    "project": 0.03,
    "agent": 0.05,
}

# ---------------------------------------------------------------------------
# Category-based half-life (days) — inspired by TORMENT retention tiers.
# Memories in higher tiers persist longer. "protected" temporal_class or
# category=identity memories never decay (infinite half-life).
# ---------------------------------------------------------------------------
CATEGORY_HALF_LIFE = {
    "identity":    365.0,   # ~1 year — who the user/agent is
    "user":        365.0,
    "convention":  180.0,   # ~6 months — project standards
    "environment": 180.0,
    "preference":  120.0,   # ~4 months — user likes/dislikes
    "project":      90.0,   # ~3 months — project-specific facts
    "decision":     60.0,   # ~2 months — decisions made
    "lesson":       60.0,
    "integration":  30.0,   # ~1 month — integration details change fast
}
DEFAULT_HALF_LIFE = 90.0  # fallback

# Minimum score floor — prevents ghost memories from polluting results
DECAY_FLOOR = 0.03

# Hard memory cap per agent — emergency compression fires above this
HARD_MEMORY_CAP = 10_000
HARD_MEMORY_TARGET = 8_000

# Pre-ingest duplicate FTS similarity threshold (0-1 range for normalized score)
DEDUP_FTS_MIN_RESULTS = 1
DEDUP_CONTENT_SIMILARITY_THRESHOLD = 0.85  # Jaccard word overlap

# ---------------------------------------------------------------------------
# Temporal recency helpers
# ---------------------------------------------------------------------------

def _scope_lambda(scope):
    """Return lambda decay constant for the given scope string."""
    if scope and scope.startswith("project:"):
        return RECENCY_LAMBDA["project"]
    if scope and scope.startswith("agent:"):
        return RECENCY_LAMBDA["agent"]
    return RECENCY_LAMBDA["global"]

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_ts() -> str:
    """Return current UTC time as ISO 8601 string."""
    return _utc_now_iso()


def _days_since(created_at_str):
    """Return float days elapsed since the given SQLite/ISO timestamp."""
    if not created_at_str:
        return 0.0
    try:
        ts = created_at_str.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts, fmt)
                    break
                except ValueError:
                    continue
            else:
                return 0.0
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0

def _temporal_weight(created_at_str, scope=None):
    """Return recency weight in (0, 1] using exponential decay."""
    return math.exp(-_scope_lambda(scope) * _days_since(created_at_str))


def _halflife_decay(memory_row):
    """Query-time half-life decay — inspired by TORMENT.

    Uses 2^(-age/half_life) where age is measured from the more recent of
    created_at and last_recalled_at (reinforcement resets the clock).
    Protected memories and identity categories return 1.0 (no decay).
    Returns a score in [DECAY_FLOOR, 1.0].
    """
    # Protected memories never decay
    if memory_row.get("protected") or memory_row.get("temporal_class") == "permanent":
        return 1.0

    category = memory_row.get("category", "")
    half_life = CATEGORY_HALF_LIFE.get(category, DEFAULT_HALF_LIFE)

    # Use the more recent of created_at and last_recalled_at
    created = memory_row.get("created_at")
    recalled = memory_row.get("last_recalled_at")
    reference_ts = recalled if recalled else created

    age_days = _days_since(reference_ts)
    if age_days <= 0 or half_life <= 0:
        return 1.0

    decay = 2.0 ** (-age_days / half_life)
    return max(DECAY_FLOOR, decay)


def _jaccard_word_similarity(a, b):
    """Word-level Jaccard similarity between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)

def _is_reflexion(r):
    """Return True if a memory result is tagged 'reflexion'."""
    tags_raw = r.get("tags")
    if not tags_raw:
        return False
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        return "reflexion" in tags
    except (ValueError, TypeError):
        return False

def _age_str(created_at_str):
    """Return human-readable relative age like '3 days ago'."""
    days = _days_since(created_at_str)
    if days < 1 / 1440:
        return "just now"
    if days < 1 / 24:
        minutes = int(days * 1440)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if days < 1:
        hours = int(days * 24)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days < 7:
        d = int(days)
        return f"{d} day{'s' if d != 1 else ''} ago"
    if days < 30:
        w = int(days / 7)
        return f"{w} week{'s' if w != 1 else ''} ago"
    if days < 365:
        m = int(days / 30)
        return f"{m} month{'s' if m != 1 else ''} ago"
    y = int(days / 365)
    return f"{y} year{'s' if y != 1 else ''} ago"

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    global DB_PATH, BLOBS_DIR, BACKUPS_DIR
    if os.environ.get("BRAIN_DB") or os.environ.get("BRAINCTL_HOME"):
        DB_PATH = get_db_path()
        BLOBS_DIR = get_blobs_dir()
        BACKUPS_DIR = get_backups_dir()
    if not DB_PATH.exists():
        json_out({"error": f"Database not found at {DB_PATH}",
                  "hint": "Run 'brainctl init' to create a new database, or set BRAIN_DB env var."})
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_agent(db, agent_id):
    """Auto-register an agent if it doesn't exist. Prevents FK violations on fresh DBs."""
    if not agent_id:
        return
    try:
        exists = db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not exists:
            db.execute(
                "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
                "VALUES (?, ?, 'cli', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
                (agent_id, agent_id)
            )
            db.commit()
    except Exception:
        pass  # agents table may not exist in minimal schemas

def log_access(conn, agent_id, action, target_table=None, target_id=None, query=None, result_count=None, tokens_consumed=None):
    conn.execute(
        "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count, tokens_consumed) VALUES (?,?,?,?,?,?,?)",
        (agent_id, action, target_table, target_id, query, result_count, tokens_consumed)
    )


def _estimate_tokens(obj) -> int:
    """Estimate tokens for a JSON-serialisable object. 1 token ≈ 4 chars."""
    try:
        return max(1, len(json.dumps(obj, default=str)) // 4)
    except Exception:
        return 1

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def json_out(data, compact=False):
    """Output data as JSON. compact=True uses minimal whitespace for lower token consumption."""
    if compact:
        print(json.dumps(data, separators=(",", ":"), default=str))
    else:
        print(json.dumps(data, indent=2, default=str))


def oneline_out(items, fields=("content", "summary", "name", "title", "rationale", "source_ref")):
    """Print one-line-per-result format: ID | first available text field. Ultra-compact for agents."""
    if isinstance(items, dict):
        # Flatten search results dict
        flat = []
        for key, val in items.items():
            if isinstance(val, list):
                flat.extend(val)
        items = flat
    if not isinstance(items, list):
        print(json.dumps(items, separators=(",", ":"), default=str))
        return
    for item in items:
        item_id = item.get("id", "?")
        text = ""
        for f in fields:
            if item.get(f):
                text = str(item[f])[:120]
                break
        ttype = item.get("type", item.get("category", item.get("entity_type", "")))
        conf = item.get("confidence") or item.get("final_score") or ""
        conf_str = f" [{conf:.2f}]" if isinstance(conf, (int, float)) else ""
        print(f"{item_id}|{ttype}|{text}{conf_str}")

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]


def _require_nonempty_str(value, field: str, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if max_len is not None and len(value) > max_len:
        raise ValueError(f"{field} exceeds max length {max_len}")
    return value


def _optional_str(value, field: str, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        return None
    if max_len is not None and len(value) > max_len:
        raise ValueError(f"{field} exceeds max length {max_len}")
    return value


def _optional_json_string(value, field: str) -> str | None:
    text = _optional_str(value, field, 20000)
    if text is None:
        return None
    json.loads(text)
    return text


def _optional_int(value, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _validate_handoff_fields(*, agent_id: str, goal: str | None = None, current_state: str | None = None,
                             open_loops: str | None = None, next_step: str | None = None,
                             title: str | None = None, session_id: str | None = None,
                             chat_id: str | None = None, thread_id: str | None = None,
                             user_id: str | None = None, project: str | None = None,
                             scope: str | None = None, status: str | None = None,
                             recent_tail: str | None = None, decisions_json: str | None = None,
                             entities_json: str | None = None, tasks_json: str | None = None,
                             facts_json: str | None = None, source_event_id=None,
                             expires_at: str | None = None) -> dict:
    allowed_status = {"pending", "consumed", "pinned", "expired"}
    data = {
        "agent_id": _require_nonempty_str(agent_id, "agent_id", 255),
        "goal": _require_nonempty_str(goal, "goal", 4000) if goal is not None else None,
        "current_state": _require_nonempty_str(current_state, "current_state", 8000) if current_state is not None else None,
        "open_loops": _require_nonempty_str(open_loops, "open_loops", 8000) if open_loops is not None else None,
        "next_step": _require_nonempty_str(next_step, "next_step", 4000) if next_step is not None else None,
        "title": _optional_str(title, "title", 255),
        "session_id": _optional_str(session_id, "session_id", 255),
        "chat_id": _optional_str(chat_id, "chat_id", 255),
        "thread_id": _optional_str(thread_id, "thread_id", 255),
        "user_id": _optional_str(user_id, "user_id", 255),
        "project": _optional_str(project, "project", 255),
        "scope": _optional_str(scope, "scope", 255) or "global",
        "status": _optional_str(status, "status", 32) or "pending",
        "recent_tail": _optional_str(recent_tail, "recent_tail", 12000),
        "decisions_json": _optional_json_string(decisions_json, "decisions_json"),
        "entities_json": _optional_json_string(entities_json, "entities_json"),
        "tasks_json": _optional_json_string(tasks_json, "tasks_json"),
        "facts_json": _optional_json_string(facts_json, "facts_json"),
        "source_event_id": _optional_int(source_event_id, "source_event_id"),
        "expires_at": _optional_str(expires_at, "expires_at", 64),
    }
    if data["status"] not in allowed_status:
        raise ValueError("status must be one of: pending, consumed, pinned, expired")
    if data["expires_at"] is not None:
        datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    return data

# ---------------------------------------------------------------------------
# AGENT commands
# ---------------------------------------------------------------------------

def cmd_agent_register(args):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO agents (id, display_name, agent_type, adapter_info, status, last_seen_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now'))",
        (args.id, args.name, args.type, args.adapter_info)
    )
    db.commit()
    json_out({"ok": True, "agent_id": args.id})

def cmd_agent_list(args):
    db = get_db()
    rows = db.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    json_out(rows_to_list(rows))

def cmd_agent_ping(args):
    db = get_db()
    db.execute("UPDATE agents SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?", (args.agent,))
    db.commit()
    json_out({"ok": True, "agent": args.agent, "pinged_at": datetime.utcnow().isoformat()})

# ---------------------------------------------------------------------------
# ENTITY commands — Knowledge graph entity registry
# ---------------------------------------------------------------------------

VALID_ENTITY_TYPES = {
    "person", "organization", "project", "tool", "concept",
    "agent", "location", "event", "document", "service", "other"
}


def cmd_entity_create(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    name = args.name
    entity_type = args.entity_type
    scope = getattr(args, "scope", "global") or "global"
    confidence = getattr(args, "confidence", None)

    properties = "{}"
    if args.properties:
        try:
            properties = json.dumps(json.loads(args.properties))
        except json.JSONDecodeError:
            print("ERROR: --properties must be valid JSON", file=sys.stderr)
            sys.exit(1)

    observations = "[]"
    if args.observations:
        obs_list = [o.strip() for o in args.observations.split(";") if o.strip()]
        observations = json.dumps(obs_list)

    existing = db.execute(
        "SELECT id FROM entities WHERE name = ? AND scope = ? AND retired_at IS NULL",
        (name, scope)
    ).fetchone()
    if existing:
        json_out({"ok": False, "error": f"Entity '{name}' already exists in scope '{scope}' (id={existing['id']})"})
        return

    base_confidence = confidence if confidence is not None else 1.0
    cur = db.execute(
        "INSERT INTO entities (name, entity_type, properties, observations, agent_id, confidence, scope) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, entity_type, properties, observations, agent_id, base_confidence, scope)
    )
    entity_id = cur.lastrowid
    log_access(db, agent_id, "write", "entities", entity_id)

    # Embed on write
    embedded = False
    try:
        embed_text = f"{name} ({entity_type}): {' '.join(json.loads(observations))}"
        blob = _embed_query_safe(embed_text)
        if blob:
            db_vec = _try_get_db_with_vec()
            if db_vec:
                db_vec.execute("INSERT OR REPLACE INTO vec_entities(rowid, embedding) VALUES (?, ?)",
                               (entity_id, blob))
                db_vec.commit()
                db_vec.close()
                embedded = True
    except Exception:
        pass

    db.commit()
    json_out({"ok": True, "entity_id": entity_id, "name": name, "entity_type": entity_type,
              "scope": scope, "embedded": embedded})


def cmd_entity_get(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    identifier = args.identifier

    if identifier.isdigit():
        row = db.execute("SELECT * FROM entities WHERE id = ? AND retired_at IS NULL", (int(identifier),)).fetchone()
    else:
        row = db.execute("SELECT * FROM entities WHERE name = ? AND retired_at IS NULL", (identifier,)).fetchone()

    if not row:
        json_out({"ok": False, "error": f"Entity not found: {identifier}"})
        return

    entity = dict(row)
    entity["properties"] = json.loads(entity["properties"])
    entity["observations"] = json.loads(entity["observations"])

    # Fetch relations via knowledge_edges
    relations = []
    edges = db.execute(
        "SELECT * FROM knowledge_edges "
        "WHERE (source_table = 'entities' AND source_id = ?) "
        "   OR (target_table = 'entities' AND target_id = ?)",
        (entity["id"], entity["id"])
    ).fetchall()

    for edge in edges:
        e = dict(edge)
        if e["source_table"] == "entities" and e["source_id"] == entity["id"]:
            other = db.execute("SELECT name, entity_type FROM entities WHERE id = ?",
                               (e["target_id"],)).fetchone() if e["target_table"] == "entities" else None
            relations.append({"direction": "outgoing", "relation": e["relation_type"],
                              "target_table": e["target_table"], "target_id": e["target_id"],
                              "target_name": other["name"] if other else None})
        else:
            other = db.execute("SELECT name, entity_type FROM entities WHERE id = ?",
                               (e["source_id"],)).fetchone() if e["source_table"] == "entities" else None
            relations.append({"direction": "incoming", "relation": e["relation_type"],
                              "source_table": e["source_table"], "source_id": e["source_id"],
                              "source_name": other["name"] if other else None})

    entity["relations"] = relations
    log_access(db, agent_id, "read", "entities", entity["id"])
    db.commit()
    json_out(entity)


def cmd_entity_search(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    query = args.query
    limit = getattr(args, "limit", 20) or 20
    entity_type = getattr(args, "entity_type", None)

    safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
    if not safe_query:
        json_out({"ok": False, "error": "Empty query"})
        return

    fts_query = " OR ".join(safe_query.split())

    if entity_type:
        rows = db.execute(
            "SELECT e.* FROM entities_fts fts "
            "JOIN entities e ON e.id = fts.rowid "
            "WHERE entities_fts MATCH ? AND e.entity_type = ? AND e.retired_at IS NULL "
            "ORDER BY rank LIMIT ?",
            (fts_query, entity_type, limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT e.* FROM entities_fts fts "
            "JOIN entities e ON e.id = fts.rowid "
            "WHERE entities_fts MATCH ? AND e.retired_at IS NULL "
            "ORDER BY rank LIMIT ?",
            (fts_query, limit)
        ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["properties"] = json.loads(d["properties"])
        d["observations"] = json.loads(d["observations"])
        results.append(d)

    log_access(db, agent_id, "search", "entities", query=query, result_count=len(results))
    db.commit()
    json_out({"ok": True, "count": len(results), "entities": results})


def cmd_entity_list(args):
    db = get_db()
    entity_type = getattr(args, "entity_type", None)
    scope = getattr(args, "scope", None)
    limit = getattr(args, "limit", 50) or 50

    conditions = ["retired_at IS NULL"]
    params = []
    if entity_type:
        conditions.append("entity_type = ?")
        params.append(entity_type)
    if scope:
        conditions.append("scope = ?")
        params.append(scope)

    where = " AND ".join(conditions)
    params.append(limit)
    rows = db.execute(f"SELECT * FROM entities WHERE {where} ORDER BY updated_at DESC LIMIT ?", params).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["properties"] = json.loads(d["properties"])
        d["observations"] = json.loads(d["observations"])
        results.append(d)

    json_out({"ok": True, "count": len(results), "entities": results})


def cmd_entity_update(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    identifier = args.identifier

    if identifier.isdigit():
        row = db.execute("SELECT * FROM entities WHERE id = ? AND retired_at IS NULL", (int(identifier),)).fetchone()
    else:
        row = db.execute("SELECT * FROM entities WHERE name = ? AND retired_at IS NULL", (identifier,)).fetchone()

    if not row:
        json_out({"ok": False, "error": f"Entity not found: {identifier}"})
        return

    entity_id = row["id"]
    updates = []
    params = []

    if args.properties:
        try:
            new_props = json.loads(args.properties)
            old_props = json.loads(row["properties"])
            old_props.update(new_props)
            updates.append("properties = ?")
            params.append(json.dumps(old_props))
        except json.JSONDecodeError:
            print("ERROR: --properties must be valid JSON", file=sys.stderr)
            sys.exit(1)

    if getattr(args, "name", None):
        updates.append("name = ?")
        params.append(args.name)

    if getattr(args, "entity_type", None):
        updates.append("entity_type = ?")
        params.append(args.entity_type)

    if not updates:
        json_out({"ok": False, "error": "Nothing to update"})
        return

    updates.append("updated_at = datetime('now')")
    params.append(entity_id)
    db.execute(f"UPDATE entities SET {', '.join(updates)} WHERE id = ?", params)
    log_access(db, agent_id, "write", "entities", entity_id)
    db.commit()
    json_out({"ok": True, "entity_id": entity_id, "updated_fields": len(updates) - 1})


def cmd_entity_observe(args):
    """Add observations to an existing entity."""
    db = get_db()
    agent_id = args.agent or "unknown"
    identifier = args.identifier

    if identifier.isdigit():
        row = db.execute("SELECT * FROM entities WHERE id = ? AND retired_at IS NULL", (int(identifier),)).fetchone()
    else:
        row = db.execute("SELECT * FROM entities WHERE name = ? AND retired_at IS NULL", (identifier,)).fetchone()

    if not row:
        json_out({"ok": False, "error": f"Entity not found: {identifier}"})
        return

    entity_id = row["id"]
    current_obs = json.loads(row["observations"])
    new_obs = [o.strip() for o in args.observations.split(";") if o.strip()]
    added = [o for o in new_obs if o not in current_obs]
    current_obs.extend(added)

    db.execute("UPDATE entities SET observations = ?, updated_at = datetime('now') WHERE id = ?",
               (json.dumps(current_obs), entity_id))
    log_access(db, agent_id, "write", "entities", entity_id)

    # Re-embed with updated observations
    try:
        embed_text = f"{row['name']} ({row['entity_type']}): {' '.join(current_obs)}"
        blob = _embed_query_safe(embed_text)
        if blob:
            db_vec = _try_get_db_with_vec()
            if db_vec:
                db_vec.execute("INSERT OR REPLACE INTO vec_entities(rowid, embedding) VALUES (?, ?)",
                               (entity_id, blob))
                db_vec.commit()
                db_vec.close()
    except Exception:
        pass

    db.commit()
    json_out({"ok": True, "entity_id": entity_id, "added": added, "total_observations": len(current_obs)})


def cmd_entity_relate(args):
    """Create a relation between two entities using knowledge_edges."""
    db = get_db()
    agent_id = args.agent or "unknown"

    from_name = args.from_entity
    if from_name.isdigit():
        from_row = db.execute("SELECT id FROM entities WHERE id = ? AND retired_at IS NULL", (int(from_name),)).fetchone()
    else:
        from_row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (from_name,)).fetchone()
    if not from_row:
        json_out({"ok": False, "error": f"From entity not found: {from_name}"})
        return

    to_name = args.to_entity
    if to_name.isdigit():
        to_row = db.execute("SELECT id FROM entities WHERE id = ? AND retired_at IS NULL", (int(to_name),)).fetchone()
    else:
        to_row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (to_name,)).fetchone()
    if not to_row:
        json_out({"ok": False, "error": f"To entity not found: {to_name}"})
        return

    relation = args.relation
    confidence = getattr(args, "confidence", None) or 1.0

    try:
        db.execute(
            "INSERT INTO knowledge_edges (source_table, source_id, target_table, target_id, "
            "relation_type, weight, agent_id) VALUES ('entities', ?, 'entities', ?, ?, ?, ?)",
            (from_row["id"], to_row["id"], relation, confidence, agent_id)
        )
    except sqlite3.IntegrityError:
        json_out({"ok": False, "error": f"Relation '{relation}' already exists between these entities"})
        return

    log_access(db, agent_id, "write", "knowledge_edges")
    db.commit()
    json_out({"ok": True, "from_id": from_row["id"], "to_id": to_row["id"], "relation": relation})


def cmd_entity_delete(args):
    """Soft-delete an entity."""
    db = get_db()
    agent_id = args.agent or "unknown"
    identifier = args.identifier

    if identifier.isdigit():
        row = db.execute("SELECT id, name FROM entities WHERE id = ? AND retired_at IS NULL", (int(identifier),)).fetchone()
    else:
        row = db.execute("SELECT id, name FROM entities WHERE name = ? AND retired_at IS NULL", (identifier,)).fetchone()

    if not row:
        json_out({"ok": False, "error": f"Entity not found: {identifier}"})
        return

    db.execute("UPDATE entities SET retired_at = datetime('now') WHERE id = ?", (row["id"],))
    log_access(db, agent_id, "write", "entities", row["id"])
    db.commit()
    json_out({"ok": True, "retired_id": row["id"], "name": row["name"]})


# ---------------------------------------------------------------------------
# TRIGGER commands  (prospective memory)
# ---------------------------------------------------------------------------

def cmd_trigger_create(args):
    """Create a prospective memory trigger."""
    db = get_db()
    agent_id = args.agent or "unknown"
    condition = args.condition
    keywords = args.keywords
    action_text = args.action
    entity_id = None
    memory_id = getattr(args, "memory", None)
    priority = getattr(args, "priority", "medium") or "medium"
    expires_at = getattr(args, "expires", None)

    # Resolve entity name to ID if provided
    entity_name = getattr(args, "entity", None)
    if entity_name:
        if entity_name.isdigit():
            row = db.execute("SELECT id FROM entities WHERE id = ? AND retired_at IS NULL", (int(entity_name),)).fetchone()
        else:
            row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (entity_name,)).fetchone()
        if not row:
            json_out({"ok": False, "error": f"Entity not found: {entity_name}"})
            return
        entity_id = row["id"]

    cur = db.execute(
        "INSERT INTO memory_triggers (agent_id, trigger_condition, trigger_keywords, action, entity_id, memory_id, priority, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, condition, keywords, action_text, entity_id, memory_id, priority, expires_at)
    )
    trigger_id = cur.lastrowid
    log_access(db, agent_id, "write", "memory_triggers", trigger_id)
    db.commit()
    json_out({"ok": True, "trigger_id": trigger_id, "condition": condition, "keywords": keywords})


def cmd_trigger_list(args):
    """List memory triggers, optionally filtered by status."""
    db = get_db()
    status = getattr(args, "status", None)
    if status:
        rows = db.execute(
            "SELECT * FROM memory_triggers WHERE status = ? ORDER BY created_at DESC",
            (status,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM memory_triggers ORDER BY created_at DESC"
        ).fetchall()
    json_out(rows_to_list(rows))


def _check_triggers(db, query_text):
    """Check active triggers against a query string. Returns list of matching triggers."""
    now_rows = db.execute("SELECT datetime('now') as now").fetchone()
    now = now_rows["now"] if now_rows else None

    # Expire overdue triggers
    if now:
        db.execute(
            "UPDATE memory_triggers SET status = 'expired' WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,)
        )

    rows = db.execute(
        "SELECT * FROM memory_triggers WHERE status = 'active'"
    ).fetchall()

    query_lower = query_text.lower()
    query_words = set(query_lower.split())
    matches = []
    for row in rows:
        kw_list = [k.strip().lower() for k in row["trigger_keywords"].split(",") if k.strip()]
        # Match if any keyword appears in query text (substring or word match)
        matched_kw = [kw for kw in kw_list if kw in query_lower or kw in query_words]
        if matched_kw:
            trigger = dict(row)
            trigger["matched_keywords"] = matched_kw
            matches.append(trigger)

    # Sort by priority
    prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    matches.sort(key=lambda t: prio_order.get(t.get("priority", "medium"), 2))
    return matches


def cmd_trigger_check(args):
    """Check if any active triggers match a query."""
    db = get_db()
    query_text = args.query
    matches = _check_triggers(db, query_text)
    db.commit()
    json_out({"ok": True, "query": query_text, "matched_triggers": matches, "count": len(matches)})


def cmd_trigger_fire(args):
    """Mark a trigger as fired."""
    db = get_db()
    agent_id = args.agent or "unknown"
    trigger_id = args.id
    row = db.execute("SELECT * FROM memory_triggers WHERE id = ?", (trigger_id,)).fetchone()
    if not row:
        json_out({"ok": False, "error": f"Trigger not found: {trigger_id}"})
        return
    if row["status"] != "active":
        json_out({"ok": False, "error": f"Trigger {trigger_id} is already {row['status']}"})
        return
    db.execute(
        "UPDATE memory_triggers SET status = 'fired', fired_at = datetime('now') WHERE id = ?",
        (trigger_id,)
    )
    log_access(db, agent_id, "write", "memory_triggers", trigger_id)
    db.commit()
    json_out({"ok": True, "trigger_id": trigger_id, "status": "fired", "action": row["action"]})


def cmd_trigger_cancel(args):
    """Cancel a trigger."""
    db = get_db()
    agent_id = args.agent or "unknown"
    trigger_id = args.id
    row = db.execute("SELECT * FROM memory_triggers WHERE id = ?", (trigger_id,)).fetchone()
    if not row:
        json_out({"ok": False, "error": f"Trigger not found: {trigger_id}"})
        return
    if row["status"] not in ("active",):
        json_out({"ok": False, "error": f"Trigger {trigger_id} is already {row['status']}"})
        return
    db.execute(
        "UPDATE memory_triggers SET status = 'cancelled' WHERE id = ?",
        (trigger_id,)
    )
    log_access(db, agent_id, "write", "memory_triggers", trigger_id)
    db.commit()
    json_out({"ok": True, "trigger_id": trigger_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# MEMORY commands
# ---------------------------------------------------------------------------

REFLEXION_BOOST = 1.5  # score multiplier for reflexion-tagged memories in retrieval

def cmd_memory_add(args):
    db = get_db()
    # --reflexion shorthand: force category=lesson, inject 'reflexion' tag
    if getattr(args, "reflexion", False):
        args.category = "lesson"
        existing_tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        if "reflexion" not in existing_tags:
            existing_tags.insert(0, "reflexion")
        args.tags = ",".join(existing_tags)
    if not args.category:
        import sys; print('error: --category is required unless --reflexion is set', file=sys.stderr); sys.exit(2)
    tags_json = json.dumps(args.tags.split(",")) if args.tags else None
    memory_type = getattr(args, "type", None) or "episodic"
    force_write = getattr(args, "force", False)
    dry_run_worthiness = getattr(args, "dry_run_worthiness", False)

    # Source-weighted confidence at write time
    # If the writing agent has domain expertise, scale confidence accordingly.
    # effective_confidence = base_confidence * (0.5 + 0.5 * expertise_strength)
    # Neutral strength=1.0 leaves confidence unchanged (1.0 * (0.5 + 0.5*1.0) = 1.0).
    base_confidence = args.confidence or 1.0
    source_weight_applied = 1.0
    try:
        mem_domain = _expertise_scope_to_domain(args.scope or "global") or args.category
        if mem_domain and args.agent:
            sw_row = db.execute(
                "SELECT strength FROM agent_expertise WHERE agent_id=? AND domain=?",
                (args.agent, mem_domain)
            ).fetchone()
            if sw_row:
                source_weight_applied = float(sw_row["strength"])
    except Exception:
        pass
    effective_confidence = round(min(1.0, base_confidence * (0.5 + 0.5 * source_weight_applied)), 6)

    # Surprise scoring — lightweight novelty check before W(m) gate
    surprise = None
    surprise_method = None
    blob = None  # reused for vec_memories insert below
    try:
        blob = _embed_query_safe(args.content)
    except Exception:
        pass
    try:
        surprise, surprise_method = _surprise_score(db, args.content, blob=blob)
    except Exception:
        surprise, surprise_method = 0.7, "error"

    # Lightweight W(m) worthiness pre-check using surprise score
    # worthiness = surprise * importance_estimate * (1 - redundancy) * arousal_boost
    # importance_estimate derived from confidence and category weight
    importance_estimate = effective_confidence * (0.5 + 0.5 * source_weight_applied)
    _pre_redundancy = 0.0
    if surprise is not None and surprise < 0.2:
        _pre_redundancy = 0.5  # high overlap implies some redundancy

    # Affect-modulated worthiness: high-arousal memories are more worth storing
    # Grounded in McGaugh (2004) emotional modulation of memory consolidation
    _arousal_boost = 1.0
    try:
        from agentmemory.affect import classify_affect, arousal_write_boost
        _affect = classify_affect(args.content)
        _arousal_boost = arousal_write_boost(_affect.get("arousal", 0.0))
    except Exception:
        pass  # affect module failure is never fatal

    _pre_worthiness = (surprise or 0.7) * importance_estimate * (1.0 - _pre_redundancy) * _arousal_boost
    if _pre_worthiness < 0.3 and not force_write:
        # Log rejected memory as observation event
        try:
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
                "VALUES (?, 'observation', ?, ?, ?)",
                (args.agent,
                 f"Memory rejected by W(m) gate: {args.content[:60]}",
                 json.dumps({
                     "content_preview": args.content[:120],
                     "surprise": surprise,
                     "surprise_method": surprise_method,
                     "importance_estimate": round(importance_estimate, 4),
                     "pre_worthiness": round(_pre_worthiness, 4),
                     "category": args.category,
                     "scope": args.scope or "global",
                 }),
                 _now_ts())
            )
            db.commit()
        except Exception:
            pass
        json_out({
            "ok": False,
            "rejected": True,
            "surprise_score": surprise,
            "surprise_method": surprise_method,
            "pre_worthiness": round(_pre_worthiness, 4),
            "reason": "Low surprise/worthiness — memory is too similar to existing content.",
            "hint": "Use --force to bypass the gate.",
        })
        return

    # W(m) worthiness gate — runs BEFORE INSERT
    # Deeper semantic gate using write_decision.py (embedding-based).
    worthiness_score = None
    worthiness_reason = ""
    worthiness_components = {}
    try:
        if blob and not force_write:
            import importlib.util as _ilu
            _wdpath = str(Path.home() / "agentmemory" / "bin" / "lib" / "write_decision.py")
            _spec = _ilu.spec_from_file_location("write_decision", _wdpath)
            _wd = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_wd)

            db_vec_gate = _try_get_db_with_vec()
            if db_vec_gate:
                try:
                    worthiness_score, worthiness_reason, worthiness_components = _wd.gate_write(
                        candidate_blob=blob,
                        confidence=effective_confidence,
                        temporal_class=None,  # infer from category
                        category=args.category,
                        scope=args.scope or "global",
                        db_vec=db_vec_gate,
                        force=False,
                        arousal_gain=_arousal_boost,
                    )
                finally:
                    db_vec_gate.close()
    except Exception as exc:
        logger.debug("W(m) gate failed (non-fatal): %s", exc)

    # --dry-run-worthiness: print score and exit without writing
    if dry_run_worthiness:
        json_out({
            "dry_run": True,
            "worthiness_score": worthiness_score,
            "rejection_reason": worthiness_reason,
            "components": worthiness_components,
            "would_write": (worthiness_reason == ""),
        })
        return

    # Gate rejection: exit with structured output, no INSERT
    if worthiness_reason and not force_write:
        # Log the rejection as an event for monitoring
        try:
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
                "VALUES (?, 'write_rejected', ?, ?, ?)",
                (args.agent,
                 f"W(m) gate rejected: {worthiness_reason} (score={worthiness_score})",
                 json.dumps({
                     "content_preview": args.content[:120],
                     "category": args.category,
                     "scope": args.scope or "global",
                     "score": worthiness_score,
                     "reason": worthiness_reason,
                     "components": worthiness_components,
                 }),
                 _now_ts())
            )
            db.commit()
        except Exception:
            pass
        json_out({
            "ok": False,
            "rejected": True,
            "worthiness_score": worthiness_score,
            "rejection_reason": worthiness_reason,
            "components": worthiness_components,
            "hint": "Use --force to bypass the gate.",
        })
        return

    # PII recency gate — applied when --supersedes <id> is specified
    # Computes entrenchment of the incumbent and raises alpha_floor on the new memory.
    supersedes_id = getattr(args, "supersedes", None)
    alpha_floor = 1
    pii_info = {}
    if supersedes_id:
        incumbent_pii = _compute_pii(db, supersedes_id)
        pii_info = {
            "supersedes_id": supersedes_id,
            "incumbent_pii": round(incumbent_pii, 4),
            "incumbent_tier": _pii_tier(incumbent_pii),
        }
        alpha_floor = 1 + math.ceil(max(0.0, incumbent_pii - 0.20) * 0.5 * 5)

    # ── Pre-ingest duplicate suppression (TORMENT-inspired) ────────────
    # Before inserting, check if a very similar memory already exists for
    # this agent. If so, reinforce it instead of creating a duplicate.
    # This prevents unbounded growth from repeated similar observations.
    dedup_hit = None
    if not force_write and args.agent:
        try:
            fts_q = _sanitize_fts_query(args.content)
            if fts_q:
                candidates = db.execute(
                    "SELECT m.id, m.content, m.confidence, m.category, m.recalled_count "
                    "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.agent_id = ? AND m.retired_at IS NULL "
                    "AND m.category = ? "
                    "ORDER BY f.rank LIMIT 5",
                    (fts_q, args.agent, args.category)
                ).fetchall()
                for cand in candidates:
                    sim = _jaccard_word_similarity(args.content, cand["content"])
                    if sim >= DEDUP_CONTENT_SIMILARITY_THRESHOLD:
                        # Reinforce existing memory instead of duplicating
                        new_conf = min(0.98, cand["confidence"] + (1.0 - cand["confidence"]) * 0.3)
                        db.execute(
                            "UPDATE memories SET confidence=?, recalled_count=recalled_count+1, "
                            "last_recalled_at=?, updated_at=? WHERE id=?",
                            (new_conf, _now_ts(), _now_ts(), cand["id"])
                        )
                        db.commit()
                        dedup_hit = cand["id"]
                        json_out({
                            "ok": True,
                            "deduplicated": True,
                            "reinforced_memory_id": cand["id"],
                            "similarity": round(sim, 4),
                            "new_confidence": round(new_conf, 4),
                        })
                        return
        except Exception as exc:
            logger.debug("Dedup check failed (non-fatal): %s", exc)

    # ── Hard memory cap check (TORMENT-inspired) ─────────────────────
    # If this agent has exceeded the hard cap, force-compress before inserting.
    if args.agent:
        try:
            count_row = db.execute(
                "SELECT COUNT(*) as cnt FROM memories WHERE agent_id=? AND retired_at IS NULL",
                (args.agent,)
            ).fetchone()
            if count_row and count_row["cnt"] >= HARD_MEMORY_CAP:
                # Emergency compression: retire lowest-confidence memories down to target
                excess = count_row["cnt"] - HARD_MEMORY_TARGET
                if excess > 0:
                    db.execute(
                        "UPDATE memories SET retired_at=?, retraction_reason='hard_cap_emergency' "
                        "WHERE id IN ("
                        "  SELECT id FROM memories WHERE agent_id=? AND retired_at IS NULL "
                        "  AND protected=0 AND temporal_class != 'permanent' "
                        "  ORDER BY confidence ASC, created_at ASC LIMIT ?"
                        ")",
                        (_now_ts(), args.agent, excess)
                    )
                    db.commit()
                    # Log the emergency compression
                    db.execute(
                        "INSERT INTO events (agent_id, event_type, summary, created_at) "
                        "VALUES (?, 'warning', ?, ?)",
                        (args.agent,
                         f"Hard memory cap hit ({count_row['cnt']}/{HARD_MEMORY_CAP}). "
                         f"Emergency-retired {excess} lowest-confidence memories.",
                         _now_ts())
                    )
                    db.commit()
        except Exception as exc:
            logger.debug("Cap check failed (non-fatal): %s", exc)

    file_path = getattr(args, "file_path", None)
    file_line = getattr(args, "file_line", None)
    cursor = db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, tags, source_event_id, "
        "memory_type, supersedes_id, alpha, file_path, file_line, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.agent, args.category, args.scope or "global", args.content,
         effective_confidence, tags_json, args.source_event, memory_type,
         supersedes_id, float(alpha_floor), file_path, file_line, _now_ts(), _now_ts())
    )
    memory_id = cursor.lastrowid

    # Record gated_from_memory_id audit trail if column exists (migration 025)
    if supersedes_id:
        try:
            db.execute(
                "UPDATE memories SET gated_from_memory_id = ? WHERE id = ?",
                (supersedes_id, memory_id)
            )
        except Exception:
            pass  # column not yet migrated — non-fatal

    log_access(db, args.agent, "write", "memories", memory_id)
    db.commit()

    # ── Auto-link entities (self-building knowledge graph) ───────────
    # Scan the memory content for known entity names and create
    # knowledge_edges linking this memory to each mentioned entity.
    # Pure string matching — no LLM call. Uses word boundaries to avoid
    # false positives (e.g. "Go" matching inside "Google").
    auto_linked = []
    try:
        entities = db.execute(
            "SELECT id, name FROM entities WHERE retired_at IS NULL"
        ).fetchall()
        content_lower = args.content.lower()
        for ent in entities:
            ename = ent["name"]
            # Skip very short names (<=2 chars) to avoid noise
            if len(ename) <= 2:
                continue
            # Word-boundary check: the name must appear as a whole word/phrase
            ename_lower = ename.lower()
            idx = content_lower.find(ename_lower)
            if idx < 0:
                continue
            # Verify word boundary (not inside a larger word)
            before_ok = (idx == 0 or not content_lower[idx - 1].isalnum())
            after_idx = idx + len(ename_lower)
            after_ok = (after_idx >= len(content_lower) or not content_lower[after_idx].isalnum())
            if not (before_ok and after_ok):
                continue
            # Create edge (or reinforce existing)
            try:
                db.execute(
                    "INSERT INTO knowledge_edges "
                    "(source_table, source_id, target_table, target_id, relation_type, weight, agent_id, created_at) "
                    "VALUES ('memories', ?, 'entities', ?, 'mentions', 0.5, ?, ?) "
                    "ON CONFLICT (source_table, source_id, target_table, target_id, relation_type) "
                    "DO UPDATE SET co_activation_count = co_activation_count + 1, "
                    "weight = MIN(1.0, weight + 0.1), last_reinforced_at = ?",
                    (memory_id, ent["id"], args.agent, _now_ts(), _now_ts())
                )
                auto_linked.append(ename)
            except Exception:
                pass
        if auto_linked:
            db.commit()
    except Exception as exc:
        logger.debug("Auto-linking failed (non-fatal): %s", exc)

    # Conflict preservation: --attribute mode
    # If --attribute is set, scan for memories from other agents that cover the same
    # scope/topic and log a belief_conflict entry if found. Memory is still written.
    conflict_logged = False
    if getattr(args, "attribute", False):
        try:
            # Find memories from other agents with the same scope (simple provenance check)
            scope_val = args.scope or "global"
            conflict_rows = db.execute(
                "SELECT id, agent_id, content FROM memories "
                "WHERE scope=? AND agent_id != ? AND retired_at IS NULL "
                "ORDER BY created_at DESC LIMIT 5",
                (scope_val, args.agent)
            ).fetchall()
            for cr in conflict_rows:
                db.execute(
                    "INSERT INTO belief_conflicts "
                    "(topic, agent_a_id, agent_b_id, belief_a, belief_b, conflict_type, severity, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, 'factual', 0.3, ?)",
                    (scope_val, args.agent, cr["agent_id"],
                     args.content[:500], cr["content"][:500], _now_ts())
                )
                conflict_logged = True
            if conflict_logged:
                db.commit()
        except Exception:
            pass  # non-fatal

    # Sync embedding on write — keep vec_memories in lock-step
    # blob was already computed above for the gate; reuse it here.
    embedded = False
    try:
        if not blob:
            blob = _embed_query_safe(args.content)
        if blob:
            db_vec = _try_get_db_with_vec()
            if db_vec:
                db_vec.execute(
                    "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (memory_id, blob)
                )
                db_vec.execute(
                    "INSERT OR IGNORE INTO embeddings (source_table, source_id, model, dimensions, vector) VALUES (?,?,?,?,?)",
                    ("memories", memory_id, EMBED_MODEL, EMBED_DIMENSIONS, blob)
                )
                db_vec.commit()
                db_vec.close()
                embedded = True
    except Exception:
        pass  # non-fatal: backfill cron handles coverage gaps

    out = {
        "ok": True,
        "memory_id": memory_id,
        "reflexion": getattr(args, "reflexion", False),
        "embedded": embedded,
        "effective_confidence": effective_confidence,
        "source_weight": round(source_weight_applied, 4),
        "conflict_logged": conflict_logged,
        "worthiness_score": worthiness_score,
    }
    if auto_linked:
        out["auto_linked_entities"] = auto_linked
    if pii_info:
        out["pii_gate"] = {**pii_info, "alpha_floor": alpha_floor}
    json_out(out)

def cmd_memory_search(args):
    db = get_db()
    query = args.query
    limit = args.limit or 20
    no_recency = getattr(args, "no_recency", False)

    if args.exact:
        rows = db.execute(
            "SELECT * FROM memories WHERE retired_at IS NULL AND content LIKE ? ORDER BY confidence DESC LIMIT ?",
            (f"%{query}%", limit * 5 if not no_recency else limit)
        ).fetchall()
        results = rows_to_list(rows)
        if not no_recency:
            for r in results:
                tw = _temporal_weight(r.get("created_at"), r.get("scope"))
                hl = _halflife_decay(r)
                r["temporal_weight"] = round(tw, 4)
                r["halflife_decay"] = round(hl, 4)
                r["age"] = _age_str(r.get("created_at"))
                score = (r.get("confidence") or 1.0) * tw * hl
                if _is_reflexion(r):
                    score *= REFLEXION_BOOST
                    r["reflexion_boosted"] = True
                # Active Inference Phase 1: precision weighting from source trust
                trust = r.get("trust_score") or 1.0
                conf = r.get("confidence") or 1.0
                precision_weight = 0.90 + 0.10 * (trust * conf)
                score *= precision_weight
                r["precision_weight"] = round(precision_weight, 4)
                r["final_score"] = score
            results.sort(key=lambda r: -r["final_score"])
            results = results[:limit]
    else:
        # Fetch a larger pool for reranking; ORDER BY rank gives FTS best-first
        fetch_limit = limit * 5 if not no_recency else limit
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            rows = []
        else:
            rows = db.execute(
                "SELECT m.*, f.rank as fts_rank FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? AND m.retired_at IS NULL "
                "ORDER BY rank LIMIT ?",
                (fts_query, fetch_limit)
            ).fetchall()
        results = rows_to_list(rows)
        if not no_recency:
            for r in results:
                tw = _temporal_weight(r.get("created_at"), r.get("scope"))
                hl = _halflife_decay(r)
                r["temporal_weight"] = round(tw, 4)
                r["halflife_decay"] = round(hl, 4)
                r["age"] = _age_str(r.get("created_at"))
                # fts_rank is negative; multiply by tw and halflife to boost recent/reinforced items
                score = (r.get("fts_rank") or 0.0) * tw * hl
                if _is_reflexion(r):
                    score *= REFLEXION_BOOST
                    r["reflexion_boosted"] = True
                # Active Inference Phase 1: precision weighting from source trust
                trust = r.get("trust_score") or 1.0
                conf = r.get("confidence") or 1.0
                precision_weight = 0.90 + 0.10 * (trust * conf)
                score *= precision_weight
                r["precision_weight"] = round(precision_weight, 4)
                # File proximity boost: memories anchored to the queried file rank higher
                search_file = getattr(args, "file_path", None)
                if search_file and r.get("file_path") and search_file in r["file_path"]:
                    score *= 1.5  # fts_rank is negative, so *1.5 makes it more negative = higher rank
                    r["file_proximity_boost"] = True
                r["final_score"] = score
            # ascending sort: more negative = better FTS match + recent boost
            results.sort(key=lambda r: r["final_score"])
            results = results[:limit]

    # Scope/category filter BEFORE recall update so only actually-returned memories get credited
    if args.scope:
        results = [r for r in results if r["scope"] == args.scope]
    if args.category:
        results = [r for r in results if r["category"] == args.category]

    # Epistemic foraging mode: re-rank by (1-confidence)*importance — high uncertainty first
    if getattr(args, "epistemic", False):
        for r in results:
            conf = r.get("confidence") or 1.0
            imp = r.get("importance") or 0.5
            r["epistemic_score"] = round((1.0 - conf) * imp, 4)
        results.sort(key=lambda r: -r.get("epistemic_score", 0.0))

    # Update recall stats for memories the caller actually sees (reconsolidation boost)
    for r in results:
        db.execute(
            "UPDATE memories SET recalled_count = recalled_count + 1, last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), confidence = MIN(1.0, confidence + 0.15 * (1.0 - confidence)) WHERE id = ?",
            (r["id"],)
        )

    log_access(db, args.agent or "unknown", "search", "memories", query=query, result_count=len(results))
    db.commit()

    _ofmt = getattr(args, "output", "json")
    if _ofmt == "oneline":
        oneline_out(results)
    elif _ofmt == "compact":
        json_out(results, compact=True)
    else:
        json_out(results)

def cmd_memory_list(args):
    db = get_db()
    sql = "SELECT * FROM memories WHERE retired_at IS NULL"
    params = []
    if args.category:
        sql += " AND category = ?"
        params.append(args.category)
    if args.scope:
        sql += " AND scope = ?"
        params.append(args.scope)
    if args.agent:
        sql += " AND agent_id = ?"
        params.append(args.agent)
    sort = getattr(args, "sort", None)
    _valid_sorts = {"confidence", "updated_at", "recalled_count", "ewc_importance"}
    if sort and sort in _valid_sorts:
        # ewc_importance requires the column; degrade gracefully if migration not yet applied
        if sort == "ewc_importance":
            cols = {r[1] for r in db.execute("PRAGMA table_info(memories)").fetchall()}
            if "ewc_importance" not in cols:
                sort = "confidence"
        sql += f" ORDER BY {sort} DESC, updated_at DESC"
    else:
        sql += " ORDER BY confidence DESC, updated_at DESC"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = db.execute(sql, params).fetchall()
    json_out(rows_to_list(rows))

def cmd_memory_retire(args):
    db = get_db()
    db.execute("UPDATE memories SET retired_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?", (args.id,))
    log_access(db, args.agent or "unknown", "retire", "memories", args.id)
    db.commit()
    _try_vec_delete_memories(args.id)
    json_out({"ok": True, "retired_id": args.id})

def cmd_memory_replace(args):
    db = get_db()
    # Retire old
    db.execute("UPDATE memories SET retired_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?", (args.old_id,))
    # Insert new
    tags_json = json.dumps(args.tags.split(",")) if args.tags else None
    cursor = db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, tags, supersedes_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.agent, args.category, args.scope or "global", args.content,
         args.confidence or 1.0, tags_json, args.old_id, _now_ts(), _now_ts())
    )
    new_id = cursor.lastrowid
    log_access(db, args.agent, "write", "memories", new_id)
    db.commit()
    _try_vec_delete_memories(args.old_id)
    json_out({"ok": True, "retired_id": args.old_id, "new_id": new_id})

def cmd_memory_update(args):
    """Compare-and-swap update for a memory record.

    Callers must supply the expected current version (--expected-version).
    The UPDATE only proceeds when id matches AND version equals the expected
    value. If another writer incremented the version first, rowcount will be
    0 and this command returns {"ok": false, "error": "version_conflict"}.
    """
    db = get_db()

    sets = ["version = version + 1", "updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')"]
    params = []

    if args.content is not None:
        sets.append("content = ?")
        params.append(args.content)
    if args.confidence is not None:
        sets.append("confidence = ?")
        params.append(args.confidence)
    if args.tags is not None:
        sets.append("tags = ?")
        params.append(json.dumps(args.tags.split(",")))
    if args.scope is not None:
        sets.append("scope = ?")
        params.append(args.scope)

    if len(sets) == 2:
        json_out({"ok": False, "error": "no_fields",
                  "message": "Provide at least one of: --content, --confidence, --tags, --scope"})
        return

    # CAS predicate: id match + version guard + not retired
    params.extend([args.id, args.expected_version])
    sql = f"UPDATE memories SET {', '.join(sets)} WHERE id = ? AND version = ? AND retired_at IS NULL"
    cursor = db.execute(sql, params)

    if cursor.rowcount == 0:
        row = db.execute("SELECT id, version FROM memories WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            json_out({"ok": False, "error": "memory_not_found", "id": args.id})
        else:
            json_out({"ok": False, "error": "version_conflict",
                      "id": args.id,
                      "expected_version": args.expected_version,
                      "actual_version": row["version"]})
        return

    log_access(db, args.agent or "unknown", "write", "memories", args.id)
    db.commit()
    row = db.execute("SELECT id, version FROM memories WHERE id = ?", (args.id,)).fetchone()
    json_out({"ok": True, "memory_id": args.id, "new_version": row["version"]})


def cmd_memory_confidence(args):
    """Show Beta(α,β) Bayesian confidence breakdown for a memory."""
    import math
    db = get_db()
    row = db.execute(
        "SELECT id, content, confidence, recalled_count, temporal_class, created_at, "
        "last_recalled_at, retired_at "
        "FROM memories WHERE id = ?", (args.id,)
    ).fetchone()
    if not row:
        json_out({"ok": False, "error": f"Memory {args.id} not found"})
        return

    # Try to fetch alpha/beta; fall back gracefully if columns missing
    try:
        ab_row = db.execute(
            "SELECT alpha, beta FROM memories WHERE id = ?", (args.id,)
        ).fetchone()
        alpha = float(ab_row["alpha"] or 1.0) if ab_row else 1.0
        beta  = float(ab_row["beta"]  or 1.0) if ab_row else 1.0
    except Exception:
        alpha = float(row["confidence"] or 0.5) * 2.0
        beta  = (1.0 - float(row["confidence"] or 0.5)) * 2.0

    n = alpha + beta  # total evidence mass
    point_estimate = alpha / n if n > 0 else 0.5

    # Wilson / exact Beta credible interval using normal approx (fast, accurate for n≥5)
    if n >= 5:
        z = 1.96  # 95% CI
        se = math.sqrt(point_estimate * (1 - point_estimate) / n)
        ci_lo = max(0.0, point_estimate - z * se)
        ci_hi = min(1.0, point_estimate + z * se)
    else:
        ci_lo, ci_hi = 0.0, 1.0

    json_out({
        "id": row["id"],
        "content_preview": (row["content"] or "")[:120],
        "confidence_scalar": round(float(row["confidence"] or 0.0), 6),
        "bayesian": {
            "alpha": round(alpha, 4),
            "beta":  round(beta,  4),
            "evidence_mass": round(n, 4),
            "point_estimate": round(point_estimate, 6),
            "ci_95_lo": round(ci_lo, 4),
            "ci_95_hi": round(ci_hi, 4),
            "distribution": f"Beta({round(alpha,2)}, {round(beta,2)})",
        },
        "recalled_count": row["recalled_count"],
        "temporal_class": row["temporal_class"],
        "retired": row["retired_at"] is not None,
    })


def cmd_memory_retract(args):
    """Retract a memory and optionally cascade to derived memories."""
    db = get_db()
    memory = db.execute("SELECT * FROM memories WHERE id = ?", (args.id,)).fetchone()
    if not memory:
        json_out({"ok": False, "error": f"Memory {args.id} not found"})
        return
    if memory["retracted_at"]:
        json_out({"ok": False, "error": f"Memory {args.id} already retracted"})
        return

    reason = args.reason or "Retracted by agent"
    retracted_ids = []

    def _retract_cascade(mem_id, depth, visited):
        if depth > 10:
            return
        if mem_id in visited:
            return
        visited.add(mem_id)
        db.execute(
            "UPDATE memories SET retracted_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), retraction_reason = ?, "
            "trust_score = 0.0 WHERE id = ? AND retracted_at IS NULL",
            (reason if depth == 0 else f"Cascade from retracted memory #{args.id}", mem_id)
        )
        retracted_ids.append(mem_id)
        if not args.no_cascade:
            derived = db.execute(
                "SELECT id, derived_from_ids FROM memories "
                "WHERE retracted_at IS NULL AND derived_from_ids IS NOT NULL"
            ).fetchall()
            for m in derived:
                try:
                    ids = json.loads(m["derived_from_ids"])
                    if mem_id in ids:
                        _retract_cascade(m["id"], depth + 1, visited)
                except (json.JSONDecodeError, TypeError):
                    pass

    _retract_cascade(args.id, 0, set())

    mem_dict = dict(memory)
    agent_id, category = mem_dict["agent_id"], mem_dict["category"]
    existing = db.execute(
        "SELECT * FROM memory_trust_scores WHERE agent_id = ? AND category = ?",
        (agent_id, category)
    ).fetchone()

    trust_updates = []
    if existing:
        new_retracted = existing["retracted_count"] + len(retracted_ids)
        new_score = round(max(0.0, min(1.0,
            (existing["validated_count"] - new_retracted) / max(1, existing["sample_count"])
        )), 4)
        db.execute(
            "UPDATE memory_trust_scores SET retracted_count = ?, trust_score = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
            (new_retracted, new_score, existing["id"])
        )
        trust_updates.append({"agent_id": agent_id, "category": category, "new_score": new_score})
    else:
        db.execute(
            "INSERT INTO memory_trust_scores (agent_id, category, trust_score, sample_count, retracted_count) "
            "VALUES (?, ?, 0.0, 1, 1)", (agent_id, category)
        )
        trust_updates.append({"agent_id": agent_id, "category": category, "new_score": 0.0})

    db.execute(
        "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) VALUES (?, 'memory_retired', ?, ?, ?)",
        (args.agent or "unknown", f"Retracted memory #{args.id}: {reason}",
         json.dumps({"retracted_ids": retracted_ids, "trust_updates": trust_updates}), _now_ts())
    )
    log_access(db, args.agent or "unknown", "retract", "memories", args.id)
    db.commit()
    json_out({"ok": True, "retracted_ids": retracted_ids,
              "cascade_count": len(retracted_ids) - 1, "trust_updates": trust_updates})


_PII_TEMPORAL_WEIGHTS = {
    "permanent": 1.00, "long": 0.80, "medium": 0.50, "short": 0.30, "ephemeral": 0.15
}
_PII_TIERS = [(0.70, "CRYSTALLIZED"), (0.40, "ENTRENCHED"), (0.20, "ESTABLISHED"), (0.00, "OPEN")]


def _compute_pii(db, memory_id: int) -> float:
    """Compute Proactive Interference Index for a memory. Returns float in [0.0, 1.0]."""
    row = db.execute(
        "SELECT alpha, beta, recalled_count, temporal_class FROM memories "
        "WHERE id = ? AND retired_at IS NULL", (memory_id,)
    ).fetchone()
    if not row:
        return 0.0
    alpha = float(row["alpha"] or 1.0)
    beta  = float(row["beta"]  or 1.0)
    recalled = int(row["recalled_count"] or 0)
    temporal_class = row["temporal_class"] or "medium"
    max_row = db.execute(
        "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
    ).fetchone()
    max_recalled = int(max_row[0] or 1)
    if max_recalled < 1:
        max_recalled = 1
    bayesian_strength = alpha / (alpha + beta)
    recall_weight = math.log(1 + recalled) / math.log(1 + max_recalled) if max_recalled > 0 else 0.0
    temporal_weight = _PII_TEMPORAL_WEIGHTS.get(temporal_class, 0.50)
    return min(1.0, max(0.0, bayesian_strength * recall_weight * temporal_weight))


def _pii_tier(score: float) -> str:
    for threshold, label in _PII_TIERS:
        if score >= threshold:
            return label
    return "OPEN"


def cmd_memory_pii(args):
    """Compute and display Proactive Interference Index for a single memory."""
    db = get_db()
    row = db.execute(
        "SELECT id, content, alpha, beta, recalled_count, temporal_class FROM memories "
        "WHERE id = ? AND retired_at IS NULL", (args.id,)
    ).fetchone()
    if not row:
        json_out({"ok": False, "error": f"Memory {args.id} not found or retired"})
        return
    score = _compute_pii(db, args.id)
    tier = _pii_tier(score)
    result = {
        "ok": True,
        "memory_id": args.id,
        "pii": round(score, 4),
        "tier": tier,
        "alpha": float(row["alpha"] or 1.0),
        "beta": float(row["beta"] or 1.0),
        "recalled_count": int(row["recalled_count"] or 0),
        "temporal_class": row["temporal_class"] or "medium",
        "content_snippet": (row["content"] or "")[:120],
    }
    if getattr(args, "json", False):
        json_out(result)
    else:
        print(f"Memory #{args.id} — PII: {score:.3f} ({tier})")
        print(f"  alpha={result['alpha']:.2f}, beta={result['beta']:.2f}, "
              f"recalled={result['recalled_count']}, temporal={result['temporal_class']}")
        print(f"  Content: {result['content_snippet']}")


def cmd_memory_pii_scan(args):
    """Scan all active memories sorted by PII descending."""
    db = get_db()
    top_n = getattr(args, "top", None) or 20
    rows = db.execute(
        "SELECT id, content, alpha, beta, recalled_count, temporal_class "
        "FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    max_row = db.execute("SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL").fetchone()
    max_recalled = int(max_row[0] or 1)
    if max_recalled < 1:
        max_recalled = 1

    scored = []
    for r in rows:
        alpha = float(r["alpha"] or 1.0)
        beta  = float(r["beta"]  or 1.0)
        recalled = int(r["recalled_count"] or 0)
        temporal_class = r["temporal_class"] or "medium"
        bayesian_strength = alpha / (alpha + beta)
        recall_weight = math.log(1 + recalled) / math.log(1 + max_recalled)
        temporal_weight = _PII_TEMPORAL_WEIGHTS.get(temporal_class, 0.50)
        pii = min(1.0, max(0.0, bayesian_strength * recall_weight * temporal_weight))
        scored.append({
            "memory_id": r["id"],
            "pii": round(pii, 4),
            "tier": _pii_tier(pii),
            "alpha": round(alpha, 2),
            "beta": round(beta, 2),
            "recalled_count": recalled,
            "temporal_class": temporal_class,
            "content_snippet": (r["content"] or "")[:100],
        })

    scored.sort(key=lambda x: x["pii"], reverse=True)
    scored = scored[:top_n]

    if getattr(args, "json", False):
        json_out({"ok": True, "count": len(scored), "memories": scored})
        return

    print(f"\n{'PII':>6}  {'TIER':<14} {'ID':>6}  {'temporal':<10}  snippet")
    print("-" * 80)
    for m in scored:
        print(f"  {m['pii']:.3f}  {m['tier']:<14} #{m['memory_id']:<5}  {m['temporal_class']:<10}  {m['content_snippet']}")
    print()


def _walk_trust_chain(db, memory_id, max_depth, visited):
    """Walk derived_from chain returning minimum trust. Max 10 hops, cycle-safe."""
    if max_depth <= 0 or memory_id in visited:
        return 1.0
    visited.add(memory_id)
    row = db.execute(
        "SELECT trust_score, derived_from_ids, retracted_at FROM memories WHERE id = ?",
        (memory_id,)
    ).fetchone()
    if not row:
        return 1.0
    if row["retracted_at"]:
        return 0.0
    current_trust = row["trust_score"] or 1.0
    if row["derived_from_ids"]:
        try:
            for pid in json.loads(row["derived_from_ids"]):
                current_trust = min(current_trust,
                                    _walk_trust_chain(db, pid, max_depth - 1, visited))
        except (json.JSONDecodeError, TypeError):
            pass
    return current_trust


def cmd_memory_trust_propagate(args):
    """Recalculate trust scores. Propagates through derived_from chains (max 10 hops)."""
    db = get_db()
    updated = []
    rows = db.execute(
        "SELECT agent_id, category, COUNT(*) as total, "
        "SUM(CASE WHEN retracted_at IS NOT NULL THEN 1 ELSE 0 END) as retracted, "
        "SUM(CASE WHEN validated_at IS NOT NULL THEN 1 ELSE 0 END) as validated "
        "FROM memories WHERE retired_at IS NULL GROUP BY agent_id, category"
    ).fetchall()

    for row in rows:
        a, c, t, ret, val = row["agent_id"], row["category"], row["total"], row["retracted"], row["validated"]
        score = max(0.0, min(1.0, (t - ret * 2) / max(1, t)))
        if val > 0:
            score = min(1.0, score + 0.1 * (val / t))
        score = round(score, 4)
        db.execute(
            "INSERT INTO memory_trust_scores (agent_id, category, trust_score, sample_count, "
            "validated_count, retracted_count, last_evaluated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now')) "
            "ON CONFLICT(agent_id, category) DO UPDATE SET "
            "trust_score=excluded.trust_score, sample_count=excluded.sample_count, "
            "validated_count=excluded.validated_count, retracted_count=excluded.retracted_count, "
            "last_evaluated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now'), updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')",
            (a, c, score, t, val, ret)
        )
        updated.append({"agent_id": a, "category": c, "score": score, "total": t})

    derived = db.execute(
        "SELECT id, trust_score, derived_from_ids FROM memories "
        "WHERE retired_at IS NULL AND retracted_at IS NULL AND derived_from_ids IS NOT NULL"
    ).fetchall()
    propagated = 0
    for mem in derived:
        try:
            source_ids = json.loads(mem["derived_from_ids"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not source_ids:
            continue
        min_trust = 1.0
        for sid in source_ids:
            min_trust = min(min_trust, _walk_trust_chain(db, sid, max_depth=10, visited=set()))
        inherited = round(min(mem["trust_score"] or 1.0, min_trust), 4)
        if abs(inherited - (mem["trust_score"] or 1.0)) > 0.001:
            db.execute("UPDATE memories SET trust_score = ? WHERE id = ?", (inherited, mem["id"]))
            propagated += 1

    db.commit()
    json_out({"ok": True, "agent_category_scores": updated, "derived_propagated": propagated})


# ---------------------------------------------------------------------------
# TRUST ENGINE (Sentinel 2)
# ---------------------------------------------------------------------------

_TRUST_CATEGORY_PRIORS = {
    "identity":    0.85,
    "decision":    0.80,
    "environment": 0.72,
    "project":     0.70,
    "lesson":      0.68,
    "preference":  0.65,
    "convention":  0.65,
    "user":        0.62,
    "integration": 0.70,
}

_TRUST_AGENT_MULTIPLIERS = [
    ("supervisor",  1.15),
    ("hippocampus", 0.90),
    ("sentinel",    1.10),
    ("prune",       1.10),
]

_TRUST_DECAY_RATES = {
    "ephemeral": 0.03,
    "short":     0.02,
    "medium":    0.01,
    "long":      0.005,
    "permanent": 0.0,
}


def _trust_agent_multiplier(agent_id: str) -> float:
    if not agent_id:
        return 1.0
    aid = agent_id.lower()
    for keyword, mult in _TRUST_AGENT_MULTIPLIERS:
        if keyword in aid:
            return mult
    return 1.0


def _compute_trust_breakdown(db, mem) -> dict:
    mem = dict(mem)
    mem_id = mem["id"]
    if mem.get("retracted_at"):
        return {"memory_id": mem_id, "retracted": True, "trust_score": 0.05,
                "components": {"retracted": True}}

    base_trust = _TRUST_CATEGORY_PRIORS.get(mem.get("category", ""), 0.70)
    agent_id = mem.get("agent_id", "")
    category = mem.get("category", "")
    sr_row = db.execute(
        "SELECT trust_score FROM memory_trust_scores WHERE agent_id = ? AND category = ?",
        (agent_id, category)
    ).fetchone()
    source_reliability = sr_row["trust_score"] if sr_row else 0.75
    source_reliability = min(1.0, source_reliability * _trust_agent_multiplier(agent_id))

    validation_count = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'memory_validated' "
        "AND JSON_EXTRACT(metadata, '$.memory_id') = ?", (mem_id,)
    ).fetchone()[0]
    if mem.get("validated_at") or validation_count > 0:
        validation_bonus = min(1.50, 1.0 + validation_count * 0.35)
    else:
        validation_bonus = 1.0

    if mem.get("validated_at"):
        age_penalty = 1.0
        days_unvalidated = 0.0
    else:
        days_unvalidated = _days_since(mem.get("created_at", ""))
        age_penalty = max(0.50, 1.0 - 0.01 * days_unvalidated)

    n_unresolved = db.execute(
        "SELECT COUNT(*) FROM knowledge_edges "
        "WHERE relation_type = 'contradicts' AND (source_id = ? OR target_id = ?)",
        (mem_id, mem_id)
    ).fetchone()[0]
    n_resolved = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'contradiction_resolved' "
        "AND (JSON_EXTRACT(metadata, '$.kept') = ? OR JSON_EXTRACT(metadata, '$.retired') = ?)",
        (str(mem_id), str(mem_id))
    ).fetchone()[0]
    contradiction_penalty = max(0.30, 1.0 - (n_unresolved * 0.20) - (n_resolved * 0.05))

    score = round(min(1.0, max(0.05,
        base_trust * source_reliability * validation_bonus * age_penalty * contradiction_penalty
    )), 4)

    return {
        "memory_id": mem_id,
        "retracted": False,
        "trust_score": score,
        "components": {
            "base_trust": round(base_trust, 4),
            "source_reliability": round(source_reliability, 4),
            "validation_bonus": round(validation_bonus, 4),
            "age_penalty": round(age_penalty, 4),
            "days_unvalidated": round(days_unvalidated, 1),
            "contradiction_penalty": round(contradiction_penalty, 4),
            "n_unresolved_contradictions": n_unresolved,
            "n_resolved_contradictions": n_resolved,
            "validation_count": validation_count,
            "validated": mem.get("validated_at") is not None,
        },
    }


def cmd_trust_show(args):
    db = get_db()
    mem = db.execute("SELECT * FROM memories WHERE id = ?", (args.memory_id,)).fetchone()
    if not mem:
        json_out({"ok": False, "error": f"Memory {args.memory_id} not found"})
        return
    breakdown = _compute_trust_breakdown(db, mem)
    mem_d = dict(mem)
    breakdown.update({
        "content_preview": (mem_d.get("content", "") or "")[:120],
        "category": mem_d.get("category"),
        "agent_id": mem_d.get("agent_id"),
        "temporal_class": mem_d.get("temporal_class"),
        "stored_trust_score": mem_d.get("trust_score"),
    })
    json_out(breakdown)


def cmd_trust_audit(args):
    db = get_db()
    rows = db.execute(
        "SELECT id, agent_id, category, scope, temporal_class, trust_score, "
        "validated_at, retracted_at, created_at, content "
        "FROM memories WHERE retired_at IS NULL AND trust_score < ? "
        "ORDER BY trust_score ASC LIMIT ?",
        (args.threshold, args.limit)
    ).fetchall()
    result = []
    for r in rows:
        rd = dict(r)
        rd["content_preview"] = (rd.pop("content", "") or "")[:100]
        result.append(rd)
    json_out({"ok": True, "threshold": args.threshold, "count": len(result), "memories": result})


def cmd_trust_calibrate(args):
    db = get_db()
    dry_run = getattr(args, "dry_run", False)
    updated = 0
    rows = db.execute(
        "SELECT id, agent_id, category, trust_score, retracted_at, validated_at "
        "FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    for r in rows:
        r = dict(r)
        if r.get("retracted_at"):
            new_trust = 0.05
        elif r.get("validated_at"):
            cur = r.get("trust_score") or 1.0
            new_trust = cur if cur < 0.999 else _TRUST_CATEGORY_PRIORS.get(r.get("category", ""), 0.70)
        else:
            prior = _TRUST_CATEGORY_PRIORS.get(r.get("category", ""), 0.70)
            new_trust = round(min(1.0, prior * _trust_agent_multiplier(r.get("agent_id", ""))), 4)
        if abs((r.get("trust_score") or 1.0) - new_trust) > 0.001:
            if not dry_run:
                db.execute(
                    "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                    (new_trust, r["id"])
                )
            updated += 1
    if not dry_run:
        db.execute("""
            INSERT OR IGNORE INTO memory_trust_scores (agent_id, category, trust_score, sample_count)
            SELECT agent_id, category, 0.75, COUNT(*)
            FROM memories WHERE retired_at IS NULL GROUP BY agent_id, category
        """)
        db.execute("""
            UPDATE memory_trust_scores SET
              sample_count = (SELECT COUNT(*) FROM memories m
                WHERE m.agent_id = memory_trust_scores.agent_id
                AND m.category = memory_trust_scores.category AND m.retired_at IS NULL),
              retracted_count = (SELECT COUNT(*) FROM memories m
                WHERE m.agent_id = memory_trust_scores.agent_id
                AND m.category = memory_trust_scores.category AND m.retracted_at IS NOT NULL),
              last_evaluated_at = strftime('%Y-%m-%dT%H:%M:%S','now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S','now')
        """)
        db.execute(
            "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) VALUES (?,?,?,?,?)",
            ("trust-engine", "result",
             f"Trust calibration: {updated} memories updated with category priors",
             json.dumps({"updated_count": updated, "dry_run": dry_run}), _now_ts())
        )
        db.commit()
    json_out({"ok": True, "updated": updated, "dry_run": dry_run})


def cmd_trust_decay(args):
    db = get_db()
    dry_run = getattr(args, "dry_run", False)
    updated = 0
    rows = db.execute(
        "SELECT id, trust_score, temporal_class, updated_at, created_at "
        "FROM memories WHERE retired_at IS NULL AND retracted_at IS NULL "
        "  AND validated_at IS NULL AND temporal_class != 'permanent'"
    ).fetchall()
    for r in rows:
        r = dict(r)
        rate = _TRUST_DECAY_RATES.get(r.get("temporal_class", "medium"), 0.01)
        if rate == 0.0:
            continue
        days = _days_since(r.get("updated_at") or r.get("created_at", ""))
        if days < 1.0:
            continue
        current = r.get("trust_score") or 1.0
        new_trust = round(max(0.50, current * (1.0 - rate * days)), 4)
        if abs(new_trust - current) > 0.001:
            if not dry_run:
                db.execute(
                    "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                    (new_trust, r["id"])
                )
            updated += 1
    if not dry_run and updated > 0:
        db.commit()
    json_out({"ok": True, "decayed": updated, "dry_run": dry_run})


def cmd_trust_update_contradiction(args):
    db = get_db()
    id_a, id_b = args.memory_id_a, args.memory_id_b
    resolved = getattr(args, "resolved", False)
    if resolved:
        db.execute(
            "UPDATE memories SET trust_score = ROUND(MAX(0.30, trust_score - 0.05), 4), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?", (id_a,)
        )
    else:
        db.execute(
            "UPDATE memories SET trust_score = ROUND(MAX(0.30, trust_score - 0.20), 4), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id IN (?, ?)", (id_a, id_b)
        )
    rows = db.execute("SELECT id, trust_score FROM memories WHERE id IN (?, ?)", (id_a, id_b)).fetchall()
    db.commit()
    json_out({"ok": True, "resolved": resolved, "updated_memories": rows_to_list(rows)})


def cmd_trust_process_meb(args):
    db = get_db()
    since = getattr(args, "since", None) or 0
    dry_run = getattr(args, "dry_run", False)
    rows = db.execute(
        "SELECT me.id, me.memory_id, me.operation, me.category, me.agent_id "
        "FROM memory_events me WHERE me.id > ? ORDER BY me.id ASC LIMIT 200",
        (since,)
    ).fetchall()
    processed = 0
    new_watermark = since
    for ev in rows:
        ev = dict(ev)
        new_watermark = max(new_watermark, ev["id"])
        mem_id = ev["memory_id"]
        mem = db.execute(
            "SELECT id, trust_score, category, agent_id, validated_at, retracted_at "
            "FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        if not mem:
            continue
        mem = dict(mem)
        op = ev.get("operation", "")
        if op in ("insert", "backfill") and (mem.get("trust_score") or 1.0) >= 0.999:
            prior = _TRUST_CATEGORY_PRIORS.get(mem.get("category", ""), 0.70)
            new_trust = round(min(1.0, prior * _trust_agent_multiplier(mem.get("agent_id", ""))), 4)
            if not dry_run:
                db.execute(
                    "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                    (new_trust, mem_id)
                )
            processed += 1
        elif op == "update":
            if mem.get("retracted_at") and (mem.get("trust_score") or 1.0) > 0.05:
                if not dry_run:
                    db.execute(
                        "UPDATE memories SET trust_score = 0.05, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                        (mem_id,)
                    )
                processed += 1
            elif mem.get("validated_at") and (mem.get("trust_score") or 0.0) < 0.80:
                new_trust = round(min(0.95, (mem.get("trust_score") or 0.70) * 1.25), 4)
                if not dry_run:
                    db.execute(
                        "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                        (new_trust, mem_id)
                    )
                processed += 1
    if not dry_run and processed > 0:
        db.commit()
    json_out({"ok": True, "processed": processed, "new_watermark": new_watermark, "dry_run": dry_run})


# ---------------------------------------------------------------------------
# EVENT commands
# ---------------------------------------------------------------------------

def _resolve_causal_chain_root(db, caused_by_event_id):
    """Walk the caused_by chain to find the root event (one with no parent)."""
    current_id = caused_by_event_id
    visited = set()
    while current_id is not None:
        if current_id in visited:
            break  # cycle guard
        visited.add(current_id)
        row = db.execute(
            "SELECT caused_by_event_id, causal_chain_root FROM events WHERE id = ?",
            (current_id,)
        ).fetchone()
        if row is None:
            break
        # If this ancestor already has a causal_chain_root, use it directly
        if row["causal_chain_root"] is not None:
            return row["causal_chain_root"]
        if row["caused_by_event_id"] is None:
            # This is the root
            return current_id
        current_id = row["caused_by_event_id"]
    return caused_by_event_id


def cmd_event_add(args):
    db = get_db()
    metadata_json = args.metadata  # already JSON string or None
    refs_json = json.dumps(args.refs.split(",")) if args.refs else None

    caused_by = getattr(args, "caused_by", None)
    causal_chain_root = None
    if caused_by is not None:
        # Validate that the parent event exists
        parent = db.execute("SELECT id FROM events WHERE id = ?", (caused_by,)).fetchone()
        if parent is None:
            json_out({"ok": False, "error": f"caused_by event {caused_by} does not exist"})
            return
        causal_chain_root = _resolve_causal_chain_root(db, caused_by)

    cursor = db.execute(
        "INSERT INTO events (agent_id, event_type, summary, detail, metadata, session_id, project, refs, importance, caused_by_event_id, causal_chain_root, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.agent, args.type, args.summary, args.detail, metadata_json,
         args.session, args.project, refs_json, args.importance or 0.5,
         caused_by, causal_chain_root, _now_ts())
    )
    event_id = cursor.lastrowid
    log_access(db, args.agent, "write", "events", event_id)
    db.commit()
    result = {"ok": True, "event_id": event_id}
    if caused_by is not None:
        result["caused_by_event_id"] = caused_by
        result["causal_chain_root"] = causal_chain_root
    json_out(result)

def cmd_event_search(args):
    db = get_db()
    limit = args.limit or 20
    no_recency = getattr(args, "no_recency", False)

    if args.query:
        fetch_limit = limit * 5 if not no_recency else limit
        fts_query = _sanitize_fts_query(args.query)
        if not fts_query:
            rows = []
        else:
            rows = db.execute(
                "SELECT e.*, f.rank as fts_rank FROM events e JOIN events_fts f ON e.id = f.rowid "
                "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, fetch_limit)
            ).fetchall()
        results = rows_to_list(rows)
        if not no_recency:
            for r in results:
                # Events use project field to determine scope; no explicit scope column
                scope = ("project:" + r["project"]) if r.get("project") else "global"
                tw = _temporal_weight(r.get("created_at"), scope)
                r["temporal_weight"] = round(tw, 4)
                r["age"] = _age_str(r.get("created_at"))
                r["final_score"] = (r.get("fts_rank") or 0.0) * tw
            results.sort(key=lambda r: r["final_score"])
            results = results[:limit]
    else:
        sql = "SELECT * FROM events WHERE 1=1"
        params = []
        if args.type:
            sql += " AND event_type = ?"
            params.append(args.type)
        if args.agent:
            sql += " AND agent_id = ?"
            params.append(args.agent)
        if args.project:
            sql += " AND project = ?"
            params.append(args.project)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(sql, params).fetchall()
        results = rows_to_list(rows)
        if not no_recency:
            for r in results:
                scope = ("project:" + r["project"]) if r.get("project") else "global"
                r["age"] = _age_str(r.get("created_at"))
                r["temporal_weight"] = round(_temporal_weight(r.get("created_at"), scope), 4)

    log_access(db, args.agent or "unknown", "search", "events", query=args.query, result_count=len(results))
    db.commit()
    json_out(results)

def cmd_event_tail(args):
    db = get_db()
    n = args.n or 20
    rows = db.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    json_out(rows_to_list(reversed(rows)))

# ---------------------------------------------------------------------------
# EPOCH commands
# ---------------------------------------------------------------------------

_EPOCH_TOKEN_STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "into", "over",
    "under", "after", "before", "agent", "agents", "event", "events",
    "result", "results", "update", "status", "error", "warning", "task",
    "tasks", "memory", "memories", "project", "work", "done", "added",
    "completed", "created",
}


def _parse_timestamp(raw: str):
    if not raw:
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _sqlite_ts(raw: str):
    dt = _parse_timestamp(raw)
    if dt is None:
        raise ValueError(f"Invalid timestamp: {raw}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _humanize_slug(value: str):
    cleaned = re.sub(r"[_\-\/]+", " ", (value or "").strip())
    words = [w for w in cleaned.split() if w]
    if not words:
        return "Operational"
    return " ".join(w.capitalize() for w in words)


def _event_topic_tokens(event_row: dict):
    counter = Counter()
    project = (event_row.get("project") or "").strip()
    if project:
        counter[project.lower()] += 3
    event_type = (event_row.get("event_type") or "").strip()
    if event_type:
        counter[event_type.lower()] += 1
    blob_parts = [
        event_row.get("summary") or "",
        event_row.get("detail") or "",
    ]
    refs_raw = event_row.get("refs")
    if refs_raw:
        try:
            refs = json.loads(refs_raw)
            if isinstance(refs, list):
                blob_parts.extend(str(r) for r in refs if r)
        except json.JSONDecodeError:
            blob_parts.append(str(refs_raw))
    token_blob = " ".join(blob_parts).lower()
    for token in re.findall(r"[a-z0-9][a-z0-9_\-]{2,}", token_blob):
        if token in _EPOCH_TOKEN_STOPWORDS:
            continue
        counter[token] += 1
    return counter


def _counter_cosine(left: Counter, right: Counter):
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    dot = 0.0
    for key, lv in left.items():
        dot += lv * right.get(key, 0.0)
    left_norm = sum(v * v for v in left.values()) ** 0.5
    right_norm = sum(v * v for v in right.values()) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def detect_epoch_boundaries(
    events: list[dict],
    *,
    gap_hours: float = 48.0,
    window_size: int = 8,
    min_window: int = 4,
    topic_shift_threshold: float = 0.2,
    min_boundary_distance: int = 8,
):
    candidates = []
    if len(events) < 2:
        return candidates

    for idx in range(1, len(events)):
        prev_ts = _parse_timestamp(events[idx - 1].get("created_at"))
        curr_ts = _parse_timestamp(events[idx].get("created_at"))
        if prev_ts is None or curr_ts is None:
            continue

        gap_h = (curr_ts - prev_ts).total_seconds() / 3600.0
        gap_signal = gap_h >= gap_hours
        reasons = []
        if gap_signal:
            reasons.append("time_gap")

        left_slice = events[max(0, idx - window_size):idx]
        right_slice = events[idx:min(len(events), idx + window_size)]
        topic_similarity = None
        topic_signal = False
        left_top = None
        right_top = None
        if len(left_slice) >= min_window and len(right_slice) >= min_window:
            left_counter = Counter()
            right_counter = Counter()
            left_projects = Counter(str(r.get("project")).strip().lower() for r in left_slice if r.get("project"))
            right_projects = Counter(str(r.get("project")).strip().lower() for r in right_slice if r.get("project"))
            for row in left_slice:
                left_counter.update(_event_topic_tokens(row))
            for row in right_slice:
                right_counter.update(_event_topic_tokens(row))
            topic_similarity = _counter_cosine(left_counter, right_counter)
            left_top = left_counter.most_common(1)[0][0] if left_counter else None
            right_top = right_counter.most_common(1)[0][0] if right_counter else None
            strong_project_shift = False
            if left_projects and right_projects:
                left_proj, left_proj_count = left_projects.most_common(1)[0]
                right_proj, right_proj_count = right_projects.most_common(1)[0]
                left_share = left_proj_count / len(left_slice)
                right_share = right_proj_count / len(right_slice)
                strong_project_shift = left_proj != right_proj and left_share >= 0.5 and right_share >= 0.5
            topic_signal = (
                (strong_project_shift and topic_similarity <= max(topic_shift_threshold, 0.3))
                or topic_similarity <= (topic_shift_threshold / 2.0)
            ) and (
                left_top
                and right_top
                and left_top != right_top
                and gap_h >= 6
            )
            if topic_signal:
                reasons.append("topic_shift")

        if not reasons:
            continue

        score = 0.0
        if gap_signal:
            score += min(gap_h / gap_hours, 3.0)
        if topic_signal and topic_similarity is not None:
            score += max(0.0, 1.0 - topic_similarity)

        candidates.append(
            {
                "boundary_index": idx,
                "boundary_at": events[idx].get("created_at"),
                "reasons": reasons,
                "gap_hours": round(gap_h, 2),
                "topic_similarity": None if topic_similarity is None else round(topic_similarity, 3),
                "left_topic": left_top,
                "right_topic": right_top,
                "score": round(score, 3),
            }
        )

    # Conservative filter: keep stronger candidates and skip nearby weak splits.
    filtered = []
    for cand in sorted(candidates, key=lambda c: c["boundary_index"]):
        if not filtered:
            filtered.append(cand)
            continue
        prev = filtered[-1]
        idx_distance = cand["boundary_index"] - prev["boundary_index"]
        if idx_distance >= min_boundary_distance:
            filtered.append(cand)
            continue
        if cand["score"] > prev["score"] + 0.35:
            filtered[-1] = cand
    return filtered


def _proposed_epoch_name(segment: list[dict]):
    projects = [str(r.get("project")).strip() for r in segment if r.get("project")]
    if projects:
        proj, _ = Counter(projects).most_common(1)[0]
        return f"{_humanize_slug(proj)} Sprint"

    token_counter = Counter()
    for row in segment:
        token_counter.update(_event_topic_tokens(row))
    for noisy in ("observation", "result", "decision", "task_update"):
        token_counter.pop(noisy, None)
    top_tokens = [t for t, _ in token_counter.most_common(2)]
    if len(top_tokens) >= 2:
        return f"{_humanize_slug(top_tokens[0])} {_humanize_slug(top_tokens[1])} Phase"
    if top_tokens:
        return f"{_humanize_slug(top_tokens[0])} Phase"
    return "Operational Phase"


def suggest_epoch_ranges(events: list[dict], boundaries: list[dict], *, min_events_per_epoch: int = 5):
    if not events:
        return []
    kept = []
    start_idx = 0
    for boundary in boundaries:
        idx = boundary["boundary_index"]
        segment_size = idx - start_idx
        # Keep tiny segments only when the time-gap signal is substantial.
        if segment_size < min_events_per_epoch and boundary.get("gap_hours", 0) < 72:
            continue
        kept.append(boundary)
        start_idx = idx

    suggestions = []
    segment_start = 0
    for boundary in kept:
        segment = events[segment_start:boundary["boundary_index"]]
        if segment:
            suggestions.append(
                {
                    "name": _proposed_epoch_name(segment),
                    "started_at": segment[0]["created_at"],
                    "ended_at": segment[-1]["created_at"],
                    "event_count": len(segment),
                    "trigger_next_boundary": {
                        "reasons": boundary["reasons"],
                        "gap_hours": boundary["gap_hours"],
                        "topic_similarity": boundary["topic_similarity"],
                    },
                }
            )
        segment_start = boundary["boundary_index"]

    tail = events[segment_start:]
    if tail:
        suggestions.append(
            {
                "name": _proposed_epoch_name(tail),
                "started_at": tail[0]["created_at"],
                "ended_at": None,
                "event_count": len(tail),
                "trigger_next_boundary": None,
            }
        )
    return suggestions


def cmd_epoch_detect(args):
    db = get_db()
    rows = db.execute(
        "SELECT id, event_type, summary, detail, project, refs, metadata, created_at "
        "FROM events ORDER BY datetime(created_at) ASC, id ASC"
    ).fetchall()
    events = rows_to_list(rows)
    boundaries = detect_epoch_boundaries(
        events,
        gap_hours=args.gap_hours,
        window_size=args.window_size,
        min_window=args.min_window,
        topic_shift_threshold=args.topic_shift_threshold,
        min_boundary_distance=args.min_boundary_distance,
    )
    suggestions = suggest_epoch_ranges(events, boundaries, min_events_per_epoch=args.min_events)
    payload = {
        "ok": True,
        "event_count": len(events),
        "boundary_count": len(boundaries),
        "suggested_epochs": suggestions,
    }
    if args.verbose:
        payload["boundaries"] = boundaries
    json_out(payload)


def cmd_epoch_create(args):
    db = get_db()
    started_at = _sqlite_ts(args.started)
    ended_at = _sqlite_ts(args.ended) if args.ended else None
    if ended_at and ended_at < started_at:
        json_out({"ok": False, "error": "--ended must be >= --started"})
        return

    cursor = db.execute(
        "INSERT INTO epochs (name, description, started_at, ended_at, parent_epoch_id) VALUES (?, ?, ?, ?, ?)",
        (args.name, args.description, started_at, ended_at, args.parent),
    )
    epoch_id = cursor.lastrowid

    if ended_at:
        mem_res = db.execute(
            "UPDATE memories SET epoch_id = ? "
            "WHERE epoch_id IS NULL AND created_at >= ? AND created_at <= ?",
            (epoch_id, started_at, ended_at),
        )
        evt_res = db.execute(
            "UPDATE events SET epoch_id = ? "
            "WHERE epoch_id IS NULL AND created_at >= ? AND created_at <= ?",
            (epoch_id, started_at, ended_at),
        )
    else:
        mem_res = db.execute(
            "UPDATE memories SET epoch_id = ? "
            "WHERE epoch_id IS NULL AND created_at >= ?",
            (epoch_id, started_at),
        )
        evt_res = db.execute(
            "UPDATE events SET epoch_id = ? "
            "WHERE epoch_id IS NULL AND created_at >= ?",
            (epoch_id, started_at),
        )

    db.commit()
    json_out(
        {
            "ok": True,
            "epoch_id": epoch_id,
            "name": args.name,
            "started_at": started_at,
            "ended_at": ended_at,
            "parent_epoch_id": args.parent,
            "backfilled": {
                "memories": mem_res.rowcount,
                "events": evt_res.rowcount,
            },
        }
    )


def cmd_epoch_list(args):
    db = get_db()
    sql = (
        "SELECT e.*, "
        "(SELECT count(*) FROM events ev WHERE ev.epoch_id = e.id) AS event_count, "
        "(SELECT count(*) FROM memories m WHERE m.epoch_id = e.id) AS memory_count "
        "FROM epochs e"
    )
    params = []
    if args.active:
        sql += " WHERE e.started_at <= strftime('%Y-%m-%dT%H:%M:%S', 'now') AND (e.ended_at IS NULL OR e.ended_at > strftime('%Y-%m-%dT%H:%M:%S', 'now'))"
    sql += " ORDER BY datetime(e.started_at) DESC"
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = db.execute(sql, params).fetchall()
    json_out(rows_to_list(rows))

# ---------------------------------------------------------------------------
# CONTEXT commands
# ---------------------------------------------------------------------------

def cmd_context_add(args):
    db = get_db()
    tags_json = json.dumps(args.tags.split(",")) if args.tags else None
    cursor = db.execute(
        "INSERT INTO context (source_type, source_ref, chunk_index, content, summary, project, tags, token_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (args.source_type, args.source_ref, args.chunk or 0, args.content,
         args.summary, args.project, tags_json, args.tokens)
    )
    ctx_id = cursor.lastrowid
    log_access(db, args.agent or "unknown", "write", "context", ctx_id)
    db.commit()
    json_out({"ok": True, "context_id": ctx_id})

def cmd_context_search(args):
    db = get_db()
    limit = args.limit or 20
    fts_query = _sanitize_fts_query(args.query)
    if not fts_query:
        rows = []
    else:
        rows = db.execute(
            "SELECT c.* FROM context c JOIN context_fts f ON c.id = f.rowid "
            "WHERE context_fts MATCH ? AND c.stale_at IS NULL "
            "ORDER BY rank LIMIT ?",
            (fts_query, limit)
        ).fetchall()
    results = rows_to_list(rows)
    log_access(db, args.agent or "unknown", "search", "context", query=args.query, result_count=len(results))
    db.commit()
    json_out(results)

# ---------------------------------------------------------------------------
# TASK commands
# ---------------------------------------------------------------------------

def cmd_task_add(args):
    db = get_db()
    metadata_json = args.metadata
    cursor = db.execute(
        "INSERT INTO tasks (external_id, external_system, title, description, status, priority, "
        "assigned_agent_id, project, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.external_id, args.external_system, args.title, args.description,
         args.status or "pending", args.priority or "medium",
         args.assign, args.project, metadata_json)
    )
    task_id = cursor.lastrowid
    log_access(db, args.agent or "unknown", "write", "tasks", task_id)
    db.commit()
    json_out({"ok": True, "task_id": task_id})

def cmd_task_update(args):
    db = get_db()
    sets = []
    params = []
    if args.status:
        sets.append("status = ?")
        params.append(args.status)
        if args.status == "completed":
            sets.append("completed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        if args.status == "in_progress" and not args.no_claim:
            sets.append("claimed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
            sets.append("claimed_by = ?")
            params.append(args.agent or "unknown")
    if args.assign:
        sets.append("assigned_agent_id = ?")
        params.append(args.assign)
    if args.priority:
        sets.append("priority = ?")
        params.append(args.priority)
    sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
    params.append(args.id)
    db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
    log_access(db, args.agent or "unknown", "write", "tasks", args.id)
    db.commit()
    json_out({"ok": True, "task_id": args.id})

def cmd_task_list(args):
    db = get_db()
    sql = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if args.status:
        sql += " AND status = ?"
        params.append(args.status)
    if args.agent:
        sql += " AND assigned_agent_id = ?"
        params.append(args.agent)
    if args.project:
        sql += " AND project = ?"
        params.append(args.project)
    sql += " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END, created_at"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = db.execute(sql, params).fetchall()
    json_out(rows_to_list(rows))

# ---------------------------------------------------------------------------
# DECISION commands
# ---------------------------------------------------------------------------

def cmd_decision_add(args):
    db = get_db()
    alts_json = json.dumps(args.alternatives.split("|")) if args.alternatives else None
    cursor = db.execute(
        "INSERT INTO decisions (agent_id, title, rationale, alternatives_considered, project, reversible, source_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (args.agent, args.title, args.rationale, alts_json, args.project,
         1 if args.reversible else 0, args.source_event)
    )
    dec_id = cursor.lastrowid
    log_access(db, args.agent, "write", "decisions", dec_id)
    db.commit()
    json_out({"ok": True, "decision_id": dec_id})

def cmd_decision_list(args):
    db = get_db()
    sql = "SELECT * FROM decisions WHERE reversed_at IS NULL"
    params = []
    if args.project:
        sql += " AND project = ?"
        params.append(args.project)
    sql += " ORDER BY created_at DESC"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = db.execute(sql, params).fetchall()
    json_out(rows_to_list(rows))

# ---------------------------------------------------------------------------
# HANDOFF commands — temporary working-state continuity across session resets
# ---------------------------------------------------------------------------


def cmd_handoff_add(args):
    validated = _validate_handoff_fields(
        agent_id=args.agent, goal=args.goal, current_state=args.current_state, open_loops=args.open_loops,
        next_step=args.next_step, title=args.title, session_id=args.session, chat_id=args.chat_id,
        thread_id=args.thread_id, user_id=args.user_id, project=args.project, scope=args.scope,
        status=args.status, recent_tail=args.recent_tail, decisions_json=args.decisions_json,
        entities_json=args.entities_json, tasks_json=args.tasks_json, facts_json=args.facts_json,
        source_event_id=args.source_event, expires_at=args.expires_at,
    )
    db = get_db()
    now = _now_ts()
    cursor = db.execute(
        """
        INSERT INTO handoff_packets (
            agent_id, session_id, chat_id, thread_id, user_id, project, scope, status,
            title, goal, current_state, open_loops, next_step, recent_tail,
            decisions_json, entities_json, tasks_json, facts_json,
            source_event_id, expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            validated["agent_id"],
            validated["session_id"],
            validated["chat_id"],
            validated["thread_id"],
            validated["user_id"],
            validated["project"],
            validated["scope"],
            validated["status"],
            validated["title"],
            validated["goal"],
            validated["current_state"],
            validated["open_loops"],
            validated["next_step"],
            validated["recent_tail"],
            validated["decisions_json"],
            validated["entities_json"],
            validated["tasks_json"],
            validated["facts_json"],
            validated["source_event_id"],
            validated["expires_at"],
            now,
            now,
        ),
    )
    handoff_id = cursor.lastrowid
    log_access(db, args.agent, "write", "handoff_packets", handoff_id)
    db.commit()
    json_out({"ok": True, "handoff_id": handoff_id, "status": validated["status"]})


def cmd_handoff_list(args):
    db = get_db()
    sql = "SELECT * FROM handoff_packets WHERE 1=1"
    params = []
    if args.status:
        sql += " AND status = ?"
        params.append(args.status)
    if args.project:
        sql += " AND project = ?"
        params.append(args.project)
    if args.chat_id:
        sql += " AND chat_id = ?"
        params.append(args.chat_id)
    if args.thread_id:
        sql += " AND thread_id = ?"
        params.append(args.thread_id)
    if args.user_id:
        sql += " AND user_id = ?"
        params.append(args.user_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(args.limit or 20)
    rows = db.execute(sql, params).fetchall()
    json_out(rows_to_list(rows))


def cmd_handoff_latest(args):
    validated = _validate_handoff_fields(
        agent_id=args.agent, project=args.project, chat_id=args.chat_id,
        thread_id=args.thread_id, user_id=args.user_id, status=args.status or "pending",
    )
    db = get_db()
    status = validated["status"]
    candidates = []

    if validated["chat_id"] and validated["thread_id"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE chat_id = ? AND thread_id = ? AND status = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (validated["chat_id"], validated["thread_id"], status, validated["agent_id"]),
        ))
    if validated["chat_id"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE chat_id = ? AND status = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (validated["chat_id"], status, validated["agent_id"]),
        ))
    if validated["project"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE project = ? AND status = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (validated["project"], status, validated["agent_id"]),
        ))
    if validated["user_id"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE user_id = ? AND agent_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (validated["user_id"], validated["agent_id"], status),
        ))

    candidates.append((
        "SELECT * FROM handoff_packets WHERE agent_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
        (validated["agent_id"], status),
    ))

    row = None
    for sql, params in candidates:
        row = db.execute(sql, params).fetchone()
        if row:
            break

    json_out(row_to_dict(row) or {})


def cmd_handoff_consume(args):
    _validate_handoff_fields(agent_id=args.agent)
    handoff_id = _optional_int(args.id, "handoff_id")
    db = get_db()
    row = db.execute("SELECT id, status FROM handoff_packets WHERE id = ? AND agent_id = ?", (handoff_id, args.agent)).fetchone()
    if not row:
        json_out({"ok": False, "error": f"handoff {handoff_id} not found for agent {args.agent}"})
        return

    now = _now_ts()
    db.execute(
        "UPDATE handoff_packets SET status = 'consumed', consumed_at = ?, updated_at = ? WHERE id = ?",
        (now, now, args.id),
    )
    log_access(db, args.agent, "write", "handoff_packets", args.id)
    db.commit()
    json_out({"ok": True, "handoff_id": args.id, "status": "consumed", "consumed_at": now})


def cmd_handoff_pin(args):
    _validate_handoff_fields(agent_id=args.agent)
    handoff_id = _optional_int(args.id, "handoff_id")
    db = get_db()
    row = db.execute("SELECT id FROM handoff_packets WHERE id = ? AND agent_id = ?", (handoff_id, args.agent)).fetchone()
    if not row:
        json_out({"ok": False, "error": f"handoff {handoff_id} not found for agent {args.agent}"})
        return

    now = _now_ts()
    db.execute(
        "UPDATE handoff_packets SET status = 'pinned', expires_at = NULL, updated_at = ? WHERE id = ?",
        (now, args.id),
    )
    log_access(db, args.agent, "write", "handoff_packets", args.id)
    db.commit()
    json_out({"ok": True, "handoff_id": args.id, "status": "pinned"})


def cmd_handoff_expire(args):
    _validate_handoff_fields(agent_id=args.agent)
    handoff_id = _optional_int(args.id, "handoff_id")
    db = get_db()
    row = db.execute("SELECT id FROM handoff_packets WHERE id = ? AND agent_id = ?", (handoff_id, args.agent)).fetchone()
    if not row:
        json_out({"ok": False, "error": f"handoff {handoff_id} not found for agent {args.agent}"})
        return

    now = _now_ts()
    db.execute(
        "UPDATE handoff_packets SET status = 'expired', updated_at = ? WHERE id = ?",
        (now, args.id),
    )
    log_access(db, args.agent, "write", "handoff_packets", args.id)
    db.commit()
    json_out({"ok": True, "handoff_id": args.id, "status": "expired"})

# ---------------------------------------------------------------------------
# STATE commands — per-agent key/value store
# ---------------------------------------------------------------------------

def cmd_state_get(args):
    db = get_db()
    if args.key:
        row = db.execute(
            "SELECT * FROM agent_state WHERE agent_id = ? AND key = ?",
            (args.agent, args.key)
        ).fetchone()
        json_out(row_to_dict(row))
    else:
        rows = db.execute(
            "SELECT * FROM agent_state WHERE agent_id = ?", (args.agent,)
        ).fetchall()
        json_out(rows_to_list(rows))

def cmd_state_set(args):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))",
        (args.agent, args.key, args.value)
    )
    db.commit()
    json_out({"ok": True, "agent": args.agent, "key": args.key})

# ---------------------------------------------------------------------------
# ATTENTION CLASS — cognitive budget tier for agents
# Tiers: exec | ic | peripheral | dormant
# ---------------------------------------------------------------------------

_ATTENTION_CLASSES = {"exec", "ic", "peripheral", "dormant"}

_ATTENTION_CLASS_SPECS = {
    "exec":       {"context_budget": "32K", "commands": "all",            "use_case": "CEO, CTO, active managers"},
    "ic":         {"context_budget": "8K",  "commands": "all",            "use_case": "active individual contributors"},
    "peripheral": {"context_budget": "2K",  "commands": "search, push",   "use_case": "background watchers"},
    "dormant":    {"context_budget": "0",   "commands": "none",           "use_case": "idle/error agents"},
}


def cmd_attention_class_get(args):
    db = get_db()
    if args.agent:
        row = db.execute(
            "SELECT id, display_name, attention_class FROM agents WHERE id = ?",
            (args.agent,)
        ).fetchone()
        if not row:
            print(f"ERROR: Agent '{args.agent}' not found in brain.db", file=sys.stderr)
            sys.exit(1)
        result = dict(row)
    else:
        rows = db.execute(
            "SELECT id, display_name, attention_class FROM agents ORDER BY id"
        ).fetchall()
        result = [dict(r) for r in rows]
    json_out(result)


def cmd_attention_class_set(args):
    if args.class_name not in _ATTENTION_CLASSES:
        print(f"ERROR: Invalid attention class '{args.class_name}'. Must be one of: {', '.join(sorted(_ATTENTION_CLASSES))}", file=sys.stderr)
        sys.exit(1)
    db = get_db()
    agent_id = args.agent
    row = db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        print(f"ERROR: Agent '{agent_id}' not found in brain.db", file=sys.stderr)
        sys.exit(1)
    db.execute(
        "UPDATE agents SET attention_class = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
        (args.class_name, agent_id)
    )
    db.commit()
    spec = _ATTENTION_CLASS_SPECS[args.class_name]
    json_out({
        "ok": True,
        "agent": agent_id,
        "attention_class": args.class_name,
        "context_budget": spec["context_budget"],
        "commands_allowed": spec["commands"],
    })

# ---------------------------------------------------------------------------
# BUDGET STATUS — fleet-wide token consumption dashboard
# ---------------------------------------------------------------------------

# Token budget ceilings per tier (tokens/heartbeat)
_BUDGET_TIER_CEILINGS = {
    0: None,   # Tier 0 — exec/CEO: unlimited
    1: 5000,   # Tier 1 — senior IC
    2: 2000,   # Tier 2 — specialist
    3: 500,    # Tier 3 — worker
}
_BUDGET_TIER_LABELS = {
    0: "exec (unlimited)",
    1: "senior-ic (5K)",
    2: "specialist (2K)",
    3: "worker (500)",
}


def cmd_budget_status(args):
    """Show per-agent and fleet-wide token consumption for the current day."""
    db = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = db.execute(
        """
        SELECT
            al.agent_id,
            a.display_name,
            COALESCE(a.attention_budget_tier, 1) AS tier,
            COUNT(*) AS query_count,
            COALESCE(SUM(al.tokens_consumed), 0) AS tokens_today
        FROM access_log al
        LEFT JOIN agents a ON al.agent_id = a.id
        WHERE al.created_at >= ? AND al.tokens_consumed IS NOT NULL
        GROUP BY al.agent_id
        ORDER BY tokens_today DESC
        """,
        (today + " 00:00:00",),
    ).fetchall()

    fleet_total = sum(r["tokens_today"] for r in rows)
    at_cap = []
    output = []

    for r in rows:
        tier = r["tier"] if r["tier"] is not None else 1
        ceiling = _BUDGET_TIER_CEILINGS.get(tier)
        tier_label = _BUDGET_TIER_LABELS.get(tier, f"tier-{tier}")
        pct = None
        flagged = False
        if ceiling:
            # flag if average per-query consumption implies cap hits
            avg_per_query = r["tokens_today"] / max(r["query_count"], 1)
            flagged = avg_per_query >= ceiling * 0.8
            if flagged:
                at_cap.append(r["agent_id"])
            pct = round(r["tokens_today"] / ceiling * 100, 1)
        entry = {
            "agent_id": r["agent_id"],
            "display_name": r["display_name"] or r["agent_id"],
            "tier": tier,
            "tier_label": tier_label,
            "queries_today": r["query_count"],
            "tokens_today": r["tokens_today"],
            "ceiling": ceiling,
            "ceiling_pct": pct,
            "flagged": flagged,
        }
        output.append(entry)

    if getattr(args, "json", False):
        json_out({"date": today, "fleet_total": fleet_total, "agents": output, "at_cap": at_cap})
        return

    print(f"Budget Status — {today}  (fleet total: {fleet_total:,} tokens)")
    print()
    col_w = [30, 22, 12, 12, 14, 8]
    header = f"{'Agent':<{col_w[0]}} {'Tier':<{col_w[1]}} {'Queries':>{col_w[2]}} {'Tokens':>{col_w[3]}} {'Cap %':>{col_w[4]}} {'Flag':>{col_w[5]}}"
    print(header)
    print("-" * sum(col_w))
    for e in output:
        flag_str = "⚠ CAP" if e["flagged"] else ""
        cap_str = f"{e['ceiling_pct']}%" if e["ceiling_pct"] is not None else "—"
        name = (e["display_name"] or e["agent_id"])[:col_w[0] - 1]
        print(
            f"{name:<{col_w[0]}} {e['tier_label']:<{col_w[1]}} "
            f"{e['queries_today']:>{col_w[2]}} {e['tokens_today']:>{col_w[3]},} "
            f"{cap_str:>{col_w[4]}} {flag_str:>{col_w[5]}}"
        )
    print()
    print(f"Fleet total: {fleet_total:,} tokens today")
    if at_cap:
        print(f"Agents near/at cap: {', '.join(at_cap)}")


# ---------------------------------------------------------------------------
# NEUROMODULATION — org-state sensing and salience parameter modulation
# ---------------------------------------------------------------------------

_NEURO_PRESETS = {
    "normal":            {"arousal_level":0.3,"retrieval_breadth_multiplier":1.0,"consolidation_immediacy":"scheduled","consolidation_interval_mins":240,"focus_level":0.3,"similarity_threshold_delta":0.0,"exploitation_bias":0.0,"temporal_lambda":0.030,"context_window_depth":50,"confidence_decay_rate":0.020},
    "incident":          {"arousal_level":0.9,"retrieval_breadth_multiplier":1.6,"consolidation_immediacy":"immediate","consolidation_interval_mins":30, "focus_level":0.1,"similarity_threshold_delta":-0.10,"exploitation_bias":0.0,"temporal_lambda":0.100,"context_window_depth":75,"confidence_decay_rate":0.005},
    "sprint":            {"arousal_level":0.5,"retrieval_breadth_multiplier":1.2,"consolidation_immediacy":"scheduled","consolidation_interval_mins":120,"focus_level":0.5,"similarity_threshold_delta":0.0,"exploitation_bias":0.2,"temporal_lambda":0.060,"context_window_depth":30,"confidence_decay_rate":0.015},
    "strategic_planning":{"arousal_level":0.2,"retrieval_breadth_multiplier":0.9,"consolidation_immediacy":"scheduled","consolidation_interval_mins":480,"focus_level":0.4,"similarity_threshold_delta":0.05,"exploitation_bias":0.1,"temporal_lambda":0.005,"context_window_depth":200,"confidence_decay_rate":0.010},
    "focused_work":      {"arousal_level":0.6,"retrieval_breadth_multiplier":0.8,"consolidation_immediacy":"scheduled","consolidation_interval_mins":120,"focus_level":0.8,"similarity_threshold_delta":0.08,"exploitation_bias":0.4,"temporal_lambda":0.080,"context_window_depth":25,"confidence_decay_rate":0.015},
}

_MODE_ALIASES = {
    "normal":"normal","urgent":"incident","incident":"incident","sprint":"sprint",
    "strategic":"strategic_planning","strategic_planning":"strategic_planning",
    "focused":"focused_work","focused_work":"focused_work",
}


def _neuro_get_state(db):
    row = db.execute("SELECT * FROM neuromodulation_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def _neuro_is_expired(state):
    if not state.get("expires_at"):
        return False
    try:
        exp = datetime.fromisoformat(state["expires_at"])
        now = datetime.utcnow() if exp.tzinfo is None else datetime.now(timezone.utc)
        return now > exp
    except Exception:
        return False


def _neuro_detect(db):
    """Auto-detect org_state from recent events. Returns (org_state, reason)."""
    if db.execute("SELECT id FROM epochs WHERE (name LIKE '%incident%' OR name LIKE '%outage%' OR name LIKE '%emergency%') AND started_at<=strftime('%Y-%m-%dT%H:%M:%S','now') AND (ended_at IS NULL OR ended_at>=strftime('%Y-%m-%dT%H:%M:%S','now')) LIMIT 1").fetchone():
        return "incident", "active incident epoch"
    err = db.execute("SELECT COUNT(*) FROM events WHERE event_type IN ('error','warning') AND created_at>=strftime('%Y-%m-%dT%H:%M:%S',datetime('now','-2 hours'))").fetchone()[0]
    if err >= 5:
        return "incident", f"{err} error/warning events in last 2h"
    total6h = db.execute("SELECT COUNT(*) FROM events WHERE created_at>=strftime('%Y-%m-%dT%H:%M:%S',datetime('now','-6 hours'))").fetchone()[0]
    plan6h = db.execute("SELECT COUNT(*) FROM events WHERE (summary LIKE '%planning%' OR summary LIKE '%roadmap%' OR summary LIKE '%strategy%' OR event_type='decision') AND created_at>=strftime('%Y-%m-%dT%H:%M:%S',datetime('now','-6 hours'))").fetchone()[0]
    if total6h > 0 and plan6h / total6h >= 0.5:
        return "strategic_planning", f"{plan6h}/{total6h} recent events are planning-tagged"
    if db.execute("SELECT id FROM epochs WHERE name LIKE '%sprint%' AND started_at<=strftime('%Y-%m-%dT%H:%M:%S','now') AND (ended_at IS NULL OR ended_at>=strftime('%Y-%m-%dT%H:%M:%S','now')) LIMIT 1").fetchone():
        return "sprint", "active sprint epoch"
    trate = db.execute("SELECT COUNT(*) FROM events WHERE event_type='task_update' AND created_at>=strftime('%Y-%m-%dT%H:%M:%S',datetime('now','-2 hours'))").fetchone()[0]
    if trate > 16:
        return "sprint", f"high task activity: {trate} task events in last 2h"
    if total6h >= 3:
        row = db.execute("SELECT project, COUNT(*) as cnt FROM events WHERE created_at>=strftime('%Y-%m-%dT%H:%M:%S',datetime('now','-2 hours')) AND project IS NOT NULL GROUP BY project ORDER BY cnt DESC LIMIT 1").fetchone()
        if row and total6h > 0 and row[1] / total6h >= 0.80:
            return "focused_work", f"80%+ events from project: {row[0]}"
    return "normal", "no trigger conditions met"


def _neuro_apply_preset(db, org_state, method, agent_id, notes, expires_at=None):
    p = _NEURO_PRESETS[org_state]
    db.execute("""UPDATE neuromodulation_state SET
        org_state=?,arousal_level=?,retrieval_breadth_multiplier=?,
        consolidation_immediacy=?,consolidation_interval_mins=?,
        focus_level=?,similarity_threshold_delta=?,exploitation_bias=?,
        temporal_lambda=?,context_window_depth=?,confidence_decay_rate=?,
        detection_method=?,detected_at=strftime('%Y-%m-%dT%H:%M:%S','now'),
        expires_at=?,triggered_by=?,notes=? WHERE id=1""",
        (org_state,p["arousal_level"],p["retrieval_breadth_multiplier"],
         p["consolidation_immediacy"],p["consolidation_interval_mins"],
         p["focus_level"],p["similarity_threshold_delta"],p["exploitation_bias"],
         p["temporal_lambda"],p["context_window_depth"],p["confidence_decay_rate"],
         method,expires_at,agent_id,notes))


def cmd_neuro_status(args):
    db = get_db()
    state = _neuro_get_state(db)
    if not state:
        print("neuromodulation_state table not found", file=sys.stderr); sys.exit(1)
    reverted, revert_reason = False, ""
    if state.get("detection_method") == "manual" and _neuro_is_expired(state):
        new_state, reason = _neuro_detect(db)
        _neuro_apply_preset(db, new_state, "auto", "auto", f"auto-reverted: {reason}")
        db.commit(); state = _neuro_get_state(db); reverted, revert_reason = True, reason
    if getattr(args, "format", "text") == "json":
        json_out(state); return
    org = state.get("org_state", "unknown")
    labels = {"normal":"NORMAL","incident":"URGENT (incident)","sprint":"SPRINT","strategic_planning":"STRATEGIC","focused_work":"FOCUSED"}
    print(f"Neuromodulation state: {labels.get(org, org.upper())}")
    print(f"  Detection method : {state.get('detection_method','?')}")
    print(f"  Detected at      : {state.get('detected_at','?')}")
    if state.get("expires_at"): print(f"  Expires at       : {state['expires_at']}")
    if state.get("notes"):      print(f"  Notes            : {state['notes']}")
    print()
    print("Parameters:")
    print(f"  arousal_level                : {state.get('arousal_level')}")
    print(f"  retrieval_breadth_multiplier : {state.get('retrieval_breadth_multiplier')}x")
    print(f"  consolidation_immediacy      : {state.get('consolidation_immediacy')} ({state.get('consolidation_interval_mins')} min)")
    print(f"  focus_level                  : {state.get('focus_level')}")
    print(f"  similarity_threshold_delta   : {state.get('similarity_threshold_delta', 0.0):+.2f}")
    print(f"  exploitation_bias            : {state.get('exploitation_bias')}")
    print(f"  temporal_lambda              : {state.get('temporal_lambda')}")
    print(f"  context_window_depth         : {state.get('context_window_depth')}")
    print(f"  confidence_decay_rate        : {state.get('confidence_decay_rate')}/day")
    print(f"  dopamine_signal              : {state.get('dopamine_signal', 0.0):+.2f}")
    if reverted: print(f"\n  [auto-reverted from expired manual override: {revert_reason}]")


def cmd_neuro_set(args):
    db = get_db()
    org_state = _MODE_ALIASES.get(args.mode.lower())
    if org_state is None:
        print(f"Unknown mode '{args.mode}'. Valid: {', '.join(sorted(_MODE_ALIASES))}", file=sys.stderr); sys.exit(1)
    current = _neuro_get_state(db); from_state = current.get("org_state", "normal")
    agent_id = getattr(args, "agent", None) or "manual"
    notes = getattr(args, "notes", None) or f"manual override to {org_state}"
    expires_at = getattr(args, "expires", None)
    _neuro_apply_preset(db, org_state, "manual", agent_id, notes, expires_at)
    if from_state != org_state:
        db.execute("INSERT INTO neuromodulation_transitions (from_state,to_state,reason,triggered_by) VALUES (?,?,?,?)", (from_state, org_state, notes, agent_id))
    db.commit()
    print(f"Neuromodulation state set to: {org_state}")
    if expires_at: print(f"  Expires at: {expires_at}")


def cmd_neuro_detect(args):
    db = get_db()
    current = _neuro_get_state(db); from_state = current.get("org_state", "normal")
    if current.get("detection_method") == "manual" and not _neuro_is_expired(current) and not getattr(args, "force", False):
        print(f"Manual override active ({from_state}) — skipping. Use --force to override."); return
    org_state, reason = _neuro_detect(db)
    agent_id = getattr(args, "agent", None) or "auto"
    _neuro_apply_preset(db, org_state, "auto", agent_id, reason)
    if from_state != org_state:
        db.execute("INSERT INTO neuromodulation_transitions (from_state,to_state,reason,triggered_by) VALUES (?,?,?,?)", (from_state, org_state, reason, agent_id))
    db.commit()
    if getattr(args, "format", "text") == "json":
        json_out({"org_state": org_state, "reason": reason, "from_state": from_state}); return
    print(f"Detected: {org_state}")
    print(f"  Reason: {reason}")
    if from_state != org_state: print(f"  Transitioned: {from_state} \u2192 {org_state}")


def cmd_neuro_history(args):
    db = get_db()
    rows = db.execute("SELECT from_state,to_state,reason,triggered_by,transitioned_at FROM neuromodulation_transitions ORDER BY transitioned_at DESC LIMIT ?", (getattr(args, "limit", 20) or 20,)).fetchall()
    if getattr(args, "format", "text") == "json":
        json_out(rows_to_list(rows)); return
    if not rows: print("No transitions recorded."); return
    for row in rows:
        r = dict(row)
        print(f"  {r['transitioned_at']}  {r['from_state']} \u2192 {r['to_state']}")
        if r.get("reason"): print(f"    reason: {r['reason']}")


def _compute_neurotransmitter_levels(db) -> dict:
    """Compute current dopamine, norepinephrine, acetylcholine, serotonin levels
    from org activity in brain.db. Returns dict with levels in [0.0, 1.0]."""
    now_sql = _now_ts()

    # --- Dopamine (reward signal): goal completion rate in last 24h ---
    # Proxy: ratio of result/decision events to error/warning events in last 24h
    positive_24h = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('result','decision','memory_promoted') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours'))"
    ).fetchone()[0]
    negative_24h = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('error','warning','stale_context') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours'))"
    ).fetchone()[0]
    total_24h = positive_24h + negative_24h
    if total_24h == 0:
        dopamine = 0.4  # neutral baseline
    else:
        dopamine = min(1.0, max(0.0, positive_24h / total_24h))

    # Factor in dopamine_signal from neuromodulation_state (injection-adjusted)
    nm_row = db.execute("SELECT dopamine_signal FROM neuromodulation_state WHERE id=1").fetchone()
    if nm_row and nm_row["dopamine_signal"]:
        # Blend computed with injected signal (injected signal adjusts by up to ±0.3)
        dopamine = min(1.0, max(0.0, dopamine + nm_row["dopamine_signal"] * 0.3))

    # --- Norepinephrine (arousal/urgency): error events + high error rate ---
    error_2h = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('error','warning') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-2 hours'))"
    ).fetchone()[0]
    total_2h = db.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-2 hours'))"
    ).fetchone()[0] or 1
    # Check for active incident epoch
    incident_active = db.execute(
        "SELECT 1 FROM epochs WHERE (name LIKE '%incident%' OR name LIKE '%outage%' OR name LIKE '%emergency%') "
        "AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?) LIMIT 1",
        (now_sql, now_sql)
    ).fetchone()
    norepinephrine_raw = min(1.0, error_2h / 5.0)  # saturates at 5 errors/2h
    if incident_active:
        norepinephrine_raw = max(0.8, norepinephrine_raw)
    norepinephrine = round(norepinephrine_raw, 3)

    # --- Acetylcholine (attention/novelty): novelty of recent memory writes ---
    # High when many new unique scopes written recently vs total active memories
    new_memories_1h = db.execute(
        "SELECT COUNT(DISTINCT scope) FROM memories WHERE retired_at IS NULL "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-1 hour'))"
    ).fetchone()[0]
    active_scopes = db.execute(
        "SELECT COUNT(DISTINCT scope) FROM memories WHERE retired_at IS NULL"
    ).fetchone()[0] or 1
    acetylcholine = round(min(1.0, new_memories_1h / max(1, active_scopes * 0.2)), 3)
    # Boost if many distinct agents wrote recently (high novelty = high ACh)
    distinct_agents_1h = db.execute(
        "SELECT COUNT(DISTINCT agent_id) FROM events "
        "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-1 hour')) "
        "AND agent_id IS NOT NULL"
    ).fetchone()[0]
    if distinct_agents_1h > 3:
        acetylcholine = min(1.0, acetylcholine + 0.2)

    # --- Serotonin (time horizon/patience): derived from temporal_lambda ---
    # Low lambda = long horizon = high serotonin (patient)
    # High lambda = short horizon = low serotonin (reactive)
    nm_lambda = db.execute("SELECT temporal_lambda FROM neuromodulation_state WHERE id=1").fetchone()
    lam = nm_lambda["temporal_lambda"] if nm_lambda else 0.030
    # lambda range: 0.005 (strategic, max patience) to 0.100 (incident, min patience)
    serotonin = round(1.0 - min(1.0, max(0.0, (lam - 0.005) / 0.095)), 3)

    return {
        "dopamine_level": round(dopamine, 3),
        "norepinephrine_level": round(norepinephrine, 3),
        "acetylcholine_level": round(acetylcholine, 3),
        "serotonin_level": serotonin,
    }


def _log_neuro_event(db, levels: dict, org_state: str, source: str, agent_id: str = None, notes: str = None):
    """Log neurotransmitter levels to neuro_events history table."""
    try:
        db.execute(
            "INSERT INTO neuro_events (org_state, dopamine_level, norepinephrine_level, "
            "acetylcholine_level, serotonin_level, source, agent_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_state, levels["dopamine_level"], levels["norepinephrine_level"],
             levels["acetylcholine_level"], levels["serotonin_level"],
             source, agent_id, notes)
        )
    except Exception:
        pass  # neuro_events table may not exist on older brain.db versions


def cmd_neurostate(args):
    """brainctl neurostate — compute and display current neurotransmitter levels from org activity."""
    db = get_db()
    state = _neuro_get_state(db)
    if not state:
        print("neuromodulation_state table not found. Run brain.db migrations first.", file=sys.stderr)
        sys.exit(1)

    # Auto-detect if needed and apply
    if getattr(args, "detect", False) or state.get("detection_method") == "auto":
        new_org_state, reason = _neuro_detect(db)
        if new_org_state != state.get("org_state"):
            from_state = state.get("org_state", "normal")
            _neuro_apply_preset(db, new_org_state, "auto", getattr(args, "agent", None) or "epoch", reason)
            db.execute(
                "INSERT INTO neuromodulation_transitions (from_state,to_state,reason,triggered_by) VALUES (?,?,?,?)",
                (from_state, new_org_state, reason, getattr(args, "agent", None) or "epoch")
            )
            db.commit()
            state = _neuro_get_state(db)

    org_state = state.get("org_state", "normal")
    levels = _compute_neurotransmitter_levels(db)

    # Log to neuro_events history
    agent_id = getattr(args, "agent", None) or os.environ.get("AGENT_ID") or "epoch"
    _log_neuro_event(db, levels, org_state, "auto_detect", agent_id)
    db.commit()

    if getattr(args, "format", "text") == "json":
        json_out({"org_state": org_state, **levels, "neuromod_params": dict(state)})
        return

    org_labels = {
        "normal": "NORMAL", "incident": "INCIDENT", "sprint": "SPRINT",
        "strategic_planning": "STRATEGIC PLANNING", "focused_work": "FOCUSED WORK"
    }
    bar_width = 20

    def _bar(val):
        filled = round(val * bar_width)
        return "[" + "#" * filled + "." * (bar_width - filled) + f"] {val:.3f}"

    print(f"Neurostate — Org Mode: {org_labels.get(org_state, org_state.upper())}")
    print(f"  Detected   : {state.get('detected_at', '?')}")
    print()
    print("Neurotransmitter Levels (derived from org activity):")
    print(f"  Dopamine        (reward/confidence)  {_bar(levels['dopamine_level'])}")
    print(f"  Norepinephrine  (arousal/urgency)    {_bar(levels['norepinephrine_level'])}")
    print(f"  Acetylcholine   (attention/novelty)  {_bar(levels['acetylcholine_level'])}")
    print(f"  Serotonin       (patience/horizon)   {_bar(levels['serotonin_level'])}")
    print()
    print("Active Parameters:")
    print(f"  temporal_lambda              : {state.get('temporal_lambda')}  (retrieval decay)")
    print(f"  retrieval_breadth_multiplier : {state.get('retrieval_breadth_multiplier')}x")
    print(f"  confidence_decay_rate        : {state.get('confidence_decay_rate')}/day")
    print(f"  context_window_depth         : {state.get('context_window_depth')} events")


def cmd_neuro_signal(args):
    """Inject a dopamine signal — boost or penalize memory confidence in a scope."""
    db = get_db()
    dopamine = float(args.dopamine)
    if not (-1.0 <= dopamine <= 1.0):
        print("--dopamine must be between -1.0 and +1.0", file=sys.stderr); sys.exit(1)

    scope = getattr(args, "scope", None)
    since = getattr(args, "since", None)
    agent_id = getattr(args, "agent", None) or os.environ.get("AGENT_ID") or "epoch"
    magnitude = abs(dopamine)
    now_sql = _now_ts()

    # Build WHERE clauses
    where_parts = ["retired_at IS NULL"]
    params = []
    if scope:
        where_parts.append("scope = ?")
        params.append(scope)
    if since:
        where_parts.append("last_recalled_at >= ?")
        params.append(since)
    where = " AND ".join(where_parts)

    if dopamine > 0:
        # Positive dopamine: boost confidence
        db.execute(
            f"UPDATE memories SET confidence = MIN(1.0, confidence + ?) WHERE {where}",
            [round(0.1 * magnitude, 4)] + params
        )
        direction = "boost"
    else:
        # Negative dopamine: penalize confidence, tag for review
        db.execute(
            f"UPDATE memories SET confidence = MAX(0.1, confidence - ?), "
            f"tags = json_insert(COALESCE(tags, '[]'), '$[#]', 'needs_review') WHERE {where}",
            [round(0.08 * magnitude, 4)] + params
        )
        direction = "penalize"

    affected = db.execute("SELECT changes()").fetchone()[0]

    # Update dopamine_signal in neuromodulation_state (decaying reservoir)
    current_signal = db.execute("SELECT dopamine_signal FROM neuromodulation_state WHERE id=1").fetchone()
    cur = float(current_signal["dopamine_signal"]) if current_signal else 0.0
    new_signal = max(-1.0, min(1.0, cur + dopamine * 0.5))
    db.execute(
        "UPDATE neuromodulation_state SET dopamine_signal=?, dopamine_last_fired_at=? WHERE id=1",
        (round(new_signal, 4), now_sql)
    )

    # Log to neuro_events
    state = _neuro_get_state(db)
    levels = _compute_neurotransmitter_levels(db)
    _log_neuro_event(db, levels, state.get("org_state", "normal"), "signal_inject",
                     agent_id, f"dopamine={dopamine:+.2f} scope={scope} since={since}")
    db.commit()

    if getattr(args, "format", "text") == "json":
        json_out({"signal": dopamine, "direction": direction, "affected_memories": affected,
                  "new_dopamine_signal": new_signal, "scope": scope})
        return
    print(f"Dopamine signal {dopamine:+.2f} applied — {direction} {affected} memories in scope '{scope or 'all'}'")
    print(f"  Updated dopamine_signal reservoir: {new_signal:+.4f}")


# ---------------------------------------------------------------------------
# SEARCH — universal cross-table search (hybrid FTS5 + vec RRF)
# ---------------------------------------------------------------------------

def _try_get_db_with_vec():
    """Open DB with sqlite-vec loaded. Returns None (never raises) if unavailable."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.enable_load_extension(True)
        conn.load_extension(VEC_DYLIB)
        conn.enable_load_extension(False)
        return conn
    except Exception:
        return None


def _surprise_score(db, content: str, blob=None):
    """Compute surprise score for a candidate memory against existing memories.

    Returns (surprise: float, method: str) where surprise ∈ [0, 1].
    1.0 = maximally novel, 0.0 = exact duplicate.
    Uses cosine similarity via embeddings if available, falls back to FTS5 word overlap.
    """
    # Method 1: Cosine similarity via embeddings (if blob available)
    if blob:
        try:
            db_vec = _try_get_db_with_vec()
            if db_vec:
                try:
                    rows = db_vec.execute(
                        "SELECT rowid FROM vec_memories WHERE embedding MATCH ? AND k=?",
                        (blob, 5)
                    ).fetchall()
                    if rows:
                        import struct as _ss
                        cand_n = len(blob) // 4
                        cand_vec = list(_ss.unpack(f"{cand_n}f", blob[:cand_n * 4]))
                        max_sim = 0.0
                        for row in rows:
                            e = db_vec.execute(
                                "SELECT vector FROM embeddings WHERE source_table='memories' AND source_id=?",
                                (row[0] if isinstance(row, tuple) else row["rowid"],)
                            ).fetchone()
                            if e:
                                v_bytes = bytes(e[0] if isinstance(e, tuple) else e["vector"])
                                n2 = len(v_bytes) // 4
                                v2 = list(_ss.unpack(f"{n2}f", v_bytes[:n2 * 4]))
                                dot = sum(a * b for a, b in zip(cand_vec, v2))
                                import math as _sm
                                na = _sm.sqrt(sum(x * x for x in cand_vec))
                                nb = _sm.sqrt(sum(x * x for x in v2))
                                if na > 0 and nb > 0:
                                    sim = max(-1.0, min(1.0, dot / (na * nb)))
                                    max_sim = max(max_sim, sim)
                        return round(max(0.0, min(1.0, 1.0 - max_sim)), 4), "cosine"
                    else:
                        return 1.0, "cosine_no_neighbors"
                finally:
                    db_vec.close()
        except Exception:
            pass  # fall through to FTS5

    # Method 2: FTS5 word overlap
    try:
        words = set(content.lower().split())
        if not words:
            return 1.0, "empty"
        # Use FTS5 to find similar content
        # Build a simple query from content words (limit to first 20 words to keep it fast)
        query_words = list(words)[:20]
        fts_query = " OR ".join(w for w in query_words if w.isalnum() and len(w) > 2)
        if not fts_query:
            return 1.0, "fts5_no_query"
        rows = db.execute(
            "SELECT m.content FROM memories m JOIN memories_fts f ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL ORDER BY rank LIMIT 5",
            (fts_query,)
        ).fetchall()
        if not rows:
            return 1.0, "fts5_no_matches"
        max_overlap = 0.0
        for row in rows:
            existing_words = set((row["content"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]).lower().split())
            if not existing_words:
                continue
            intersection = words & existing_words
            union = words | existing_words
            overlap = len(intersection) / len(union) if union else 0.0
            max_overlap = max(max_overlap, overlap)
        # Map overlap to surprise: >90% overlap → 0.1-0.3, no overlap → 0.9-1.0
        if max_overlap > 0.9:
            surprise = 0.1 + (1.0 - max_overlap) * 2.0  # 0.1-0.3
        elif max_overlap < 0.1:
            surprise = 0.9 + (0.1 - max_overlap)  # 0.9-1.0
        else:
            surprise = 1.0 - max_overlap
        return round(max(0.0, min(1.0, surprise)), 4), "fts5"
    except Exception:
        return 0.7, "fts5_error"  # default moderate surprise on error


def _embed_query_safe(text: str):
    """Embed query text via Ollama. Returns packed float32 bytes, or None on failure."""
    try:
        import urllib.request, urllib.error, struct
        payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(
            OLLAMA_EMBED_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
            return struct.pack(f"{len(vec)}f", *vec)
    except Exception:
        return None


_source_weight_cache = {}  # (agent_id, domain) -> strength, cleared on build


def _get_source_weight(db, agent_id, domain):
    """Return source weight (0.0-1.0) for an agent+domain from agent_expertise.

    Returns 1.0 (neutral) if no expertise data exists, so unknown agents
    don't get penalized. Cache results within a search call to avoid N+1.
    """
    if not agent_id or not domain:
        return 1.0
    key = (agent_id, domain)
    if key in _source_weight_cache:
        return _source_weight_cache[key]
    try:
        row = db.execute(
            "SELECT strength FROM agent_expertise WHERE agent_id=? AND domain=?",
            (agent_id, domain)
        ).fetchone()
        w = float(row["strength"]) if row else 1.0
    except Exception:
        w = 1.0
    _source_weight_cache[key] = w
    return w


def _rrf_fuse(fts_list, vec_list, k=60):
    """Reciprocal Rank Fusion of two ranked lists (rank 0 = best).

    Returns merged list sorted by RRF score descending, with each item
    annotated with ``rrf_score`` and ``source`` ("keyword"/"semantic"/"both").
    """
    scores = {}
    sources = {}
    rows = {}
    for rank, row in enumerate(fts_list):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
        sources[rid] = "keyword"
        rows[rid] = row
    for rank, row in enumerate(vec_list):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
        sources[rid] = "both" if rid in sources else "semantic"
        if rid not in rows:
            rows[rid] = row
    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    out = []
    for rid in sorted_ids:
        r = rows[rid].copy()
        r["rrf_score"] = round(scores[rid], 6)
        r["source"] = sources[rid]
        out.append(r)
    return out


def _graph_expand(db, top_results, table_hint, already_ids, limit=5):
    """1-hop knowledge_edges expansion from top-``limit`` results.

    Returns list of neighbor rows annotated with source="graph" and the
    relation that connected them.  Skips IDs already in ``already_ids``.
    """
    graph_rows = []
    seen = set(already_ids)
    # Map table_hint to content column for label fetching
    content_col = {"memories": "content", "events": "summary", "context": "content"}.get(table_hint, "content")

    for item in top_results[:limit]:
        node_id = item.get("id")
        if node_id is None:
            continue
        # Fetch both outbound and inbound edges for this node
        edges = db.execute(
            "SELECT target_table as nb_table, target_id as nb_id, relation_type, weight "
            "FROM knowledge_edges WHERE source_table=? AND source_id=? "
            "AND target_table IN ('memories','events','context') "
            "UNION ALL "
            "SELECT source_table as nb_table, source_id as nb_id, relation_type, weight "
            "FROM knowledge_edges WHERE target_table=? AND target_id=? "
            "AND source_table IN ('memories','events','context') "
            "ORDER BY weight DESC LIMIT 10",
            (table_hint, node_id, table_hint, node_id),
        ).fetchall()

        for edge in edges:
            nb_table = edge["nb_table"]
            nb_id = edge["nb_id"]
            key = (nb_table, nb_id)
            if key in seen:
                continue
            seen.add(key)
            # Fetch neighbor content
            col = {"memories": "content", "events": "summary", "context": "content"}.get(nb_table, "content")
            nb_row = None
            if nb_table == "memories":
                nb_row = db.execute(
                    "SELECT id, 'memory' as type, category, content, confidence, scope, created_at "
                    "FROM memories WHERE id=? AND retired_at IS NULL", (nb_id,)
                ).fetchone()
            elif nb_table == "events":
                nb_row = db.execute(
                    "SELECT id, 'event' as type, event_type, summary, importance, project, created_at "
                    "FROM events WHERE id=?", (nb_id,)
                ).fetchone()
            elif nb_table == "context":
                nb_row = db.execute(
                    "SELECT id, 'context' as type, source_type, source_ref, summary, project, created_at "
                    "FROM context WHERE id=? AND stale_at IS NULL", (nb_id,)
                ).fetchone()
            if nb_row:
                r = dict(nb_row)
                r["source"] = "graph"
                r["relation"] = edge["relation_type"]
                r["relation_weight"] = round(edge["weight"], 4)
                r["rrf_score"] = 0.0
                graph_rows.append(r)

    return graph_rows


def _mmr_rerank(results_list, lambda_mmr=0.7):
    """Maximal Marginal Relevance reranking.

    MMR_score = λ * sim(q, m) - (1-λ) * max_sim(m, already_selected)
    sim(q, m)    = final_score (pre-computed)
    max_sim pair = Jaccard overlap on content word sets (no embeddings needed)

    Returns results in MMR order (same list, reordered in-place by greedy selection).
    """
    if len(results_list) <= 1:
        return results_list

    def _word_jaccard(a, b):
        wa = set((a.get("content") or "").lower().split())
        wb = set((b.get("content") or "").lower().split())
        union = wa | wb
        return len(wa & wb) / len(union) if union else 0.0

    remaining = sorted(results_list, key=lambda r: r.get("final_score", 0.0), reverse=True)
    selected = [remaining.pop(0)]
    selected[0]["mmr_score"] = round(selected[0].get("final_score", 0.0), 6)

    while remaining:
        best_score = -1.0
        best_idx = 0
        for i, candidate in enumerate(remaining):
            sim_q = candidate.get("final_score", 0.0)
            max_sim = max(_word_jaccard(candidate, s) for s in selected)
            mmr = lambda_mmr * sim_q - (1.0 - lambda_mmr) * max_sim
            if mmr > best_score:
                best_score = mmr
                best_idx = i
        best = remaining.pop(best_idx)
        best["mmr_score"] = round(best_score, 6)
        selected.append(best)

    return selected


def cmd_search(args):
    db = get_db()
    query = args.query
    limit = args.limit or 10
    no_recency = getattr(args, "no_recency", False)
    no_graph = getattr(args, "no_graph", False)
    budget_tokens = getattr(args, "budget", None)        # --budget: hard token cap on output
    min_salience = getattr(args, "min_salience", None)   # --min-salience: suppress low-salience memories
    use_mmr = getattr(args, "mmr", False)                # --mmr: MMR diversity reranking
    mmr_lambda = getattr(args, "mmr_lambda", 0.7)        # --mmr-lambda: relevance/diversity trade-off
    use_explore = getattr(args, "explore", False)        # --explore: curiosity mode
    results = {"memories": [], "events": [], "context": [], "decisions": []}

    # Read neuromod state to modulate retrieval parameters
    _nm = _neuro_get_state(db)
    _nm_lambda = _nm.get("temporal_lambda", 0.030) if _nm else 0.030
    _nm_breadth = _nm.get("retrieval_breadth_multiplier", 1.0) if _nm else 1.0
    _nm_exploit = _nm.get("exploitation_bias", 0.0) if _nm else 0.0

    # Intent-aware table routing: when --tables not specified, classify query intent
    # and route to the most relevant tables for that intent type.
    _intent_result = None
    if args.tables:
        tables = args.tables.split(",")
    elif _INTENT_AVAILABLE:
        try:
            _intent_result = _classify_intent(query)
            tables = _intent_result.tables
        except Exception:
            _intent_result = _builtin_classify_intent(query)
            tables = _intent_result.tables
    else:
        _intent_result = _builtin_classify_intent(query)
        tables = _intent_result.tables
    # For decision_lookup intent, ensure decisions table is searched
    if _intent_result and _intent_result.intent == "decision_lookup" and "decisions" not in results:
        tables = list(set(tables) | {"memories", "events", "context"})
    base_fetch = limit * 5 if not no_recency else limit * 3
    fetch_limit = max(limit, round(base_fetch * _nm_breadth))
    fts_query = _sanitize_fts_query(query)

    # Try to load vec extension for hybrid mode (non-fatal)
    db_vec = _try_get_db_with_vec()
    q_blob = _embed_query_safe(query) if db_vec else None
    hybrid = db_vec is not None and q_blob is not None
    mode = "hybrid-rrf" if hybrid else "fts"

    # Compute adaptive salience weights for memory reranking
    _adaptive_weights = None
    if _SAL_AVAILABLE:
        try:
            _adaptive_weights = _sal.compute_adaptive_weights(db, query=query, neuro=_nm or {})
        except Exception:
            _adaptive_weights = None
    _max_recalls_cache = [None]  # lazy-compute once per cmd_search

    def _fts_memories():
        if not fts_query:
            return []
        rows = db.execute(
            "SELECT m.id, 'memory' as type, m.category, m.content, m.confidence, m.scope, "
            "m.created_at, m.recalled_count, m.temporal_class, m.last_recalled_at, f.rank as fts_rank "
            "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_query, fetch_limit)
        ).fetchall()
        return rows_to_list(rows)

    def _vec_memories():
        if not hybrid:
            return []
        try:
            vec_rows = db_vec.execute(
                "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k=?",
                (q_blob, fetch_limit)
            ).fetchall()
        except Exception:
            return []
        if not vec_rows:
            return []
        rowids = [r["rowid"] for r in vec_rows]
        dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
        ph = ",".join("?" * len(rowids))
        src_rows = db_vec.execute(
            f"SELECT id, 'memory' as type, category, content, confidence, scope, "
            f"created_at, recalled_count, temporal_class, last_recalled_at "
            f"FROM memories WHERE id IN ({ph}) AND retired_at IS NULL",
            rowids
        ).fetchall()
        out = [dict(r) | {"distance": round(dist_map.get(r["id"], 1.0), 4)} for r in src_rows]
        out.sort(key=lambda r: r["distance"])
        return out

    def _fts_events():
        if not fts_query:
            return []
        rows = db.execute(
            "SELECT e.id, 'event' as type, e.event_type, e.summary, e.importance, e.project, e.created_at, f.rank as fts_rank "
            "FROM events e JOIN events_fts f ON e.id = f.rowid "
            "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, fetch_limit)
        ).fetchall()
        return rows_to_list(rows)

    def _vec_events():
        if not hybrid:
            return []
        try:
            vec_rows = db_vec.execute(
                "SELECT rowid, distance FROM vec_events WHERE embedding MATCH ? AND k=?",
                (q_blob, fetch_limit)
            ).fetchall()
        except Exception:
            return []
        if not vec_rows:
            return []
        rowids = [r["rowid"] for r in vec_rows]
        dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
        ph = ",".join("?" * len(rowids))
        src_rows = db_vec.execute(
            f"SELECT id, 'event' as type, event_type, summary, importance, project, created_at "
            f"FROM events WHERE id IN ({ph})",
            rowids
        ).fetchall()
        out = [dict(r) | {"distance": round(dist_map.get(r["id"], 1.0), 4)} for r in src_rows]
        out.sort(key=lambda r: r["distance"])
        return out

    def _fts_context():
        if not fts_query:
            return []
        rows = db.execute(
            "SELECT c.id, 'context' as type, c.source_type, c.source_ref, c.summary, c.project, c.created_at, f.rank as fts_rank "
            "FROM context c JOIN context_fts f ON c.id = f.rowid "
            "WHERE context_fts MATCH ? AND c.stale_at IS NULL ORDER BY rank LIMIT ?",
            (fts_query, fetch_limit)
        ).fetchall()
        return rows_to_list(rows)

    def _vec_context():
        if not hybrid:
            return []
        try:
            vec_rows = db_vec.execute(
                "SELECT rowid, distance FROM vec_context WHERE embedding MATCH ? AND k=?",
                (q_blob, fetch_limit)
            ).fetchall()
        except Exception:
            return []
        if not vec_rows:
            return []
        rowids = [r["rowid"] for r in vec_rows]
        dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
        ph = ",".join("?" * len(rowids))
        src_rows = db_vec.execute(
            f"SELECT id, 'context' as type, source_type, source_ref, content, summary, project, created_at "
            f"FROM context WHERE id IN ({ph}) AND stale_at IS NULL",
            rowids
        ).fetchall()
        out = [dict(r) | {"distance": round(dist_map.get(r["id"], 1.0), 4)} for r in src_rows]
        out.sort(key=lambda r: r["distance"])
        return out

    def _apply_recency_and_trim(merged, scope_fn, use_adaptive_salience=False):
        if no_recency:
            return merged[:limit]

        # Lazy-compute max_recalls once for adaptive salience importance normalization
        if use_adaptive_salience and _adaptive_weights and _SAL_AVAILABLE:
            if _max_recalls_cache[0] is None:
                try:
                    row = db.execute(
                        "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
                    ).fetchone()
                    _max_recalls_cache[0] = (row[0] or 1) if row else 1
                except Exception:
                    _max_recalls_cache[0] = 1
            max_recalls = _max_recalls_cache[0]
        else:
            max_recalls = 1

        for r in merged:
            scope = scope_fn(r)
            r["age"] = _age_str(r.get("created_at"))

            if use_adaptive_salience and _adaptive_weights and _SAL_AVAILABLE and r.get("recalled_count") is not None:
                # Full adaptive salience formula: rrf_score → similarity input
                sim = r.get("rrf_score", 0.0)
                salience = _sal.compute_salience(
                    similarity=sim,
                    last_recalled_at=r.get("last_recalled_at"),
                    created_at=r.get("created_at"),
                    confidence=float(r.get("confidence") or 0.5),
                    recalled_count=int(r.get("recalled_count") or 0),
                    max_recalls=max_recalls,
                    weights=_adaptive_weights,
                    temporal_class=r.get("temporal_class"),
                )
                r["temporal_weight"] = 1.0  # subsumed in salience
                r["final_score"] = round(salience, 8)
            else:
                # Original temporal decay path (events, context, fallback)
                # permanent and long temporal_class are immune to temporal decay
                if r.get("temporal_class") in ("permanent", "long"):
                    tw = 1.0
                else:
                    tw = math.exp(-_nm_lambda * _days_since(r.get("created_at")))
                # Apply exploitation bias (acetylcholine: favor previously-recalled memories)
                if _nm_exploit > 0 and r.get("recalled_count", 0) > 0:
                    tw = tw * (1.0 + _nm_exploit * math.log1p(r["recalled_count"]) * 0.3)
                r["temporal_weight"] = round(min(1.0, tw), 4)
                r["final_score"] = round(r.get("rrf_score", 0.0) * tw, 8)

            # Source weighting: boost/attenuate memories from agents with domain expertise
            # Factor: 0.90 + 0.10 * strength (neutral=1.0 for unknown agents, max 1.0 for experts)
            if r.get("agent_id"):
                mem_domain = _expertise_scope_to_domain(r.get("scope") or "global") or r.get("category")
                sw = _get_source_weight(db, r["agent_id"], mem_domain) if mem_domain else 1.0
                r["source_weight"] = round(sw, 4)
                r["final_score"] = round(r["final_score"] * (0.90 + 0.10 * sw), 8)

        # PageRank reranking boost: score *= (1 + alpha * norm_pagerank)
        pr_alpha = getattr(args, "pagerank_boost", 0.0)
        if pr_alpha and pr_alpha > 0:
            try:
                import json as _pjson
                _pr_row = db.execute(
                    "SELECT value FROM agent_state WHERE agent_id='graph-weaver' AND key='graph_pagerank'"
                ).fetchone()
                if _pr_row:
                    _pr_raw = _pjson.loads(_pr_row["value"])
                    _pr_scores = {(p[0], int(p[1])): v
                                  for x, v in _pr_raw.items()
                                  for p in [x.split("|", 1)]}
                    _pr_max = max(_pr_scores.values()) if _pr_scores else 1.0
                    for r in merged:
                        _pr_key = ("memories" if r.get("type") == "memory" else r.get("type", ""), r.get("id"))
                        _pr_val = _pr_scores.get(_pr_key, 0.0)
                        _pr_norm = _pr_val / _pr_max if _pr_max > 0 else 0.0
                        r["pagerank_score"] = round(_pr_val, 6)
                        r["final_score"] = round(r["final_score"] * (1.0 + pr_alpha * _pr_norm), 8)
            except Exception:
                pass  # PageRank boost is optional; never break search

        merged.sort(key=lambda r: r["final_score"], reverse=True)
        return merged[:limit]

    if "memories" in tables:
        if use_explore:
            # Curiosity mode: sample bottom quartile of recalled_count, weighted by confidence
            import random as _random
            explore_rows = db.execute(
                "SELECT id, 'memory' as type, category, content, confidence, scope, "
                "created_at, recalled_count, temporal_class, last_recalled_at "
                "FROM memories WHERE retired_at IS NULL ORDER BY recalled_count ASC, RANDOM() LIMIT ?",
                (limit * 10,)
            ).fetchall()
            explore_list = rows_to_list(explore_rows)
            if explore_list:
                recalls = sorted([r.get("recalled_count") or 0 for r in explore_list])
                q25_thresh = recalls[len(recalls) // 4] if recalls else 0
                candidates = [r for r in explore_list if (r.get("recalled_count") or 0) <= q25_thresh]
                # Weighted sample by confidence
                weights = [max(float(r.get("confidence") or 0.5), 0.01) for r in candidates]
                total_w = sum(weights)
                sampled = []
                if total_w > 0 and len(candidates) > limit:
                    remaining_c = list(zip(weights, candidates))
                    for _ in range(min(limit, len(candidates))):
                        r_val = _random.uniform(0, sum(w for w, _ in remaining_c))
                        cum = 0.0
                        for i, (w, c) in enumerate(remaining_c):
                            cum += w
                            if cum >= r_val:
                                sampled.append(c)
                                remaining_c.pop(i)
                                break
                else:
                    sampled = candidates[:limit]
                for r in sampled:
                    r["rrf_score"] = 0.0
                    r["source"] = "explore"
                    r["final_score"] = round(float(r.get("confidence") or 0.5), 6)
                    r["age"] = _age_str(r.get("created_at"))
                trimmed = sampled
            else:
                trimmed = []
        else:
            fts_list = _fts_memories()
            vec_list = _vec_memories()
            if hybrid:
                merged = _rrf_fuse(fts_list, vec_list)
            else:
                merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_list]
            trimmed = _apply_recency_and_trim(merged, lambda r: r.get("scope"), use_adaptive_salience=True)
            # MMR diversity reranking — applied after salience scoring, before graph expand
            if use_mmr and trimmed:
                trimmed = _mmr_rerank(trimmed, lambda_mmr=mmr_lambda)
        if not no_graph:
            already = {r["id"] for r in trimmed}
            graph = _graph_expand(db, trimmed, "memories", already)
            trimmed.extend(graph)

        # Quantum amplitude re-ranking
        use_quantum = getattr(args, "quantum", False) or getattr(args, "benchmark", False)
        if use_quantum and _QUANTUM_AVAILABLE and trimmed:
            try:
                _bench = getattr(args, "benchmark", False)
                trimmed = _quantum_rerank(trimmed, db_path=str(DB_PATH), benchmark=_bench)
            except Exception:
                pass  # quantum re-ranking is optional; never break search

        results["memories"] = trimmed

    if "events" in tables:
        fts_list = _fts_events()
        vec_list = _vec_events()
        if hybrid:
            merged = _rrf_fuse(fts_list, vec_list)
        else:
            merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_list]
        trimmed = _apply_recency_and_trim(
            merged,
            lambda r: ("project:" + r["project"]) if r.get("project") else "global"
        )
        if not no_graph:
            already = {r["id"] for r in trimmed}
            graph = _graph_expand(db, trimmed, "events", already)
            trimmed.extend(graph)
        results["events"] = trimmed

    if "context" in tables:
        fts_list = _fts_context()
        vec_list = _vec_context()
        if hybrid:
            merged = _rrf_fuse(fts_list, vec_list)
        else:
            merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_list]
        trimmed = _apply_recency_and_trim(
            merged,
            lambda r: ("project:" + r["project"]) if r.get("project") else "global"
        )
        if not no_graph:
            already = {r["id"] for r in trimmed}
            graph = _graph_expand(db, trimmed, "context", already)
            trimmed.extend(graph)
        results["context"] = trimmed

    # Intent-based result weighting and decision search
    if _intent_result and _intent_result.intent != "general":
        _intent = _intent_result.intent
        # entity_lookup → boost entities/entity results 2x via final_score
        if _intent == "entity_lookup":
            for r in results.get("events", []):
                if r.get("type") == "entity":
                    r["final_score"] = round(r.get("final_score", 0.0) * 2.0, 8)
            # Also search entities directly if not in tables
            if fts_query:
                try:
                    ent_rows = db.execute(
                        "SELECT e.id, 'entity' as type, e.name, e.entity_type, e.confidence, e.created_at "
                        "FROM entities_fts fts JOIN entities e ON e.id = fts.rowid "
                        "WHERE entities_fts MATCH ? AND e.retired_at IS NULL ORDER BY rank LIMIT ?",
                        (fts_query, limit)
                    ).fetchall()
                    for r in rows_to_list(ent_rows):
                        r["final_score"] = round(float(r.get("confidence", 0.5)) * 2.0, 8)
                        r["source"] = "intent_entity"
                    results.setdefault("entities", []).extend(rows_to_list(ent_rows))
                except Exception:
                    pass
        # event_lookup → boost events results 2x
        elif _intent == "event_lookup":
            for r in results.get("events", []):
                r["final_score"] = round(r.get("final_score", 0.0) * 2.0, 8)
            results["events"] = sorted(results.get("events", []),
                                        key=lambda r: r.get("final_score", 0), reverse=True)
        # decision_lookup → also search decisions table
        elif _intent == "decision_lookup":
            if fts_query:
                try:
                    dec_rows = db.execute(
                        "SELECT d.id, 'decision' as type, d.title, d.rationale, d.project, d.created_at "
                        "FROM decisions d "
                        "WHERE d.title LIKE ? OR d.rationale LIKE ? "
                        "ORDER BY d.created_at DESC LIMIT ?",
                        (f"%{query}%", f"%{query}%", limit)
                    ).fetchall()
                    dec_list = rows_to_list(dec_rows)
                    for r in dec_list:
                        r["final_score"] = round(1.0, 8)
                        r["source"] = "intent_decision"
                    results["decisions"] = dec_list
                except Exception:
                    pass
        # graph_traversal → include knowledge_edges neighbors for top results
        elif _intent == "graph_traversal" and not no_graph:
            for tbl_key in ("memories", "events", "context"):
                top_items = results.get(tbl_key, [])[:3]
                if top_items:
                    already = {r["id"] for r in results.get(tbl_key, [])}
                    extra = _graph_expand(db, top_items, tbl_key.rstrip("s") if tbl_key != "memories" else "memories", already)
                    results.get(tbl_key, []).extend(extra)

    if db_vec:
        db_vec.close()

    # --min-salience: suppress memories below the salience floor
    if min_salience is not None:
        filtered = []
        for m in results.get("memories", []):
            if m.get("final_score", 1.0) >= min_salience:
                filtered.append(m)
        results["memories"] = filtered

    # --budget: trim results from lowest-ranked first until output fits within token cap
    if budget_tokens is not None:
        # Estimate current size; trim tail entries until we fit
        for key in ("memories", "events", "context", "decisions"):
            lst = results.get(key, [])
            if not lst:
                continue
            while lst and _estimate_tokens(results) > budget_tokens:
                lst.pop()  # remove lowest-ranked (already sorted desc)
            results[key] = lst

    total = sum(len(v) for v in results.values())
    tokens_out = _estimate_tokens(results)
    log_access(db, args.agent or "unknown", "search", query=query, result_count=total, tokens_consumed=tokens_out)

    # Update recalled_count for direct (non-graph) memory hits only ()
    # Bayesian recall update: increment alpha, recompute confidence = alpha/(alpha+beta)
    _has_ab_cols = any(
        col[1] == "alpha" for col in db.execute("PRAGMA table_info(memories)").fetchall()
    )
    for r in results.get("memories", []):
        if r.get("source") != "graph":
            if _has_ab_cols:
                db.execute(
                    "UPDATE memories SET recalled_count = recalled_count + 1, "
                    "last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
                    "alpha = COALESCE(alpha, 1.0) + 1.0, "
                    "confidence = (COALESCE(alpha, 1.0) + 1.0) / "
                    "             ((COALESCE(alpha, 1.0) + 1.0) + COALESCE(beta, 1.0)) "
                    "WHERE id = ?",
                    (r["id"],)
                )
            else:
                db.execute(
                    "UPDATE memories SET recalled_count = recalled_count + 1, last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), confidence = MIN(1.0, confidence + 0.15 * (1.0 - confidence)) WHERE id = ?",
                    (r["id"],)
                )

    # Online phase learning: nudge confidence_phase toward constructive (0) after recall
    # Uses existing db connection to avoid lock contention with uncommitted recall_count updates.
    try:
        _has_phase_col = any(
            col[1] == "confidence_phase"
            for col in db.execute("PRAGMA table_info(memories)").fetchall()
        )
        if _has_phase_col:
            _delta = 0.05
            for r in results.get("memories", []):
                if r.get("source") != "graph":
                    _pm_id = r["id"]
                    _pm_row = db.execute(
                        "SELECT confidence_phase FROM memories WHERE id=? AND retired_at IS NULL",
                        (_pm_id,)
                    ).fetchone()
                    if _pm_row and _pm_row[0] is not None:
                        import math as _pmath
                        _ph = float(_pm_row[0])
                        _ph = (_ph + _delta if _ph > _pmath.pi else max(0.0, _ph - _delta)) % (2 * _pmath.pi)
                        db.execute("UPDATE memories SET confidence_phase=? WHERE id=?", (_ph, _pm_id))
    except Exception:
        pass  # phase learning is optional; never break search

    # Post-retrieval metacognitive tier annotation
    # Tier 1: high-confidence fresh results  (≥3 direct results, avg_conf ≥ 0.7)
    # Tier 2: moderate results               (≥3 direct results, avg_conf 0.4-0.7)
    # Tier 3: weak matches                   (<3 direct results)
    # Tier 4: coverage gap                   (0 direct results)
    # Exclude graph-expanded neighbours (source="graph") — they don't reflect query coverage
    memory_results = [r for r in results.get("memories", []) if r.get("source") != "graph"]
    # Keyword/both hits: FTS5 textual matches — strongest evidence of genuine coverage
    keyword_hits = [r for r in memory_results if r.get("source") in ("keyword", "both")]
    k_count = len(keyword_hits)

    if not memory_results:
        tier = 4
        tier_label = "gap-detected"
        tier_note = "COVERAGE GAP — no memories match this query"
        try:
            _log_gap(db, "coverage_hole", f"query:{_sanitize_fts_query(query)[:80]}", 1.0, triggered_by=query[:200])
        except Exception:
            pass
        # Log to incubation queue for dream-pass retry
        try:
            _agent_id = getattr(args, "agent", None) or "unknown"
            db.execute(
                "INSERT INTO deferred_queries (agent_id, query_text, expires_at) "
                "VALUES (?, ?, datetime('now', '+30 days'))",
                (_agent_id, query[:500]),
            )
        except Exception:
            pass
    elif k_count >= _COVERAGE_GAP_RESULT_COUNT:
        avg_conf = sum(r.get("confidence") or 1.0 for r in keyword_hits) / k_count
        if avg_conf >= 0.7:
            tier = 1
            tier_label = "high-confidence"
            tier_note = f"{k_count} keyword matches, avg_confidence={round(avg_conf, 2)}"
        else:
            tier = 2
            tier_label = "moderate"
            tier_note = f"{k_count} keyword matches, avg_confidence={round(avg_conf, 2)}"
    elif k_count > 0:
        tier = 2
        tier_label = "moderate"
        tier_note = f"Only {k_count} keyword match(es); {len(memory_results)} total (includes semantic)"
    else:
        tier = 3
        tier_label = "weak-coverage"
        tier_note = f"No keyword matches; {len(memory_results)} semantic-only result(s) — potential gap"

    # Passive search instrumentation — append row to agent_uncertainty_log
    try:
        _unc_agent = getattr(args, "agent", None) or "unknown"
        _unc_domain = getattr(args, "scope", None) or (tables[0] if tables else "memories")
        _unc_avg_conf = None
        if memory_results:
            _conf_vals = [r.get("confidence") for r in memory_results if r.get("confidence") is not None]
            if _conf_vals:
                _unc_avg_conf = round(sum(_conf_vals) / len(_conf_vals), 4)
        db.execute(
            "INSERT INTO agent_uncertainty_log "
            "(agent_id, domain, query, result_count, avg_confidence, retrieved_at, temporal_class, ttl_days) "
            "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), 'ephemeral', 30)",
            (_unc_agent, _unc_domain, query[:500], total, _unc_avg_conf),
        )
    except Exception:
        pass  # instrumentation must never break search

    db.commit()
    _intent_meta = {}
    if _intent_result is not None:
        _intent_meta = {
            "intent": _intent_result.intent,
            "intent_confidence": _intent_result.confidence,
            "intent_rule": _intent_result.matched_rule,
            "format_hint": _intent_result.format_hint,
        }
    # Prospective memory trigger check — surface any matching triggers
    _triggered = []
    try:
        _triggered = _check_triggers(db, query)
        if _triggered:
            db.commit()  # persist any expired status changes
    except Exception:
        pass  # trigger check is optional; never break search

    _out = {"mode": mode, "metacognition": {"tier": tier, "label": tier_label, "note": tier_note, **_intent_meta}, **results}
    if _triggered:
        _out["triggered_memories"] = _triggered
    _ofmt = getattr(args, "output", "json")
    if _ofmt == "oneline":
        oneline_out(_out)
    elif _ofmt == "compact":
        json_out(_out, compact=True)
    else:
        json_out(_out)

# ---------------------------------------------------------------------------
# VECTOR SEARCH
# ---------------------------------------------------------------------------

def _find_vec_dylib():
    """Auto-discover sqlite-vec extension."""
    try:
        import sqlite_vec
        return sqlite_vec.loadable_path()
    except (ImportError, AttributeError):
        pass
    import glob
    candidates = glob.glob('/opt/homebrew/lib/python*/site-packages/sqlite_vec/vec0.*') + \
                 glob.glob('/usr/lib/python*/site-packages/sqlite_vec/vec0.*') + \
                 glob.glob(str(Path.home() / '.local/lib/python*/site-packages/sqlite_vec/vec0.*'))
    for c in sorted(candidates, reverse=True):
        if Path(c).exists():
            return c
    return None

VEC_DYLIB = _find_vec_dylib()
OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIMENSIONS = 768


def _get_db_with_vec() -> sqlite3.Connection:
    """Open DB with sqlite-vec extension loaded. Returns None if unavailable."""
    import urllib.request
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        conn.enable_load_extension(True)
        conn.load_extension(VEC_DYLIB)
        conn.enable_load_extension(False)
    except Exception as e:
        print(f"ERROR: sqlite-vec not available: {e}", file=sys.stderr)
        sys.exit(1)
    return conn


def _try_vec_delete_memories(*memory_ids: int) -> int:
    """Delete vec_memories rows for the given memory IDs. Returns count deleted.
    Returns 0 silently if sqlite-vec is unavailable — retire/replace still succeed."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.enable_load_extension(True)
        conn.load_extension(VEC_DYLIB)
        conn.enable_load_extension(False)
        deleted = 0
        for mid in memory_ids:
            cursor = conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (mid,))
            deleted += cursor.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return 0


def cmd_vec_purge_retired(args):
    """Bulk-delete vec_memories entries whose memory has been retired. One-time cleanup."""
    db_vec = _get_db_with_vec()
    retired_ids = db_vec.execute(
        "SELECT id FROM memories WHERE retired_at IS NOT NULL"
    ).fetchall()
    retired_ids = [r["id"] for r in retired_ids]
    if not retired_ids:
        json_out({"ok": True, "purged": 0, "message": "No retired memories found."})
        db_vec.close()
        return
    deleted = 0
    for mid in retired_ids:
        cursor = db_vec.execute("DELETE FROM vec_memories WHERE rowid = ?", (mid,))
        deleted += cursor.rowcount
    db_vec.commit()
    db_vec.close()
    json_out({"ok": True, "purged": deleted, "checked": len(retired_ids)})


def _embed_query(text: str) -> bytes:
    """Embed query text via Ollama, return packed float32 bytes."""
    import urllib.request, urllib.error, struct
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
            return struct.pack(f"{len(vec)}f", *vec)
    except urllib.error.URLError as e:
        print(f"ERROR: Ollama not reachable for embedding: {e}", file=sys.stderr)
        sys.exit(1)


def _normalize(scores: list[float]) -> list[float]:
    """Min-max normalize a list of scores to [0, 1]. Higher = better."""
    if not scores:
        return scores
    mn, mx = min(scores), max(scores)
    if mx == mn:
        return [1.0] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]


def cmd_vsearch(args):
    import struct
    db = _get_db_with_vec()
    query = args.query
    limit = args.limit or 10
    alpha = max(0.0, min(1.0, args.alpha))  # clamp to [0,1]
    vec_only = getattr(args, "vec_only", False)
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else ["memories", "events", "context"]

    q_blob = _embed_query(query)

    results = {}
    # When graph boost is enabled, keep more candidates so the graph reranker
    # can surface nodes that would otherwise fall below the cutoff.
    graph_boost = getattr(args, "graph_boost", False)
    candidate_limit = limit * 3 if graph_boost else limit

    def _vsearch_table(vec_table, src_table, text_col, extra_cols, fts_table):
        # Step 1: vector KNN search (fetch 3x limit for re-ranking)
        fetch_n = limit * 3
        vec_rows = db.execute(
            f"SELECT rowid, distance FROM {vec_table} WHERE embedding MATCH ? AND k=?",
            (q_blob, fetch_n),
        ).fetchall()
        if not vec_rows:
            return []

        rowids = [r["rowid"] for r in vec_rows]
        dist_map = {r["rowid"]: r["distance"] for r in vec_rows}

        placeholder = ",".join("?" * len(rowids))
        # Filter retired memories at the SQL level (retired_at is only on the memories table)
        retired_filter = " AND retired_at IS NULL" if src_table == "memories" else ""

        if vec_only or not fts_table:
            # Pure vector: sort by distance ascending
            src_rows = db.execute(
                f"SELECT id, {text_col}{', ' + extra_cols if extra_cols else ''} FROM {src_table} "
                f"WHERE id IN ({placeholder}){retired_filter}",
                rowids,
            ).fetchall()
            out = []
            for row in src_rows:
                d = dist_map.get(row["id"], 999.0)
                out.append(dict(row) | {"distance": round(d, 4), "score": round(1.0 - d, 4)})
            out.sort(key=lambda r: r["distance"])
            return out[:candidate_limit]

        # Step 2: hybrid — also fetch FTS5 scores
        _fts_q = _sanitize_fts_query(query)
        if _fts_q:
            fts_rows = db.execute(
                f"SELECT f.rowid, f.rank FROM {fts_table} f "
                f"WHERE {fts_table} MATCH ? AND f.rowid IN ({placeholder})",
                [_fts_q] + rowids,
            ).fetchall()
        else:
            fts_rows = []
        fts_map = {r["rowid"]: r["rank"] for r in fts_rows}

        src_rows = db.execute(
            f"SELECT id, {text_col}{', ' + extra_cols if extra_cols else ''} FROM {src_table} "
            f"WHERE id IN ({placeholder}){retired_filter}",
            rowids,
        ).fetchall()

        # Build candidate list
        candidates = []
        for row in src_rows:
            rid = row["id"]
            d = dist_map.get(rid, 1.0)
            fts_rank = fts_map.get(rid, 0.0)  # 0 = no FTS match
            candidates.append({"row": dict(row), "distance": d, "fts_rank": fts_rank})

        # Normalize both axes (higher = better)
        vec_scores = _normalize([1.0 - c["distance"] for c in candidates])
        # FTS rank is negative BM25; flip sign so higher = more relevant
        fts_scores = _normalize([-c["fts_rank"] for c in candidates])

        out = []
        for i, c in enumerate(candidates):
            hybrid = alpha * fts_scores[i] + (1.0 - alpha) * vec_scores[i]
            out.append(c["row"] | {
                "distance": round(c["distance"], 4),
                "fts_rank": round(c["fts_rank"], 4),
                "score": round(hybrid, 4),
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return out[:candidate_limit]

    if "memories" in tables:
        rows = _vsearch_table(
            "vec_memories", "memories", "content",
            "category, scope, confidence, created_at, recalled_count, temporal_class, last_recalled_at",
            "memories_fts"
        )
        # Apply adaptive salience reranking if available
        if _SAL_AVAILABLE and rows:
            try:
                _nm_vs = _neuro_get_state(db)
                _vs_weights = _sal.compute_adaptive_weights(db, query=query, neuro=_nm_vs or {})
                _vs_max_r = db.execute(
                    "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
                ).fetchone()
                _vs_max_recalls = (_vs_max_r[0] or 1) if _vs_max_r else 1
                for r in rows:
                    if r.get("recalled_count") is not None:
                        r["score"] = round(_sal.compute_salience(
                            similarity=r.get("score", 0.0),
                            last_recalled_at=r.get("last_recalled_at"),
                            created_at=r.get("created_at"),
                            confidence=float(r.get("confidence") or 0.5),
                            recalled_count=int(r.get("recalled_count") or 0),
                            max_recalls=_vs_max_recalls,
                            weights=_vs_weights,
                            temporal_class=r.get("temporal_class"),
                        ), 4)
                rows.sort(key=lambda r: r["score"], reverse=True)
                rows = rows[:limit]
            except Exception:
                pass
        results["memories"] = rows

    if "events" in tables:
        rows = _vsearch_table(
            "vec_events", "events", "summary",
            "event_type, importance, project, created_at", "events_fts"
        )
        results["events"] = rows

    if "context" in tables:
        rows = _vsearch_table(
            "vec_context", "context", "content",
            "source_type, source_ref, summary, project, created_at", "context_fts"
        )
        results["context"] = rows

    mode = "vec-only" if vec_only else f"hybrid(alpha={alpha})"

    # Optional graph boost: run spreading activation from top direct results,
    # then rerank the full candidate pool and trim to limit.
    if graph_boost:
        graph_boost_weight = getattr(args, "graph_boost_weight", 0.3)
        # Use the top min(limit, 5) results per table as seeds (not all candidates,
        # so non-seed candidates can still receive a graph activation boost).
        seeds = []
        for tbl, rows in results.items():
            for r in rows[:min(limit, 5)]:
                seeds.append((tbl, int(r["id"])))
        if seeds:
            activated = spreading_activation(db, seeds, hops=2, decay=0.6, top_k=limit * 3)
            activation_map = {(a["table"], int(a["id"])): a["activation"] for a in activated}

            # Apply activation boost to ALL candidates (including non-seed ones)
            # and mark which source each result came from.
            seed_keys = set(seeds)
            for tbl in results:
                for r in results[tbl]:
                    key = (tbl, int(r["id"]))
                    act = activation_map.get(key, 0.0)
                    r["graph_activation"] = round(act, 4)
                    r["score"] = round(r.get("score", 0.0) + graph_boost_weight * act, 4)
                results[tbl].sort(key=lambda r: r["score"], reverse=True)
                results[tbl] = results[tbl][:limit]

        mode = f"{mode}+graph_boost(w={graph_boost_weight})"

    total = sum(len(v) for v in results.values())
    log_access(db, args.agent or "unknown", "vsearch", query=query, result_count=total)

    # Update recalled_count for every memory surfaced by this vsearch ()
    for r in results.get("memories", []):
        db.execute(
            "UPDATE memories SET recalled_count = recalled_count + 1, last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), confidence = MIN(1.0, confidence + 0.15 * (1.0 - confidence)) WHERE id = ?",
            (r["id"],)
        )

    db.commit()
    json_out({"mode": mode, "query": query, **results})


# ---------------------------------------------------------------------------
# GRAPH — knowledge_edges relationship queries
# ---------------------------------------------------------------------------

def _graph_node_label(db, table, node_id):
    """Return a short human-readable label for a graph node."""
    try:
        if table == "memories":
            r = db.execute("SELECT content FROM memories WHERE id=?", (node_id,)).fetchone()
            return r["content"][:80] if r else f"memory#{node_id}"
        if table == "events":
            r = db.execute("SELECT summary FROM events WHERE id=?", (node_id,)).fetchone()
            return r["summary"][:80] if r else f"event#{node_id}"
        if table == "context":
            r = db.execute("SELECT content FROM context WHERE id=?", (node_id,)).fetchone()
            return r["content"][:80] if r else f"context#{node_id}"
    except Exception:
        pass
    return f"{table}#{node_id}"


def spreading_activation(db, seed_ids, hops=2, decay=0.6, weight_by_type=None, top_k=20):
    """
    Spreads activation from seed nodes through knowledge_edges.
    Returns ranked list of dicts: {table, id, activation}.

    Based on Collins & Loftus (1975) spreading-activation theory.
    """
    weight_by_type = weight_by_type or {
        "semantic_similar": 1.0,
        "causal_chain_member": 0.8,
        "causes": 0.9,
        "topical_tag": 0.5,
        "topical_project": 0.4,
        "topical_scope": 0.4,
    }
    activation = {}
    for table, id_ in seed_ids:
        activation[(table, id_)] = 1.0

    frontier = list(seed_ids)
    for hop in range(hops):
        next_frontier = []
        decay_at_hop = decay ** (hop + 1)
        for source_table, source_id in frontier:
            rows = db.execute(
                "SELECT target_table, target_id, relation_type, weight "
                "FROM knowledge_edges WHERE source_table=? AND source_id=? "
                "UNION ALL "
                "SELECT source_table, source_id, relation_type, weight "
                "FROM knowledge_edges WHERE target_table=? AND target_id=?",
                (source_table, source_id, source_table, source_id),
            ).fetchall()
            for t_table, t_id, rel_type, edge_weight in rows:
                type_weight = weight_by_type.get(rel_type, 0.5)
                contribution = decay_at_hop * edge_weight * type_weight
                key = (t_table, t_id)
                if key not in activation or activation[key] < contribution:
                    activation[key] = contribution
                    next_frontier.append((t_table, int(t_id)))
        frontier = next_frontier

    seed_set = set(seed_ids)
    results = sorted(
        [(k, v) for k, v in activation.items() if k not in seed_set],
        key=lambda x: -x[1],
    )[:top_k]
    return [{"table": t, "id": i, "activation": s} for (t, i), s in results]


def cmd_graph(args):
    db = get_db()
    sub = args.graph_cmd

    if sub == "stats":
        rows = db.execute(
            "SELECT relation_type, source_table, target_table, COUNT(*) as cnt, "
            "ROUND(AVG(weight),3) as avg_weight "
            "FROM knowledge_edges "
            "GROUP BY relation_type, source_table, target_table "
            "ORDER BY cnt DESC"
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM knowledge_edges").fetchone()[0]
        json_out({"total_edges": total, "by_type": [dict(r) for r in rows]})
        return

    if sub == "neighbors":
        table = args.table
        node_id = args.id
        rows = db.execute(
            "SELECT target_table, target_id, relation_type, weight, created_at "
            "FROM knowledge_edges WHERE source_table=? AND source_id=? "
            "UNION ALL "
            "SELECT source_table, source_id, relation_type, weight, created_at "
            "FROM knowledge_edges WHERE target_table=? AND target_id=? "
            "ORDER BY weight DESC",
            (table, node_id, table, node_id),
        ).fetchall()
        limit = getattr(args, "limit", 20) or 20
        results = []
        for r in rows[:limit]:
            results.append({
                "table": r[0], "id": r[1],
                "relation": r[2], "weight": r[3],
                "label": _graph_node_label(db, r[0], r[1]),
                "created_at": r[4],
            })
        json_out({"source": {"table": table, "id": node_id}, "neighbors": results, "count": len(results)})
        return

    if sub == "related":
        table = args.table
        node_id = args.id
        hops = getattr(args, "hops", 1) or 1
        limit = getattr(args, "limit", 20) or 20

        visited = set()
        frontier = {(table, node_id)}
        all_edges = []

        for _ in range(hops):
            next_frontier = set()
            for (src_t, src_id) in frontier:
                if (src_t, src_id) in visited:
                    continue
                visited.add((src_t, src_id))
                rows = db.execute(
                    "SELECT target_table, target_id, relation_type, weight FROM knowledge_edges "
                    "WHERE source_table=? AND source_id=? "
                    "UNION ALL "
                    "SELECT source_table, source_id, relation_type, weight FROM knowledge_edges "
                    "WHERE target_table=? AND target_id=?",
                    (src_t, src_id, src_t, src_id),
                ).fetchall()
                for r in rows:
                    t_t, t_id, rel, w = r[0], r[1], r[2], r[3]
                    if (t_t, t_id) not in visited:
                        all_edges.append({"table": t_t, "id": t_id, "relation": rel, "weight": w})
                        next_frontier.add((t_t, t_id))
            frontier = next_frontier

        # Deduplicate by (table, id), keep highest weight
        seen = {}
        for e in all_edges:
            key = (e["table"], e["id"])
            if key not in seen or e["weight"] > seen[key]["weight"]:
                seen[key] = e
        results = sorted(seen.values(), key=lambda x: x["weight"], reverse=True)[:limit]
        for r in results:
            r["label"] = _graph_node_label(db, r["table"], r["id"])

        json_out({"source": {"table": table, "id": node_id}, "hops": hops, "related": results})
        return

    if sub == "causal":
        event_id = args.event_id
        depth = getattr(args, "depth", 10) or 10
        chain = []
        current_id = event_id
        seen_ids = set()
        while current_id and len(chain) < depth:
            if current_id in seen_ids:
                break
            seen_ids.add(current_id)
            row = db.execute(
                "SELECT id, summary, event_type, project, created_at, caused_by_event_id "
                "FROM events WHERE id=?", (current_id,)
            ).fetchone()
            if not row:
                break
            chain.append({
                "id": row["id"], "summary": row["summary"],
                "event_type": row["event_type"], "project": row["project"],
                "created_at": row["created_at"],
                "caused_by": row["caused_by_event_id"],
            })
            current_id = row["caused_by_event_id"]
        json_out({"event_id": event_id, "chain_length": len(chain), "causal_chain": chain})
        return

    if sub == "add-edge":
        source_table = args.source_table
        source_id = args.source_id
        target_table = args.target_table
        target_id = args.target_id
        relation = args.relation
        weight = getattr(args, "weight", 1.0) or 1.0
        agent_id = args.agent or "unknown"
        db.execute(
            "INSERT OR REPLACE INTO knowledge_edges "
            "(source_table, source_id, target_table, target_id, relation_type, weight, agent_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (source_table, source_id, target_table, target_id, relation, weight, agent_id),
        )
        db.commit()
        json_out({"ok": True, "edge": {
            "source": f"{source_table}#{source_id}",
            "target": f"{target_table}#{target_id}",
            "relation": relation, "weight": weight,
        }})
        return

    if sub == "activate":
        hops = getattr(args, "hops", 2) or 2
        decay = getattr(args, "decay", 0.6)
        top_k = getattr(args, "top_k", 20) or 20

        # Build seed list — either from --from-stdin or positional table+id
        if getattr(args, "from_stdin", False):
            import sys as _sys
            raw = _sys.stdin.read().strip()
            try:
                data = json.loads(raw)
            except Exception:
                json_out({"error": "stdin must be valid JSON (vsearch output)"})
                return
            seeds = []
            if isinstance(data, list):
                # Flat list with "table"+"id" keys
                seeds = [(r["table"], int(r["id"])) for r in data if "table" in r and "id" in r]
            elif isinstance(data, dict):
                if "results" in data:
                    seeds = [(r["table"], int(r["id"])) for r in data["results"] if "table" in r and "id" in r]
                else:
                    # vsearch output: separate "memories", "events", "context" arrays
                    for tbl in ("memories", "events", "context"):
                        for r in data.get(tbl, []):
                            if "id" in r:
                                seeds.append((tbl, int(r["id"])))
            if not seeds:
                json_out({"error": "unrecognised stdin JSON structure or no seed nodes found"})
                return
        else:
            seeds = [(args.table, args.id)]

        if not seeds:
            json_out({"error": "no seed nodes — provide table+id or use --from-stdin"})
            return

        results = spreading_activation(db, seeds, hops=hops, decay=decay, top_k=top_k)
        for r in results:
            r["label"] = _graph_node_label(db, r["table"], r["id"])

        json_out({
            "seeds": [{"table": t, "id": i} for t, i in seeds],
            "hops": hops,
            "decay": decay,
            "activated": results,
            "count": len(results),
        })
        return

    if sub == "pagerank":
        force = getattr(args, "force", False)
        damping = getattr(args, "damping", 0.85) or 0.85
        iters = getattr(args, "iters", 50) or 50
        top_k = getattr(args, "top_k", 20) or 20
        fmt = getattr(args, "format", "text") or "text"
        table_filter = getattr(args, "table", None)
        scores = _graph_pagerank(db, damping=damping, max_iter=iters, force=force)
        # Store per-node PageRank in agent_state for downstream use
        now = _now_ts()
        for (tbl, nid), score in scores.items():
            state_key = f"pagerank_{tbl}_{nid}"
            db.execute(
                "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) "
                "VALUES ('graph-weaver', ?, ?, ?)",
                (state_key, json.dumps({"score": round(score, 8), "table": tbl, "id": nid}), now)
            )
        db.commit()
        # Apply --table filter
        if table_filter:
            filtered = {k: v for k, v in scores.items() if k[0] == table_filter}
        else:
            filtered = scores
        top = sorted(filtered.items(), key=lambda x: -x[1])[:top_k]
        results = []
        for (tbl, nid), score in top:
            results.append({
                "table": tbl, "id": nid, "pagerank": round(score, 6),
                "label": _graph_node_label(db, tbl, nid),
            })
        if fmt == "json":
            json_out({"node_count": len(filtered), "total_nodes": len(scores),
                       "table_filter": table_filter, "top_k": results})
        else:
            filter_str = f" (table={table_filter})" if table_filter else ""
            print(f"PageRank — top {len(results)} of {len(filtered)} nodes{filter_str} (damping={damping})")
            for r in results:
                print(f"  {r['pagerank']:.6f}  {r['table']}#{r['id']}  {r['label'][:60]}")
        return

    if sub == "communities":
        force = getattr(args, "force", False)
        seed = getattr(args, "seed", 42) or 42
        fmt = getattr(args, "format", "text") or "text"
        communities = _graph_communities(db, seed=seed, force=force)
        # Summarize community sizes
        from collections import Counter as _Counter
        sizes = _Counter(communities.values())
        if fmt == "json":
            comm_list = {}
            for (tbl, nid), cid in communities.items():
                comm_list.setdefault(cid, []).append({
                    "table": tbl, "id": nid,
                    "label": _graph_node_label(db, tbl, nid),
                })
            json_out({
                "node_count": len(communities),
                "community_count": len(sizes),
                "communities": comm_list,
            })
        else:
            print(f"Community detection — {len(sizes)} communities, {len(communities)} nodes")
            for cid, count in sorted(sizes.items(), key=lambda x: -x[1])[:20]:
                members = [(tbl, nid) for (tbl, nid), c in communities.items() if c == cid][:3]
                examples = ", ".join(f"{t}#{i}" for t, i in members)
                print(f"  community {cid:4d}: {count:4d} nodes  [{examples}...]")
        return

    if sub == "betweenness":
        force = getattr(args, "force", False)
        top_k = getattr(args, "top_k", 20) or 20
        fmt = getattr(args, "format", "text") or "text"
        scores = _graph_betweenness(db, force=force)
        top = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        results = []
        for (tbl, nid), score in top:
            results.append({
                "table": tbl, "id": nid, "betweenness": round(score, 6),
                "label": _graph_node_label(db, tbl, nid),
            })
        if fmt == "json":
            json_out({"node_count": len(scores), "top_k": results})
        else:
            print(f"Betweenness centrality — top {len(results)} of {len(scores)} nodes")
            for r in results:
                print(f"  {r['betweenness']:.6f}  {r['table']}#{r['id']}  {r['label'][:60]}")
        return

    if sub == "protect-bridges":
        threshold = getattr(args, "threshold", 0.005) or 0.005
        dry_run = getattr(args, "dry_run", False)
        force = getattr(args, "force", False)
        fmt = getattr(args, "format", "text") or "text"
        scores = _graph_betweenness(db, force=force)
        # Filter to memory nodes above threshold
        max_score = max(scores.values()) if scores else 1.0
        candidates = [
            (tbl, nid, s) for (tbl, nid), s in scores.items()
            if tbl == "memories" and s >= threshold
        ]
        candidates.sort(key=lambda x: -x[2])
        updated = []
        for tbl, nid, score in candidates:
            ewc = round(min(1.0, score / max_score), 4) if max_score > 0 else 0.0
            if not dry_run:
                db.execute(
                    "UPDATE memories SET protected=1, ewc_importance=? WHERE id=?",
                    (ewc, nid)
                )
            updated.append({"id": nid, "betweenness": round(score, 6), "ewc_importance": ewc,
                             "label": _graph_node_label(db, "memories", nid)})
        if not dry_run:
            db.commit()
        if fmt == "json":
            json_out({"dry_run": dry_run, "protected": len(updated), "nodes": updated})
        else:
            tag = "(dry-run)" if dry_run else ""
            print(f"protect-bridges {tag}: {len(updated)} memory nodes marked protected")
            for u in updated:
                print(f"  betweenness={u['betweenness']:.6f}  ewc={u['ewc_importance']}  memories#{u['id']}  {u['label'][:60]}")
        return

    if sub == "path":
        src_table = args.from_table
        src_id = args.from_id
        dst_table = args.to_table
        dst_id = args.to_id
        max_hops = getattr(args, "max_hops", 6) or 6
        fmt = getattr(args, "format", "text") or "text"
        path = _graph_shortest_path(db, src_table, src_id, dst_table, dst_id, max_hops=max_hops)
        if fmt == "json":
            if path is None:
                json_out({"found": False, "from": f"{src_table}#{src_id}", "to": f"{dst_table}#{dst_id}"})
            else:
                annotated = []
                for step in path:
                    annotated.append({**step, "label": _graph_node_label(db, step["table"], step["id"])})
                json_out({"found": True, "hops": len(path) - 1, "path": annotated})
        else:
            if path is None:
                print(f"No path found between {src_table}#{src_id} and {dst_table}#{dst_id} within {max_hops} hops")
            else:
                print(f"Shortest path: {len(path) - 1} hops")
                for i, step in enumerate(path):
                    label = _graph_node_label(db, step["table"], step["id"])
                    edge_info = f"  --[{step['edge_type']}]-->" if step.get("edge_type") else ""
                    print(f"  [{i}]{edge_info} {step['table']}#{step['id']}  {label[:60]}")
        return

    # fallback
    json_out({"error": "unknown graph subcommand", "subcommand": sub})


# ---------------------------------------------------------------------------
# Graph algorithm helpers (PageRank, community detection, betweenness, path)
# ---------------------------------------------------------------------------

def _graph_load_edges(db, tables=("memories", "events", "context", "entities")):
    """Load knowledge_edges into an adjacency dict. Returns (nodes, adj).

    adj[node] = list of (neighbor, weight)  — undirected (both directions).
    node = (table, id) tuple.
    """
    rows = db.execute(
        "SELECT source_table, source_id, target_table, target_id, weight "
        "FROM knowledge_edges"
    ).fetchall()
    adj = {}
    nodes = set()
    for src_tbl, src_id, tgt_tbl, tgt_id, w in rows:
        if src_tbl not in tables or tgt_tbl not in tables:
            continue
        u = (src_tbl, int(src_id))
        v = (tgt_tbl, int(tgt_id))
        nodes.add(u)
        nodes.add(v)
        adj.setdefault(u, []).append((v, w))
        adj.setdefault(v, []).append((u, w))
    return nodes, adj


def _graph_pagerank(db, damping=0.85, max_iter=50, tol=1e-6, force=False):
    """Compute PageRank over knowledge_edges using power iteration.

    Results cached in agent_state under key 'graph_pagerank'.
    Returns dict: {(table, id): score}.
    """
    import json as _json

    # Check cache unless forced
    if not force:
        row = db.execute(
            "SELECT value, updated_at FROM agent_state WHERE agent_id='graph-weaver' AND key='graph_pagerank'"
        ).fetchone()
        if row:
            age_hours = (datetime.now() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
            if age_hours < 24:
                raw = _json.loads(row["value"])
                return {(parts[0], int(parts[1])): v
                        for x, v in raw.items()
                        for parts in [x.split("|", 1)]}

    nodes, adj = _graph_load_edges(db)
    if not nodes:
        return {}

    node_list = list(nodes)
    n = len(node_list)
    idx = {node: i for i, node in enumerate(node_list)}

    # Initialize uniform scores
    scores = [1.0 / n] * n

    # Build out-degree weighted adjacency for directed PageRank
    # (treat edges as undirected, split weight evenly)
    out_weight = [0.0] * n
    for node in node_list:
        i = idx[node]
        for _, w in adj.get(node, []):
            out_weight[i] += w

    for _ in range(max_iter):
        new_scores = [(1.0 - damping) / n] * n
        for node in node_list:
            i = idx[node]
            total_out = out_weight[i]
            if total_out == 0:
                continue
            contrib = damping * scores[i] / total_out
            for neighbor, w in adj.get(node, []):
                j = idx.get(neighbor)
                if j is not None:
                    new_scores[j] += contrib * w

        # Check convergence
        diff = sum(abs(new_scores[i] - scores[i]) for i in range(n))
        scores = new_scores
        if diff < tol:
            break

    result = {node_list[i]: scores[i] for i in range(n)}

    # Cache in agent_state
    import json as _json
    cached = {f"{tbl}|{nid}": v for (tbl, nid), v in result.items()}
    now = _now_ts()
    db.execute(
        "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) VALUES ('graph-weaver', 'graph_pagerank', ?, ?)",
        (_json.dumps(cached), now)
    )
    db.commit()

    return result


def _graph_communities(db, seed=42, max_iter=30, force=False):
    """Label propagation community detection on knowledge_edges.

    Returns dict: {(table, id): community_id}.
    Results cached for 24h in agent_state under key 'graph_communities'.
    """
    import json as _json
    import random as _random

    if not force:
        row = db.execute(
            "SELECT value, updated_at FROM agent_state WHERE agent_id='graph-weaver' AND key='graph_communities'"
        ).fetchone()
        if row:
            age_hours = (datetime.now() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
            if age_hours < 24:
                raw = _json.loads(row["value"])
                return {(parts[0], int(parts[1])): v
                        for x, v in raw.items()
                        for parts in [x.split("|", 1)]}

    nodes, adj = _graph_load_edges(db)
    if not nodes:
        return {}

    node_list = list(nodes)
    _random.seed(seed)

    # Initialize each node with its own unique label (index)
    labels = {node: i for i, node in enumerate(node_list)}

    for iteration in range(max_iter):
        changed = False
        shuffled = node_list[:]
        _random.shuffle(shuffled)
        for node in shuffled:
            neighbors = adj.get(node, [])
            if not neighbors:
                continue
            # Count weighted votes for each neighbor label
            votes = {}
            for neighbor, w in neighbors:
                lbl = labels.get(neighbor, -1)
                votes[lbl] = votes.get(lbl, 0.0) + w
            # Pick label with highest vote (tie-break: smallest label)
            best_label = max(votes.items(), key=lambda x: (x[1], -x[0]))[0]
            if labels[node] != best_label:
                labels[node] = best_label
                changed = True
        if not changed:
            break

    # Normalize: remap community IDs to sequential integers
    unique_labels = sorted(set(labels.values()))
    remap = {old: new for new, old in enumerate(unique_labels)}
    result = {node: remap[lbl] for node, lbl in labels.items()}

    # Cache
    cached = {f"{tbl}|{nid}": v for (tbl, nid), v in result.items()}
    now = _now_ts()
    db.execute(
        "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) VALUES ('graph-weaver', 'graph_communities', ?, ?)",
        (_json.dumps(cached), now)
    )
    db.commit()

    return result


def _graph_betweenness(db, normalized=True, force=False):
    """Betweenness centrality via Brandes algorithm (unweighted BFS).

    Returns dict: {(table, id): score}.
    Cached for 48h in agent_state under key 'graph_betweenness'.
    WARNING: O(V*E) — runs in seconds on 4,750 edges but will be slow on larger graphs.
    """
    import json as _json
    from collections import deque as _deque

    if not force:
        row = db.execute(
            "SELECT value, updated_at FROM agent_state WHERE agent_id='graph-weaver' AND key='graph_betweenness'"
        ).fetchone()
        if row:
            age_hours = (datetime.now() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
            if age_hours < 48:
                raw = _json.loads(row["value"])
                return {(parts[0], int(parts[1])): v
                        for x, v in raw.items()
                        for parts in [x.split("|", 1)]}

    nodes, adj = _graph_load_edges(db)
    if not nodes:
        return {}

    node_list = list(nodes)
    n = len(node_list)
    betweenness = {node: 0.0 for node in node_list}

    # Brandes algorithm (unweighted)
    for s in node_list:
        # BFS from s
        stack = []
        pred = {node: [] for node in node_list}
        sigma = {node: 0.0 for node in node_list}
        sigma[s] = 1.0
        dist = {node: -1 for node in node_list}
        dist[s] = 0
        queue = _deque([s])

        while queue:
            v = queue.popleft()
            stack.append(v)
            for w, _ in adj.get(v, []):
                if dist[w] < 0:
                    queue.append(w)
                    dist[w] = dist[v] + 1
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)

        # Accumulate dependencies
        delta = {node: 0.0 for node in node_list}
        while stack:
            w = stack.pop()
            for v in pred[w]:
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                betweenness[w] += delta[w]

    # Normalize
    if normalized and n > 2:
        scale = 1.0 / ((n - 1) * (n - 2))
        for node in betweenness:
            betweenness[node] *= scale

    # Cache
    cached = {f"{tbl}|{nid}": v for (tbl, nid), v in betweenness.items()}
    now = _now_ts()
    db.execute(
        "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) VALUES ('graph-weaver', 'graph_betweenness', ?, ?)",
        (_json.dumps(cached), now)
    )
    db.commit()

    return betweenness


def _graph_shortest_path(db, src_table, src_id, dst_table, dst_id, max_hops=6):
    """BFS shortest path between two nodes in knowledge_edges.

    Returns list of dicts [{table, id, edge_type}] from source to dest,
    or None if no path found within max_hops.
    """
    from collections import deque as _deque

    src = (src_table, int(src_id))
    dst = (dst_table, int(dst_id))

    if src == dst:
        return [{"table": src_table, "id": src_id, "edge_type": None}]

    # Load edges for BFS (with edge type tracking)
    rows = db.execute(
        "SELECT source_table, source_id, target_table, target_id, relation_type, weight "
        "FROM knowledge_edges"
    ).fetchall()

    # Build adjacency: node -> [(neighbor, edge_type, weight)]
    adj = {}
    for src_t, s_id, tgt_t, t_id, rel, w in rows:
        u = (src_t, int(s_id))
        v = (tgt_t, int(t_id))
        adj.setdefault(u, []).append((v, rel, w))
        adj.setdefault(v, []).append((u, rel, w))

    # BFS
    visited = {src}
    # queue entries: (node, path_so_far)
    queue = _deque([(src, [{"table": src[0], "id": src[1], "edge_type": None}])])

    while queue:
        node, path = queue.popleft()
        if len(path) > max_hops + 1:
            continue
        for neighbor, edge_type, _ in adj.get(node, []):
            if neighbor == dst:
                return path + [{"table": dst[0], "id": dst[1], "edge_type": edge_type}]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [{"table": neighbor[0], "id": neighbor[1], "edge_type": edge_type}]))

    return None


# ---------------------------------------------------------------------------
# MAINTENANCE commands
# ---------------------------------------------------------------------------

def cmd_backup(args):
    db = get_db()
    db.close()  # ensure WAL checkpoint
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUPS_DIR / f"brain_{ts}.db"
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, backup_path)

    # Also export to SQL for iCloud-safe backup
    sql_path = BACKUPS_DIR / f"brain_{ts}.sql"
    import subprocess
    subprocess.run(["sqlite3", str(DB_PATH), ".dump"], stdout=open(str(sql_path), "w"), check=True)

    # Prune old backups (keep last 30)
    backups = sorted(BACKUPS_DIR.glob("brain_*.db"), reverse=True)
    for old in backups[30:]:
        old.unlink()
        sql_sibling = old.with_suffix(".sql")
        if sql_sibling.exists():
            sql_sibling.unlink()

    size = backup_path.stat().st_size
    json_out({"ok": True, "backup": str(backup_path), "sql": str(sql_path), "size_bytes": size})


def cmd_dashboard(args):
    """Unified telemetry dashboard — single-pane-of-glass health view of brain.db."""
    from agentmemory.telemetry import get_dashboard

    db_path = str(DB_PATH)
    agent_id = getattr(args, "dashboard_agent", None)
    fmt = getattr(args, "format", "text")

    data = get_dashboard(db_path, agent_id=agent_id)

    if fmt == "json":
        json_out(data)
        return

    # ── Terminal-friendly summary ────────────────────────────────────────────
    score = data["health_score"]
    grade = data["grade"]
    computed = data["computed_at"]

    # Header
    agent_label = f"  agent={agent_id}" if agent_id else ""
    print(f"brainctl dashboard{agent_label}  [{computed}]")
    print()

    # Score bar
    bar_len = 30
    filled = int(round(score * bar_len))
    bar = "#" * filled + "-" * (bar_len - filled)
    print(f"  Health Score : {score:.2f} / 1.00  [{bar}]  Grade: {grade}")
    print()

    # Memory
    m = data["memory"]
    print(f"  Memory       : {m['active']} active / {m['count']} total"
          f"  (avg confidence: {m['avg_confidence']:.2f})")

    # Events
    e = data["events"]
    print(f"  Events       : {e['count']} total  |  {e['last_7d']} last 7d")

    # Entities
    en = data["entities"]
    print(f"  Entities     : {en['active']} active / {en['count']} total")

    # Decisions
    d = data["decisions"]
    print(f"  Decisions    : {d['count']}")

    # Affect
    af = data["affect"]
    if af:
        print(f"  Affect       : {af['current_state'] or 'unknown'}"
              f"  (valence={af['valence']:.2f}, arousal={af['arousal']:.2f})")
    else:
        print("  Affect       : no data")

    # Budget
    b = data["budget"]
    print(f"  Budget       : ~{b['token_estimate']:,} tokens logged")

    # Alerts
    alerts = data["alerts"]
    print()
    if alerts:
        print(f"  Alerts ({len(alerts)}):")
        for a in alerts:
            print(f"    ! {a}")
    else:
        print("  No alerts.")
    print()


def cmd_stats(args):
    db = get_db()
    stats = {}
    for table in ["agents", "memories", "events", "context", "tasks", "decisions", "blobs", "access_log"]:
        row = db.execute(f"SELECT count(*) as cnt FROM {table}").fetchone()
        stats[table] = row["cnt"]

    # Active memories only
    row = db.execute("SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL").fetchone()
    stats["active_memories"] = row["cnt"]

    # DB file size
    stats["db_size_bytes"] = DB_PATH.stat().st_size
    stats["db_size_mb"] = round(stats["db_size_bytes"] / 1048576, 2)

    # WAL size
    wal_path = DB_PATH.with_suffix(".db-wal")
    if wal_path.exists():
        stats["wal_size_bytes"] = wal_path.stat().st_size
    else:
        stats["wal_size_bytes"] = 0

    # Uncertainty log search row count
    try:
        unc_row = db.execute(
            "SELECT count(*) as cnt FROM agent_uncertainty_log WHERE query IS NOT NULL"
        ).fetchone()
        stats["uncertainty_log_search_rows"] = unc_row["cnt"]
    except Exception:
        stats["uncertainty_log_search_rows"] = 0

    # Bayesian alpha/beta coverage (Phase 1)
    try:
        ab_row = db.execute(
            """SELECT
                COUNT(*) AS active,
                SUM(CASE WHEN alpha IS NOT NULL AND beta IS NOT NULL THEN 1 ELSE 0 END) AS ab_populated,
                ROUND(AVG(alpha), 4) AS avg_alpha,
                ROUND(AVG(beta),  4) AS avg_beta
               FROM memories WHERE retired_at IS NULL"""
        ).fetchone()
        stats["bayesian_alpha_beta_coverage"] = round((ab_row["ab_populated"] / ab_row["active"]), 4) if ab_row["active"] else 0.0
        stats["bayesian_avg_alpha"] = ab_row["avg_alpha"]
        stats["bayesian_avg_beta"]  = ab_row["avg_beta"]
    except Exception:
        pass

    json_out(stats)

def cmd_init(args):
    """Initialize a fresh brain.db with the full production schema."""
    target = Path(getattr(args, "path", None) or DB_PATH)
    if target.exists() and not getattr(args, "force", False):
        json_out({"ok": False, "error": f"Database already exists at {target}. Use --force to overwrite."})
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()  # --force: remove existing

    # Try to find init_schema.sql from the package
    schema_sql = None
    schema_locations = [
        Path(__file__).parent / "db" / "init_schema.sql",  # pip-installed
        Path.home() / "agentmemory" / "db" / "init_schema.sql",  # dev checkout
    ]
    for loc in schema_locations:
        if loc.exists():
            schema_sql = loc.read_text()
            break

    try:
        conn = sqlite3.connect(str(target))
        if schema_sql:
            conn.executescript(schema_sql)
        else:
            conn.close()
            from agentmemory.brain import Brain
            Brain(str(target))
            conn = sqlite3.connect(str(target))

        # Seed required rows that triggers and commands depend on
        _now = "strftime('%Y-%m-%dT%H:%M:%S','now')"
        seed_sql = f"""
            INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('enabled', '0');
            INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('ignition_threshold', '0.7');
            INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('urgent_threshold', '0.9');
            INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('governor_max_per_hour', '5');
            INSERT OR IGNORE INTO neuromodulation_state (id, org_state, dopamine_signal, arousal_level,
                confidence_boost_rate, confidence_decay_rate, retrieval_breadth_multiplier,
                focus_level, temporal_lambda, context_window_depth)
                VALUES (1, 'normal', 0.0, 0.3, 0.1, 0.02, 1.0, 0.3, 0.03, 50);
        """
        try:
            conn.executescript(seed_sql)
        except Exception:
            pass  # Some tables may not exist in minimal schema

        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        conn.close()

        json_out({"ok": True, "path": str(target), "tables": len(tables), "table_list": tables})
    except Exception as e:
        json_out({"ok": False, "error": str(e)})


def cmd_cost(args):
    """Estimate token cost of brain operations — helps users understand and reduce model usage."""
    db = get_db()
    report = {}

    # 1. Average search result size (tokens)
    # Simulate a broad search to estimate typical output size
    sample_rows = db.execute(
        "SELECT * FROM memories WHERE retired_at IS NULL ORDER BY recalled_count DESC LIMIT 10"
    ).fetchall()
    sample_data = [dict(r) for r in sample_rows]
    avg_mem_tokens = _estimate_tokens(sample_data) // max(len(sample_data), 1) if sample_data else 0

    # 2. Per-format comparison
    sample_json = json.dumps(sample_data, indent=2, default=str)
    sample_compact = json.dumps(sample_data, separators=(",", ":"), default=str)
    sample_oneline = "\n".join(
        f"{r.get('id', '?')}|{r.get('category', '')}|{str(r.get('content', ''))[:120]}"
        for r in sample_data
    )

    report["format_comparison"] = {
        "sample_size": len(sample_data),
        "json_tokens": len(sample_json) // 4,
        "compact_tokens": len(sample_compact) // 4,
        "oneline_tokens": len(sample_oneline) // 4,
        "savings_compact_pct": round((1 - len(sample_compact) / max(len(sample_json), 1)) * 100, 1),
        "savings_oneline_pct": round((1 - len(sample_oneline) / max(len(sample_json), 1)) * 100, 1),
    }

    # 3. Access log: queries today + tokens consumed
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_row = db.execute(
        "SELECT COUNT(*) as queries, COALESCE(SUM(tokens_consumed), 0) as tokens "
        "FROM access_log WHERE created_at >= ?",
        (today + " 00:00:00",)
    ).fetchone()
    report["today"] = {
        "queries": today_row["queries"],
        "tokens_consumed": today_row["tokens"],
    }

    # 4. Last 7 days
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    week_row = db.execute(
        "SELECT COUNT(*) as queries, COALESCE(SUM(tokens_consumed), 0) as tokens "
        "FROM access_log WHERE created_at >= ?",
        (week_ago + " 00:00:00",)
    ).fetchone()
    report["last_7_days"] = {
        "queries": week_row["queries"],
        "tokens_consumed": week_row["tokens"],
        "avg_tokens_per_query": round(week_row["tokens"] / max(week_row["queries"], 1)),
    }

    # 5. Top token-consuming agents
    top_agents = db.execute(
        "SELECT agent_id, COUNT(*) as queries, COALESCE(SUM(tokens_consumed), 0) as tokens "
        "FROM access_log WHERE created_at >= ? AND tokens_consumed IS NOT NULL "
        "GROUP BY agent_id ORDER BY tokens DESC LIMIT 5",
        (week_ago + " 00:00:00",)
    ).fetchall()
    report["top_agents_7d"] = [
        {"agent": r["agent_id"], "queries": r["queries"], "tokens": r["tokens"]}
        for r in top_agents
    ]

    # 6. Recommendations
    tips = []
    if report["format_comparison"]["savings_oneline_pct"] > 30:
        tips.append(f"Use --output oneline to save ~{report['format_comparison']['savings_oneline_pct']}% tokens on search results")
    if report["format_comparison"]["savings_compact_pct"] > 15:
        tips.append(f"Use --output compact to save ~{report['format_comparison']['savings_compact_pct']}% tokens vs pretty JSON")
    tips.append("Use --budget N to cap search output at N tokens (e.g. --budget 500)")
    tips.append("Use --limit 5 instead of default 10 for focused queries")
    avg_tpq = report["last_7_days"]["avg_tokens_per_query"]
    if avg_tpq > 2000:
        tips.append(f"Avg {avg_tpq} tokens/query is high — consider --min-salience 0.1 to filter noise")
    report["recommendations"] = tips

    json_out(report)


# ---------------------------------------------------------------------------
# AFFECT TRACKING — functional affect states for AI agents
# Functional affect classification for agent state tracking
# ---------------------------------------------------------------------------

def cmd_affect_log(args):
    """Log an affect observation for an agent by classifying text."""
    from agentmemory.affect import classify_affect
    db = get_db()
    text = args.text
    result = classify_affect(text)

    safety = None
    if result["safety_flags"]:
        safety = result["safety_flags"][0]["severity"]

    db.execute(
        "INSERT INTO affect_log (agent_id, valence, arousal, dominance, affect_label, "
        "cluster, functional_state, safety_flag, trigger, source, metadata, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (args.agent or "unknown", result["valence"], result["arousal"], result["dominance"],
         result["affect_label"], result["cluster"], result["functional_state"],
         safety, text[:200], args.source or "observation",
         json.dumps({"emotions": result["emotions"], "safety_flags": result["safety_flags"]}))
    )
    db.commit()
    json_out(result)


def cmd_affect_check(args):
    """Check current affect state for an agent (latest entry + safety probe)."""
    from agentmemory.affect import SAFETY_PATTERNS, affect_velocity
    db = get_db()
    agent = args.agent or "unknown"

    # Latest entry
    row = db.execute(
        "SELECT * FROM affect_log WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
        (agent,)
    ).fetchone()
    if not row:
        json_out({"agent": agent, "status": "no_data"})
        return

    current = dict(row)

    # Recent history for velocity
    history_rows = db.execute(
        "SELECT valence, arousal, dominance FROM affect_log WHERE agent_id=? ORDER BY created_at DESC LIMIT 10",
        (agent,)
    ).fetchall()
    history = list(reversed([dict(r) for r in history_rows]))
    velocity = affect_velocity(history)

    # Safety check on current state
    v, a, d = current["valence"], current["arousal"], current["dominance"]
    active_flags = []
    for name, pattern in SAFETY_PATTERNS.items():
        try:
            if pattern["conditions"](v, a, d):
                active_flags.append({"pattern": name, "severity": pattern["severity"],
                                     "description": pattern["description"]})
        except Exception:
            pass

    json_out({
        "agent": agent,
        "current": {
            "valence": current["valence"], "arousal": current["arousal"],
            "dominance": current["dominance"], "affect_label": current["affect_label"],
            "cluster": current["cluster"], "functional_state": current["functional_state"],
            "recorded_at": current["created_at"],
        },
        "velocity": velocity,
        "safety_flags": active_flags,
        "status": "critical" if any(f["severity"] == "critical" for f in active_flags)
                  else "warning" if active_flags
                  else "healthy",
    })


def cmd_affect_history(args):
    """Show affect history for an agent."""
    db = get_db()
    agent = args.agent or "unknown"
    limit = args.limit or 20

    rows = db.execute(
        "SELECT id, valence, arousal, dominance, affect_label, cluster, "
        "functional_state, safety_flag, trigger, created_at "
        "FROM affect_log WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
        (agent, limit)
    ).fetchall()
    json_out(rows_to_list(rows))


def cmd_affect_monitor(args):
    """Fleet-wide affect monitoring — scan all agents for safety flags."""
    from agentmemory.affect import SAFETY_PATTERNS, fleet_affect_summary
    db = get_db()

    # Get latest affect state per agent
    rows = db.execute("""
        SELECT a.agent_id, a.valence, a.arousal, a.dominance,
               a.affect_label, a.cluster, a.functional_state, a.safety_flag
        FROM affect_log a
        INNER JOIN (
            SELECT agent_id, MAX(id) as max_id FROM affect_log GROUP BY agent_id
        ) latest ON a.id = latest.max_id
    """).fetchall()

    agent_states = {}
    for r in rows:
        state = dict(r)
        v, a, d = state["valence"], state["arousal"], state["dominance"]
        flags = []
        for name, pattern in SAFETY_PATTERNS.items():
            try:
                if pattern["conditions"](v, a, d):
                    flags.append({"pattern": name, "severity": pattern["severity"],
                                  "description": pattern["description"]})
            except Exception:
                pass
        state["safety_flags"] = flags
        agent_states[state["agent_id"]] = state

    summary = fleet_affect_summary(agent_states)
    json_out(summary)


def cmd_affect_classify(args):
    """Classify affect from text without logging. Dry-run mode."""
    from agentmemory.affect import classify_affect
    result = classify_affect(args.text)
    json_out(result)


# ---------------------------------------------------------------------------
# REPORT — compile brain knowledge into readable markdown
# ---------------------------------------------------------------------------

def cmd_report(args):
    """Compile brain knowledge into a structured markdown report."""
    db = get_db()
    topic = getattr(args, "topic", None)
    agent = args.agent
    entity_name = getattr(args, "entity", None)
    out_file = getattr(args, "out", None)
    limit = getattr(args, "limit", 20) or 20

    lines = []

    def h1(text): lines.append(f"\n# {text}\n")
    def h2(text): lines.append(f"\n## {text}\n")
    def h3(text): lines.append(f"\n### {text}\n")
    def p(text): lines.append(f"{text}\n")
    def bullet(text): lines.append(f"- {text}")
    def blank(): lines.append("")

    # --- Header ---
    h1("Brain Report")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p(f"*Generated: {now_str}*")
    if topic:
        p(f"*Topic filter: {topic}*")
    if agent:
        p(f"*Agent filter: {agent}*")
    if entity_name:
        p(f"*Entity focus: {entity_name}*")

    # --- Stats overview ---
    h2("Overview")
    stats = {}
    for tbl in ["memories", "events", "entities", "decisions", "knowledge_edges"]:
        try:
            stats[tbl] = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            stats[tbl] = 0
    active = db.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
    p(f"**{active}** active memories, **{stats['entities']}** entities, "
      f"**{stats['events']}** events, **{stats['decisions']}** decisions, "
      f"**{stats['knowledge_edges']}** knowledge edges")

    # --- Entity focus mode ---
    if entity_name:
        _report_entity(db, entity_name, lines, h2, h3, p, bullet, blank, limit)

    # --- Memories ---
    h2("Key Memories")
    mem_sql = "SELECT id, category, content, confidence, created_at FROM memories WHERE retired_at IS NULL"
    mem_params = []
    if topic:
        mem_sql += " AND (content LIKE ? OR category LIKE ?)"
        mem_params.extend([f"%{topic}%", f"%{topic}%"])
    if agent:
        mem_sql += " AND agent_id = ?"
        mem_params.append(agent)
    mem_sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
    mem_params.append(limit)
    rows = db.execute(mem_sql, mem_params).fetchall()

    if rows:
        # Group by category
        by_cat = {}
        for r in rows:
            cat = r["category"] or "general"
            by_cat.setdefault(cat, []).append(r)
        for cat, mems in sorted(by_cat.items()):
            h3(f"{cat.title()} ({len(mems)})")
            for m in mems:
                conf = f"[{m['confidence']:.0%}]" if m["confidence"] else ""
                bullet(f"{m['content'][:200]} {conf}")
            blank()
    else:
        p("*No memories found.*")

    # --- Entities ---
    h2("Entities")
    ent_sql = "SELECT id, name, entity_type, observations, confidence FROM entities WHERE retired_at IS NULL"
    ent_params = []
    if topic:
        ent_sql += " AND (name LIKE ? OR observations LIKE ?)"
        ent_params.extend([f"%{topic}%", f"%{topic}%"])
    ent_sql += " ORDER BY confidence DESC LIMIT ?"
    ent_params.append(limit)
    ent_rows = db.execute(ent_sql, ent_params).fetchall()

    if ent_rows:
        for e in ent_rows:
            obs = []
            try:
                obs = json.loads(e["observations"] or "[]")
            except Exception:
                pass
            obs_str = "; ".join(str(o)[:80] for o in obs[:3]) if obs else ""
            bullet(f"**{e['name']}** ({e['entity_type']}) — {obs_str}")
        blank()

        # Relations between listed entities
        ent_ids = [e["id"] for e in ent_rows]
        if ent_ids:
            ph = ",".join("?" * len(ent_ids))
            edges = db.execute(
                f"SELECT ke.relation_type, es.name as src, et.name as tgt "
                f"FROM knowledge_edges ke "
                f"JOIN entities es ON ke.source_id = es.id AND ke.source_table = 'entities' "
                f"JOIN entities et ON ke.target_id = et.id AND ke.target_table = 'entities' "
                f"WHERE ke.source_id IN ({ph}) OR ke.target_id IN ({ph})",
                ent_ids + ent_ids
            ).fetchall()
            if edges:
                h3("Relations")
                seen = set()
                for edge in edges:
                    key = f"{edge['src']}-{edge['relation_type']}-{edge['tgt']}"
                    if key not in seen:
                        bullet(f"{edge['src']} **{edge['relation_type']}** → {edge['tgt']}")
                        seen.add(key)
                blank()
    else:
        p("*No entities found.*")

    # --- Recent Decisions ---
    h2("Recent Decisions")
    dec_sql = "SELECT title, rationale, project, created_at FROM decisions"
    dec_params = []
    if topic:
        dec_sql += " WHERE title LIKE ? OR rationale LIKE ?"
        dec_params.extend([f"%{topic}%", f"%{topic}%"])
    dec_sql += " ORDER BY created_at DESC LIMIT ?"
    dec_params.append(min(limit, 10))
    dec_rows = db.execute(dec_sql, dec_params).fetchall()

    if dec_rows:
        for d in dec_rows:
            proj = f" [{d['project']}]" if d["project"] else ""
            bullet(f"**{d['title']}**{proj}")
            p(f"  *{d['rationale'][:200]}*")
        blank()
    else:
        p("*No decisions found.*")

    # --- Recent Events ---
    h2("Recent Activity")
    ev_sql = "SELECT event_type, summary, project, created_at FROM events"
    ev_params = []
    if topic:
        ev_sql += " WHERE summary LIKE ?"
        ev_params.append(f"%{topic}%")
    if agent:
        ev_sql += (" AND" if topic else " WHERE") + " agent_id = ?"
        ev_params.append(agent)
    ev_sql += " ORDER BY created_at DESC LIMIT ?"
    ev_params.append(min(limit, 15))
    ev_rows = db.execute(ev_sql, ev_params).fetchall()

    if ev_rows:
        for e in ev_rows:
            ts = e["created_at"][:10] if e["created_at"] else ""
            proj = f" [{e['project']}]" if e["project"] else ""
            bullet(f"`{ts}` {e['event_type']}{proj}: {(e['summary'] or '')[:150]}")
        blank()

    # --- Affect State ---
    h2("Current Affect State")
    try:
        aff_rows = db.execute("""
            SELECT a.agent_id, a.valence, a.arousal, a.dominance,
                   a.affect_label, a.functional_state, a.safety_flag
            FROM affect_log a INNER JOIN (
                SELECT agent_id, MAX(id) as max_id FROM affect_log GROUP BY agent_id
            ) latest ON a.id = latest.max_id
            ORDER BY a.created_at DESC LIMIT 10
        """).fetchall()
        if aff_rows:
            for a in aff_rows:
                flag = f" ⚠️ {a['safety_flag']}" if a["safety_flag"] else ""
                bullet(f"**{a['agent_id']}**: {a['affect_label']} (v={a['valence']:.2f} "
                       f"a={a['arousal']:.2f} d={a['dominance']:.2f}) → {a['functional_state']}{flag}")
            blank()
        else:
            p("*No affect data.*")
    except Exception:
        p("*Affect tracking not available.*")

    # --- Output ---
    report = "\n".join(lines) + "\n"

    if out_file:
        Path(out_file).write_text(report)
        json_out({"ok": True, "path": out_file, "lines": len(lines), "chars": len(report)})
    else:
        print(report)


def _report_entity(db, name, lines, h2, h3, p, bullet, blank, limit):
    """Focused report on a single entity and everything connected to it."""
    row = db.execute(
        "SELECT * FROM entities WHERE name LIKE ? AND retired_at IS NULL LIMIT 1",
        (f"%{name}%",)
    ).fetchone()
    if not row:
        p(f"*Entity '{name}' not found.*")
        return

    ent = dict(row)
    h2(f"Entity: {ent['name']}")
    p(f"**Type:** {ent['entity_type']}  |  **Confidence:** {ent['confidence']:.0%}  |  **Created:** {ent['created_at'][:10]}")

    obs = []
    try:
        obs = json.loads(ent.get("observations") or "[]")
    except Exception:
        pass
    if obs:
        h3("Observations")
        for o in obs:
            bullet(str(o))
        blank()

    # Outgoing relations
    out_edges = db.execute(
        "SELECT ke.relation_type, et.name, et.entity_type FROM knowledge_edges ke "
        "JOIN entities et ON ke.target_id = et.id AND ke.target_table = 'entities' "
        "WHERE ke.source_table = 'entities' AND ke.source_id = ? LIMIT ?",
        (ent["id"], limit)
    ).fetchall()
    if out_edges:
        h3("Relationships (outgoing)")
        for e in out_edges:
            bullet(f"**{e['relation_type']}** → {e['name']} ({e['entity_type']})")
        blank()

    # Incoming relations
    in_edges = db.execute(
        "SELECT ke.relation_type, es.name, es.entity_type FROM knowledge_edges ke "
        "JOIN entities es ON ke.source_id = es.id AND ke.source_table = 'entities' "
        "WHERE ke.target_table = 'entities' AND ke.target_id = ? LIMIT ?",
        (ent["id"], limit)
    ).fetchall()
    if in_edges:
        h3("Relationships (incoming)")
        for e in in_edges:
            bullet(f"{e['name']} ({e['entity_type']}) **{e['relation_type']}** → this")
        blank()

    # Related memories (by name mention)
    related_mems = db.execute(
        "SELECT content, confidence, created_at FROM memories "
        "WHERE retired_at IS NULL AND content LIKE ? ORDER BY confidence DESC LIMIT ?",
        (f"%{ent['name']}%", limit)
    ).fetchall()
    if related_mems:
        h3("Related Memories")
        for m in related_mems:
            bullet(f"{m['content'][:200]} [{m['confidence']:.0%}]")
        blank()


# ---------------------------------------------------------------------------
# LINT — brain health check
# ---------------------------------------------------------------------------

def cmd_lint(args):
    """Run health checks on brain.db — find issues, suggest fixes."""
    db = get_db()
    fix = getattr(args, "fix", False)
    issues = []
    fixed = 0

    # 1. Low-confidence memories
    low_conf = db.execute(
        "SELECT id, content, confidence FROM memories WHERE retired_at IS NULL AND confidence < 0.3"
    ).fetchall()
    if low_conf:
        issues.append({
            "check": "low_confidence",
            "severity": "warning",
            "count": len(low_conf),
            "description": f"{len(low_conf)} memories with confidence < 0.3 — may be unreliable",
            "items": [{"id": r["id"], "confidence": r["confidence"],
                       "preview": r["content"][:100]} for r in low_conf[:5]],
        })

    # 2. Never-recalled memories (potentially useless)
    never_recalled = db.execute(
        "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL AND recalled_count = 0"
    ).fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
    if never_recalled > 0 and active > 0:
        pct = round(never_recalled / active * 100, 1)
        issues.append({
            "check": "never_recalled",
            "severity": "info" if pct < 50 else "warning",
            "count": never_recalled,
            "description": f"{never_recalled}/{active} memories ({pct}%) have never been recalled — potential dead weight",
        })

    # 3. Orphan entities (no edges)
    orphans = db.execute("""
        SELECT e.id, e.name, e.entity_type FROM entities e
        WHERE e.retired_at IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM knowledge_edges ke
            WHERE (ke.source_table='entities' AND ke.source_id=e.id)
               OR (ke.target_table='entities' AND ke.target_id=e.id)
        )
    """).fetchall()
    if orphans:
        issues.append({
            "check": "orphan_entities",
            "severity": "info",
            "count": len(orphans),
            "description": f"{len(orphans)} entities have no edges — isolated in the knowledge graph",
            "items": [{"id": r["id"], "name": r["name"], "type": r["entity_type"]}
                      for r in orphans[:10]],
        })

    # 4. Knowledge gaps (unresolved)
    try:
        gaps = db.execute(
            "SELECT COUNT(*) FROM knowledge_gaps WHERE resolved_at IS NULL"
        ).fetchone()[0]
        if gaps > 0:
            gap_rows = db.execute(
                "SELECT domain, gap_description FROM knowledge_gaps WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            issues.append({
                "check": "knowledge_gaps",
                "severity": "warning",
                "count": gaps,
                "description": f"{gaps} unresolved knowledge gaps detected",
                "items": [{"domain": r["domain"], "gap": r["gap_description"][:100]}
                          for r in gap_rows],
            })
    except Exception:
        pass

    # 5. Duplicate entity names (case-insensitive)
    dupes = db.execute("""
        SELECT LOWER(name) as lname, COUNT(*) as c, GROUP_CONCAT(id) as ids
        FROM entities WHERE retired_at IS NULL
        GROUP BY LOWER(name) HAVING c > 1
    """).fetchall()
    if dupes:
        issues.append({
            "check": "duplicate_entities",
            "severity": "warning",
            "count": len(dupes),
            "description": f"{len(dupes)} entity names appear more than once (case-insensitive)",
            "items": [{"name": r["lname"], "count": r["c"], "ids": r["ids"]}
                      for r in dupes[:5]],
        })
        if fix:
            # Auto-fix: retire duplicates, keep the one with highest confidence
            for d in dupes:
                ids = [int(x) for x in d["ids"].split(",")]
                rows = db.execute(
                    f"SELECT id, confidence FROM entities WHERE id IN ({','.join('?' * len(ids))}) ORDER BY confidence DESC",
                    ids
                ).fetchall()
                keep = rows[0]["id"]
                retire = [r["id"] for r in rows[1:]]
                for rid in retire:
                    db.execute("UPDATE entities SET retired_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?", (rid,))
                    fixed += 1
            db.commit()

    # 6. Stale affect data (agents not reporting)
    try:
        stale_affect = db.execute("""
            SELECT agent_id, MAX(created_at) as last_report
            FROM affect_log
            GROUP BY agent_id
            HAVING last_report < datetime('now', '-7 days')
        """).fetchall()
        if stale_affect:
            issues.append({
                "check": "stale_affect",
                "severity": "info",
                "count": len(stale_affect),
                "description": f"{len(stale_affect)} agents haven't reported affect state in 7+ days",
                "items": [{"agent": r["agent_id"], "last_report": r["last_report"]}
                          for r in stale_affect[:5]],
            })
    except Exception:
        pass

    # 7. Access log bloat
    try:
        log_count = db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        if log_count > 10000:
            issues.append({
                "check": "access_log_bloat",
                "severity": "info",
                "count": log_count,
                "description": f"Access log has {log_count:,} entries — consider running brainctl prune-log",
            })
            if fix:
                cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
                cursor = db.execute("DELETE FROM access_log WHERE created_at < ?", (cutoff,))
                db.commit()
                fixed += cursor.rowcount
    except Exception:
        pass

    # 8. DB size check
    try:
        db_size = DB_PATH.stat().st_size / 1048576
        if db_size > 100:
            issues.append({
                "check": "large_database",
                "severity": "warning",
                "count": 1,
                "description": f"Database is {db_size:.1f} MB — consider running consolidation (brainctl-consolidate sweep)",
            })
    except Exception:
        pass

    # Summary
    critical = sum(1 for i in issues if i["severity"] == "critical")
    warnings = sum(1 for i in issues if i["severity"] == "warning")
    infos = sum(1 for i in issues if i["severity"] == "info")

    result = {
        "health": "critical" if critical else "warning" if warnings else "healthy",
        "issues": len(issues),
        "critical": critical,
        "warnings": warnings,
        "info": infos,
        "fixed": fixed,
        "checks": issues,
    }

    _ofmt = getattr(args, "output", "json")
    if _ofmt == "text":
        # Human-readable text output
        print(f"Brain Health: {result['health'].upper()}")
        print(f"  {critical} critical, {warnings} warnings, {infos} info")
        if fixed:
            print(f"  {fixed} issues auto-fixed")
        print()
        for issue in issues:
            icon = "🔴" if issue["severity"] == "critical" else "🟡" if issue["severity"] == "warning" else "🔵"
            print(f"  {icon} [{issue['check']}] {issue['description']}")
            for item in issue.get("items", [])[:3]:
                print(f"      {item}")
        print()
    else:
        json_out(result)


def cmd_prune_access_log(args):
    db = get_db()
    days = args.days or 30
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor = db.execute("DELETE FROM access_log WHERE created_at < ?", (cutoff,))
    db.commit()
    json_out({"ok": True, "deleted": cursor.rowcount})

# ---------------------------------------------------------------------------
# REFLEXION commands — failure taxonomy, lesson lifecycle, cross-agent propagation
# ---------------------------------------------------------------------------

_REFLEXION_DEFAULT_CONFIDENCE = {
    "COORDINATION_FAILURE": 0.95,
    "TOOL_MISUSE": 0.80,
    "CONTEXT_LOSS": 0.75,
    "REASONING_ERROR": 0.60,
    "HALLUCINATION": 0.55,
}

_REFLEXION_DEFAULT_N = {
    "COORDINATION_FAILURE": 3,
    "CONTEXT_LOSS": 5,
    "TOOL_MISUSE": 5,
    "REASONING_ERROR": 10,
    "HALLUCINATION": 10,
}

_REFLEXION_DEFAULT_TTL = {
    "COORDINATION_FAILURE": 30,
    "CONTEXT_LOSS": 90,
    "TOOL_MISUSE": 60,
    "REASONING_ERROR": 180,
    "HALLUCINATION": 365,
}

_REFLEXION_DEFAULT_GENERALIZABLE = {
    "COORDINATION_FAILURE": ["agent_type:external"],
    "TOOL_MISUSE": ["capability:brainctl"],
    "CONTEXT_LOSS": ["scope:global"],
    "REASONING_ERROR": [],
    "HALLUCINATION": [],
}

_REFLEXION_DEFAULT_OVERRIDE = {
    "COORDINATION_FAILURE": "HARD_OVERRIDE",
    "TOOL_MISUSE": "HARD_OVERRIDE",
    "CONTEXT_LOSS": "SOFT_HINT",
    "REASONING_ERROR": "SOFT_HINT",
    "HALLUCINATION": "SOFT_HINT",
}


def cmd_reflexion_write(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    failure_class = args.failure_class.upper()
    if failure_class not in _REFLEXION_DEFAULT_CONFIDENCE:
        print(f"ERROR: Invalid failure_class '{failure_class}'", file=sys.stderr)
        sys.exit(1)
    confidence = args.confidence if args.confidence is not None else _REFLEXION_DEFAULT_CONFIDENCE[failure_class]
    override_level = args.override_level or _REFLEXION_DEFAULT_OVERRIDE[failure_class]
    expiration_policy = args.expiration_policy or "success_count"
    expiration_n = args.expiration_n if args.expiration_n is not None else _REFLEXION_DEFAULT_N[failure_class]
    expiration_ttl_days = args.expiration_ttl_days if args.expiration_ttl_days is not None else _REFLEXION_DEFAULT_TTL[failure_class]
    if args.generalizable_to:
        generalizable = json.dumps(args.generalizable_to.split(","))
    else:
        base = _REFLEXION_DEFAULT_GENERALIZABLE[failure_class][:]
        if failure_class in ("REASONING_ERROR", "HALLUCINATION"):
            base = [f"agent:{agent_id}"]
        generalizable = json.dumps(base)
    cur = db.execute(
        """INSERT INTO reflexion_lessons (
            source_agent_id, source_event_id, source_run_id,
            failure_class, failure_subclass,
            trigger_conditions, lesson_content, generalizable_to,
            confidence, override_level, status,
            expiration_policy, expiration_n, expiration_ttl_days,
            root_cause_ref
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (agent_id, args.source_event or None, args.source_run or None,
         failure_class, args.failure_subclass or None,
         args.trigger, args.lesson, generalizable,
         confidence, override_level, "active",
         expiration_policy, expiration_n, expiration_ttl_days,
         args.root_cause_ref or None)
    )
    db.commit()
    lesson_id = cur.lastrowid
    log_access(db, agent_id, "reflexion_write", "reflexion_lessons", lesson_id)
    db.commit()
    json_out({
        "ok": True, "lesson_id": lesson_id,
        "failure_class": failure_class, "confidence": confidence,
        "override_level": override_level, "expiration_policy": expiration_policy,
        "generalizable_to": json.loads(generalizable),
    })


def cmd_reflexion_list(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    where_clauses, params = [], []
    if args.failure_class:
        where_clauses.append("failure_class = ?")
        params.append(args.failure_class.upper())
    if args.status:
        where_clauses.append("status = ?")
        params.append(args.status)
    else:
        where_clauses.append("status = 'active'")
    if args.source_agent:
        where_clauses.append("source_agent_id = ?")
        params.append(args.source_agent)
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    limit = args.limit or 50
    rows = db.execute(
        f"SELECT * FROM reflexion_lessons {where} ORDER BY confidence DESC, created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    log_access(db, agent_id, "reflexion_list", "reflexion_lessons", None, None, len(rows))
    db.commit()
    json_out(rows_to_list(rows))


def cmd_reflexion_query(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    task_desc = args.task_description
    # Build OR-based FTS5 query so any matching keyword is sufficient
    _raw = _sanitize_fts_query(task_desc)
    sanitized = " OR ".join(_raw.split()) if _raw else ""
    scope_filters, scope_params = [], []
    if args.scope:
        for s in args.scope.split(","):
            scope_filters.append("generalizable_to LIKE ?")
            scope_params.append(f'%"{s.strip()}"%')
        scope_filters.append("generalizable_to LIKE '%\"scope:global\"%'")
    scope_where = ("AND (" + " OR ".join(scope_filters) + ")") if scope_filters else ""
    top_k = args.top_k or 5
    min_confidence = args.min_confidence or 0.0
    if sanitized:
        rows = db.execute(
            f"""SELECT rl.*, reflexion_lessons_fts.rank as fts_rank
            FROM reflexion_lessons_fts
            JOIN reflexion_lessons rl ON rl.id = reflexion_lessons_fts.rowid
            WHERE reflexion_lessons_fts MATCH ?
              AND rl.status = 'active' AND rl.confidence >= ?
              {scope_where}
            ORDER BY rl.confidence DESC, fts_rank LIMIT ?""",
            [sanitized, min_confidence] + scope_params + [top_k]
        ).fetchall()
    else:
        rows = db.execute(
            f"""SELECT * FROM reflexion_lessons
            WHERE status = 'active' AND confidence >= ?
              {scope_where}
            ORDER BY confidence DESC LIMIT ?""",
            [min_confidence] + scope_params + [top_k]
        ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        placeholders = ",".join("?" * len(ids))
        db.execute(
            f"UPDATE reflexion_lessons SET times_retrieved = times_retrieved + 1 WHERE id IN ({placeholders})",
            ids
        )
        db.commit()
    log_access(db, agent_id, "reflexion_query", "reflexion_lessons", None, task_desc, len(rows))
    db.commit()
    json_out(rows_to_list(rows))


def cmd_reflexion_success(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    lesson_ids = [int(x.strip()) for x in args.lesson_ids.split(",")]
    now = datetime.utcnow().isoformat()
    archived, updated = [], []
    for lid in lesson_ids:
        row = db.execute("SELECT * FROM reflexion_lessons WHERE id = ?", (lid,)).fetchone()
        if not row:
            continue
        new_successes = row["consecutive_successes"] + 1
        new_confidence = min(1.0, row["confidence"] + 0.02)
        exp_n = row["expiration_n"] or 5
        if new_successes >= exp_n and row["expiration_policy"] == "success_count":
            db.execute(
                """UPDATE reflexion_lessons SET consecutive_successes=?, confidence=?,
                   status='archived', archived_at=?, last_validated_at=?,
                   times_prevented_failure=times_prevented_failure+1 WHERE id=?""",
                (new_successes, new_confidence, now, now, lid)
            )
            archived.append(lid)
        else:
            db.execute(
                """UPDATE reflexion_lessons SET consecutive_successes=?, confidence=?,
                   last_validated_at=?, times_prevented_failure=times_prevented_failure+1 WHERE id=?""",
                (new_successes, new_confidence, now, lid)
            )
            updated.append(lid)
    db.commit()
    log_access(db, agent_id, "reflexion_success", "reflexion_lessons")
    db.commit()
    json_out({"ok": True, "updated": updated, "archived": archived})


def cmd_reflexion_failure_recurrence(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    lid = args.lesson_id
    row = db.execute("SELECT * FROM reflexion_lessons WHERE id = ?", (lid,)).fetchone()
    if not row:
        print(f"ERROR: lesson {lid} not found", file=sys.stderr)
        sys.exit(1)
    new_confidence = max(0.0, row["confidence"] - 0.15)
    db.execute(
        """UPDATE reflexion_lessons SET confidence=?, consecutive_successes=0,
           times_failed_to_prevent=times_failed_to_prevent+1 WHERE id=?""",
        (new_confidence, lid)
    )
    db.commit()
    if args.note:
        db.execute(
            "INSERT INTO events (agent_id, type, summary, tags) VALUES (?,?,?,?)",
            (agent_id, "warning",
             f"Reflexion lesson {lid} failed to prevent recurrence: {args.note}",
             json.dumps(["reflexion", "failure_recurrence", f"lesson:{lid}"]))
        )
        db.commit()
    log_access(db, agent_id, "reflexion_failure_recurrence", "reflexion_lessons", lid)
    db.commit()
    json_out({"ok": True, "lesson_id": lid, "new_confidence": new_confidence})


def cmd_reflexion_retire(args):
    db = get_db()
    agent_id = args.agent or "unknown"
    lid = args.lesson_id
    row = db.execute("SELECT * FROM reflexion_lessons WHERE id = ?", (lid,)).fetchone()
    if not row:
        print(f"ERROR: lesson {lid} not found", file=sys.stderr)
        sys.exit(1)
    now = datetime.utcnow().isoformat()
    reason = args.reason or "manual retirement"
    db.execute(
        "UPDATE reflexion_lessons SET status='retired', retired_at=?, retirement_reason=? WHERE id=?",
        (now, reason, lid)
    )
    db.commit()
    log_access(db, agent_id, "reflexion_retire", "reflexion_lessons", lid)
    db.commit()
    json_out({"ok": True, "lesson_id": lid, "retired_at": now, "reason": reason})


# ---------------------------------------------------------------------------
# POLICY commands
# ---------------------------------------------------------------------------

import uuid as _uuid_mod

_POLICY_CATEGORIES = {'routing', 'escalation', 'tone', 'retry', 'format', 'coordination', 'resource', 'general'}




# ---------------------------------------------------------------------------
# Global Workspace Broadcasting
# ---------------------------------------------------------------------------

def _ws_config(db):
    """Return workspace_config as a dict. Falls back to defaults if table missing."""
    try:
        rows = db.execute("SELECT key, value FROM workspace_config").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {
            "ignition_threshold": "0.85",
            "urgent_threshold": "0.65",
            "governor_max_per_hour": "20",
            "broadcast_ttl_hours": "48",
            "phi_window_hours": "24",
            "phi_warn_below": "0.05",
            "enabled": "1",
        }


def _ws_ignition_threshold(db):
    """Return current effective ignition threshold (adjusted for neuromod state)."""
    cfg = _ws_config(db)
    try:
        row = db.execute(
            "SELECT org_state FROM neuromodulation_state WHERE id = 1"
        ).fetchone()
        if row and row["org_state"] == "incident":
            return float(cfg.get("urgent_threshold", "0.65"))
    except Exception:
        pass
    return float(cfg.get("ignition_threshold", "0.85"))


def _ws_compute_salience(category, confidence, scope, tags_json=None):
    """Compute salience score for a memory (0.0-1.0)."""
    base = 0.0
    cat_weights = {
        "decision": 0.30, "identity": 0.30, "convention": 0.25,
        "lesson": 0.20, "preference": 0.15, "project": 0.15,
        "user": 0.10, "environment": 0.10, "integration": 0.05,
    }
    base += cat_weights.get(category, 0.10)
    base += confidence * 0.50
    if scope == "global" or not scope:
        base += 0.10
    elif scope.startswith("project:"):
        base += 0.08
    if tags_json:
        try:
            tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
            if any(t in ("critical", "incident", "urgent", "blocker") for t in (tags or [])):
                base += 0.15
        except Exception:
            pass
    return round(min(base, 1.0), 4)


# =============================================================================
# WORLD MODEL commands
# =============================================================================

def _ensure_world_model_tables(db):
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


def _world_rebuild_caps_for_agent(db, agent_id):
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

    exp_rows = db.execute(
        "SELECT domain, strength, evidence_count FROM agent_expertise WHERE agent_id=?",
        (agent_id,)
    ).fetchall()

    cap_accum = {}
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
    event_stats = {r["project"].lower(): r for r in ev_rows}

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    total_ev = sum(r["total"] for r in ev_rows)
    bad_ev = sum(r["bad"] for r in ev_rows)
    block_rate = (bad_ev / total_ev) if total_ev > 0 else 0.0
    last_active = max((r["last_active"] for r in ev_rows), default=None)

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


def cmd_world_rebuild_caps(args):
    """Rebuild agent_capabilities from event + expertise history."""
    db = get_db()
    _ensure_world_model_tables(db)

    agent_id = getattr(args, "agent_id", None)
    agent_ids = [agent_id] if agent_id else [r["id"] for r in
        db.execute("SELECT id FROM agents WHERE status='active'").fetchall()]

    results = []
    for aid in agent_ids:
        n = _world_rebuild_caps_for_agent(db, aid)
        results.append({"agent_id": aid, "capabilities_written": n})

    if getattr(args, "json", False):
        json_out({"ok": True, "agents_processed": len(results), "results": results})
    else:
        print(f"Rebuilt capabilities for {len(results)} agents.")
        for r in results:
            print(f"  {r['agent_id']}: {r['capabilities_written']} capabilities")


def cmd_world_agent(args):
    """Show world model capability profile for an agent."""
    db = get_db()
    _ensure_world_model_tables(db)

    agent_id = args.agent_id
    limit = getattr(args, "limit", None) or 20

    agent_row = db.execute(
        "SELECT id, display_name, agent_type, status FROM agents WHERE id=?",
        (agent_id,)
    ).fetchone()
    if not agent_row:
        print(f"ERROR: agent '{agent_id}' not found", file=sys.stderr)
        sys.exit(1)

    cap_rows = db.execute(
        """SELECT capability, skill_level, task_count, block_rate, last_active
           FROM agent_capabilities WHERE agent_id=?
           ORDER BY skill_level DESC LIMIT ?""",
        (agent_id, limit)
    ).fetchall()

    if not cap_rows:
        print(f"No capability data for '{agent_id}'. Run: brainctl world rebuild-caps --agent {agent_id}")
        return

    ev_summary = db.execute(
        "SELECT COUNT(*) as total, MAX(created_at) as last_event FROM events WHERE agent_id=?",
        (agent_id,)
    ).fetchone()

    if getattr(args, "json", False):
        json_out({
            "agent_id": agent_id,
            "display_name": agent_row["display_name"],
            "status": agent_row["status"],
            "total_events": ev_summary["total"] if ev_summary else 0,
            "last_event": ev_summary["last_event"] if ev_summary else None,
            "capabilities": rows_to_list(cap_rows),
        })
        return

    print(f"Agent:   {agent_row['display_name']} ({agent_id})")
    print(f"Status:  {agent_row['status']}")
    if ev_summary:
        last = _age_str(ev_summary["last_event"]) if ev_summary["last_event"] else "never"
        print(f"Events:  {ev_summary['total']} total — last: {last}")
    print()
    print(f"{'Capability':<28} {'Skill':>6}  {'Tasks':>6}  {'BlockRate':>9}  Last Active")
    print("-" * 72)
    for r in cap_rows:
        last = (r["last_active"] or "")[:10]
        br = f"{r['block_rate']:.1%}" if r["block_rate"] is not None else "  n/a"
        print(f"  {r['capability']:<26} {r['skill_level']:>6.3f}  {r['task_count']:>6}  {br:>9}  {last}")


def cmd_world_project(args):
    """Show project dynamics — velocity, agent activity, event breakdown."""
    db = get_db()

    project = args.project
    days = getattr(args, "days", None) or 14
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    ev_rows = db.execute(
        """SELECT agent_id, event_type, importance, summary, created_at
           FROM events WHERE project LIKE ? AND created_at >= ?
           ORDER BY created_at DESC""",
        (f"%{project}%", cutoff)
    ).fetchall()

    if not ev_rows:
        print(f"No events found for project matching '{project}' in last {days} days.")
        return

    agent_set = {}
    type_counts = Counter()
    daily_counts = Counter()
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

    if getattr(args, "json", False):
        json_out({
            "project": project,
            "window_days": days,
            "total_events": total,
            "velocity_per_day": round(velocity, 2),
            "avg_importance": round(avg_importance, 3),
            "error_block_rate": round(block_rate, 3),
            "event_type_counts": dict(type_counts.most_common()),
            "active_agents": [{"agent_id": a, "event_count": c} for a, c in active_agents],
            "daily_activity": dict(sorted(daily_counts.items())),
        })
        return

    print(f"Project:         {project}")
    print(f"Window:          last {days} days")
    print(f"Total events:    {total}")
    print(f"Velocity:        {velocity:.1f} events/day")
    print(f"Avg importance:  {avg_importance:.3f}")
    print(f"Error/warn rate: {block_rate:.1%}")
    print()
    print("Event types:")
    for et, cnt in type_counts.most_common():
        print(f"  {et:<20} {cnt:>4}  {'#' * min(cnt, 40)}")
    print()
    print("Active agents:")
    for aid, cnt in active_agents[:10]:
        print(f"  {aid:<35} {cnt:>4} events")
    print()
    print("Daily activity:")
    for day in sorted(daily_counts.keys())[-14:]:
        print(f"  {day}  {'#' * min(daily_counts[day], 50)} ({daily_counts[day]})")


def cmd_world_status(args):
    """Generate compressed org snapshot — the core World Model output."""
    db = get_db()
    _ensure_world_model_tables(db)

    days = getattr(args, "days", None) or 7
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

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if getattr(args, "json", False):
        json_out({
            "snapshot_at": now,
            "window_days": days,
            "org_state": org_state,
            "active_agents": rows_to_list(agent_activity),
            "project_dynamics": rows_to_list(project_activity),
            "capability_hotspots": rows_to_list(top_caps),
            "capability_gaps": rows_to_list(gaps),
            "memory_health": row_to_dict(mem_stats),
            "highlights": rows_to_list(highlights),
        })
        return

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║  brainctl — Organizational World Model Snapshot                   ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"  Generated:  {now}")
    print(f"  Window:     last {days} days")
    print(f"  Org state:  {org_state.upper()}")
    print()

    print("─── Active Agents ──────────────────────────────────────────────────")
    if agent_activity:
        for r in agent_activity:
            bar = "█" * min(r["event_count"], 20)
            print(f"  {r['agent_id']:<35} {r['event_count']:>4} ev  [{_age_str(r['last_active'])}]  {bar}")
    else:
        print("  (no activity in window)")
    print()

    print("─── Project Dynamics ───────────────────────────────────────────────")
    if project_activity:
        for r in project_activity:
            err_pct = (r["errors"] / r["events"] * 100) if r["events"] else 0
            print(f"  {r['project']:<28}  {r['events']:>4}ev  {r['agent_count']:>2}ag  "
                  f"err:{err_pct:>4.1f}%  [{_age_str(r['last_active'])}]")
    else:
        print("  (no project activity in window)")
    print()

    print("─── Capability Hotspots ────────────────────────────────────────────")
    if top_caps:
        for r in top_caps:
            print(f"  {r['capability']:<28}  agents:{r['agent_count']:>2}  "
                  f"skill:{r['avg_skill']:.3f}  tasks:{r['total_tasks']:>4}")
    else:
        print("  (run: brainctl world rebuild-caps  to populate)")
    print()

    if gaps:
        print("─── Capability Gaps ────────────────────────────────────────────────")
        for r in gaps:
            print(f"  {r['capability']:<28}  agents:{r['agent_count']:>2}  "
                  f"skill:{r['avg_skill']:.3f}  ⚠ LOW COVERAGE")
        print()

    if mem_stats:
        print("─── Memory Health ──────────────────────────────────────────────────")
        conf = mem_stats["avg_confidence"] or 0.0
        print(f"  Active memories: {mem_stats['active']} / {mem_stats['total']}   "
              f"avg confidence: {conf:.3f}")
        print()

    if highlights:
        print("─── High-Importance Events ─────────────────────────────────────────")
        for r in highlights:
            proj = f"[{r['project']}] " if r["project"] else ""
            print(f"  {r['agent_id']:<20}  {r['event_type']:<16}  "
                  f"{proj}{(r['summary'] or '')[:60]}")
            print(f"  {'':20}  importance:{r['importance']:.2f}  {_age_str(r['created_at'])}")
        print()

    print("─── Commands ───────────────────────────────────────────────────────")
    print("  brainctl world project <name>    — per-project dynamics")
    print("  brainctl world agent <id>        — per-agent capability profile")
    print("  brainctl world rebuild-caps      — refresh capability data")


def cmd_world_predict(args):
    """Log a world model prediction for later calibration."""
    db = get_db()
    _ensure_world_model_tables(db)
    agent_id = getattr(args, "author", None) or os.environ.get("AGENT_ID", "unknown")
    row_id = db.execute(
        """INSERT INTO world_model_snapshots
           (snapshot_type, subject_id, subject_type, predicted_state, author_agent_id)
           VALUES ('prediction', ?, ?, ?, ?)""",
        (args.subject, getattr(args, "subject_type", None) or "task", args.predicted, agent_id)
    ).lastrowid
    db.commit()
    json_out({"ok": True, "snapshot_id": row_id, "subject": args.subject})


def cmd_world_resolve(args):
    """Resolve a world model prediction with actual outcome."""
    db = get_db()
    _ensure_world_model_tables(db)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "UPDATE world_model_snapshots SET actual_state=?, prediction_error=?, resolved_at=? WHERE id=?",
        (args.actual, getattr(args, "error", None), now_str, args.snapshot_id)
    )
    db.commit()
    if db.execute("SELECT changes()").fetchone()[0] == 0:
        print(f"ERROR: snapshot {args.snapshot_id} not found", file=sys.stderr)
        sys.exit(1)
    json_out({"ok": True, "snapshot_id": args.snapshot_id, "resolved_at": now_str})


def cmd_workspace_status(args):
    """Show current global workspace - broadcasts active right now."""
    db = get_db()
    cfg = _ws_config(db)
    threshold = _ws_ignition_threshold(db)
    n = getattr(args, "n", 20) or 20
    scope = getattr(args, "scope", None)
    ttl_hours = int(cfg.get("broadcast_ttl_hours", 48))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    sql = """
        SELECT wb.id, wb.memory_id, wb.agent_id, wb.salience, wb.summary,
               wb.target_scope, wb.broadcast_at, wb.ack_count, wb.triggered_by,
               m.category, m.confidence, m.scope as mem_scope
        FROM workspace_broadcasts wb
        JOIN memories m ON wb.memory_id = m.id
        WHERE wb.broadcast_at >= ?
    """
    params = [cutoff]
    if scope:
        sql += " AND wb.target_scope LIKE ?"
        params.append(f"{scope}%")
    sql += " ORDER BY wb.salience DESC, wb.broadcast_at DESC LIMIT ?"
    params.append(n)
    rows = db.execute(sql, params).fetchall()
    results = rows_to_list(rows)
    for r in results:
        r["age"] = _age_str(r.get("broadcast_at"))
    try:
        nm_row = db.execute("SELECT org_state FROM neuromodulation_state WHERE id=1").fetchone()
        org_state = nm_row["org_state"] if nm_row else "normal"
    except Exception:
        org_state = "unknown"
    json_out({
        "active_broadcasts": len(results),
        "ignition_threshold": threshold,
        "org_state": org_state,
        "broadcasts": results,
    })


def cmd_workspace_history(args):
    """Show recent broadcast history (all time, paginated)."""
    db = get_db()
    n = getattr(args, "n", 30) or 30
    since_id = getattr(args, "since", None)
    agent = getattr(args, "agent", None)
    sql = """
        SELECT wb.id, wb.memory_id, wb.agent_id, wb.salience, wb.summary,
               wb.target_scope, wb.broadcast_at, wb.ack_count, wb.triggered_by,
               m.category
        FROM workspace_broadcasts wb
        JOIN memories m ON wb.memory_id = m.id
        WHERE 1=1
    """
    params = []
    if since_id is not None:
        sql += " AND wb.id > ?"
        params.append(since_id)
    if agent:
        sql += " AND wb.agent_id = ?"
        params.append(agent)
    sql += " ORDER BY wb.id DESC LIMIT ?"
    params.append(n)
    rows = db.execute(sql, params).fetchall()
    results = list(reversed(rows_to_list(rows)))
    for r in results:
        r["age"] = _age_str(r.get("broadcast_at"))
    json_out(results)


def cmd_workspace_broadcast(args):
    """Manually broadcast a memory into the global workspace."""
    db = get_db()
    agent_id = getattr(args, "agent", None) or "manual"
    memory_id = args.memory_id
    summary = getattr(args, "summary", None)
    scope = getattr(args, "scope", "global")
    row = db.execute(
        "SELECT id, content, confidence, category, scope, tags FROM memories WHERE id = ? AND retired_at IS NULL",
        (memory_id,)
    ).fetchone()
    if not row:
        json_out({"error": f"Memory {memory_id} not found or retired"})
        return
    salience = _ws_compute_salience(row["category"], row["confidence"], row["scope"], row["tags"])
    if not summary:
        summary = str(row["content"])[:200]
    db.execute(
        "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) VALUES (?,?,?,?,?,?)",
        (memory_id, agent_id, salience, summary, scope, "manual")
    )
    db.commit()
    broadcast_id = db.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    json_out({"ok": True, "broadcast_id": broadcast_id, "salience": salience, "scope": scope})


def cmd_workspace_ack(args):
    """Acknowledge receipt of a broadcast."""
    db = get_db()
    agent_id = getattr(args, "agent", None) or "unknown"
    broadcast_id = args.broadcast_id
    try:
        db.execute(
            "INSERT INTO workspace_acks (broadcast_id, agent_id) VALUES (?,?)",
            (broadcast_id, agent_id)
        )
        db.commit()
        json_out({"ok": True, "broadcast_id": broadcast_id, "agent_id": agent_id})
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            json_out({"ok": True, "already_acked": True})
        else:
            json_out({"error": str(e)})


def cmd_workspace_phi(args):
    """Compute and display the organizational integration (Phi) metric."""
    db = get_db()
    cfg = _ws_config(db)
    window_hours = int(cfg.get("phi_window_hours", 24))
    phi_warn = float(cfg.get("phi_warn_below", 0.05))
    breakdown = getattr(args, "breakdown", False)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    agent_rows = db.execute(
        "SELECT agent_id, COUNT(*) as cnt FROM workspace_broadcasts WHERE broadcast_at >= ? GROUP BY agent_id",
        (cutoff,)
    ).fetchall()
    total_broadcasts = sum(r["cnt"] for r in agent_rows)
    total_acks = db.execute(
        "SELECT COUNT(*) FROM workspace_acks wa JOIN workspace_broadcasts wb ON wa.broadcast_id = wb.id WHERE wb.broadcast_at >= ?",
        (cutoff,)
    ).fetchone()[0]
    ack_rate = round(total_acks / total_broadcasts, 4) if total_broadcasts > 0 else 0.0
    active_agents = len(agent_rows)
    phi_org = ack_rate
    result = {
        "phi_org": phi_org,
        "ack_rate": ack_rate,
        "total_broadcasts": total_broadcasts,
        "total_acks": total_acks,
        "active_agents": active_agents,
        "window_hours": window_hours,
        "warn": phi_org < phi_warn and total_broadcasts > 0,
        "warn_threshold": phi_warn,
    }
    if breakdown:
        result["agent_breakdown"] = rows_to_list(agent_rows)
    window_start = cutoff
    window_end = _now_ts()
    db.execute(
        "INSERT INTO workspace_phi (window_start, window_end, phi_org, broadcast_count, ack_rate, agent_pair_count) VALUES (?,?,?,?,?,?)",
        (window_start, window_end, phi_org, total_broadcasts, ack_rate, active_agents)
    )
    db.commit()
    json_out(result)


def cmd_workspace_config_cmd(args):
    """Get or set workspace configuration values."""
    db = get_db()
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)
    if key and value is not None:
        db.execute(
            "INSERT OR REPLACE INTO workspace_config (key, value, updated_at) VALUES (?,?,?)",
            (key, str(value), _now_ts())
        )
        db.commit()
        json_out({"ok": True, "key": key, "value": value})
    elif key:
        row = db.execute("SELECT key, value, updated_at FROM workspace_config WHERE key=?", (key,)).fetchone()
        json_out(dict(row) if row else {"error": f"key '{key}' not found"})
    else:
        rows = db.execute("SELECT key, value, updated_at FROM workspace_config ORDER BY key").fetchall()
        json_out(rows_to_list(rows))


def cmd_workspace_ingest(args):
    """Score recent memories for ignition and broadcast any above threshold."""
    db = get_db()
    threshold = _ws_ignition_threshold(db)
    agent_id = getattr(args, "agent", None) or "workspace-ingest"
    lookback_hours = getattr(args, "hours", 1) or 1
    dry_run = getattr(args, "dry_run", False)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.execute("""
        SELECT m.id, m.category, m.confidence, m.scope, m.content, m.tags
        FROM memories m
        WHERE m.created_at >= ?
          AND m.retired_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM workspace_broadcasts wb WHERE wb.memory_id = m.id)
        ORDER BY m.confidence DESC
        LIMIT 50
    """, (cutoff,)).fetchall()
    fired = []
    for row in rows:
        salience = _ws_compute_salience(row["category"], row["confidence"], row["scope"], row["tags"])
        if salience >= threshold:
            fired.append({"memory_id": row["id"], "salience": salience, "scope": row["scope"]})
            if not dry_run:
                db.execute(
                    "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) VALUES (?,?,?,?,?,?)",
                    (row["id"], agent_id, salience, str(row["content"])[:200], row["scope"] or "global", "ingest")
                )
    if not dry_run and fired:
        db.commit()
    json_out({
        "scanned": len(rows),
        "ignited": len(fired),
        "threshold": threshold,
        "dry_run": dry_run,
        "broadcasts": fired,
    })

def _ensure_policy_tables(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS policy_memories (
            policy_id               TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            category                TEXT NOT NULL DEFAULT 'general',
            status                  TEXT NOT NULL DEFAULT 'active',
            scope                   TEXT NOT NULL DEFAULT 'global',
            priority                INTEGER NOT NULL DEFAULT 50,
            trigger_condition       TEXT NOT NULL,
            action_directive        TEXT NOT NULL,
            authored_by             TEXT NOT NULL DEFAULT 'unknown',
            derived_from            TEXT,
            confidence_threshold    REAL NOT NULL DEFAULT 0.5,
            wisdom_half_life_days   INTEGER NOT NULL DEFAULT 30,
            version                 INTEGER NOT NULL DEFAULT 1,
            active_since            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            last_validated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            expires_at              TEXT,
            feedback_count          INTEGER NOT NULL DEFAULT 0,
            success_count           INTEGER NOT NULL DEFAULT 0,
            failure_count           INTEGER NOT NULL DEFAULT 0,
            created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_pm_status_category ON policy_memories(status, category)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pm_scope ON policy_memories(scope)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pm_confidence ON policy_memories(confidence_threshold DESC)")
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS policy_memories_fts USING fts5(
            trigger_condition, action_directive, name,
            content=policy_memories, content_rowid=rowid
        )
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS pm_fts_insert AFTER INSERT ON policy_memories BEGIN
            INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
            VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS pm_fts_update AFTER UPDATE ON policy_memories BEGIN
            INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
            VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
            INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
            VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS pm_fts_delete AFTER DELETE ON policy_memories BEGIN
            INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
            VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
        END
    """)
    db.commit()


def _policy_effective_confidence(confidence, half_life_days, last_validated_at):
    try:
        from datetime import datetime as _dt
        validated = _dt.fromisoformat(last_validated_at)
        age_days = (_dt.utcnow() - validated).days
        if half_life_days <= 0:
            return confidence
        decay = 0.5 ** (age_days / half_life_days)
        return confidence * decay
    except Exception:
        return confidence


def cmd_policy_match(args):
    db = get_db()
    _ensure_policy_tables(db)
    agent_id = args.agent or "unknown"
    context = args.context
    staleness_mode = args.staleness_mode or "warn"
    now_str = datetime.utcnow().isoformat()

    # Neuromod mode: surface ALL policies for scope when org_state is incident/urgent
    org_state = _neuromod_org_state(db)
    neuromod_active = getattr(args, 'all', False) or org_state in ('incident', 'sprint')
    top_k = 9999 if neuromod_active else (args.top_k or 3)
    min_conf = 0.0 if neuromod_active else (args.min_confidence if args.min_confidence is not None else 0.4)

    base_where = "status = 'active' AND (expires_at IS NULL OR expires_at > ?)"
    base_params = [now_str]

    if args.category:
        base_where += " AND category = ?"
        base_params.append(args.category)

    if args.scope:
        base_where += " AND (scope = 'global' OR scope = ?)"
        base_params.append(args.scope)

    fts_rows = []
    try:
        fts_query = " OR ".join(w for w in context.split() if len(w) > 3)
        if fts_query:
            fts_rows = db.execute(
                f"""SELECT pm.*, pmf.rank as fts_rank
                    FROM policy_memories_fts pmf
                    JOIN policy_memories pm ON pm.rowid = pmf.rowid
                    WHERE pmf MATCH ? AND {base_where}
                    ORDER BY pmf.rank
                    LIMIT ?""",
                [fts_query] + base_params + [top_k * 2]
            ).fetchall()
    except Exception:
        fts_rows = []

    if not fts_rows:
        fts_rows = db.execute(
            f"SELECT *, NULL as fts_rank FROM policy_memories WHERE {base_where} ORDER BY priority DESC, confidence_threshold DESC LIMIT ?",
            base_params + [top_k * 2]
        ).fetchall()

    results = []
    stale_warnings = []
    for row in fts_rows:
        r = dict(row)
        eff_conf = _policy_effective_confidence(
            r['confidence_threshold'], r['wisdom_half_life_days'], r['last_validated_at']
        )
        r['confidence_effective'] = round(eff_conf, 4)
        if eff_conf < min_conf:
            if staleness_mode == "warn":
                r['staleness_warning'] = True
                stale_warnings.append(r)
            continue
        r['staleness_warning'] = eff_conf < r['confidence_threshold'] * 0.8
        results.append(r)

    results = sorted(results, key=lambda x: (x['priority'], x['confidence_effective']), reverse=True)[:top_k]
    log_access(db, agent_id, "policy_match", "policy_memories", None, context, len(results))
    db.commit()

    if args.format == "json":
        json_out({"policies": results, "stale_excluded": stale_warnings, "query": context})
        return

    if not results:
        print(f"No matching policies for: {context!r}")
        if stale_warnings:
            print(f"  ({len(stale_warnings)} stale policies excluded — use --staleness-mode ignore to include)")
        return

    neuromod_note = f"  [NEUROMOD: {org_state.upper()} — all policies surfaced]" if neuromod_active else ""
    print(f"\nPolicy Match Results ({len(results)} found){neuromod_note}:\n")
    for i, r in enumerate(results, 1):
        stale_flag = " [STALE WARNING]" if r.get('staleness_warning') else ""
        total = r['success_count'] + r['failure_count']
        sr = f"{r['success_count']}/{total}" if total else "no data"
        print(f"[{i}] {r['name']}  [confidence: {r['confidence_effective']:.2f}]  [category: {r['category']}]{stale_flag}")
        print(f"    Trigger:   {r['trigger_condition']}")
        print(f"    Directive: {r['action_directive']}")
        print(f"    Success rate: {sr}  |  Last validated: {r['last_validated_at'][:10]}")
        print()


def cmd_policy_add(args):
    db = get_db()
    _ensure_policy_tables(db)
    agent_id = args.agent or "unknown"
    policy_id = f"pol_{_uuid_mod.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()

    db.execute(
        """INSERT INTO policy_memories
           (policy_id, name, category, scope, priority, trigger_condition, action_directive,
            authored_by, derived_from, confidence_threshold, wisdom_half_life_days,
            active_since, last_validated_at, expires_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            policy_id,
            args.name,
            args.category or "general",
            args.scope or "global",
            args.priority or 50,
            args.trigger,
            args.directive,
            agent_id,
            args.derived_from or None,
            args.confidence if args.confidence is not None else 0.5,
            args.half_life or 30,
            now, now,
            args.expires_at or None,
            now, now,
        )
    )
    db.commit()
    log_access(db, agent_id, "policy_add", "policy_memories", policy_id)
    db.commit()
    json_out({"ok": True, "policy_id": policy_id, "name": args.name, "created_at": now})


def cmd_policy_feedback(args):
    db = get_db()
    _ensure_policy_tables(db)
    agent_id = args.agent or "unknown"
    pid = args.policy_id
    now = datetime.utcnow().isoformat()

    row = db.execute(
        "SELECT * FROM policy_memories WHERE policy_id = ? OR name = ?", (pid, pid)
    ).fetchone()
    if not row:
        print(f"ERROR: Policy not found: {pid}", file=sys.stderr)
        sys.exit(1)

    row = dict(row)
    old_conf = row['confidence_threshold']

    if args.success:
        delta = args.boost or 0.02
        new_conf = min(1.0, old_conf + delta)
        sc_delta, fc_delta = 1, 0
        outcome = "success"
    elif args.failure:
        new_conf = max(0.1, old_conf - 0.05)
        sc_delta, fc_delta = 0, 1
        outcome = "failure"
    else:
        print("ERROR: Specify --success or --failure", file=sys.stderr)
        sys.exit(1)

    new_feedback_count = row['feedback_count'] + 1
    new_failure_count = row['failure_count'] + fc_delta
    new_success_count = row['success_count'] + sc_delta

    # Auto-flag for review if >50% failure rate with ≥5 feedback events
    total_feedback = new_feedback_count
    stale_flagged = False
    if total_feedback >= 5 and new_failure_count / total_feedback > 0.5:
        stale_flagged = True

    db.execute(
        """UPDATE policy_memories SET
           confidence_threshold = ?,
           success_count = success_count + ?,
           failure_count = failure_count + ?,
           feedback_count = feedback_count + 1,
           last_validated_at = ?,
           updated_at = ?
           WHERE policy_id = ?""",
        (new_conf, sc_delta, fc_delta, now, now, row['policy_id'])
    )
    db.commit()
    log_access(db, agent_id, f"policy_feedback_{outcome}", "policy_memories", row['policy_id'])
    db.commit()
    result = {
        "ok": True,
        "policy_id": row['policy_id'],
        "name": row['name'],
        "outcome": outcome,
        "confidence_before": round(old_conf, 4),
        "confidence_after": round(new_conf, 4),
        "feedback_count": new_feedback_count,
        "notes": args.notes or None,
    }
    if stale_flagged:
        result["stale_warning"] = f"Policy failure rate > 50% over {new_feedback_count} events — flagged for review"
    json_out(result)


def _neuromod_org_state(db):
    """Return the current org_state from neuromodulation_state if the table exists, else 'normal'."""
    try:
        row = db.execute("SELECT org_state FROM neuromodulation_state WHERE id=1").fetchone()
        return row["org_state"] if row else "normal"
    except Exception:
        return "normal"


def cmd_policy_list(args):
    db = get_db()
    _ensure_policy_tables(db)
    agent_id = args.agent or "unknown"
    now_str = datetime.utcnow().isoformat()

    where = "1=1"
    params = []

    status_filter = args.status or "active"
    if status_filter != "all":
        where += " AND status = ?"
        params.append(status_filter)

    if args.category:
        where += " AND category = ?"
        params.append(args.category)

    if args.scope:
        where += " AND (scope = 'global' OR scope = ?)"
        params.append(args.scope)

    rows = db.execute(
        f"SELECT * FROM policy_memories WHERE {where} ORDER BY priority DESC, confidence_threshold DESC",
        params
    ).fetchall()

    results = []
    flagged = []
    for row in rows:
        r = dict(row)
        eff_conf = _policy_effective_confidence(
            r['confidence_threshold'], r['wisdom_half_life_days'], r['last_validated_at']
        )
        r['confidence_effective'] = round(eff_conf, 4)
        total = r['success_count'] + r['failure_count']
        r['failure_rate'] = round(r['failure_count'] / total, 3) if total >= 5 else None
        r['stale'] = r['failure_rate'] is not None and r['failure_rate'] > 0.5
        if r['stale']:
            flagged.append(r)
        results.append(r)

    log_access(db, agent_id, "policy_list", "policy_memories", None, status_filter, len(results))
    db.commit()

    if args.format == "json":
        json_out({"policies": results, "stale_flagged": [r['policy_id'] for r in flagged]})
        return

    if not results:
        print(f"No policies found (status={status_filter})")
        return

    print(f"\nPolicies ({len(results)} total):\n")
    for r in results:
        total = r['success_count'] + r['failure_count']
        sr = f"{r['success_count']}/{total}" if total else "no data"
        stale_flag = "  [!! STALE — HIGH FAILURE RATE]" if r['stale'] else ""
        expired_flag = "  [EXPIRED]" if (r['expires_at'] and r['expires_at'] < now_str) else ""
        print(f"  {r['name']}  [{r['status']}]  [conf: {r['confidence_effective']:.2f}]  [cat: {r['category']}]  [scope: {r['scope']}]{stale_flag}{expired_flag}")
        print(f"    Trigger:   {r['trigger_condition'][:120]}")
        print(f"    Directive: {r['action_directive'][:120]}")
        print(f"    Success rate: {sr}  |  Last validated: {r['last_validated_at'][:10]}  |  Priority: {r['priority']}")
        print()

    if flagged:
        print(f"⚠  {len(flagged)} policy/policies flagged for review (>50% failure rate with ≥5 feedback events):")
        for r in flagged:
            print(f"   - {r['name']}  (failure_rate={r['failure_rate']:.0%})")
        print()



# ---------------------------------------------------------------------------
# THEORY OF MIND — Agent Mental Models
# Tables: agent_beliefs, belief_conflicts, agent_perspective_models, agent_bdi_state
# ---------------------------------------------------------------------------

_STALE_HOURS = 24  # beliefs older than this are considered stale


def _tom_tables_exist(db) -> bool:
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    return "agent_beliefs" in tables


def _require_tom(db):
    if not _tom_tables_exist(db):
        print("ERROR: Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.", file=sys.stderr)
        sys.exit(1)


def _tom_compute_bdi(db, agent_id: str) -> dict:
    """Compute BDI snapshot components for an agent. Returns dict for upsert."""
    now_iso = datetime.now(timezone.utc).isoformat()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    active_beliefs = db.execute(
        "SELECT id, topic, belief_content, confidence, is_assumption, last_updated_at "
        "FROM agent_beliefs WHERE agent_id=? AND invalidated_at IS NULL",
        (agent_id,)
    ).fetchall()

    active_count = len(active_beliefs)
    stale_count = sum(1 for b in active_beliefs if (b["last_updated_at"] or "") < stale_cutoff)
    assumption_count = sum(1 for b in active_beliefs if b["is_assumption"])
    conflict_count = db.execute(
        "SELECT count(*) as cnt FROM belief_conflicts "
        "WHERE (agent_a_id=? OR agent_b_id=?) AND resolved_at IS NULL",
        (agent_id, agent_id)
    ).fetchone()["cnt"]
    key_topics = [b["topic"] for b in active_beliefs[:10]]

    beliefs_summary = json.dumps({
        "active_belief_count": active_count,
        "stale_belief_count": stale_count,
        "assumption_count": assumption_count,
        "conflict_count": conflict_count,
        "key_topics": key_topics,
    })

    task_rows = db.execute(
        "SELECT id, external_id, title, priority, status FROM tasks "
        "WHERE assigned_agent_id=? AND status IN ('pending','in_progress') "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END LIMIT 20",
        (agent_id,)
    ).fetchall()
    primary = task_rows[0] if task_rows else None
    desires_summary = json.dumps({
        "active_task_count": len(task_rows),
        "primary_goal": primary["title"] if primary else None,
        "priority": primary["priority"] if primary else None,
        "task_ids": [(r["external_id"] or str(r["id"])) for r in task_rows],
    })

    inprog = [r for r in task_rows if r["status"] == "in_progress"]
    recent_events = db.execute(
        "SELECT summary FROM events WHERE agent_id=? ORDER BY created_at DESC LIMIT 5",
        (agent_id,)
    ).fetchall()
    intentions_summary = json.dumps({
        "in_progress_tasks": [(r["external_id"] or str(r["id"])) for r in inprog],
        "committed_actions": [r["summary"][:80] for r in recent_events],
    })

    if task_rows:
        covered = 0
        for t in task_rows:
            topic_key = f"task:{t['external_id'] or t['id']}:status"
            hit = db.execute(
                "SELECT 1 FROM agent_beliefs WHERE agent_id=? AND topic=? AND invalidated_at IS NULL",
                (agent_id, topic_key)
            ).fetchone()
            if hit:
                covered += 1
        knowledge_coverage_score = covered / len(task_rows)
    else:
        knowledge_coverage_score = 1.0

    belief_staleness_score = (stale_count / active_count) if active_count > 0 else 0.0

    cr_row = db.execute(
        "SELECT MAX(confusion_risk) as max_cr FROM agent_perspective_models "
        "WHERE subject_agent_id=?",
        (agent_id,)
    ).fetchone()
    confusion_risk_score = cr_row["max_cr"] if cr_row and cr_row["max_cr"] is not None else 0.0

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


def cmd_tom_update(args):
    """Refresh BDI state snapshot for one or all agents."""
    db = get_db()
    _require_tom(db)

    agent_id = getattr(args, "agent_id", None)
    agent_ids = [agent_id] if agent_id else [
        r["id"] for r in db.execute("SELECT id FROM agents WHERE status='active'").fetchall()
    ]

    results = []
    for aid in agent_ids:
        bdi = _tom_compute_bdi(db, aid)
        db.execute(
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
            bdi
        )
        db.commit()
        results.append(bdi)
        if not getattr(args, "json", False):
            bs = json.loads(bdi["beliefs_summary"])
            print(f"  {aid}: beliefs={bs['active_belief_count']} stale={bs['stale_belief_count']} "
                  f"coverage={bdi['knowledge_coverage_score']:.2f} "
                  f"confusion={bdi['confusion_risk_score']:.2f}")

    if getattr(args, "json", False):
        json_out({"ok": True, "agents_updated": len(results), "results": results})
    elif not getattr(args, "quiet", False):
        print(f"\nDone. {len(results)} agent(s) updated.")


def cmd_tom_belief_set(args):
    """Record or update a belief for an agent."""
    db = get_db()
    _require_tom(db)

    agent_id = args.agent_id
    topic = args.topic
    content = args.content
    is_assumption = 1 if getattr(args, "assumption", False) else 0
    confidence = getattr(args, "confidence", None) or 1.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    existing = db.execute(
        "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?",
        (agent_id, topic)
    ).fetchone()

    if existing:
        db.execute(
            """UPDATE agent_beliefs SET
               belief_content=?, confidence=?, is_assumption=?,
               last_updated_at=?, invalidated_at=NULL, invalidation_reason=NULL, updated_at=?
               WHERE agent_id=? AND topic=?""",
            (content, confidence, is_assumption, now, now, agent_id, topic)
        )
        action = "updated"
    else:
        db.execute(
            """INSERT INTO agent_beliefs
               (agent_id, topic, belief_content, confidence, is_assumption,
                last_updated_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (agent_id, topic, content, confidence, is_assumption, now, now, now)
        )
        action = "created"

    db.commit()
    log_access(db, agent_id, f"belief_{action}", "agent_beliefs", None, topic)
    db.commit()

    if getattr(args, "json", False):
        json_out({"ok": True, "action": action, "agent_id": agent_id, "topic": topic})
    else:
        print(f"Belief {action}: [{agent_id}] {topic}")
        print(f"  Content: {content[:100]}")
        if is_assumption:
            print("  (marked as assumption)")


def cmd_tom_belief_invalidate(args):
    """Mark a belief as invalid and create a conflict record."""
    db = get_db()
    _require_tom(db)

    agent_id = args.agent_id
    topic = args.topic
    reason = args.reason
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    row = db.execute(
        "SELECT id, belief_content FROM agent_beliefs "
        "WHERE agent_id=? AND topic=? AND invalidated_at IS NULL",
        (agent_id, topic)
    ).fetchone()
    if not row:
        print(f"ERROR: No active belief for agent '{agent_id}' on topic '{topic}'", file=sys.stderr)
        sys.exit(1)

    db.execute(
        "UPDATE agent_beliefs SET invalidated_at=?, invalidation_reason=?, updated_at=? "
        "WHERE agent_id=? AND topic=?",
        (now, reason, now, agent_id, topic)
    )

    existing_conflict = db.execute(
        "SELECT id FROM belief_conflicts WHERE agent_a_id=? AND topic=? AND resolved_at IS NULL",
        (agent_id, topic)
    ).fetchone()
    if not existing_conflict:
        db.execute(
            """INSERT INTO belief_conflicts
               (topic, agent_a_id, agent_b_id, belief_a, belief_b,
                conflict_type, severity, detected_at, requires_supervisor_intervention)
               VALUES (?,?,NULL,?,?,?,?,?,?)""",
            (topic, agent_id, row["belief_content"], f"Invalidated: {reason}",
             "staleness", 0.6, now, 1)
        )
    db.commit()

    if getattr(args, "json", False):
        json_out({"ok": True, "agent_id": agent_id, "topic": topic, "reason": reason})
    else:
        print(f"Belief invalidated: [{agent_id}] {topic}")
        print(f"  Reason: {reason}")
        print(f"  Old belief: {row['belief_content'][:100]}")


def cmd_tom_conflicts_list(args):
    """List open belief conflicts sorted by severity."""
    db = get_db()
    _require_tom(db)

    agent_filter = getattr(args, "agent", None)
    topic_filter = getattr(args, "topic", None)
    min_severity = getattr(args, "severity", None) or 0.0
    limit = getattr(args, "limit", None) or 50

    q = (
        "SELECT bc.id, bc.topic, bc.agent_a_id, bc.agent_b_id, "
        "bc.belief_a, bc.belief_b, bc.conflict_type, bc.severity, "
        "bc.detected_at, bc.requires_supervisor_intervention "
        "FROM belief_conflicts bc "
        "WHERE bc.resolved_at IS NULL AND bc.severity >= ?"
    )
    params = [min_severity]

    if agent_filter:
        q += " AND (bc.agent_a_id=? OR bc.agent_b_id=?)"
        params += [agent_filter, agent_filter]
    if topic_filter:
        q += " AND bc.topic LIKE ?"
        params.append(f"%{topic_filter}%")

    q += " ORDER BY bc.severity DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(q, params).fetchall()

    if getattr(args, "json", False):
        json_out({"open_conflicts": len(rows), "conflicts": rows_to_list(rows)})
        return

    if not rows:
        print("No open belief conflicts.")
        return

    print(f"Open belief conflicts ({len(rows)}):")
    print(f"  {'ID':>4}  {'Sev':>5}  {'Type':<12}  {'Topic':<35}  {'Agent A':<25}  Super?")
    print("  " + "-" * 100)
    for r in rows:
        super_flag = "!!" if r["requires_supervisor_intervention"] else "  "
        print(f"  {r['id']:>4}  {r['severity']:>5.2f}  {r['conflict_type']:<12}  "
              f"{r['topic'][:35]:<35}  {r['agent_a_id'][:25]:<25}  {super_flag}")
        print(f"         A: {r['belief_a'][:80]}")
        print(f"         B: {r['belief_b'][:80]}")
        print()


def cmd_tom_conflicts_resolve(args):
    """Mark a conflict as resolved."""
    db = get_db()
    _require_tom(db)

    conflict_id = args.conflict_id
    resolution = args.resolution
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    row = db.execute(
        "SELECT id, topic FROM belief_conflicts WHERE id=?", (conflict_id,)
    ).fetchone()
    if not row:
        print(f"ERROR: Conflict #{conflict_id} not found.", file=sys.stderr)
        sys.exit(1)

    db.execute(
        "UPDATE belief_conflicts SET resolved_at=?, resolution=? WHERE id=?",
        (now, resolution, conflict_id)
    )
    db.commit()

    if getattr(args, "json", False):
        json_out({"ok": True, "conflict_id": conflict_id, "resolved_at": now})
    else:
        print(f"Conflict #{conflict_id} resolved: {row['topic']}")
        print(f"  Resolution: {resolution}")


def cmd_tom_perspective_set(args):
    """Update observer's perspective model of subject on a topic."""
    db = get_db()
    _require_tom(db)

    observer = args.observer
    subject = args.subject
    topic = args.topic
    belief = getattr(args, "belief", None) or ""
    gap = getattr(args, "gap", None)
    confusion = getattr(args, "confusion", None) or 0.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    existing = db.execute(
        "SELECT id FROM agent_perspective_models "
        "WHERE observer_agent_id=? AND subject_agent_id=? AND topic=?",
        (observer, subject, topic)
    ).fetchone()

    if existing:
        db.execute(
            """UPDATE agent_perspective_models SET
               estimated_belief=?, knowledge_gap=?, confusion_risk=?, last_updated_at=?
               WHERE observer_agent_id=? AND subject_agent_id=? AND topic=?""",
            (belief or None, gap, confusion, now, observer, subject, topic)
        )
        action = "updated"
    else:
        db.execute(
            """INSERT INTO agent_perspective_models
               (observer_agent_id, subject_agent_id, topic, estimated_belief,
                knowledge_gap, confusion_risk, last_updated_at, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (observer, subject, topic, belief or None, gap, confusion, now, now)
        )
        action = "created"

    db.commit()

    if getattr(args, "json", False):
        json_out({"ok": True, "action": action, "observer": observer,
                  "subject": subject, "topic": topic, "confusion_risk": confusion})
    else:
        print(f"Perspective model {action}: {observer} -> {subject} on '{topic}'")
        if gap:
            print(f"  Gap: {gap[:100]}")
        print(f"  Confusion risk: {confusion:.2f}")


def cmd_tom_perspective_get(args):
    """Print all perspective model entries for an observer->subject pair."""
    db = get_db()
    _require_tom(db)

    observer = args.observer
    subject = args.subject

    rows = db.execute(
        """SELECT topic, estimated_belief, knowledge_gap, confusion_risk,
                  estimated_confidence, last_updated_at
           FROM agent_perspective_models
           WHERE observer_agent_id=? AND subject_agent_id=?
           ORDER BY confusion_risk DESC""",
        (observer, subject)
    ).fetchall()

    if getattr(args, "json", False):
        json_out({"observer": observer, "subject": subject,
                  "perspective_models": rows_to_list(rows)})
        return

    if not rows:
        print(f"No perspective model: {observer} -> {subject}")
        return

    print(f"Perspective: {observer} -> {subject}  ({len(rows)} topics)")
    print(f"  {'Topic':<35}  {'Confusion':>9}  {'Gap?':>5}  Last Updated")
    print("  " + "-" * 75)
    for r in rows:
        has_gap = "yes" if r["knowledge_gap"] else "no"
        updated = (r["last_updated_at"] or "")[:16]
        print(f"  {r['topic'][:35]:<35}  {r['confusion_risk']:>9.2f}  {has_gap:>5}  {updated}")
        if r["estimated_belief"]:
            print(f"    Belief: {r['estimated_belief'][:90]}")
        if r["knowledge_gap"]:
            print(f"    Gap:    {r['knowledge_gap'][:90]}")


def cmd_tom_gap_scan(args):
    """Scan agent's active tasks vs beliefs -- emit gap report."""
    db = get_db()
    _require_tom(db)

    agent_id = args.agent_id
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    tasks = db.execute(
        "SELECT id, external_id, title, description, priority FROM tasks "
        "WHERE assigned_agent_id=? AND status IN ('pending','in_progress') "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END",
        (agent_id,)
    ).fetchall()

    if not tasks:
        print(f"No active tasks for {agent_id}. Nothing to scan.")
        return

    beliefs = db.execute(
        "SELECT topic, last_updated_at, confidence FROM agent_beliefs "
        "WHERE agent_id=? AND invalidated_at IS NULL",
        (agent_id,)
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

    if getattr(args, "json", False):
        json_out({"agent_id": agent_id, "gaps": rows_out,
                  "missing": sum(1 for r in rows_out if r["status"] == "MISSING"),
                  "stale": sum(1 for r in rows_out if r["status"] == "STALE")})
        return

    print(f"Gap scan: {agent_id}  ({len(tasks)} active tasks)")
    print(f"  {'Topic':<45}  {'Status':<8}  {'Staleness':<12}  Confusion")
    print("  " + "-" * 85)
    for r in rows_out:
        flag = "!!" if r["status"] == "MISSING" else ("! " if r["status"] == "STALE" else "  ")
        print(f"{flag} {r['topic'][:45]:<45}  {r['status']:<8}  {r['staleness']:<12}  "
              f"{r['confusion_risk']:.2f}")
        print(f"    {r['task_title'][:80]}")

    missing = sum(1 for r in rows_out if r["status"] == "MISSING")
    stale = sum(1 for r in rows_out if r["status"] == "STALE")
    print(f"\nSummary: {missing} missing, {stale} stale, "
          f"{len(rows_out) - missing - stale} current")


def cmd_tom_inject(args):
    """Write a gap-filling memory scoped to agent, update perspective model."""
    db = get_db()
    _require_tom(db)

    agent_id = args.agent_id
    topic = args.topic
    content = getattr(args, "content", None)
    observer = getattr(args, "observer", None) or agent_id
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if not content:
        pm = db.execute(
            "SELECT knowledge_gap FROM agent_perspective_models "
            "WHERE subject_agent_id=? AND topic=? ORDER BY last_updated_at DESC LIMIT 1",
            (agent_id, topic)
        ).fetchone()
        if pm and pm["knowledge_gap"]:
            content = pm["knowledge_gap"]
        else:
            print("ERROR: No content provided and no knowledge gap in perspective model.", file=sys.stderr)
            sys.exit(1)

    scope = f"agent:{agent_id}"
    row = db.execute(
        """INSERT INTO memories
           (agent_id, content, category, scope, confidence, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           RETURNING id""",
        (observer, f"[ToM inject -> {agent_id}] Topic: {topic}\n{content}",
         "environment", scope, 0.7, now, now)
    ).fetchone()
    memory_id = row["id"] if row else None

    db.execute(
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
        (agent_id, topic, content, 0.9, 0, memory_id, now, now, now)
    )

    old_cr = db.execute(
        "SELECT confusion_risk FROM agent_perspective_models "
        "WHERE observer_agent_id=? AND subject_agent_id=? AND topic=?",
        (observer, agent_id, topic)
    ).fetchone()
    old_cr_val = old_cr["confusion_risk"] if old_cr else None
    new_cr = 0.1

    db.execute(
        """INSERT INTO agent_perspective_models
           (observer_agent_id, subject_agent_id, topic, estimated_belief,
            knowledge_gap, confusion_risk, last_updated_at, created_at)
           VALUES (?,?,?,?,NULL,?,?,?)
           ON CONFLICT(observer_agent_id, subject_agent_id, topic) DO UPDATE SET
             estimated_belief=excluded.estimated_belief,
             knowledge_gap=NULL,
             confusion_risk=?,
             last_updated_at=excluded.last_updated_at""",
        (observer, agent_id, topic, content, new_cr, now, now, new_cr)
    )
    db.commit()

    if getattr(args, "json", False):
        json_out({
            "ok": True, "memory_id": memory_id, "agent_id": agent_id, "topic": topic,
            "confusion_risk_before": old_cr_val, "confusion_risk_after": new_cr,
        })
    else:
        print(f"Memory injected -> {agent_id} on topic: {topic}")
        print(f"  Memory ID: {memory_id}  scope={scope}")
        if old_cr_val is not None:
            print(f"  Confusion risk: {old_cr_val:.2f} -> {new_cr:.2f}")


def cmd_tom_status(args):
    """Print BDI health summary -- all agents ranked by confusion_risk."""
    db = get_db()
    _require_tom(db)

    agent_id = getattr(args, "agent_id", None)

    if agent_id:
        rows = db.execute(
            """SELECT b.agent_id, a.display_name,
                      b.knowledge_coverage_score, b.belief_staleness_score,
                      b.confusion_risk_score, b.last_full_assessment_at
               FROM agent_bdi_state b JOIN agents a ON a.id = b.agent_id
               WHERE b.agent_id=?""",
            (agent_id,)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT b.agent_id, a.display_name,
                      b.knowledge_coverage_score, b.belief_staleness_score,
                      b.confusion_risk_score, b.last_full_assessment_at
               FROM agent_bdi_state b JOIN agents a ON a.id = b.agent_id
               ORDER BY b.confusion_risk_score DESC"""
        ).fetchall()

    if getattr(args, "json", False):
        json_out({"agents": rows_to_list(rows)})
        return

    if not rows:
        print("No BDI state data. Run: brainctl tom update")
        return

    print(f"{'Agent':<30}  {'Coverage':>8}  {'Staleness':>9}  {'Confusion':>9}  Status")
    print("-" * 75)
    for r in rows:
        cr = r["confusion_risk_score"] or 0.0
        cov = r["knowledge_coverage_score"] or 0.0
        stale = r["belief_staleness_score"] or 0.0
        status = "!! HIGH RISK" if cr > 0.7 else ("!  MODERATE" if cr > 0.4 else "   OK")
        name = (r["display_name"] or r["agent_id"])[:30]
        print(f"  {name:<28}  {cov:>8.2f}  {stale:>9.2f}  {cr:>9.2f}  {status}")


def cmd_agent_model(args):
    """Show full mental model for an agent: beliefs, BDI state, conflicts, gaps."""
    db = get_db()
    _require_tom(db)

    agent_id = args.agent_id
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    agent_row = db.execute(
        "SELECT id, display_name, status FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    if not agent_row:
        print(f"ERROR: Agent '{agent_id}' not found.", file=sys.stderr)
        sys.exit(1)

    bdi = db.execute("SELECT * FROM agent_bdi_state WHERE agent_id=?", (agent_id,)).fetchone()

    beliefs = db.execute(
        "SELECT topic, belief_content, confidence, is_assumption, last_updated_at "
        "FROM agent_beliefs WHERE agent_id=? AND invalidated_at IS NULL "
        "ORDER BY last_updated_at DESC",
        (agent_id,)
    ).fetchall()

    conflicts = db.execute(
        "SELECT id, topic, conflict_type, severity, belief_a, belief_b, "
        "agent_b_id, requires_supervisor_intervention "
        "FROM belief_conflicts "
        "WHERE (agent_a_id=? OR agent_b_id=?) AND resolved_at IS NULL "
        "ORDER BY severity DESC",
        (agent_id, agent_id)
    ).fetchall()

    perspective = db.execute(
        "SELECT observer_agent_id, topic, knowledge_gap, confusion_risk "
        "FROM agent_perspective_models "
        "WHERE subject_agent_id=? AND knowledge_gap IS NOT NULL "
        "ORDER BY confusion_risk DESC LIMIT 10",
        (agent_id,)
    ).fetchall()

    if getattr(args, "json", False):
        json_out({
            "agent_id": agent_id,
            "display_name": agent_row["display_name"],
            "bdi_state": row_to_dict(bdi),
            "active_beliefs": rows_to_list(beliefs),
            "open_conflicts": rows_to_list(conflicts),
            "knowledge_gaps": rows_to_list(perspective),
        })
        return

    display = agent_row["display_name"] or agent_id
    print(f"Agent Mental Model: {display} ({agent_id})")
    print(f"  Status: {agent_row['status']}")

    if bdi:
        bs = json.loads(bdi["beliefs_summary"] or "{}")
        ds = json.loads(bdi["desires_summary"] or "{}")
        ins = json.loads(bdi["intentions_summary"] or "{}")
        cr = bdi["confusion_risk_score"] or 0.0
        risk_label = "HIGH RISK" if cr > 0.7 else ("MODERATE" if cr > 0.4 else "OK")
        print(f"")
        print(f"BDI State  [assessed: {(bdi['last_full_assessment_at'] or '')[:16]}]")
        print(f"  Coverage: {bdi['knowledge_coverage_score'] or 0:.2f}  "
              f"Staleness: {bdi['belief_staleness_score'] or 0:.2f}  "
              f"Confusion: {cr:.2f}  [{risk_label}]")
        print(f"  Beliefs:  {bs.get('active_belief_count',0)} active, "
              f"{bs.get('stale_belief_count',0)} stale, "
              f"{bs.get('assumption_count',0)} assumptions, "
              f"{bs.get('conflict_count',0)} conflicts")
        if ds.get("primary_goal"):
            print(f"  Primary:  [{ds.get('priority','?')}] {ds['primary_goal'][:70]}")
        if ins.get("in_progress_tasks"):
            print(f"  In-flight: {', '.join(ins['in_progress_tasks'][:5])}")
    else:
        print(f"  [No BDI snapshot -- run: brainctl tom update {agent_id}]")

    print(f"")
    print(f"Active Beliefs ({len(beliefs)})")
    if beliefs:
        for b in beliefs[:15]:
            stale_flag = " [STALE]" if b["last_updated_at"] and b["last_updated_at"] < stale_cutoff else ""
            assump_flag = " [assumption]" if b["is_assumption"] else ""
            updated = (b["last_updated_at"] or "")[:10]
            print(f"  {b['topic'][:45]:<45}  conf={b['confidence']:.2f}  {updated}{stale_flag}{assump_flag}")
            print(f"    -> {b['belief_content'][:90]}")
        if len(beliefs) > 15:
            print(f"  ... and {len(beliefs)-15} more")
    else:
        print(f"  (none)")

    print(f"")
    print(f"Open Belief Conflicts ({len(conflicts)})")
    if conflicts:
        for c in conflicts[:5]:
            super_flag = " [!SUPERVISOR]" if c["requires_supervisor_intervention"] else ""
            other = c["agent_b_id"] or "ground truth"
            print(f"  #{c['id']} [{c['conflict_type']}] sev={c['severity']:.2f}{super_flag}")
            print(f"    topic: {c['topic']}")
            print(f"    vs {other}: {c['belief_b'][:80]}")
    else:
        print(f"  (none)")

    print(f"")
    print(f"Knowledge Gaps (observer perspective)")
    if perspective:
        for p in perspective[:5]:
            print(f"  [{p['observer_agent_id'][:20]}] {p['topic'][:40]}  "
                  f"confusion={p['confusion_risk']:.2f}")
            print(f"    Gap: {p['knowledge_gap'][:90]}")
    else:
        print(f"  (none recorded)")


def cmd_belief_conflicts(args):
    """Top-level alias: list open belief conflicts."""
    db = get_db()
    _require_tom(db)
    cmd_tom_conflicts_list(args)


# ---------------------------------------------------------------------------
# Belief Collapse Mechanics — collapse-log / collapse-stats
# ---------------------------------------------------------------------------

def cmd_collapse_log(args):
    """List collapse events from belief_collapse_events."""
    try:
        from collapse_mechanics import list_collapse_events
    except ImportError:
        sys.path.insert(0, str(Path.home() / "agentmemory"))
        from collapse_mechanics import list_collapse_events

    events = list_collapse_events(
        belief_id=getattr(args, "belief_id", None),
        agent_id=getattr(args, "agent_id", None),
        limit=getattr(args, "limit", 50),
    )
    if getattr(args, "json", False):
        print(json.dumps(events, indent=2, default=str))
        return

    if not events:
        print("No collapse events found.")
        return

    header = f"{'WHEN':<22} {'TRIGGER':<22} {'BELIEF_ID':<38} {'COLLAPSED_TO':<30} {'PROB':>6}"
    print(header)
    print("-" * len(header))
    for ev in events:
        ctx = ev.get("collapse_context") or "{}"
        try:
            ctx_data = json.loads(ctx) if isinstance(ctx, str) else ctx
        except Exception:
            ctx_data = {}
        pre_coh = ctx_data.get("pre_coherence", "")
        prob = ev.get("measured_amplitude") or 0.0
        print(
            f"{str(ev.get('created_at', '')):<22} "
            f"{str(ev.get('collapse_type', '')):<22} "
            f"{str(ev.get('belief_id', '')):<38} "
            f"{str(ev.get('collapsed_state', '')):<30} "
            f"{prob:>6.3f}"
        )


def cmd_collapse_stats(args):
    """Show aggregate statistics for belief collapses."""
    try:
        from collapse_mechanics import collapse_stats
    except ImportError:
        sys.path.insert(0, str(Path.home() / "agentmemory"))
        from collapse_mechanics import collapse_stats

    stats = collapse_stats()
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2))
        return

    print(f"Total collapses:       {stats['total_collapses']}")
    print(f"Last 7 days:           {stats['collapses_last_7d']}")
    print(f"Avg collapse prob:     {stats['avg_collapse_probability']:.4f}")
    print()
    print(f"{'TRIGGER':<26} {'COUNT':>7} {'AVG PROB':>10} {'AVG FIDELITY':>13}")
    print("-" * 60)
    for row in stats["by_trigger_type"]:
        print(
            f"{row['trigger']:<26} {row['count']:>7} "
            f"{row['avg_probability']:>10.4f} {row['avg_fidelity']:>13.4f}"
        )


# ---------------------------------------------------------------------------
# AGM Belief Revision — resolve-conflict
# ---------------------------------------------------------------------------

def cmd_resolve_conflict(args):
    """AGM credibility-weighted resolution of open belief conflicts."""
    try:
        sys.path.insert(0, str(Path.home() / "bin" / "lib"))
        from belief_revision import resolve_conflict, list_conflicts, auto_resolve
    except ImportError as e:
        print(f"ERROR: Cannot import belief_revision: {e}", file=sys.stderr)
        sys.exit(1)

    db_path = str(DB_PATH)
    use_json = getattr(args, "json", False)

    # --list mode
    if getattr(args, "list", False):
        conflicts = list_conflicts(db_path)
        if use_json:
            json_out(conflicts)
            return
        if not conflicts:
            print("No open belief conflicts.")
            return
        print(f"Open belief conflicts ({len(conflicts)}):")
        for c in conflicts:
            super_flag = " [SUPERVISOR]" if c["requires_supervisor_intervention"] else ""
            print(f"  #{c['id']:4d}  [{c['conflict_type']:10s}]  sev={c['severity']:.2f}{super_flag}  {c['topic'][:60]}")
            print(f"         A ({c['agent_a_id'][:16]}): {c['belief_a'][:60]}")
            b_agent = c['agent_b_id'] or 'ground-truth'
            print(f"         B ({b_agent[:16]}): {c['belief_b'][:60]}")
        return

    # --auto mode
    if getattr(args, "auto", False):
        threshold = getattr(args, "threshold", 0.05) or 0.05
        dry_run   = getattr(args, "dry_run", False)
        results   = auto_resolve(db_path=db_path, threshold=threshold, dry_run=dry_run)
        if use_json:
            json_out(results)
            return
        resolved = [r for r in results if not r.get("escalated") and not r.get("error")]
        escalated = [r for r in results if r.get("escalated")]
        errors    = [r for r in results if r.get("error")]
        tag = "[DRY RUN] " if dry_run else ""
        print(f"{tag}Auto-resolve: {len(results)} conflicts processed.")
        print(f"  Resolved : {len(resolved)}")
        print(f"  Escalated: {len(escalated)}")
        print(f"  Errors   : {len(errors)}")
        for r in escalated:
            print(f"  ESCALATE #{r['conflict_id']}: {r['escalation_reason']}")
        for r in errors:
            print(f"  ERROR: {r['error']}")
        if not dry_run and resolved:
            # Log to events
            db = get_db()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            for r in resolved:
                db.execute(
                    "INSERT INTO events (agent_id, event_type, summary, detail, created_at) VALUES (?,?,?,?,?)",
                    (
                        os.environ.get("BRAINCTL_AGENT_ID", "brainctl"),
                        "result",
                        f"AGM resolved conflict #{r['conflict_id']}: {r['topic'][:60]}",
                        r.get("resolution", ""),
                        now,
                    )
                )
            db.commit()
        return

    # Single conflict_id mode
    conflict_id = getattr(args, "conflict_id", None)
    if conflict_id is None:
        print("ERROR: Provide a conflict_id, --list, or --auto.", file=sys.stderr)
        sys.exit(1)

    dry_run       = getattr(args, "dry_run", False)
    force_winner  = getattr(args, "force_winner", None)
    threshold     = getattr(args, "threshold", 0.05) or 0.05

    result = resolve_conflict(
        conflict_id=conflict_id,
        db_path=db_path,
        dry_run=dry_run,
        force_winner_id=force_winner,
        threshold=threshold,
    )

    if use_json:
        json_out(result)
        return

    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)

    tag = "[DRY RUN] " if result.get("dry_run") else ""
    if result.get("escalated"):
        print(f"{tag}ESCALATED conflict #{result['conflict_id']} ({result['topic'][:50]})")
        print(f"  Reason : {result['escalation_reason']}")
        print(f"  Score A: {result['score_a']:.4f}  Score B: {result['score_b']:.4f}  Delta: {result['score_delta']:.4f}")
    else:
        print(f"{tag}Resolved conflict #{result['conflict_id']} ({result['topic'][:50]})")
        print(f"  Winner : agent {result.get('winner_agent', '?')} (score={result['score_a' if result.get('winner_agent') == result.get('winner_agent') else 'score_b']:.4f})")
        print(f"  Delta  : {result['score_delta']:.4f}")
        print(f"  Action : {result['action']}")
        if result.get("loser_mem_id"):
            print(f"  Retracted memory #{result['loser_mem_id']}; supersedes edge → #{result['winner_mem_id']}")

    # Log event unless dry run
    if not dry_run and not result.get("escalated") and result.get("action") == "retract_loser":
        db = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        db.execute(
            "INSERT INTO events (agent_id, event_type, summary, detail, created_at) VALUES (?,?,?,?,?)",
            (
                os.environ.get("BRAINCTL_AGENT_ID", "brainctl"),
                "result",
                f"AGM resolved conflict #{result['conflict_id']}: {result.get('topic', '')[:60]}",
                result.get("resolution", ""),
                now,
            )
        )
        db.commit()


# ---------------------------------------------------------------------------
# Top-level belief commands
# Simpler interface: belief set/get/seed — agent-centric, topic derived from type
# ---------------------------------------------------------------------------

def cmd_belief_set(args):
    """Write a belief about a target agent. Observer = --agent flag."""
    db = get_db()
    _require_tom(db)

    observer = getattr(args, "agent", None) or "unknown"
    target = args.target_agent
    btype = args.belief_type
    content = args.content
    confidence = getattr(args, "confidence", 1.0) or 1.0
    assumption = getattr(args, "assumption", False)
    is_assumption = 1 if assumption else 0

    topic = f"agent:{target}:{btype}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    existing = db.execute(
        "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?",
        (observer, topic)
    ).fetchone()

    if existing:
        db.execute(
            """UPDATE agent_beliefs SET
               belief_content=?, confidence=?, is_assumption=?,
               last_updated_at=?, invalidated_at=NULL, invalidation_reason=NULL, updated_at=?
               WHERE agent_id=? AND topic=?""",
            (content, confidence, is_assumption, now, now, observer, topic)
        )
        action = "updated"
    else:
        db.execute(
            """INSERT INTO agent_beliefs
               (agent_id, topic, belief_content, confidence, is_assumption,
                last_updated_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (observer, topic, content, confidence, is_assumption, now, now, now)
        )
        action = "created"

    db.commit()
    log_access(db, observer, f"belief_{action}", "agent_beliefs", None, topic)
    db.commit()

    if getattr(args, "json", False):
        json_out({"ok": True, "action": action, "observer": observer, "target": target,
                  "belief_type": btype, "topic": topic})
    else:
        print(f"Belief {action}: [{observer}] → [{target}] ({btype})")
        print(f"  Topic:   {topic}")
        print(f"  Content: {content[:120]}")
        if is_assumption:
            print("  (marked as assumption)")


def cmd_belief_get(args):
    """Retrieve all active beliefs about a target agent held by any observer."""
    db = get_db()
    _require_tom(db)

    target = args.target_agent
    observer = getattr(args, "observer", None)
    pattern = f"agent:{target}:%"

    query = (
        "SELECT agent_id, topic, belief_content, confidence, is_assumption, last_updated_at "
        "FROM agent_beliefs "
        "WHERE topic LIKE ? AND invalidated_at IS NULL"
    )
    params: list = [pattern]

    if observer:
        query += " AND agent_id=?"
        params.append(observer)

    query += " ORDER BY last_updated_at DESC"
    rows = db.execute(query, params).fetchall()

    if getattr(args, "json", False):
        out = [
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
        json_out({"target": target, "belief_count": len(out), "beliefs": out})
    else:
        if not rows:
            print(f"No active beliefs found about agent '{target}'")
            return
        print(f"Beliefs about '{target}' ({len(rows)} active):")
        for r in rows:
            btype = r["topic"].split(":")[-1] if r["topic"] else "?"
            flag = " [assumption]" if r["is_assumption"] else ""
            print(f"  [{r['agent_id']}] {btype} (conf={r['confidence']:.2f}){flag}")
            print(f"    {r['belief_content'][:120]}")
            print(f"    updated: {r['last_updated_at']}")


def cmd_belief_seed(args):
    """Seed capability beliefs from agent_expertise entries."""
    db = get_db()
    _require_tom(db)

    observer = getattr(args, "agent", None) or "cortex"
    min_strength = getattr(args, "min_strength", None) or 0.3
    dry_run = getattr(args, "dry_run", False)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Get distinct agents with expertise
    agents_q = db.execute(
        "SELECT DISTINCT agent_id FROM agent_expertise WHERE strength >= ?",
        (min_strength,)
    ).fetchall()
    target_agents = [r["agent_id"] for r in agents_q]

    created = 0
    updated = 0
    skipped = 0

    for agent_id in target_agents:
        # Get top domains for this agent
        domains = db.execute(
            "SELECT domain, strength FROM agent_expertise "
            "WHERE agent_id=? AND strength>=? ORDER BY strength DESC LIMIT 10",
            (agent_id, min_strength)
        ).fetchall()

        if not domains:
            continue

        # Build a combined capability belief
        top_domains = [f"{r['domain']} ({r['strength']:.2f})" for r in domains[:5]]
        content = "Capable in: " + ", ".join(top_domains)
        topic = f"agent:{agent_id}:capability"
        confidence = min(1.0, max(r["strength"] for r in domains))

        if dry_run:
            print(f"  [dry-run] Would set belief: [{observer}] {topic}")
            print(f"    {content[:100]}")
            skipped += 1
            continue

        existing = db.execute(
            "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=?",
            (observer, topic)
        ).fetchone()

        if existing:
            db.execute(
                """UPDATE agent_beliefs SET
                   belief_content=?, confidence=?, is_assumption=0,
                   last_updated_at=?, invalidated_at=NULL, invalidation_reason=NULL, updated_at=?
                   WHERE agent_id=? AND topic=?""",
                (content, confidence, now, now, observer, topic)
            )
            updated += 1
        else:
            db.execute(
                """INSERT INTO agent_beliefs
                   (agent_id, topic, belief_content, confidence, is_assumption,
                    last_updated_at, created_at, updated_at)
                   VALUES (?,?,?,?,0,?,?,?)""",
                (observer, topic, content, confidence, now, now, now)
            )
            created += 1

    if not dry_run:
        db.commit()
        log_access(db, observer, "belief_seed", "agent_beliefs", None, f"seeded {created+updated} beliefs")
        db.commit()

    if getattr(args, "json", False):
        json_out({"ok": True, "created": created, "updated": updated,
                  "dry_run": dry_run, "agents_processed": len(target_agents)})
    else:
        if dry_run:
            print(f"Dry run: would process {len(target_agents)} agents ({skipped} beliefs)")
        else:
            print(f"Seeded beliefs: {created} created, {updated} updated across {len(target_agents)} agents")


# ---------------------------------------------------------------------------
# INDEX — browsable catalog of all knowledge (Karpathy LLM Wiki pattern)
# ---------------------------------------------------------------------------

def cmd_index(args):
    """Generate a browsable catalog of all knowledge in the brain.

    Inspired by Karpathy's LLM Wiki pattern: an index.md that lets the LLM
    (or human) quickly orient — see what's known, find relevant pages, and
    identify gaps. The index is a snapshot, not a live view.
    """
    db = get_db()
    category_filter = getattr(args, "category", None)
    scope_filter = getattr(args, "scope", None)
    out_format = getattr(args, "format", "markdown")
    out_file = getattr(args, "out", None)

    # ── Gather memories ──────────────────────────────────────────────
    where_clauses = ["retired_at IS NULL"]
    params = []
    if category_filter:
        where_clauses.append("category = ?")
        params.append(category_filter)
    if scope_filter:
        where_clauses.append("scope = ?")
        params.append(scope_filter)

    where = " AND ".join(where_clauses)
    memories = db.execute(
        f"SELECT id, category, scope, content, confidence, recalled_count, "
        f"file_path, file_line, created_at, agent_id "
        f"FROM memories WHERE {where} ORDER BY category, confidence DESC",
        params
    ).fetchall()

    # ── Gather entities ──────────────────────────────────────────────
    entities = db.execute(
        "SELECT id, name, entity_type, created_at FROM entities "
        "WHERE retired_at IS NULL ORDER BY entity_type, name"
    ).fetchall()

    # ── Gather decisions ─────────────────────────────────────────────
    decisions = db.execute(
        "SELECT id, title, rationale, agent_id, created_at FROM decisions "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    if out_format == "json":
        result = {
            "memories_by_category": {},
            "entities_by_type": {},
            "decisions": [],
            "stats": {
                "total_memories": len(memories),
                "total_entities": len(entities),
                "total_decisions": len(decisions),
            }
        }
        for m in memories:
            cat = m["category"]
            if cat not in result["memories_by_category"]:
                result["memories_by_category"][cat] = []
            entry = {
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
            result["memories_by_category"][cat].append(entry)
        for e in entities:
            etype = e["entity_type"]
            if etype not in result["entities_by_type"]:
                result["entities_by_type"][etype] = []
            result["entities_by_type"][etype].append({
                "id": e["id"], "name": e["name"], "created": e["created_at"],
            })
        for d in decisions:
            result["decisions"].append({
                "id": d["id"],
                "title": d["title"][:200],
                "rationale": (d["rationale"] or "")[:100],
                "agent": d["agent_id"],
                "created": d["created_at"],
            })
        output = json.dumps(result, indent=2)
    else:
        # Markdown format
        lines = ["# Brain Index", ""]
        lines.append(f"Generated: {_now_ts()}  ")
        lines.append(f"Memories: {len(memories)} | Entities: {len(entities)} | Decisions: {len(decisions)}")
        lines.append("")

        # Group memories by category
        by_cat = {}
        for m in memories:
            by_cat.setdefault(m["category"], []).append(m)

        for cat in sorted(by_cat.keys()):
            items = by_cat[cat]
            lines.append(f"## {cat.title()} ({len(items)})")
            lines.append("")
            for m in items[:30]:  # cap per category for readability
                preview = m["content"][:120].replace("\n", " ")
                file_tag = f" `{m['file_path']}`" if m["file_path"] else ""
                conf = f" (conf={m['confidence']:.2f})" if m["confidence"] < 1.0 else ""
                lines.append(f"- **[{m['id']}]** {preview}{file_tag}{conf}")
            if len(items) > 30:
                lines.append(f"- *...and {len(items) - 30} more*")
            lines.append("")

        # Entities
        if entities:
            lines.append("## Entities")
            lines.append("")
            by_type = {}
            for e in entities:
                by_type.setdefault(e["entity_type"], []).append(e)
            for etype in sorted(by_type.keys()):
                items = by_type[etype]
                names = ", ".join(e["name"] for e in items[:20])
                if len(items) > 20:
                    names += f", ...+{len(items) - 20}"
                lines.append(f"- **{etype}** ({len(items)}): {names}")
            lines.append("")

        # Decisions
        if decisions:
            lines.append("## Recent Decisions")
            lines.append("")
            for d in decisions[:15]:
                lines.append(f"- **[{d['id']}]** {d['title'][:100]}")
            lines.append("")

        output = "\n".join(lines)

    if out_file:
        with open(out_file, "w") as f:
            f.write(output)
        print(f"Index written to {out_file}")
    else:
        print(output)


def cmd_validate(args):
    db = get_db()
    issues = []

    # Check all required tables exist
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {r["name"] for r in tables}
    required = {"agents", "memories", "events", "context", "tasks", "decisions", "agent_state", "blobs", "access_log", "memory_trust_scores"}
    missing = required - table_names
    if missing:
        issues.append(f"Missing tables: {missing}")

    # Check FTS tables
    for fts in ["memories_fts", "events_fts", "context_fts", "reflexion_lessons_fts"]:
        if fts not in table_names:
            issues.append(f"Missing FTS table: {fts}")

    # Check reflexion_lessons table exists
    if "reflexion_lessons" not in table_names:
        issues.append("Missing table: reflexion_lessons (migration 008 not applied)")

    # Check for orphaned reflexion lessons (source agent doesn't exist)
    if "reflexion_lessons" in table_names:
        rlex_orphans = db.execute(
            "SELECT count(*) as cnt FROM reflexion_lessons WHERE source_agent_id NOT IN (SELECT id FROM agents)"
        ).fetchone()
        if rlex_orphans["cnt"] > 0:
            issues.append(f"Orphaned reflexion_lessons (no matching agent): {rlex_orphans['cnt']}")

    # Check integrity
    result = db.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        issues.append(f"Integrity check failed: {result[0]}")

    # Check for orphaned memories (agent doesn't exist)
    orphans = db.execute(
        "SELECT count(*) as cnt FROM memories WHERE agent_id NOT IN (SELECT id FROM agents)"
    ).fetchone()
    if orphans["cnt"] > 0:
        issues.append(f"Orphaned memories (no matching agent): {orphans['cnt']}")

    if issues:
        json_out({"valid": False, "issues": issues})
    else:
        json_out({"valid": True, "issues": []})

def cmd_version(args):
    json_out({"version": VERSION, "db_path": str(DB_PATH)})

# ---------------------------------------------------------------------------
# HEALTH — Memory SLO dashboard
# ---------------------------------------------------------------------------

def _slo_signal(value, green_thresh, yellow_thresh, higher_is_better=True):
    """Return 'green', 'yellow', or 'red' for a metric value.

    higher_is_better=True  → green >= green_thresh, red < yellow_thresh
    higher_is_better=False → green <= green_thresh, red > yellow_thresh
    """
    if higher_is_better:
        if value >= green_thresh:
            return "green"
        elif value >= yellow_thresh:
            return "yellow"
        else:
            return "red"
    else:
        if value <= green_thresh:
            return "green"
        elif value <= yellow_thresh:
            return "yellow"
        else:
            return "red"


def _signal_icon(signal, use_color):
    icons = {"green": "●", "yellow": "●", "red": "●"}
    if not use_color:
        labels = {"green": "[GREEN]", "yellow": "[YELLOW]", "red": "[RED]"}
        return labels[signal]
    colors = {"green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m"}
    reset = "\033[0m"
    return f"{colors[signal]}{icons[signal]}{reset}"


def _hhi(values):
    """Herfindahl-Hirschman Index — 0 = max diversity, 1 = monopoly."""
    if not values:
        return 0.0
    counts = Counter(values)
    total = sum(counts.values())
    return sum((c / total) ** 2 for c in counts.values())


def _gini_list(values):
    """Gini coefficient over a list of non-negative values (0 = equality, 1 = monopoly)."""
    n = len(values)
    if n == 0:
        return 0.0
    s = sorted(values)
    total = sum(s)
    if total == 0:
        return 0.0
    cumsum = 0
    lorenz = 0
    for v in s:
        cumsum += v
        lorenz += cumsum
    return 1 - 2 * lorenz / (n * total)


def cmd_health(args):
    db = get_db()
    use_color = sys.stdout.isatty() and not args.json
    window_days = args.window

    # ── 1. Coverage (distillation ratio) ────────────────────────────────────
    row = db.execute(
        """
        SELECT
          CAST(COUNT(DISTINCT m.id) AS REAL) / NULLIF(COUNT(DISTINCT e.id), 0) AS ratio,
          COUNT(DISTINCT e.id) AS total_events
        FROM events e
          LEFT JOIN memories m
            ON m.source_event_id = e.id AND m.retired_at IS NULL
        WHERE e.created_at >= datetime('now', ?)
        """,
        (f"-{window_days} days",)
    ).fetchone()
    coverage = row["ratio"] if row["ratio"] is not None else 0.0
    total_events = row["total_events"] or 0

    row_hi = db.execute(
        """
        SELECT
          CAST(COUNT(DISTINCT m.id) AS REAL) / NULLIF(COUNT(DISTINCT e.id), 0) AS ratio
        FROM events e
          LEFT JOIN memories m
            ON m.source_event_id = e.id AND m.retired_at IS NULL
        WHERE e.importance >= 0.8
          AND e.created_at >= datetime('now', ?)
        """,
        (f"-{window_days} days",)
    ).fetchone()
    coverage_hi = row_hi["ratio"] if row_hi["ratio"] is not None else 0.0

    # ── 2. Freshness (median event-to-memory lag in minutes) ─────────────────
    lag_rows = db.execute(
        """
        SELECT (julianday(m.created_at) - julianday(e.created_at)) * 1440 AS lag_min
        FROM memories m
          JOIN events e ON m.source_event_id = e.id
        WHERE m.created_at >= datetime('now', ?)
          AND m.retired_at IS NULL
        """,
        (f"-{window_days} days",)
    ).fetchall()
    if lag_rows:
        lags = sorted(r["lag_min"] for r in lag_rows if r["lag_min"] is not None)
        freshness_median = lags[len(lags) // 2] if lags else None
    else:
        freshness_median = None

    # ── 3. Precision / Engagement ────────────────────────────────────────────
    prec_row = db.execute(
        """
        SELECT
          ROUND(
            CAST(SUM(CASE WHEN last_recalled_at >= datetime('now', '-30 days') THEN 1 ELSE 0 END) AS REAL)
            / NULLIF(COUNT(*), 0), 3
          ) AS engagement_rate,
          ROUND(AVG(confidence), 3) AS avg_confidence,
          COUNT(*) AS active_count,
          SUM(CASE WHEN recalled_count > 0 THEN 1 ELSE 0 END) AS ever_recalled
        FROM memories
        WHERE retired_at IS NULL
        """
    ).fetchone()
    engagement_rate = prec_row["engagement_rate"] or 0.0
    avg_confidence = prec_row["avg_confidence"] or 0.0
    active_count = prec_row["active_count"] or 0
    ever_recalled = prec_row["ever_recalled"] or 0

    # ── 3b. Recall Gini (retrieval inequality) ───────────────────────────────
    recall_rows = db.execute(
        "SELECT recalled_count FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    recall_gini = _gini_list([float(r["recalled_count"] or 0) for r in recall_rows])

    # ── 4. Diversity (HHI) ───────────────────────────────────────────────────
    mem_rows = db.execute(
        "SELECT category, scope FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    categories = [r["category"] for r in mem_rows]
    scopes = [r["scope"] for r in mem_rows]
    cat_hhi = _hhi(categories)
    scope_hhi = _hhi(scopes)
    cat_counts = Counter(categories)
    top_cat_share = (max(cat_counts.values()) / len(categories)) if categories else 0.0

    # ── 5. Temporal balance ───────────────────────────────────────────────────
    temporal_rows = db.execute(
        """
        SELECT temporal_class, COUNT(*) AS cnt
        FROM memories WHERE retired_at IS NULL
        GROUP BY temporal_class
        """
    ).fetchall()
    temporal_total = sum(r["cnt"] for r in temporal_rows)
    temporal_dist = {r["temporal_class"]: r["cnt"] for r in temporal_rows}
    def _tpct(cls):
        return (temporal_dist.get(cls, 0) / temporal_total * 100) if temporal_total else 0.0
    ephemeral_pct = _tpct("ephemeral")
    short_pct = _tpct("short")
    medium_pct = _tpct("medium")
    long_pct = _tpct("long")
    permanent_pct = _tpct("permanent")
    temporal_frozen = (ephemeral_pct + short_pct) < 1.0 and temporal_total > 0

    # ── 6. Vec coverage ──────────────────────────────────────────────────────
    try:
        # vec_memories_rowids.rowid matches memories.id; the id column is NULL in this schema
        vec_row = db.execute(
            """SELECT COUNT(DISTINCT v.rowid) AS cnt
               FROM vec_memories_rowids v
               JOIN memories m ON m.id = v.rowid AND m.retired_at IS NULL"""
        ).fetchone()
        vec_count = vec_row["cnt"] if vec_row else 0
    except Exception:
        vec_count = 0
    vec_coverage = (vec_count / active_count) if active_count else 0.0

    # ── 7. Contradiction count (unresolved retractions) ─────────────────────
    contradiction_row = db.execute(
        "SELECT COUNT(*) AS cnt FROM memories WHERE retracted_at IS NULL AND retired_at IS NULL AND retraction_reason IS NOT NULL"
    ).fetchone()
    contradictions = contradiction_row["cnt"] if contradiction_row else 0

    # ── 8. Bayesian α/β coverage (Phase 1) ────────────────────────
    try:
        ab_row = db.execute(
            """SELECT
                SUM(CASE WHEN alpha IS NOT NULL AND beta IS NOT NULL THEN 1 ELSE 0 END) AS ab_populated
               FROM memories WHERE retired_at IS NULL"""
        ).fetchone()
        ab_count = ab_row["ab_populated"] if ab_row else 0
    except Exception:
        ab_count = 0
    ab_coverage = (ab_count / active_count) if active_count else 0.0

    # ── 9. Outcome calibration ─────────────────────────────────────
    try:
        sys.path.insert(0, str(Path.home() / "bin" / "lib"))
        from outcome_eval import compute_memory_lift, compute_brier_score, compute_precision_at_k
        _outcome_lift = compute_memory_lift(period_days=window_days)
        _outcome_brier = compute_brier_score(agent_id="all", period_days=window_days)
        _outcome_p5 = compute_precision_at_k(agent_id="all", k=5, period_days=window_days)
        outcome_lift_pp = _outcome_lift.get("lift_pp")
        outcome_brier = _outcome_brier
        outcome_p5 = _outcome_p5
        outcome_tasks_with = _outcome_lift.get("tasks_with_memory", 0)
        outcome_tasks_without = _outcome_lift.get("tasks_without_memory", 0)
        _outcome_available = True
    except Exception:
        outcome_lift_pp = None
        outcome_brier = None
        outcome_p5 = None
        outcome_tasks_with = 0
        outcome_tasks_without = 0
        _outcome_available = False

    # ── SLO signals ─────────────────────────────────────────────────────────
    sig_coverage = _slo_signal(coverage, 0.10, 0.05)
    sig_coverage_hi = _slo_signal(coverage_hi, 0.50, 0.25)
    sig_freshness = _slo_signal(freshness_median if freshness_median is not None else 9999, 60, 240, higher_is_better=False)
    sig_engagement = _slo_signal(engagement_rate, 0.30, 0.10)
    sig_confidence = _slo_signal(avg_confidence, 0.80, 0.60)
    sig_recall_gini = _slo_signal(recall_gini, 0.60, 0.80, higher_is_better=False)
    sig_cat_hhi = _slo_signal(cat_hhi, 0.35, 0.55, higher_is_better=False)
    sig_scope_hhi = _slo_signal(scope_hhi, 0.40, 0.60, higher_is_better=False)
    sig_temporal = "red" if temporal_frozen else ("green" if (ephemeral_pct + short_pct) >= 10 else "yellow")
    sig_vec = _slo_signal(vec_coverage, 0.90, 0.50)
    sig_contradictions = _slo_signal(contradictions, 0, 0, higher_is_better=False) if contradictions == 0 else "red"
    sig_ab = _slo_signal(ab_coverage, 1.0, 0.50)
    # outcome calibration signals (yellow if no data, else based on thresholds)
    sig_brier = _slo_signal(outcome_brier, 0.20, 0.35, higher_is_better=False) if outcome_brier is not None else "yellow"
    sig_lift = _slo_signal(outcome_lift_pp if outcome_lift_pp is not None else 0, 10, 0) if outcome_lift_pp is not None else "yellow"
    sig_p5 = _slo_signal(outcome_p5 if outcome_p5 is not None else 0, 0.60, 0.40) if outcome_p5 is not None else "yellow"

    # ── Composite score ──────────────────────────────────────────────────────
    WEIGHTS = {"coverage": 0.25, "freshness": 0.20, "precision": 0.25, "diversity": 0.15, "temporal": 0.15}
    SCORE_MAP = {"green": 2, "yellow": 1, "red": 0}
    dimension_scores = {
        "coverage": max(SCORE_MAP[sig_coverage], SCORE_MAP[sig_coverage_hi]),
        "freshness": SCORE_MAP[sig_freshness],
        "precision": min(SCORE_MAP[sig_engagement], SCORE_MAP[sig_confidence]),
        "diversity": min(SCORE_MAP[sig_cat_hhi], SCORE_MAP[sig_scope_hhi]),
        "temporal": SCORE_MAP[sig_temporal],
    }
    composite = sum(dimension_scores[d] * WEIGHTS[d] for d in WEIGHTS) / 2.0

    if composite >= 0.7:
        overall_signal = "green"
        overall_label = "HEALTHY"
    elif composite >= 0.4:
        overall_signal = "yellow"
        overall_label = "DEGRADED"
    else:
        overall_signal = "red"
        overall_label = "CRITICAL"

    # ── Alerts ───────────────────────────────────────────────────────────────
    alerts = []
    if coverage < 0.05:
        alerts.append("Coverage below 0.05 — distillation pipeline may not be linking source events")
    if coverage_hi < 0.25:
        alerts.append("High-importance event coverage below 0.25")
    if freshness_median is not None and freshness_median > 240:
        alerts.append(f"Median distillation lag {freshness_median:.0f}m exceeds 240m threshold")
    if engagement_rate < 0.10:
        alerts.append(f"30-day recall engagement {engagement_rate:.1%} — most memories never retrieved")
    if recall_gini > 0.80:
        alerts.append(f"Recall Gini {recall_gini:.3f} > 0.80 — retrieval monopoly, retrieval-induced forgetting risk")
    elif recall_gini > 0.60:
        alerts.append(f"Recall Gini {recall_gini:.3f} > 0.60 — recall inequality elevated, consider MMR/diversity boost")
    if cat_hhi > 0.55:
        alerts.append(f"Category HHI {cat_hhi:.3f} > 0.55 — topic collapse detected")
    if scope_hhi > 0.70:
        alerts.append(f"Scope HHI {scope_hhi:.3f} > 0.70 — pipeline work narrowly concentrated")
    if temporal_frozen:
        alerts.append("Temporal class freeze — ephemeral+short at 0%, classification pipeline may be halted")
    if permanent_pct > 20:
        alerts.append(f"Permanent memory share {permanent_pct:.1f}% > 20% — over-promotion risk")
    if contradictions > 0:
        alerts.append(f"{contradictions} unresolved contradiction(s) flagged")

    if args.json:
        json_out({
            "composite_score": round(composite, 3),
            "overall": overall_label.lower(),
            "metrics": {
                "coverage": {"value": round(coverage, 4), "signal": sig_coverage},
                "coverage_hi": {"value": round(coverage_hi, 4), "signal": sig_coverage_hi},
                "freshness_median_min": {"value": round(freshness_median, 1) if freshness_median is not None else None, "signal": sig_freshness},
                "engagement_rate": {"value": round(engagement_rate, 4), "signal": sig_engagement},
                "avg_confidence": {"value": round(avg_confidence, 4), "signal": sig_confidence},
                "recall_gini": {"value": round(recall_gini, 4), "signal": sig_recall_gini},
                "category_hhi": {"value": round(cat_hhi, 4), "signal": sig_cat_hhi},
                "scope_hhi": {"value": round(scope_hhi, 4), "signal": sig_scope_hhi},
                "temporal_frozen": temporal_frozen,
                "temporal_dist_pct": {
                    "ephemeral": round(ephemeral_pct, 1), "short": round(short_pct, 1),
                    "medium": round(medium_pct, 1), "long": round(long_pct, 1),
                    "permanent": round(permanent_pct, 1),
                },
                "vec_coverage": {"value": round(vec_coverage, 4), "signal": sig_vec},
                "contradictions": {"value": contradictions, "signal": sig_contradictions},
                "bayesian_ab_coverage": {"value": round(ab_coverage, 4), "signal": sig_ab, "populated": ab_count},
                "outcome_calibration": {
                    "available": _outcome_available,
                    "memory_lift_pp": round(outcome_lift_pp, 2) if outcome_lift_pp is not None else None,
                    "brier_score": round(outcome_brier, 4) if outcome_brier is not None else None,
                    "retrieval_p_at_5": round(outcome_p5, 4) if outcome_p5 is not None else None,
                    "tasks_with_memory": outcome_tasks_with,
                    "tasks_without_memory": outcome_tasks_without,
                    "signals": {"lift": sig_lift, "brier": sig_brier, "p5": sig_p5},
                },
            },
            "alerts": alerts,
        })
        return

    # ── Human-readable dashboard ──────────────────────────────────────────────
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""
    DIM = "\033[2m" if use_color else ""

    def row(label, value, signal, note=""):
        icon = _signal_icon(signal, use_color)
        note_str = f"  {DIM}{note}{RESET}" if note else ""
        print(f"  {icon}  {label:<36} {value}{note_str}")

    print()
    print(f"{BOLD}Memory Health Dashboard{RESET}  {_signal_icon(overall_signal, use_color)} {BOLD}{overall_label}{RESET}  (composite: {composite:.2f})")
    print(f"{DIM}  window: {window_days}d  |  active memories: {active_count}  |  total events: {total_events}{RESET}")
    print()

    print(f"{BOLD}Coverage{RESET}  {DIM}(target: overall ≥0.10, high-imp ≥0.50){RESET}")
    row("Overall distillation ratio", f"{coverage:.3f}", sig_coverage, "memories linked to source events / all events")
    row("High-importance coverage (≥0.8)", f"{coverage_hi:.3f}", sig_coverage_hi)
    print()

    print(f"{BOLD}Freshness{RESET}  {DIM}(target: median lag ≤60 min){RESET}")
    freshness_str = f"{freshness_median:.0f} min" if freshness_median is not None else "n/a (no linked memories)"
    row("Median event→memory lag", freshness_str, sig_freshness)
    print()

    print(f"{BOLD}Precision{RESET}  {DIM}(target: engagement ≥0.30, confidence ≥0.80, recall Gini ≤0.60){RESET}")
    row("30-day recall engagement rate", f"{engagement_rate:.1%}", sig_engagement, f"{ever_recalled}/{active_count} ever recalled")
    row("Average confidence", f"{avg_confidence:.3f}", sig_confidence)
    row("Recall Gini (retrieval inequality)", f"{recall_gini:.3f}", sig_recall_gini, "target: ≤0.60")
    print()

    print(f"{BOLD}Diversity{RESET}  {DIM}(target: category HHI ≤0.35, scope HHI ≤0.40){RESET}")
    row("Category HHI", f"{cat_hhi:.3f}", sig_cat_hhi, f"top: {Counter(categories).most_common(1)[0] if categories else '-'}")
    row("Scope HHI", f"{scope_hhi:.3f}", sig_scope_hhi, f"top share: {top_cat_share:.0%}")
    print()

    print(f"{BOLD}Temporal Balance{RESET}  {DIM}(target: ephemeral+short ≥10%, medium 35-55%){RESET}")
    temporal_sig = sig_temporal
    print(f"  {_signal_icon(temporal_sig, use_color)}  {'ephemeral':<10} {ephemeral_pct:5.1f}%  {'short':<6} {short_pct:5.1f}%  {'medium':<8} {medium_pct:5.1f}%  {'long':<6} {long_pct:5.1f}%  {'permanent':<10} {permanent_pct:5.1f}%")
    if temporal_frozen:
        print(f"     {DIM}^ classification pipeline freeze detected — all memories in medium/long{RESET}")
    print()

    print(f"{BOLD}Infrastructure{RESET}")
    row("Vector embedding coverage", f"{vec_coverage:.1%}", sig_vec, f"{vec_count}/{active_count} memories embedded")
    row("Unresolved contradictions", str(contradictions), sig_contradictions, "target: 0")
    row("Bayesian α/β coverage", f"{ab_coverage:.1%}", sig_ab, f"{ab_count}/{active_count} memories have alpha+beta (Phase 1)")
    print()

    print(f"{BOLD}Outcome Calibration{RESET}  {DIM}( — memory lift, Brier score, P@5){RESET}")
    if not _outcome_available:
        print(f"  {_signal_icon('yellow', use_color)}  outcome_eval module unavailable — skipping")
    elif outcome_tasks_with == 0 and outcome_tasks_without == 0:
        print(f"  {_signal_icon('yellow', use_color)}  No annotated tasks in window — run `brainctl outcome annotate` at task completion")
    else:
        lift_str = (f"+{outcome_lift_pp:.1f} pp" if outcome_lift_pp is not None and outcome_lift_pp >= 0
                    else (f"{outcome_lift_pp:.1f} pp" if outcome_lift_pp is not None else "n/a"))
        brier_str = f"{outcome_brier:.4f}" if outcome_brier is not None else "n/a"
        p5_str = f"{outcome_p5:.4f}" if outcome_p5 is not None else "n/a"
        row("Memory lift (with vs without)", lift_str, sig_lift,
            f"{outcome_tasks_with} tasks w/ memory  |  {outcome_tasks_without} without")
        row("Brier score (confidence calibration)", brier_str, sig_brier, "target: ≤0.20")
        row("Precision@5 (retrieval quality)", p5_str, sig_p5, "target: ≥0.60")
    print()

    if alerts:
        print(f"{BOLD}Alerts{RESET}")
        for a in alerts:
            print(f"  {_signal_icon('red', use_color)}  {a}")
        print()
    else:
        print(f"  {_signal_icon('green', use_color)}  No alerts.")
        print()


# ---------------------------------------------------------------------------
# PROMOTE — elevate an event into a durable memory
# ---------------------------------------------------------------------------

def cmd_promote(args):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id = ?", (args.event_id,)).fetchone()
    if not event:
        json_out({"ok": False, "error": f"Event {args.event_id} not found"})
        return

    tags_json = json.dumps(args.tags.split(",")) if args.tags else None
    cursor = db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, source_event_id, tags, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event["agent_id"],
         args.category or _EVENT_TYPE_TO_CATEGORY.get(
             event["event_type"],
             _infer_category_from_content(event["summary"])
         ),
         args.scope or "global",
         args.content or event["summary"], args.confidence or 0.9,
         args.event_id, tags_json, _now_ts(), _now_ts())
    )
    memory_id = cursor.lastrowid

    # Log the promotion as an event too
    db.execute(
        "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) VALUES (?, 'memory_promoted', ?, ?, ?)",
        (event["agent_id"], f"Promoted event #{args.event_id} to memory #{memory_id}",
         json.dumps({"event_id": args.event_id, "memory_id": memory_id}), _now_ts())
    )

    log_access(db, event["agent_id"], "promote", "memories", memory_id)
    db.commit()

    # Generate embedding for the promoted memory
    _embedded = False
    try:
        _blob = _embed_query_safe(args.content or event["summary"])
        if _blob:
            _db_vec = _try_get_db_with_vec()
            if _db_vec:
                _db_vec.execute(
                    "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (memory_id, _blob)
                )
                _db_vec.execute(
                    "INSERT OR IGNORE INTO embeddings (source_table, source_id, model, dimensions, vector) VALUES (?,?,?,?,?)",
                    ("memories", memory_id, EMBED_MODEL, EMBED_DIMENSIONS, _blob)
                )
                _db_vec.commit()
                _db_vec.close()
                _embedded = True
    except Exception:
        pass  # non-fatal

    json_out({"ok": True, "memory_id": memory_id, "from_event": args.event_id, "embedded": _embedded})

# ---------------------------------------------------------------------------
# DISTILL — batch-promote high-importance events to durable memories
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

# Keyword patterns for content-based category inference (ordered by specificity).
# First matching rule wins. Falls back to "project" (not "lesson") for unknown event types.
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
    """Infer the most appropriate memory category from content text.

    Uses keyword heuristics. Falls back to "project" (never "lesson") so that
    distillation of unmapped event types does not flood the lesson bucket.
    """
    if not content:
        return "project"
    lower = content.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category
    return "project"  # safe fallback — avoids lesson flooding


def cmd_memory_suggest_category(args):
    """Return an inferred category for the given content string."""
    inferred = _infer_category_from_content(args.content)
    json_out({
        "ok": True,
        "inferred_category": inferred,
        "valid_categories": sorted(VALID_MEMORY_CATEGORIES),
        "note": "Heuristic inference — verify before use",
    })


def cmd_distill(args):
    """Batch-promote high-importance events that haven't been promoted yet."""
    db = get_db()
    threshold = args.threshold
    limit = args.limit
    dry_run = args.dry_run
    since = args.since
    agent_filter = args.filter_agent
    event_types = [t.strip() for t in args.event_types.split(",")] if args.event_types else None

    promoted_ids = set()
    for row in db.execute("SELECT source_event_id FROM memories WHERE source_event_id IS NOT NULL"):
        promoted_ids.add(row[0])

    valid_agents = {r[0] for r in db.execute("SELECT id FROM agents")}

    skip_types = {"memory_promoted", "memory_retired", "session_start"}

    sql = """
        SELECT id, agent_id, event_type, summary, detail, importance, project, created_at
        FROM events
        WHERE importance >= ?
        AND event_type NOT IN ({skip})
    """.format(skip=",".join(f"'{t}'" for t in skip_types))
    params = [threshold]

    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        sql += f" AND event_type IN ({placeholders}) "
        params.extend(event_types)

    if since:
        sql += " AND created_at >= ? "
        params.append(since)

    if agent_filter:
        sql += " AND agent_id = ? "
        params.append(agent_filter)

    sql += " ORDER BY importance DESC, created_at DESC"

    candidates = db.execute(sql, params).fetchall()

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
                    _infer_category_from_content(ev["summary"])
                ),
            })
        json_out({
            "ok": True,
            "dry_run": True,
            "threshold": threshold,
            "candidates_found": len(to_promote),
            "total_events_above_threshold": len(candidates),
            "already_promoted_skipped": len(candidates) - len(to_promote) - skipped_orphans,
            "orphan_agents_skipped": skipped_orphans,
            "promotions": results,
        })
        return

    promoted = []
    for ev in to_promote:
        category = _EVENT_TYPE_TO_CATEGORY.get(
            ev["event_type"],
            _infer_category_from_content(ev["summary"])
        )
        scope = f"project:{ev['project']}" if ev["project"] else "global"

        cursor = db.execute(
            "INSERT INTO memories (agent_id, category, scope, content, confidence, source_event_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev["agent_id"], category, scope, ev["summary"],
             min(ev["importance"], 0.95), ev["id"], _now_ts(), _now_ts())
        )
        memory_id = cursor.lastrowid

        db.execute(
            "INSERT INTO events (agent_id, event_type, summary, metadata, importance, created_at) "
            "VALUES (?, 'memory_promoted', ?, ?, 0.3, ?)",
            (ev["agent_id"],
             f"Distilled event #{ev['id']} (importance={ev['importance']}) to memory #{memory_id}",
             json.dumps({"event_id": ev["id"], "memory_id": memory_id, "source": "distill"}), _now_ts())
        )

        log_access(db, ev["agent_id"], "distill", "memories", memory_id)
        promoted.append({"event_id": ev["id"], "memory_id": memory_id, "category": category})

    db.commit()

    # Generate embeddings for all distilled memories
    _embedded_count = 0
    try:
        _db_vec = _try_get_db_with_vec()
        if _db_vec:
            for _promo in promoted:
                _mem_row = db.execute(
                    "SELECT content FROM memories WHERE id = ?", (_promo["memory_id"],)
                ).fetchone()
                if not _mem_row:
                    continue
                _blob = _embed_query_safe(_mem_row["content"])
                if not _blob:
                    continue
                _db_vec.execute(
                    "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (_promo["memory_id"], _blob)
                )
                _db_vec.execute(
                    "INSERT OR IGNORE INTO embeddings (source_table, source_id, model, dimensions, vector) VALUES (?,?,?,?,?)",
                    ("memories", _promo["memory_id"], EMBED_MODEL, EMBED_DIMENSIONS, _blob)
                )
                _embedded_count += 1
            _db_vec.commit()
            _db_vec.close()
    except Exception:
        pass  # non-fatal

    json_out({
        "ok": True,
        "dry_run": False,
        "threshold": threshold,
        "promoted_count": len(promoted),
        "embedded_count": _embedded_count,
        "promotions": promoted,
    })



# ---------------------------------------------------------------------------
# DREAM HYPOTHESES — show incubating bisociation hypotheses
# ---------------------------------------------------------------------------


def cmd_dreams(args):
    """Show recent dream hypotheses from the incubation queue."""
    db = get_db()

    tbl = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dream_hypotheses'"
    ).fetchone()
    if not tbl:
        json_out({"ok": True, "hypotheses": [], "message": "dream_hypotheses table not found — run a consolidation cycle first"})
        return

    status_filter = getattr(args, "status", None) or "incubating"
    limit = getattr(args, "limit", 20)

    rows = db.execute(
        """
        SELECT dh.id, dh.memory_a_id, dh.memory_b_id, dh.hypothesis_memory_id,
               dh.similarity, dh.status, dh.created_at, dh.promoted_at, dh.retired_at,
               dh.retirement_reason,
               m.content AS hypothesis_text,
               ma.scope AS scope_a, mb.scope AS scope_b,
               ma.content AS content_a, mb.content AS content_b
        FROM dream_hypotheses dh
        LEFT JOIN memories m  ON m.id  = dh.hypothesis_memory_id
        LEFT JOIN memories ma ON ma.id = dh.memory_a_id
        LEFT JOIN memories mb ON mb.id = dh.memory_b_id
        WHERE dh.status = ?
        ORDER BY dh.created_at DESC
        LIMIT ?
        """,
        (status_filter, limit),
    ).fetchall()

    hypotheses = []
    for row in rows:
        hypotheses.append({
            "id": row["id"],
            "memory_a": {"id": row["memory_a_id"], "scope": row["scope_a"], "snippet": (row["content_a"] or "")[:80]},
            "memory_b": {"id": row["memory_b_id"], "scope": row["scope_b"], "snippet": (row["content_b"] or "")[:80]},
            "hypothesis_memory_id": row["hypothesis_memory_id"],
            "hypothesis": (row["hypothesis_text"] or "")[:200],
            "similarity": row["similarity"],
            "status": row["status"],
            "created_at": row["created_at"],
            "promoted_at": row["promoted_at"],
            "retired_at": row["retired_at"],
        })

    output_format = getattr(args, "format", "text")
    if output_format == "json":
        json_out({"ok": True, "status": status_filter, "count": len(hypotheses), "hypotheses": hypotheses})
        return

    # Text output
    print(f"Dream hypotheses ({status_filter}) — {len(hypotheses)} result(s)\n")
    for h in hypotheses:
        print(f"  [{h['id']}] similarity={h['similarity']:.3f}  {h['created_at']}")
        print(f"    A [{h['memory_a']['scope']}]: {h['memory_a']['snippet']}")
        print(f"    B [{h['memory_b']['scope']}]: {h['memory_b']['snippet']}")
        print(f"    => {h['hypothesis'][:160]}")
        if h["promoted_at"]:
            print(f"    PROMOTED at {h['promoted_at']}")
        print()


# ---------------------------------------------------------------------------
# PROACTIVE MEMORY PUSH
# ---------------------------------------------------------------------------

import uuid as _uuid_mod


def cmd_push(args):
    """Score + select top-K memories for a task description, inject into context, and record push for utility tracking."""
    import importlib
    db = get_db()
    task_desc = args.task
    top_k = min(args.top_k or 5, 5)
    agent_id = args.agent or "unknown"
    project = args.project
    output_format = getattr(args, "format", "text")
    no_events = getattr(args, "no_events", False)

    push_id = _uuid_mod.uuid4().hex[:12]
    # More aggressive sanitize for push: strip colons, commas, plus signs that confuse FTS5
    _raw_fts = _sanitize_fts_query(task_desc)
    fts_query = re.sub(r'[,:+<>]', ' ', _raw_fts).strip()

    # ---- score memories via hybrid RRF (same pipeline as cmd_search) ----
    db_vec = _try_get_db_with_vec()
    q_blob = _embed_query_safe(task_desc) if db_vec else None
    hybrid = db_vec is not None and q_blob is not None
    fetch_limit = top_k * 6

    def _fts_mem():
        if not fts_query:
            return []
        rows = db.execute(
            "SELECT m.id, 'memory' as type, m.category, m.content, m.confidence, m.scope, m.created_at, f.rank as fts_rank "
            "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_query, fetch_limit)
        ).fetchall()
        return rows_to_list(rows)

    def _vec_mem():
        if not hybrid:
            return []
        try:
            vec_rows = db_vec.execute(
                "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k=?",
                (q_blob, fetch_limit)
            ).fetchall()
        except Exception:
            return []
        if not vec_rows:
            return []
        rowids = [r["rowid"] for r in vec_rows]
        dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
        ph = ",".join("?" * len(rowids))
        src_rows = db_vec.execute(
            f"SELECT id, 'memory' as type, category, content, confidence, scope, created_at "
            f"FROM memories WHERE id IN ({ph}) AND retired_at IS NULL",
            rowids
        ).fetchall()
        out = [dict(r) | {"distance": round(dist_map.get(r["id"], 1.0), 4)} for r in src_rows]
        out.sort(key=lambda r: r["distance"])
        return out

    fts_list = _fts_mem()
    vec_list = _vec_mem()
    if hybrid:
        merged = _rrf_fuse(fts_list, vec_list)
    else:
        merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_list]

    # Apply recency weight and trim to top_k
    for r in merged:
        tw = _temporal_weight(r.get("created_at"), r.get("scope"))
        r["temporal_weight"] = round(tw, 4)
        r["final_score"] = round(r.get("rrf_score", 0.0) * tw, 8)
    merged.sort(key=lambda r: r["final_score"], reverse=True)
    selected = merged[:top_k]

    # Snapshot recalled_count at push time for later delta tracking
    memory_ids = [r["id"] for r in selected]
    recalled_snapshot = {}
    if memory_ids:
        ph = ",".join("?" * len(memory_ids))
        snap_rows = db.execute(
            f"SELECT id, recalled_count FROM memories WHERE id IN ({ph})", memory_ids
        ).fetchall()
        recalled_snapshot = {r["id"]: r["recalled_count"] or 0 for r in snap_rows}

    # Record push event for utility tracking
    push_meta = json.dumps({
        "push_id": push_id,
        "task_desc": task_desc[:200],
        "memory_ids": memory_ids,
        "recalled_at_push": recalled_snapshot,
        "top_k": top_k,
        "hybrid": hybrid,
    })
    push_event_cur = db.execute(
        "INSERT INTO events (agent_id, event_type, summary, detail, importance, project, created_at) "
        "VALUES (?, 'push_delivered', ?, ?, 0.2, ?, ?)",
        (agent_id,
         f"push:{push_id} delivered {len(memory_ids)} memories for task: {task_desc[:80]}",
         push_meta,
         project,
         _now_ts())
    )
    push_event_id = push_event_cur.lastrowid
    log_access(db, agent_id, "push", "memories", None, task_desc[:200], len(memory_ids))
    db.commit()

    if db_vec:
        db_vec.close()

    if output_format == "json":
        json_out({
            "push_id": push_id,
            "push_event_id": push_event_id,
            "task": task_desc,
            "memories_pushed": len(selected),
            "hybrid": hybrid,
            "memories": selected,
        })
        return

    # ---- text output (default): clean context block for agent injection ----
    if not selected:
        print(f"# MEMORY PUSH [{push_id}] — no relevant memories found for this task")
        print(f"push_event_id={push_event_id}  # pass to --source-event when adding memories from this task")
        return

    lines = [
        f"# MEMORY PUSH [{push_id}] — {len(selected)} relevant memories for: {task_desc[:80]}",
        "",
    ]
    for i, m in enumerate(selected, 1):
        scope = m.get("scope") or "global"
        age = _age_str(m.get("created_at"))
        conf = m.get("confidence", 1.0)
        content = m.get("content") or m.get("summary") or ""
        lines.append(f"[{i}] ({m.get('category', '?')}, {scope}, conf={conf}, {age})")
        lines.append(f"    {content}")
        lines.append("")
    lines.append(f"push_id={push_id}  # use this id to track utility later")
    lines.append(f"push_event_id={push_event_id}  # pass to --source-event when adding memories from this task")
    print("\n".join(lines))


def cmd_push_report(args):
    """Show utility report for a specific push_id: recalled_count delta since push."""
    db = get_db()
    push_id = args.push_id

    row = db.execute(
        "SELECT id, detail, created_at FROM events WHERE event_type='push_delivered' AND summary LIKE ?",
        (f"push:{push_id}%",)
    ).fetchone()
    if not row:
        print(json.dumps({"error": f"push_id {push_id!r} not found"}))
        return

    meta = json.loads(row["detail"] or "{}")
    memory_ids = meta.get("memory_ids", [])
    recalled_at_push = meta.get("recalled_at_push", {})

    if not memory_ids:
        json_out({"push_id": push_id, "pushed_at": row["created_at"], "memories": []})
        return

    ph = ",".join("?" * len(memory_ids))
    current_rows = db.execute(
        f"SELECT id, content, recalled_count FROM memories WHERE id IN ({ph})", memory_ids
    ).fetchall()

    report = []
    for r in current_rows:
        snap = recalled_at_push.get(str(r["id"]), recalled_at_push.get(r["id"], 0))
        delta = (r["recalled_count"] or 0) - snap
        report.append({
            "memory_id": r["id"],
            "content_snippet": (r["content"] or "")[:80],
            "recalled_at_push": snap,
            "recalled_now": r["recalled_count"] or 0,
            "delta": delta,
            "was_useful": delta > 0,
        })

    total_useful = sum(1 for r in report if r["was_useful"])
    json_out({
        "push_id": push_id,
        "pushed_at": row["created_at"],
        "memories_pushed": len(memory_ids),
        "memories_useful": total_useful,
        "utility_rate": round(total_useful / len(memory_ids), 2) if memory_ids else 0.0,
        "memories": report,
    })


# ---------------------------------------------------------------------------
# TEMPORAL CONTEXT — compact orientation summary for agents
# ---------------------------------------------------------------------------

def _minutes_ago(ts_str):
    """Return human-readable elapsed time from an ISO timestamp string."""
    if not ts_str:
        return "unknown"
    try:
        # Handle both offset-aware and naive timestamps
        ts_str = ts_str.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(ts_str, fmt)
                break
            except ValueError:
                continue
        else:
            # Try dateutil-style with offset like -04:00
            import re
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}:\d{2})", ts_str)
            if m:
                dt = datetime.fromisoformat(ts_str)
            else:
                return ts_str

        # Normalize to UTC for comparison
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)

        delta = now - dt
        total_sec = int(delta.total_seconds())
        if total_sec < 60:
            return f"{total_sec}s ago"
        elif total_sec < 3600:
            return f"{total_sec // 60} min ago"
        elif total_sec < 86400:
            return f"{total_sec // 3600}h ago"
        else:
            return f"{total_sec // 86400}d ago"
    except Exception:
        return ts_str


def _epoch_day(started_at_str):
    """Return how many days since an epoch started (day 1 = today)."""
    if not started_at_str:
        return "?"
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(started_at_str.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return "?"
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            dt = dt.replace(tzinfo=None)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        days = (now.date() - dt.date()).days + 1
        return str(days)
    except Exception:
        return "?"


def cmd_temporal_context(args):
    db = get_db()
    now_local = datetime.now().astimezone()
    tz_name = now_local.strftime("%Z") or "local"
    now_str = now_local.strftime(f"%Y-%m-%d %H:%M {tz_name}")

    lines = [f"TEMPORAL CONTEXT ({now_str}):"]

    # --- Current epoch ---
    epoch_row = db.execute(
        "SELECT * FROM epochs WHERE started_at <= strftime('%Y-%m-%dT%H:%M:%S', 'now') "
        "AND (ended_at IS NULL OR ended_at > strftime('%Y-%m-%dT%H:%M:%S', 'now')) "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    if epoch_row:
        epoch_name = epoch_row["name"]
        epoch_day = _epoch_day(epoch_row["started_at"])
        parent_row = None
        if epoch_row["parent_epoch_id"]:
            parent_row = db.execute(
                "SELECT * FROM epochs WHERE id = ?", (epoch_row["parent_epoch_id"],)
            ).fetchone()
        if parent_row:
            parent_day = _epoch_day(parent_row["started_at"])
            lines.append(
                f"- Current epoch: {epoch_name} (day {epoch_day}) "
                f"within {parent_row['name']} (day {parent_day})"
            )
        else:
            lines.append(f"- Current epoch: {epoch_name} (day {epoch_day})")
    else:
        lines.append("- Current epoch: none defined")

    # --- Project age ---
    first_event = db.execute(
        "SELECT min(created_at) as first_at FROM events"
    ).fetchone()
    total_events = db.execute("SELECT count(*) as cnt FROM events").fetchone()["cnt"]
    total_memories = db.execute(
        "SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL"
    ).fetchone()["cnt"]
    if first_event and first_event["first_at"]:
        age_str = _minutes_ago(first_event["first_at"])
        # Compute calendar days
        try:
            raw = first_event["first_at"].strip()
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt0 = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                dt0 = None
            if dt0:
                if dt0.tzinfo:
                    dt0 = dt0.replace(tzinfo=None)
                days_active = (datetime.now(timezone.utc).date() - dt0.date()).days + 1
                lines.append(
                    f"- Project age: {days_active} days active, "
                    f"{total_events} events, {total_memories} active memories"
                )
            else:
                lines.append(f"- Project age: started {age_str}, {total_events} events")
        except Exception:
            lines.append(f"- Project age: started {age_str}, {total_events} events")
    else:
        lines.append("- Project age: no events recorded")

    # --- Last activity ---
    last_event = db.execute(
        "SELECT agent_id, summary, created_at FROM events ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if last_event:
        ago = _minutes_ago(last_event["created_at"])
        summary_short = (last_event["summary"] or "")[:80]
        lines.append(f"- Last activity: {ago} ({last_event['agent_id']}: {summary_short})")
    else:
        lines.append("- Last activity: none")

    # --- Cadence ---
    events_24h = db.execute(
        "SELECT count(*) as cnt FROM events WHERE created_at >= datetime('now', '-24 hours')"
    ).fetchone()["cnt"]
    active_agents_24h = db.execute(
        "SELECT count(DISTINCT agent_id) as cnt FROM events WHERE created_at >= datetime('now', '-24 hours')"
    ).fetchone()["cnt"]
    if events_24h > 10:
        cadence = "HIGH"
    elif events_24h > 3:
        cadence = "MEDIUM"
    else:
        cadence = "LOW"
    lines.append(
        f"- Cadence: {cadence} — {events_24h} events in last 24h, {active_agents_24h} agents active"
    )

    # --- Active agents (last 24h) ---
    active_agent_rows = db.execute(
        "SELECT DISTINCT agent_id FROM events WHERE created_at >= datetime('now', '-24 hours') ORDER BY agent_id"
    ).fetchall()
    active_names = [r["agent_id"] for r in active_agent_rows]
    lines.append(f"- Active agents (last 24h): {', '.join(active_names) if active_names else 'none'}")

    # --- Dormant agents (>48h) ---
    all_agents = db.execute("SELECT id FROM agents WHERE status = 'active'").fetchall()
    all_agent_ids = {r["id"] for r in all_agents}
    recently_active = db.execute(
        "SELECT DISTINCT agent_id FROM events WHERE created_at >= datetime('now', '-48 hours')"
    ).fetchall()
    recently_active_ids = {r["agent_id"] for r in recently_active}
    dormant = sorted(all_agent_ids - recently_active_ids)
    lines.append(f"- Dormant agents (>48h): {', '.join(dormant) if dormant else 'none'}")

    # --- Recent decisions ---
    decisions_48h = db.execute(
        "SELECT count(*) as cnt FROM decisions WHERE created_at >= datetime('now', '-48 hours')"
    ).fetchone()["cnt"]
    recent_decision_titles = db.execute(
        "SELECT title FROM decisions WHERE created_at >= datetime('now', '-48 hours') ORDER BY created_at DESC LIMIT 3"
    ).fetchall()
    titles_short = [r["title"][:40] for r in recent_decision_titles]
    titles_str = (", ".join(titles_short)) if titles_short else "none"
    lines.append(f"- Recent decisions: {decisions_48h} in last 48h ({titles_str})")

    # --- Memory health ---
    active_mem = db.execute(
        "SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL"
    ).fetchone()["cnt"]
    decayed_mem = db.execute(
        "SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL AND confidence < 0.3"
    ).fetchone()["cnt"]
    retired_mem = db.execute(
        "SELECT count(*) as cnt FROM memories WHERE retired_at IS NOT NULL"
    ).fetchone()["cnt"]
    lines.append(
        f"- Memory health: {active_mem} active, {decayed_mem} low-confidence (<0.3), {retired_mem} retired"
    )

    # --- Stale areas (scopes with no new memories/events in 7+ days) ---
    # Scopes from memories that haven't been updated in 7+ days
    stale_scope_rows = db.execute(
        "SELECT scope, max(updated_at) as last_update FROM memories "
        "WHERE retired_at IS NULL "
        "GROUP BY scope "
        "HAVING last_update < datetime('now', '-7 days') "
        "ORDER BY last_update ASC"
    ).fetchall()
    stale_scopes = [r["scope"] for r in stale_scope_rows]
    lines.append(
        f"- Stale areas (>7d silent): {', '.join(stale_scopes) if stale_scopes else 'none'}"
    )

    # --- Open causal threads (warning/decision events without a subsequent result) ---
    # Warning events in last 7 days from agents that haven't logged a result after the warning
    open_threads = db.execute(
        "SELECT e.id, e.agent_id, e.summary, e.created_at FROM events e "
        "WHERE e.event_type IN ('warning', 'handoff') "
        "AND e.created_at >= datetime('now', '-7 days') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM events r "
        "  WHERE r.agent_id = e.agent_id "
        "  AND r.event_type = 'result' "
        "  AND r.created_at > e.created_at"
        ") "
        "ORDER BY e.created_at DESC LIMIT 5"
    ).fetchall()
    if open_threads:
        thread_strs = [f"{r['agent_id']}: {(r['summary'] or '')[:50]}" for r in open_threads]
        lines.append(f"- Open causal threads: {len(open_threads)}")
        for t in thread_strs:
            lines.append(f"  · {t}")
    else:
        lines.append("- Open causal threads: none")

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# CAUSAL EVENT GRAPH
# Auto-detection of causal edges + traversal commands
# ---------------------------------------------------------------------------

# Type-based causal templates: (cause_type, effect_type, base_confidence)
_CAUSAL_TEMPLATES = [
    ("error",        "task_update",     0.6),
    ("decision",     "task_update",     0.7),
    ("decision",     "result",          0.7),
    ("task_update",  "result",          0.6),
    ("observation",  "decision",        0.5),
    ("observation",  "task_update",     0.4),
    ("handoff",      "task_update",     0.7),
    ("handoff",      "decision",        0.6),
    ("warning",      "decision",        0.6),
    ("warning",      "task_update",     0.5),
    ("result",       "memory_promoted", 0.7),
    ("error",        "decision",        0.7),
]


def _causal_would_create_cycle(db, source_id: int, target_id: int) -> bool:
    """Return True if adding source->target would create a cycle in knowledge_edges."""
    if source_id == target_id:
        return True
    row = db.execute("""
        WITH RECURSIVE reach(node) AS (
            SELECT target_id FROM knowledge_edges
            WHERE source_table = 'events' AND target_table = 'events'
              AND source_id = ?
              AND relation_type IN ('causes', 'triggered_by', 'contributes_to', 'follows_from')
            UNION
            SELECT ke.target_id FROM knowledge_edges ke
            JOIN reach r ON ke.source_id = r.node
            WHERE ke.source_table = 'events' AND ke.target_table = 'events'
              AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to', 'follows_from')
        )
        SELECT 1 FROM reach WHERE node = ? LIMIT 1
    """, (target_id, source_id)).fetchone()
    return row is not None


def _causal_edge_exists(db, source_id: int, target_id: int, relation: str) -> bool:
    row = db.execute("""
        SELECT 1 FROM knowledge_edges
        WHERE source_table = 'events' AND source_id = ?
          AND target_table = 'events' AND target_id = ?
          AND relation_type = ?
        LIMIT 1
    """, (source_id, target_id, relation)).fetchone()
    return row is not None


def _insert_causal_edge(db, source_id: int, target_id: int, relation: str,
                        confidence: float, agent_id=None) -> str:
    """Insert a causal edge with cycle/duplicate checks. Returns 'inserted', 'existing', or 'cycle'."""
    if _causal_edge_exists(db, source_id, target_id, relation):
        return "existing"
    if _causal_would_create_cycle(db, source_id, target_id):
        return "cycle"
    db.execute("""
        INSERT INTO knowledge_edges
            (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
        VALUES ('events', ?, 'events', ?, ?, ?, ?)
    """, (source_id, target_id, relation, round(confidence, 3), agent_id))
    return "inserted"


def _detect_reference_chains(db) -> list:
    """Find events whose refs field explicitly references other events via 'events:N' notation."""
    rows = db.execute("""
        SELECT e.id as effect_id,
               CAST(SUBSTR(ref.value, INSTR(ref.value, ':') + 1) AS INTEGER) as cause_id
        FROM events e, json_each(e.refs) ref
        WHERE ref.value GLOB 'events:*'
          AND CAST(SUBSTR(ref.value, INSTR(ref.value, ':') + 1) AS INTEGER) IN (
              SELECT id FROM events
          )
    """).fetchall()
    return [(r["effect_id"], r["cause_id"], "triggered_by", 0.9) for r in rows]


def _detect_template_edges(db, window_minutes: int = 60) -> list:
    """Apply type-based causal templates within a time window."""
    edges = []
    window_days = window_minutes / 1440.0
    for cause_type, effect_type, base_conf in _CAUSAL_TEMPLATES:
        rows = db.execute("""
            SELECT a.id as a_id, b.id as b_id,
                   (julianday(b.created_at) - julianday(a.created_at)) * 1440.0 as gap_min
            FROM events a
            JOIN events b ON julianday(b.created_at) > julianday(a.created_at)
                AND (julianday(b.created_at) - julianday(a.created_at)) <= ?
                AND a.id != b.id
            WHERE a.event_type = ? AND b.event_type = ?
              AND (a.agent_id = b.agent_id
                   OR (a.project IS NOT NULL AND a.project != '' AND a.project = b.project))
        """, (window_days, cause_type, effect_type)).fetchall()
        for r in rows:
            gap = r["gap_min"] or 0.0
            time_decay = max(0.0, 1.0 - (gap / window_minutes) * 0.3)
            confidence = round(base_conf * time_decay, 3)
            edges.append((r["a_id"], r["b_id"], "causes", confidence))
    return edges


def _detect_proximity_edges(db, window_minutes: int = 30) -> list:
    """Temporal proximity + shared agent/project heuristic (lowest confidence)."""
    window_days = window_minutes / 1440.0
    rows = db.execute("""
        SELECT a.id as a_id, b.id as b_id,
               (julianday(b.created_at) - julianday(a.created_at)) * 1440.0 as gap_min,
               (CASE WHEN a.agent_id = b.agent_id THEN 1 ELSE 0 END +
                CASE WHEN a.project IS NOT NULL AND a.project != '' AND a.project = b.project
                     THEN 1 ELSE 0 END
               ) as shared_ctx
        FROM events a
        JOIN events b ON julianday(b.created_at) > julianday(a.created_at)
            AND (julianday(b.created_at) - julianday(a.created_at)) <= ?
            AND a.id != b.id
        WHERE (a.agent_id = b.agent_id
               OR (a.project IS NOT NULL AND a.project != '' AND a.project = b.project))
    """, (window_days,)).fetchall()

    edges = []
    for r in rows:
        shared = r["shared_ctx"] or 0
        gap = r["gap_min"] or 0.0
        if shared < 1:
            continue
        time_factor = max(0.0, 1.0 - (gap / window_minutes))
        ctx_factor = min(shared / 2.0, 1.0)
        confidence = round(0.25 + 0.35 * time_factor * ctx_factor, 3)
        edges.append((r["a_id"], r["b_id"], "causes", confidence))
    return edges


def _build_causal_graph(db, since_hours: int = 168, dry_run: bool = False) -> dict:
    """Full pipeline: detect causal edges and insert into knowledge_edges."""
    stats = {"found": 0, "inserted": 0, "cycle": 0, "existing": 0}

    ref_edges = _detect_reference_chains(db)
    template_edges = _detect_template_edges(db, window_minutes=60)
    proximity_edges = _detect_proximity_edges(db, window_minutes=30)

    # Merge: (src, tgt) -> (relation, confidence), keep highest confidence per pair
    all_edges = {}

    for effect_id, cause_id, relation, conf in ref_edges:
        key = (cause_id, effect_id)
        if key not in all_edges or all_edges[key][1] < conf:
            all_edges[key] = (relation, conf)

    for src_id, tgt_id, relation, conf in template_edges:
        key = (src_id, tgt_id)
        if key not in all_edges or all_edges[key][1] < conf:
            all_edges[key] = (relation, conf)

    for src_id, tgt_id, relation, conf in proximity_edges:
        key = (src_id, tgt_id)
        if key not in all_edges:
            all_edges[key] = (relation, conf)

    stats["found"] = len(all_edges)

    if not dry_run:
        for (src_id, tgt_id), (relation, conf) in all_edges.items():
            outcome = _insert_causal_edge(db, src_id, tgt_id, relation, conf)
            stats[outcome] = stats.get(outcome, 0) + 1
        db.commit()
    else:
        for (src_id, tgt_id), (relation, conf) in all_edges.items():
            if _causal_edge_exists(db, src_id, tgt_id, relation):
                stats["existing"] += 1
            elif _causal_would_create_cycle(db, src_id, tgt_id):
                stats["cycle"] += 1
            else:
                stats["inserted"] += 1

    return stats


def cmd_temporal_causes(args):
    """Forward traversal: what did event X cause? (downstream effects chain)"""
    db = get_db()
    event_id = args.event_id
    depth = args.depth or 6
    min_conf = args.min_confidence or 0.0

    seed = db.execute(
        "SELECT id, event_type, summary, agent_id, project, created_at FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not seed:
        json_out({"error": f"event {event_id} not found"})
        return

    rows = db.execute("""
        WITH RECURSIVE fwd(caused_id, chain_conf, depth, path) AS (
            SELECT ke.target_id, ke.weight, 1,
                   CAST(ke.source_id AS TEXT) || '->' || CAST(ke.target_id AS TEXT)
            FROM knowledge_edges ke
            WHERE ke.source_table = 'events' AND ke.target_table = 'events'
              AND ke.source_id = ?
              AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
              AND ke.weight >= ?
            UNION ALL
            SELECT ke.target_id, fwd.chain_conf * ke.weight, fwd.depth + 1,
                   fwd.path || '->' || CAST(ke.target_id AS TEXT)
            FROM knowledge_edges ke
            JOIN fwd ON ke.source_id = fwd.caused_id
            WHERE ke.source_table = 'events' AND ke.target_table = 'events'
              AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
              AND ke.weight >= ?
              AND fwd.depth < ?
              AND INSTR(fwd.path, CAST(ke.target_id AS TEXT)) = 0
        )
        SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.project, e.created_at,
               MIN(fwd.depth) as depth, MAX(fwd.chain_conf) as chain_confidence
        FROM fwd JOIN events e ON e.id = fwd.caused_id
        GROUP BY e.id
        ORDER BY depth ASC, chain_confidence DESC
    """, (event_id, min_conf, min_conf, depth)).fetchall()

    json_out({
        "seed": dict(seed),
        "direction": "forward",
        "description": "downstream effects — what did this event cause?",
        "chain_length": len(rows),
        "chain": [dict(r) for r in rows],
    })


def cmd_temporal_effects(args):
    """Backward traversal: why did event X happen? (upstream causes)"""
    db = get_db()
    event_id = args.event_id
    depth = args.depth or 6
    min_conf = args.min_confidence or 0.0

    seed = db.execute(
        "SELECT id, event_type, summary, agent_id, project, created_at FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not seed:
        json_out({"error": f"event {event_id} not found"})
        return

    rows = db.execute("""
        WITH RECURSIVE bwd(cause_id, chain_conf, depth, path) AS (
            SELECT ke.source_id, ke.weight, 1,
                   CAST(ke.target_id AS TEXT) || '<-' || CAST(ke.source_id AS TEXT)
            FROM knowledge_edges ke
            WHERE ke.source_table = 'events' AND ke.target_table = 'events'
              AND ke.target_id = ?
              AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
              AND ke.weight >= ?
            UNION ALL
            SELECT ke.source_id, bwd.chain_conf * ke.weight, bwd.depth + 1,
                   bwd.path || '<-' || CAST(ke.source_id AS TEXT)
            FROM knowledge_edges ke
            JOIN bwd ON ke.target_id = bwd.cause_id
            WHERE ke.source_table = 'events' AND ke.target_table = 'events'
              AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
              AND ke.weight >= ?
              AND bwd.depth < ?
              AND INSTR(bwd.path, CAST(ke.source_id AS TEXT)) = 0
        )
        SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.project, e.created_at,
               MIN(bwd.depth) as depth, MAX(bwd.chain_conf) as chain_confidence
        FROM bwd JOIN events e ON e.id = bwd.cause_id
        GROUP BY e.id
        ORDER BY depth ASC, chain_confidence DESC
    """, (event_id, min_conf, min_conf, depth)).fetchall()

    json_out({
        "seed": dict(seed),
        "direction": "backward",
        "description": "upstream causes — why did this event happen?",
        "chain_length": len(rows),
        "chain": [dict(r) for r in rows],
    })


def cmd_temporal_chain(args):
    """Bidirectional causal chain: upstream causes + downstream effects."""
    db = get_db()
    event_id = args.event_id
    depth = args.depth or 4
    min_conf = args.min_confidence or 0.0

    seed = db.execute(
        "SELECT id, event_type, summary, agent_id, project, created_at FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not seed:
        json_out({"error": f"event {event_id} not found"})
        return

    fwd = db.execute("""
        WITH RECURSIVE fwd(caused_id, chain_conf, depth, path) AS (
            SELECT ke.target_id, ke.weight, 1,
                   CAST(ke.source_id AS TEXT)||'->'||CAST(ke.target_id AS TEXT)
            FROM knowledge_edges ke
            WHERE ke.source_table='events' AND ke.target_table='events'
              AND ke.source_id=? AND ke.weight>=?
              AND ke.relation_type IN ('causes','triggered_by','contributes_to')
            UNION ALL
            SELECT ke.target_id, fwd.chain_conf*ke.weight, fwd.depth+1,
                   fwd.path||'->'||CAST(ke.target_id AS TEXT)
            FROM knowledge_edges ke JOIN fwd ON ke.source_id=fwd.caused_id
            WHERE ke.source_table='events' AND ke.target_table='events'
              AND ke.relation_type IN ('causes','triggered_by','contributes_to')
              AND ke.weight>=? AND fwd.depth<?
              AND INSTR(fwd.path,CAST(ke.target_id AS TEXT))=0
        )
        SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.created_at,
               MIN(fwd.depth) as depth, MAX(fwd.chain_conf) as chain_confidence
        FROM fwd JOIN events e ON e.id=fwd.caused_id
        GROUP BY e.id ORDER BY depth ASC
    """, (event_id, min_conf, min_conf, depth)).fetchall()

    bwd = db.execute("""
        WITH RECURSIVE bwd(cause_id, chain_conf, depth, path) AS (
            SELECT ke.source_id, ke.weight, 1,
                   CAST(ke.target_id AS TEXT)||'<-'||CAST(ke.source_id AS TEXT)
            FROM knowledge_edges ke
            WHERE ke.source_table='events' AND ke.target_table='events'
              AND ke.target_id=? AND ke.weight>=?
              AND ke.relation_type IN ('causes','triggered_by','contributes_to')
            UNION ALL
            SELECT ke.source_id, bwd.chain_conf*ke.weight, bwd.depth+1,
                   bwd.path||'<-'||CAST(ke.source_id AS TEXT)
            FROM knowledge_edges ke JOIN bwd ON ke.target_id=bwd.cause_id
            WHERE ke.source_table='events' AND ke.target_table='events'
              AND ke.relation_type IN ('causes','triggered_by','contributes_to')
              AND ke.weight>=? AND bwd.depth<?
              AND INSTR(bwd.path,CAST(ke.source_id AS TEXT))=0
        )
        SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.created_at,
               MIN(bwd.depth) as depth, MAX(bwd.chain_conf) as chain_confidence
        FROM bwd JOIN events e ON e.id=bwd.cause_id
        GROUP BY e.id ORDER BY depth ASC
    """, (event_id, min_conf, min_conf, depth)).fetchall()

    json_out({
        "seed": dict(seed),
        "upstream_causes": [dict(r) for r in bwd],
        "downstream_effects": [dict(r) for r in fwd],
        "upstream_count": len(bwd),
        "downstream_count": len(fwd),
    })


def cmd_temporal_auto_detect(args):
    """Run causal edge auto-detection pipeline over all events."""
    db = get_db()
    dry_run = getattr(args, "dry_run", False)
    stats = _build_causal_graph(db, dry_run=dry_run)
    label = "Would insert" if dry_run else "Inserted"
    json_out({
        "ok": True,
        "dry_run": dry_run,
        "stats": stats,
        "message": (
            f"{label} {stats.get('inserted', 0)} causal edges "
            f"({stats.get('existing', 0)} already existed, "
            f"{stats.get('cycle', 0)} cycles prevented, "
            f"{stats.get('found', 0)} total candidates)"
        ),
    })


def cmd_event_link(args):
    """Agent-reported causation: explicitly link two events as cause->effect."""
    db = get_db()
    cause_id = args.cause_event_id
    effect_id = args.effect_event_id
    relation = args.relation or "causes"
    confidence = args.confidence if args.confidence is not None else 0.9

    cause_row = db.execute("SELECT id FROM events WHERE id = ?", (cause_id,)).fetchone()
    if not cause_row:
        json_out({"ok": False, "error": f"cause event {cause_id} not found"})
        return
    effect_row = db.execute("SELECT id FROM events WHERE id = ?", (effect_id,)).fetchone()
    if not effect_row:
        json_out({"ok": False, "error": f"effect event {effect_id} not found"})
        return

    agent_id = getattr(args, "agent", None) or "unknown"
    outcome = _insert_causal_edge(db, cause_id, effect_id, relation, confidence, agent_id=agent_id)

    if outcome == "inserted":
        db.commit()
        json_out({
            "ok": True,
            "edge": {
                "cause_event_id": cause_id,
                "effect_event_id": effect_id,
                "relation": relation,
                "confidence": confidence,
                "reported_by": agent_id,
            },
        })
    elif outcome == "existing":
        json_out({"ok": False, "error": "edge already exists", "cause": cause_id, "effect": effect_id})
    else:
        json_out({"ok": False, "error": "would create cycle in causal DAG", "cause": cause_id, "effect": effect_id})


# ---------------------------------------------------------------------------
# MEB — Memory Event Bus
# ---------------------------------------------------------------------------

# Default tuning values — overridden by meb_config rows in the database.
_MEB_TTL_HOURS_DEFAULT    = 72
_MEB_MAX_DEPTH_DEFAULT    = 10_000
_MEB_PRUNE_ON_READ        = True


def _meb_config(db) -> dict:
    """Load MEB configuration from meb_config table, falling back to defaults."""
    try:
        rows = db.execute("SELECT key, value FROM meb_config").fetchall()
        cfg = {r["key"]: r["value"] for r in rows}
    except Exception:
        cfg = {}
    return {
        "ttl_hours":       int(cfg.get("ttl_hours",       _MEB_TTL_HOURS_DEFAULT)),
        "max_queue_depth": int(cfg.get("max_queue_depth", _MEB_MAX_DEPTH_DEFAULT)),
        "prune_on_read":   cfg.get("prune_on_read", "true").lower() == "true",
    }


def _meb_prune(db, cfg: dict) -> int:
    """Delete TTL-expired events and enforce max queue depth.

    Returns the number of rows deleted.
    """
    deleted = 0
    # 1. TTL-based expiry
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=cfg["ttl_hours"])).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    cur = db.execute("DELETE FROM memory_events WHERE created_at < ?", (cutoff,))
    deleted += cur.rowcount

    # 2. Max queue depth — evict oldest rows beyond the cap
    count = db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    overflow = count - cfg["max_queue_depth"]
    if overflow > 0:
        cur = db.execute(
            "DELETE FROM memory_events WHERE id IN "
            "(SELECT id FROM memory_events ORDER BY id ASC LIMIT ?)",
            (overflow,),
        )
        deleted += cur.rowcount

    if deleted:
        db.commit()
    return deleted


def cmd_meb_tail(args):
    """Poll recent memory_events, optionally filtered by agent, category, or scope."""
    db = get_db()
    cfg = _meb_config(db)

    if cfg["prune_on_read"]:
        _meb_prune(db, cfg)

    n       = getattr(args, "n", 20) or 20
    since   = getattr(args, "since", None)
    agent   = getattr(args, "agent", None)
    category = getattr(args, "category", None)
    scope   = getattr(args, "scope", None)
    include_backfill = getattr(args, "include_backfill", False)

    sql    = "SELECT me.*, m.content, m.confidence FROM memory_events me JOIN memories m ON me.memory_id = m.id WHERE 1=1"
    params = []

    if not include_backfill:
        sql += " AND me.operation != 'backfill'"
    if since is not None:
        sql += " AND me.id > ?"
        params.append(since)
    if agent:
        sql += " AND me.agent_id = ?"
        params.append(agent)
    if category:
        sql += " AND me.category = ?"
        params.append(category)
    if scope:
        sql += " AND me.scope LIKE ?"
        params.append(f"{scope}%")

    sql += " ORDER BY me.id DESC LIMIT ?"
    params.append(n)

    rows = db.execute(sql, params).fetchall()
    results = list(reversed(rows_to_list(rows)))
    for r in results:
        r["age"] = _age_str(r.get("created_at"))
    json_out(results)


def cmd_meb_subscribe(args):
    """Print the most recent memory event ID — use as --since cursor for polling.

    Agents call this once on startup to capture the current watermark, then
    call `meb tail --since <watermark>` on each subsequent heartbeat to receive
    only new memory events without replaying history.
    """
    db = get_db()
    row = db.execute("SELECT MAX(id) AS watermark FROM memory_events").fetchone()
    watermark = row["watermark"] if row and row["watermark"] is not None else 0
    json_out({"watermark": watermark, "hint": "pass this value as --since to meb tail"})


def cmd_meb_stats(args):
    """Queue depth, throughput, and propagation latency summary."""
    db = get_db()
    cfg = _meb_config(db)

    total = db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    by_op = db.execute(
        "SELECT operation, COUNT(*) AS cnt FROM memory_events GROUP BY operation"
    ).fetchall()
    by_cat = db.execute(
        "SELECT category, COUNT(*) AS cnt FROM memory_events GROUP BY category ORDER BY cnt DESC"
    ).fetchall()

    oldest_row = db.execute(
        "SELECT MIN(created_at) AS oldest FROM memory_events"
    ).fetchone()
    newest_row = db.execute(
        "SELECT MAX(created_at) AS newest FROM memory_events"
    ).fetchone()
    oldest = oldest_row["oldest"] if oldest_row else None
    newest = newest_row["newest"] if newest_row else None

    # Throughput: events in the last hour
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    recent_count = db.execute(
        "SELECT COUNT(*) FROM memory_events WHERE created_at >= ?", (one_hour_ago,)
    ).fetchone()[0]

    # Propagation latency: compare memory_events.created_at vs memories.created_at
    # for the most recent 100 non-backfill events
    latency_rows = db.execute(
        """
        SELECT
            (julianday(me.created_at) - julianday(m.created_at)) * 86400.0 AS latency_secs
        FROM memory_events me
        JOIN memories m ON me.memory_id = m.id
        WHERE me.operation IN ('insert', 'update')
        ORDER BY me.id DESC
        LIMIT 100
        """
    ).fetchall()
    latencies = [r["latency_secs"] for r in latency_rows if r["latency_secs"] is not None]
    avg_latency_ms = round(sum(latencies) / len(latencies) * 1000, 2) if latencies else None
    max_latency_ms = round(max(latencies) * 1000, 2) if latencies else None

    json_out({
        "total_events":       total,
        "by_operation":       {r["operation"]: r["cnt"] for r in by_op},
        "by_category":        {r["category"]: r["cnt"] for r in by_cat},
        "oldest_event":       oldest,
        "newest_event":       newest,
        "events_last_hour":   recent_count,
        "avg_latency_ms":     avg_latency_ms,
        "max_latency_ms":     max_latency_ms,
        "latency_sample_size": len(latencies),
        "config":             cfg,
    })


def cmd_meb_prune(args):
    """Manually trigger TTL + max-depth cleanup of memory_events."""
    db   = get_db()
    cfg  = _meb_config(db)

    # Allow CLI overrides
    if getattr(args, "ttl_hours", None) is not None:
        cfg["ttl_hours"] = args.ttl_hours
    if getattr(args, "max_depth", None) is not None:
        cfg["max_queue_depth"] = args.max_depth

    deleted = _meb_prune(db, cfg)
    remaining = db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    json_out({"ok": True, "deleted": deleted, "remaining": remaining})


# ---------------------------------------------------------------------------
# GAPS — Metacognitive gap detection
# ---------------------------------------------------------------------------

# Thresholds for gap classification

# Thresholds for gap classification
_STALENESS_GAP_DAYS = 7       # memories older than this trigger a staleness hole
_CONFIDENCE_GAP_THRESHOLD = 0.4  # avg_confidence below this = confidence hole
_COVERAGE_GAP_RESULT_COUNT = 3   # search returning fewer results flags a potential gap


def _compute_coverage_density(count, avg_conf, freshest_at):
    """Composite density score: count × avg_confidence × recency_factor."""
    if count == 0 or avg_conf is None:
        return 0.0
    age_days = _days_since(freshest_at)
    recency_factor = max(0.1, 1.0 - 0.02 * age_days)
    return round(count * avg_conf * recency_factor, 4)


def cmd_weights(args):
    """Show current adaptive retrieval weights and the store stats that drove them."""
    if not _SAL_AVAILABLE:
        json_out({"error": "salience_routing module not available"})
        return
    db = get_db()
    query = getattr(args, "query", None)
    nm = _neuro_get_state(db)
    weights = _sal.compute_adaptive_weights(db, query=query, neuro=nm or {})
    # Separate diagnostics from weights
    core = {k: v for k, v in weights.items() if not k.startswith("_")}
    diag = {k: v for k, v in weights.items() if k.startswith("_")}
    json_out({
        "weights": core,
        "diagnostics": diag,
        "query": query,
        "note": "weights sum to 1.0; diagnostics explain how they were derived",
    })


def cmd_gaps_refresh(args):
    """Recompute knowledge_coverage stats from current memories."""
    db = get_db()
    now = _now_ts()

    scopes = db.execute(
        "SELECT DISTINCT scope FROM memories WHERE retired_at IS NULL"
    ).fetchall()

    updated = 0
    for row in scopes:
        scope = row["scope"]
        stats = db.execute("""
            SELECT
                COUNT(*)           AS cnt,
                AVG(confidence)    AS avg_conf,
                MIN(confidence)    AS min_conf,
                MAX(confidence)    AS max_conf,
                MAX(created_at)    AS freshest,
                MIN(created_at)    AS stalest
            FROM memories
            WHERE scope = ? AND retired_at IS NULL
        """, (scope,)).fetchone()

        if not stats or stats["cnt"] == 0:
            continue

        density = _compute_coverage_density(stats["cnt"], stats["avg_conf"], stats["freshest"])
        db.execute("""
            INSERT INTO knowledge_coverage
                (scope, memory_count, avg_confidence, min_confidence, max_confidence,
                 freshest_memory_at, stalest_memory_at, coverage_density, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                memory_count       = excluded.memory_count,
                avg_confidence     = excluded.avg_confidence,
                min_confidence     = excluded.min_confidence,
                max_confidence     = excluded.max_confidence,
                freshest_memory_at = excluded.freshest_memory_at,
                stalest_memory_at  = excluded.stalest_memory_at,
                coverage_density   = excluded.coverage_density,
                last_computed_at   = excluded.last_computed_at
        """, (scope, stats["cnt"], stats["avg_conf"], stats["min_conf"], stats["max_conf"],
              stats["freshest"], stats["stalest"], density, now))
        updated += 1

    db.commit()
    json_out({"ok": True, "scopes_updated": updated, "computed_at": now})


def _log_gap(db, gap_type, scope, severity, triggered_by=None):
    """Insert or update a gap record. Skips if an identical unresolved gap already exists."""
    existing = db.execute(
        "SELECT id FROM knowledge_gaps WHERE gap_type=? AND scope=? AND resolved_at IS NULL",
        (gap_type, scope)
    ).fetchone()
    if existing:
        return  # already tracked, don't spam
    db.execute(
        "INSERT INTO knowledge_gaps (gap_type, scope, detected_at, triggered_by, severity) "
        "VALUES (?, ?, ?, ?, ?)",
        (gap_type, scope, _now_ts(), triggered_by, round(severity, 4))
    )


def cmd_gaps_scan(args):
    """Scan for coverage holes, staleness holes, and confidence holes."""
    db = get_db()
    now = _now_ts()
    report = {"coverage_holes": [], "staleness_holes": [], "confidence_holes": [], "scanned_at": now}

    # Ensure coverage stats are current
    _run_refresh_inline(db, now)

    # 1. Coverage holes: agents/projects in memory but with zero active memories
    #    Also detect scopes explicitly from agents table
    agent_scopes = {f"agent:{r['id']}" for r in db.execute(
        "SELECT id FROM agents WHERE status='active'"
    ).fetchall()}
    covered_scopes = {r["scope"] for r in db.execute(
        "SELECT scope FROM knowledge_coverage"
    ).fetchall()}

    for scope in agent_scopes - covered_scopes:
        severity = 1.0
        _log_gap(db, "coverage_hole", scope, severity, triggered_by="gap-scan")
        report["coverage_holes"].append({"scope": scope, "severity": severity})

    # 2. Staleness holes: coverage exists but freshest memory is too old
    stale_rows = db.execute("""
        SELECT scope, freshest_memory_at, memory_count
        FROM knowledge_coverage
        WHERE freshest_memory_at IS NOT NULL
          AND (julianday('now') - julianday(freshest_memory_at)) > ?
    """, (_STALENESS_GAP_DAYS,)).fetchall()

    for row in stale_rows:
        age = _days_since(row["freshest_memory_at"])
        severity = min(1.0, (age - _STALENESS_GAP_DAYS) / 30.0)
        _log_gap(db, "staleness_hole", row["scope"], severity, triggered_by="gap-scan")
        report["staleness_holes"].append({
            "scope": row["scope"],
            "freshest_at": row["freshest_memory_at"],
            "age_days": round(age, 1),
            "severity": round(severity, 4),
        })

    # 3. Confidence holes: avg confidence below threshold
    conf_rows = db.execute("""
        SELECT scope, avg_confidence, memory_count
        FROM knowledge_coverage
        WHERE avg_confidence IS NOT NULL AND avg_confidence < ?
    """, (_CONFIDENCE_GAP_THRESHOLD,)).fetchall()

    for row in conf_rows:
        severity = round((_CONFIDENCE_GAP_THRESHOLD - row["avg_confidence"]) / _CONFIDENCE_GAP_THRESHOLD, 4)
        _log_gap(db, "confidence_hole", row["scope"], severity, triggered_by="gap-scan")
        report["confidence_holes"].append({
            "scope": row["scope"],
            "avg_confidence": round(row["avg_confidence"], 4),
            "severity": severity,
        })

    db.commit()
    report["total_gaps"] = (
        len(report["coverage_holes"]) +
        len(report["staleness_holes"]) +
        len(report["confidence_holes"])
    )
    json_out(report)


def _run_refresh_inline(db, now):
    """Refresh knowledge_coverage inline (no subprocess). Shared by scan and cmd_gaps_refresh."""
    scopes = db.execute(
        "SELECT DISTINCT scope FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    for row in scopes:
        scope = row["scope"]
        stats = db.execute("""
            SELECT COUNT(*) AS cnt, AVG(confidence) AS avg_conf,
                   MIN(confidence) AS min_conf, MAX(confidence) AS max_conf,
                   MAX(created_at) AS freshest, MIN(created_at) AS stalest
            FROM memories WHERE scope=? AND retired_at IS NULL
        """, (scope,)).fetchone()
        if not stats or stats["cnt"] == 0:
            continue
        density = _compute_coverage_density(stats["cnt"], stats["avg_conf"], stats["freshest"])
        db.execute("""
            INSERT INTO knowledge_coverage
                (scope, memory_count, avg_confidence, min_confidence, max_confidence,
                 freshest_memory_at, stalest_memory_at, coverage_density, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                memory_count=excluded.memory_count, avg_confidence=excluded.avg_confidence,
                min_confidence=excluded.min_confidence, max_confidence=excluded.max_confidence,
                freshest_memory_at=excluded.freshest_memory_at,
                stalest_memory_at=excluded.stalest_memory_at,
                coverage_density=excluded.coverage_density,
                last_computed_at=excluded.last_computed_at
        """, (scope, stats["cnt"], stats["avg_conf"], stats["min_conf"], stats["max_conf"],
              stats["freshest"], stats["stalest"], density, now))


def cmd_gaps_list(args):
    """List known blind spots (unresolved gaps), sorted by severity."""
    db = get_db()
    limit = args.limit or 50
    gap_type_filter = getattr(args, "type", None)

    query = "SELECT * FROM knowledge_gaps WHERE resolved_at IS NULL"
    params = []
    if gap_type_filter:
        query += " AND gap_type = ?"
        params.append(gap_type_filter)
    query += " ORDER BY severity DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(query, params).fetchall()
    gaps = rows_to_list(rows)

    # Enrich with coverage stats where available
    for gap in gaps:
        cov = db.execute(
            "SELECT memory_count, avg_confidence, freshest_memory_at, coverage_density "
            "FROM knowledge_coverage WHERE scope=?",
            (gap["scope"],)
        ).fetchone()
        if cov:
            gap["coverage"] = dict(cov)

    total_unresolved = db.execute(
        "SELECT COUNT(*) AS n FROM knowledge_gaps WHERE resolved_at IS NULL"
    ).fetchone()["n"]

    json_out({"total_unresolved": total_unresolved, "gaps": gaps})


def cmd_gaps_resolve(args):
    """Mark a gap as resolved."""
    db = get_db()
    row = db.execute("SELECT id FROM knowledge_gaps WHERE id=?", (args.id,)).fetchone()
    if not row:
        json_out({"ok": False, "error": f"Gap {args.id} not found"})
        return
    db.execute(
        "UPDATE knowledge_gaps SET resolved_at=?, resolution_note=? WHERE id=?",
        (_now_ts(), args.note, args.id)
    )
    db.commit()
    json_out({"ok": True, "gap_id": args.id, "resolved_at": _now_ts()})


# ---------------------------------------------------------------------------
# EXPERTISE — Agent Expertise Directory
# ---------------------------------------------------------------------------

_EXPERTISE_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "has", "have", "had", "do", "did", "done", "will", "would", "could",
    "should", "may", "might", "can", "it", "its", "this", "that", "these",
    "those", "not", "no", "so", "if", "then", "when", "where", "how",
    "what", "which", "who", "all", "any", "as", "up", "out", "into",
    "new", "old", "now", "also", "just", "using", "used", "via", "per",
    "add", "get", "set", "run", "use", "see", "let", "put", "try", "fix",
    "make", "take", "give", "show", "need", "more", "some", "than", "only",
}

_EXPERTISE_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _expertise_extract_tokens(text):
    tokens = _EXPERTISE_TOKEN_RE.findall((text or "").lower())
    return [t for t in tokens if t not in _EXPERTISE_STOP_WORDS]


def _expertise_scope_to_domain(scope):
    if not scope or scope == "global":
        return None
    parts = scope.split(":", 1)
    if len(parts) == 2:
        return parts[1].split(":")[0]
    return scope


def _ensure_expertise_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_expertise (
            agent_id       TEXT NOT NULL REFERENCES agents(id),
            domain         TEXT NOT NULL,
            strength       REAL NOT NULL DEFAULT 0.0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_active    TEXT,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (agent_id, domain)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_expertise_domain ON agent_expertise(domain)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_expertise_strength ON agent_expertise(strength DESC)")
    db.commit()


def _build_expertise_for_agent(db, agent_id):
    domain_evidence = {}

    rows = db.execute(
        "SELECT scope, category, content, created_at FROM memories "
        "WHERE agent_id=? AND retired_at IS NULL",
        (agent_id,)
    ).fetchall()
    for r in rows:
        ts = r["created_at"]
        d = _expertise_scope_to_domain(r["scope"])
        if d:
            domain_evidence.setdefault(d, []).append(ts)
        if r["category"] and r["category"] not in ("identity",):
            domain_evidence.setdefault(r["category"], []).append(ts)
        for tok in _expertise_extract_tokens(r["content"])[:8]:
            domain_evidence.setdefault(tok, []).append(ts)

    rows = db.execute(
        "SELECT project, event_type, summary, created_at FROM events WHERE agent_id=?",
        (agent_id,)
    ).fetchall()
    for r in rows:
        ts = r["created_at"]
        if r["project"]:
            domain_evidence.setdefault(r["project"], []).append(ts)
        if r["event_type"] and r["event_type"] not in ("session_start", "session_end"):
            domain_evidence.setdefault(r["event_type"], []).append(ts)
        for tok in _expertise_extract_tokens(r["summary"])[:8]:
            domain_evidence.setdefault(tok, []).append(ts)

    if not domain_evidence:
        return 0

    now = datetime.now(timezone.utc)

    def _rw(ts_str):
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_days = max(0.0, (now - ts).total_seconds() / 86400)
            return math.exp(-0.03 * age_days)
        except Exception:
            return 0.5

    max_count = max(len(v) for v in domain_evidence.values())

    upserted = 0
    for domain, timestamps in domain_evidence.items():
        count = len(timestamps)
        avg_recency = sum(_rw(t) for t in timestamps) / count
        strength = round(math.sqrt((count / max_count) * avg_recency), 4)
        last_active = max(timestamps)
        db.execute(
            """
            INSERT INTO agent_expertise (agent_id, domain, strength, evidence_count, last_active, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(agent_id, domain) DO UPDATE SET
                strength=excluded.strength,
                evidence_count=excluded.evidence_count,
                last_active=excluded.last_active,
                updated_at=excluded.updated_at
            """,
            (agent_id, domain, strength, count, last_active)
        )
        upserted += 1

    db.commit()
    return upserted


def cmd_expertise_build(args):
    db = get_db()
    _ensure_expertise_table(db)

    if getattr(args, "agent_id", None):
        agent_ids = [args.agent_id]
    else:
        rows = db.execute("SELECT id FROM agents WHERE status='active'").fetchall()
        agent_ids = [r["id"] for r in rows]

    quiet = getattr(args, "quiet", False)
    results = []
    for aid in agent_ids:
        n = _build_expertise_for_agent(db, aid)
        results.append({"agent_id": aid, "domains_indexed": n})
        if not quiet:
            print(f"  {aid}: {n} domains")

    if getattr(args, "json", False):
        json_out({"ok": True, "agents_processed": len(results), "results": results})
    elif not quiet:
        print(f"\nDone. {len(results)} agents processed.")


def cmd_expertise_show(args):
    db = get_db()
    _ensure_expertise_table(db)

    agent_id = args.agent_id
    limit = getattr(args, "limit", None) or 20

    rows = db.execute(
        "SELECT domain, strength, evidence_count, brier_score, last_active "
        "FROM agent_expertise WHERE agent_id=? ORDER BY strength DESC LIMIT ?",
        (agent_id, limit)
    ).fetchall()

    if not rows:
        print(f"No expertise data for '{agent_id}'. Run: brainctl expertise build --agent {agent_id}")
        return

    if getattr(args, "json", False):
        json_out({"agent_id": agent_id, "expertise": rows_to_list(rows)})
        return

    print(f"Expertise profile: {agent_id}")
    print(f"{'Domain':<30} {'Strength':>8}  {'Evidence':>8}  {'Brier':>8}  Last Active")
    print("-" * 78)
    for r in rows:
        last = (r["last_active"] or "")[:10]
        brier = f"{r['brier_score']:.4f}" if r["brier_score"] is not None else "     N/A"
        print(f"  {r['domain']:<28} {r['strength']:>8.4f}  {r['evidence_count']:>8}  {brier:>8}  {last}")


def cmd_expertise_list(args):
    """List all agents with their top expertise domains and scores."""
    db = get_db()
    _ensure_expertise_table(db)

    min_strength = getattr(args, "min_strength", None) or 0.0
    limit = getattr(args, "limit", None) or 50
    domain_filter = getattr(args, "domain", None)

    if domain_filter:
        rows = db.execute(
            "SELECT agent_id, domain, strength, evidence_count, brier_score, last_active "
            "FROM agent_expertise WHERE domain LIKE ? AND strength >= ? "
            "ORDER BY strength DESC LIMIT ?",
            (f"%{domain_filter}%", min_strength, limit)
        ).fetchall()
        if getattr(args, "json", False):
            json_out({"count": len(rows), "entries": rows_to_list(rows)})
            return
        if not rows:
            print(f"No expertise data for domain: {domain_filter}")
            return
        print(f"Expertise for domain: {domain_filter}")
        print(f"{'Agent':<30} {'Strength':>8}  {'Evidence':>8}  {'Brier':>8}  Last Active")
        print("-" * 78)
        for r in rows:
            brier = f"{r['brier_score']:.4f}" if r["brier_score"] is not None else "     N/A"
            last = (r["last_active"] or "")[:10]
            print(f"  {r['agent_id']:<28} {r['strength']:>8.4f}  {r['evidence_count']:>8}  {brier:>8}  {last}")
    else:
        # One row per agent: highest-strength domain + aggregate domain count
        rows = db.execute(
            """
            SELECT agent_id,
                   MIN(domain) AS top_domain,
                   MAX(strength) AS top_strength,
                   MAX(brier_score) AS brier_score,
                   COUNT(*) AS domain_count,
                   MAX(last_active) AS last_active
            FROM agent_expertise
            WHERE strength >= ?
            GROUP BY agent_id
            ORDER BY top_strength DESC
            LIMIT ?
            """,
            (min_strength, limit)
        ).fetchall()
        if getattr(args, "json", False):
            json_out({"count": len(rows), "agents": rows_to_list(rows)})
            return
        if not rows:
            print("No expertise data found. Run: brainctl expertise build")
            return
        print(f"Agent Expertise Summary (min_strength={min_strength})")
        print(f"{'Agent':<30} {'Top Domain':<25} {'Strength':>8}  {'Domains':>7}  Brier")
        print("-" * 85)
        for r in rows:
            brier = f"{r['brier_score']:.4f}" if r["brier_score"] is not None else "N/A"
            top_domain = (r["top_domain"] or "")[:24]
            print(f"  {r['agent_id']:<28} {top_domain:<25} {r['top_strength']:>8.4f}  {r['domain_count']:>7}  {brier}")


def cmd_expertise_update(args):
    """Update brier_score and/or strength for an agent+domain pair."""
    db = get_db()
    _ensure_expertise_table(db)

    agent_id = args.agent_id
    domain = args.domain
    brier = getattr(args, "brier", None)
    strength = getattr(args, "strength", None)

    if brier is None and strength is None:
        print("ERROR: provide at least one of --brier or --strength", file=sys.stderr)
        sys.exit(1)

    row = db.execute(
        "SELECT agent_id FROM agent_expertise WHERE agent_id=? AND domain=?",
        (agent_id, domain)
    ).fetchone()
    if not row:
        print(f"ERROR: no expertise entry for agent='{agent_id}' domain='{domain}'", file=sys.stderr)
        print(f"  Run: brainctl expertise build --agent {agent_id}", file=sys.stderr)
        sys.exit(1)

    updates = []
    params = []
    if brier is not None:
        if not (0.0 <= brier <= 2.0):
            print("ERROR: brier_score must be between 0.0 and 2.0", file=sys.stderr)
            sys.exit(1)
        updates.append("brier_score=?")
        params.append(brier)
    if strength is not None:
        if not (0.0 <= strength <= 1.0):
            print("ERROR: strength must be between 0.0 and 1.0", file=sys.stderr)
            sys.exit(1)
        updates.append("strength=?")
        params.append(strength)
    updates.append("updated_at=datetime('now')")
    params.extend([agent_id, domain])

    db.execute(
        f"UPDATE agent_expertise SET {', '.join(updates)} WHERE agent_id=? AND domain=?",
        params
    )
    db.commit()

    result = {"ok": True, "agent_id": agent_id, "domain": domain}
    if brier is not None:
        result["brier_score"] = brier
    if strength is not None:
        result["strength"] = strength
    json_out(result)


def cmd_whosknows(args):
    db = get_db()
    _ensure_expertise_table(db)

    topic = " ".join(args.topic)
    if not topic.strip():
        print("ERROR: topic is required", file=sys.stderr)
        sys.exit(1)

    top_n = getattr(args, "top_n", None) or 10
    min_strength = getattr(args, "min_strength", None) or 0.05

    tokens = _expertise_extract_tokens(topic)
    raw_words = [w.lower() for w in topic.split() if len(w) >= 3]
    all_terms = list(dict.fromkeys(tokens + raw_words))

    if not all_terms:
        print("ERROR: no meaningful tokens in topic query", file=sys.stderr)
        sys.exit(1)

    like_clauses = " OR ".join("e.domain LIKE ?" for _ in all_terms)
    like_params = [f"%{t}%" for t in all_terms]

    rows = db.execute(
        f"""
        SELECT
            e.agent_id,
            a.display_name,
            SUM(e.strength) as total_score,
            COUNT(DISTINCT e.domain) as matched_domains,
            GROUP_CONCAT(e.domain || ':' || ROUND(e.strength,3), ', ') as domain_breakdown,
            MAX(e.last_active) as last_active
        FROM agent_expertise e
        JOIN agents a ON a.id = e.agent_id
        WHERE ({like_clauses})
          AND e.strength >= ?
          AND a.status = 'active'
        GROUP BY e.agent_id
        ORDER BY total_score DESC
        LIMIT ?
        """,
        like_params + [min_strength, top_n]
    ).fetchall()

    if not rows:
        print(f"No agents found with expertise matching: {topic!r}")
        print(f"Searched terms: {all_terms}")
        print("Try running: brainctl expertise build")
        return

    if getattr(args, "json", False):
        json_out({"topic": topic, "terms_searched": all_terms, "results": rows_to_list(rows)})
        return

    verbose = getattr(args, "verbose", False)
    print(f"Who knows about: {topic!r}")
    print(f"(Terms: {', '.join(all_terms[:8])}{'...' if len(all_terms) > 8 else ''})")
    print()
    print(f"{'#':<3} {'Agent':<32} {'Score':>7}  {'Domains':>7}  Last Active")
    print("-" * 74)
    for i, r in enumerate(rows, 1):
        last = (r["last_active"] or "")[:10]
        name = r["display_name"] or r["agent_id"]
        print(f"  {i:<2} {name:<32} {r['total_score']:>7.4f}  {r['matched_domains']:>7}  {last}")
        if verbose:
            print(f"     ↳ {(r['domain_breakdown'] or '')[:120]}")


# ---------------------------------------------------------------------------
# NEURO-SYMBOLIC REASONING
# ---------------------------------------------------------------------------

def _reason_l1_search(db, query: str, limit: int = 10):
    """L1 associative retrieval — hybrid BM25+vec RRF over memories and events."""
    fts_query = _sanitize_fts_query(query)
    db_vec = _try_get_db_with_vec()
    q_blob = _embed_query_safe(query) if db_vec else None
    hybrid = db_vec is not None and q_blob is not None
    fetch_limit = limit * 4

    fts_mems = []
    if fts_query:
        rows = db.execute(
            "SELECT m.id, 'memory' as type, m.category, m.content, m.confidence, m.scope, m.created_at, f.rank as fts_rank "
            "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_query, fetch_limit)
        ).fetchall()
        fts_mems = rows_to_list(rows)

    vec_mems = []
    if hybrid:
        try:
            vec_rows = db_vec.execute(
                "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k=?",
                (q_blob, fetch_limit)
            ).fetchall()
            if vec_rows:
                rowids = [r["rowid"] for r in vec_rows]
                dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
                ph = ",".join("?" * len(rowids))
                src = db_vec.execute(
                    f"SELECT id, 'memory' as type, category, content, confidence, scope, created_at "
                    f"FROM memories WHERE id IN ({ph}) AND retired_at IS NULL", rowids
                ).fetchall()
                vec_mems = [dict(r) | {"distance": round(dist_map.get(r["id"], 1.0), 4)} for r in src]
        except Exception:
            pass

    mem_merged = _rrf_fuse(fts_mems, vec_mems) if hybrid else [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_mems]
    for r in mem_merged:
        tw = _temporal_weight(r.get("created_at"), r.get("scope"))
        r["temporal_weight"] = round(tw, 4)
        r["final_score"] = round(r.get("rrf_score", 0.0) * tw, 8)
    mem_merged.sort(key=lambda r: r["final_score"], reverse=True)
    mem_merged = mem_merged[:limit]

    fts_evts = []
    if fts_query:
        rows = db.execute(
            "SELECT e.id, 'event' as type, e.event_type, e.summary, e.importance, e.project, e.created_at, f.rank as fts_rank "
            "FROM events e JOIN events_fts f ON e.id = f.rowid "
            "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, fetch_limit)
        ).fetchall()
        fts_evts = rows_to_list(rows)

    evt_merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_evts]
    for r in evt_merged:
        tw = _temporal_weight(r.get("created_at"), ("project:" + r["project"]) if r.get("project") else "global")
        r["temporal_weight"] = round(tw, 4)
        r["final_score"] = round(r.get("rrf_score", 0.0) * tw, 8)
    evt_merged.sort(key=lambda r: r["final_score"], reverse=True)
    evt_merged = evt_merged[:limit]

    if db_vec:
        db_vec.close()

    return mem_merged, evt_merged


def _reason_l2_expand(db, l1_memories, l1_events, hops: int = 2, top_k: int = 15):
    """L2 structural expansion — spreading activation with provenance chain metadata."""
    seed_ids = [("memories", r["id"]) for r in l1_memories] + [("events", r["id"]) for r in l1_events]
    if not seed_ids:
        return [], {}

    provenance = {}
    for table, id_ in seed_ids:
        provenance[(table, id_)] = [{"from_id": id_, "from_table": table, "edge_type": "seed", "weight": 1.0}]

    activation = {k: 1.0 for k in seed_ids}
    frontier = list(seed_ids)
    weight_by_type = {
        "semantic_similar": 1.0, "causal_chain_member": 0.8,
        "causes": 0.9, "topical_tag": 0.5,
        "topical_project": 0.4, "topical_scope": 0.4,
    }

    for hop in range(hops):
        next_frontier = []
        decay_at_hop = 0.6 ** (hop + 1)
        for source_table, source_id in frontier:
            rows = db.execute(
                "SELECT target_table, target_id, relation_type, weight "
                "FROM knowledge_edges WHERE source_table=? AND source_id=? "
                "UNION ALL "
                "SELECT source_table, source_id, relation_type, weight "
                "FROM knowledge_edges WHERE target_table=? AND target_id=?",
                (source_table, source_id, source_table, source_id),
            ).fetchall()
            for t_table, t_id, rel_type, edge_weight in rows:
                type_weight = weight_by_type.get(rel_type, 0.5)
                contribution = decay_at_hop * edge_weight * type_weight
                key = (t_table, int(t_id))
                if key not in activation or activation[key] < contribution:
                    activation[key] = contribution
                    parent_chain = provenance.get((source_table, source_id), [])
                    provenance[key] = parent_chain + [{
                        "from_id": source_id, "from_table": source_table,
                        "edge_type": rel_type, "weight": round(edge_weight * type_weight, 4)
                    }]
                    next_frontier.append(key)
        frontier = next_frontier

    seed_set = set(seed_ids)
    expansions = sorted(
        [(k, v) for k, v in activation.items() if k not in seed_set],
        key=lambda x: -x[1],
    )[:top_k]

    expanded = []
    for (tbl, nid), act_score in expansions:
        row = None
        if tbl == "memories":
            row = db.execute(
                "SELECT id, 'memory' as type, category, content, confidence, scope, created_at "
                "FROM memories WHERE id=? AND retired_at IS NULL", (nid,)
            ).fetchone()
        elif tbl == "events":
            row = db.execute(
                "SELECT id, 'event' as type, event_type, summary, importance, project, created_at "
                "FROM events WHERE id=?", (nid,)
            ).fetchone()
        if row:
            r = dict(row)
            r["source"] = "graph"
            r["activation"] = round(act_score, 4)
            r["graph_chain"] = provenance.get((tbl, nid), [])
            expanded.append(r)

    return expanded, provenance


def _reason_l3_infer(db, query: str, l1_memories, l2_expanded, agent_id: str = "unknown", min_confidence: float = 0.0):
    """L3 inferential — policy rule evaluation + confidence chaining over L1+L2 evidence."""
    _ensure_policy_tables(db)

    all_evidence = []
    for r in l1_memories:
        conf = r.get("confidence") or 0.5
        score = r.get("final_score") or r.get("rrf_score") or 0.01
        all_evidence.append({
            "id": r["id"], "type": r.get("type", "memory"),
            "content": (r.get("content") or r.get("summary") or "")[:200],
            "confidence": conf, "score": score, "role": "premise",
            "recalled_via": r.get("source", "search"),
        })
    for r in l2_expanded:
        conf = r.get("confidence") or 0.5
        chain = r.get("graph_chain", [])
        all_evidence.append({
            "id": r["id"], "type": r.get("type", "memory"),
            "content": (r.get("content") or r.get("summary") or "")[:200],
            "confidence": conf, "score": r.get("activation", 0.1), "role": "connector",
            "recalled_via": f"graph:{chain[-1]['edge_type']}" if chain else "graph",
            "graph_chain": chain,
        })

    # Confidence chaining over top-5 by score
    chain_evidence = sorted(all_evidence, key=lambda x: -x["score"])[:5]
    if chain_evidence:
        chain_confidence = 1.0
        for e in chain_evidence:
            chain_confidence *= e["confidence"]
            if e.get("graph_chain"):
                chain_confidence *= e["graph_chain"][-1].get("weight", 1.0)
        chain_confidence = round(chain_confidence, 4)
    else:
        chain_confidence = 0.0

    # Policy rule evaluation
    matched_policies = []
    rules_evaluated = 0
    now_str = datetime.utcnow().isoformat()
    try:
        fts_q = " OR ".join(w for w in query.split() if len(w) > 3)
        if fts_q:
            pol_rows = db.execute(
                "SELECT pm.*, pmf.rank as fts_rank "
                "FROM policy_memories_fts pmf JOIN policy_memories pm ON pm.rowid = pmf.rowid "
                "WHERE pmf MATCH ? AND pm.status = 'active' AND (pm.expires_at IS NULL OR pm.expires_at > ?) "
                "ORDER BY pmf.rank LIMIT 10",
                [fts_q, now_str]
            ).fetchall()
        else:
            pol_rows = []
        rules_evaluated = len(pol_rows)
        for row in pol_rows:
            r = dict(row)
            eff_conf = _policy_effective_confidence(
                r["confidence_threshold"], r["wisdom_half_life_days"], r["last_validated_at"]
            )
            if eff_conf < min_confidence:
                continue
            matched_policies.append({
                "rule_id": r["id"], "name": r["name"],
                "trigger": r.get("trigger_condition", ""),
                "action": r.get("action_directive", ""),
                "confidence": round(eff_conf, 4),
                "category": r.get("category", ""),
            })
    except Exception:
        rules_evaluated = 0

    if not all_evidence:
        conclusion = f"No evidence found for: {query!r}"
        tier = "L1-gap"
    elif matched_policies:
        top_pol = matched_policies[0]
        conclusion = f"Policy match: {top_pol['action']} (triggered by: {top_pol['trigger']})"
        tier = "L3-policy"
    elif chain_confidence >= 0.5:
        top = chain_evidence[0]
        snippet = (top["content"] or "")[:120].replace("\n", " ")
        conclusion = f"High-confidence evidence: {snippet}"
        tier = "L3-inferential"
    elif chain_confidence >= 0.2:
        conclusion = f"Moderate evidence chain ({len(chain_evidence)} nodes, confidence={chain_confidence})"
        tier = "L3-inferential"
    else:
        conclusion = f"Weak evidence — chain confidence {chain_confidence} below threshold"
        tier = "L3-weak"

    return (
        {"conclusion": conclusion, "confidence": chain_confidence, "tier": tier,
         "chain_depth": max((len(e.get("graph_chain", [])) for e in chain_evidence), default=0)},
        all_evidence, matched_policies, rules_evaluated
    )


def cmd_reason(args):
    """brainctl reason <query> — L1+L2: hybrid search + structural graph expansion."""
    import time
    t0 = time.monotonic()
    db = get_db()
    query = args.query
    agent_id = args.agent or "unknown"
    limit = args.limit or 10
    hops = args.hops or 2

    l1_memories, l1_events = _reason_l1_search(db, query, limit=limit)
    l2_expanded, _ = _reason_l2_expand(db, l1_memories, l1_events, hops=hops, top_k=15)

    latency_ms = round((time.monotonic() - t0) * 1000)
    log_access(db, agent_id, "reason", query=query, result_count=len(l1_memories) + len(l2_expanded))
    db.commit()

    result = {
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

    if args.format == "json":
        json_out(result)
        return

    print(f"\nReason: {query!r}  [L1+L2, {latency_ms}ms]\n")
    print(f"L1 Direct ({len(l1_memories)} memories, {len(l1_events)} events):")
    for r in l1_memories[:5]:
        content = (r.get("content") or "")[:100].replace("\n", " ")
        print(f"  [{r['id']}] conf={r.get('confidence', '?')}  {content}")
    for r in l1_events[:3]:
        summary = (r.get("summary") or "")[:100].replace("\n", " ")
        print(f"  [evt:{r['id']}]  {summary}")
    print(f"\nL2 Graph Expansions ({len(l2_expanded)}):")
    for r in l2_expanded[:5]:
        content = (r.get("content") or r.get("summary") or "")[:100].replace("\n", " ")
        chain = r.get("graph_chain", [])
        via = chain[-1]["edge_type"] if chain else "?"
        print(f"  [{r['id']}] act={r.get('activation', '?')}  via={via}  {content}")


def cmd_infer(args):
    """brainctl infer <query> — L1+L2+L3: full neuro-symbolic inference."""
    import time
    t0 = time.monotonic()
    db = get_db()
    query = args.query
    agent_id = args.agent or "unknown"
    limit = args.limit or 10
    hops = args.hops or 2
    min_confidence = args.min_confidence if args.min_confidence is not None else 0.0

    l1_memories, l1_events = _reason_l1_search(db, query, limit=limit)
    l2_expanded, _ = _reason_l2_expand(db, l1_memories, l1_events, hops=hops, top_k=15)
    inference, all_evidence, matched_policies, rules_evaluated = _reason_l3_infer(
        db, query, l1_memories, l2_expanded, agent_id=agent_id, min_confidence=min_confidence
    )

    latency_ms = round((time.monotonic() - t0) * 1000)
    log_access(db, agent_id, "infer", query=query, result_count=len(all_evidence))
    db.commit()

    result = {
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

    if args.format == "json":
        json_out(result)
        return

    inf = inference
    print(f"\nInfer: {query!r}  [L1+L2+L3, {latency_ms}ms]\n")
    print(f"Conclusion ({inf['tier']}, confidence={inf['confidence']}):")
    print(f"  {inf['conclusion']}\n")
    print(f"Evidence ({len(all_evidence)} nodes, chain_depth={inf['chain_depth']}):")
    for e in all_evidence[:5]:
        snippet = (e.get("content") or "")[:100].replace("\n", " ")
        print(f"  [{e['type']}:{e['id']}] conf={e['confidence']}  role={e['role']}  via={e['recalled_via']}")
        print(f"    {snippet}")
    if matched_policies:
        print(f"\nMatched Policies ({len(matched_policies)}):")
        for p in matched_policies:
            print(f"  [{p['rule_id']}] {p['name']}  conf={p['confidence']}")
            print(f"    Trigger: {p['trigger']}")
            print(f"    Action:  {p['action']}")
    print(f"\nProvenance: L1={result['provenance']['l1_results']} "
          f"L2={result['provenance']['l2_expansions']} "
          f"policies={rules_evaluated} triggered={len(matched_policies)}")


# ---------------------------------------------------------------------------
# ACTIVE INFERENCE LAYER — brainctl infer-pretask / infer-gapfill
# ---------------------------------------------------------------------------

_AIL_FREE_ENERGY_THRESHOLD = 0.15  # (1-confidence)*importance must exceed this to flag a gap


def cmd_infer_pretask(args):
    """Pre-task uncertainty scan: query low-confidence memories, log gaps, report free energy."""
    import time
    db = get_db()
    agent_id = args.agent or "unknown"
    task_desc = args.task_desc
    limit = getattr(args, "limit", None) or 10
    t0 = time.monotonic()
    fts_q = _sanitize_fts_query(task_desc)

    gap_hits = []
    if fts_q:
        try:
            first_word = fts_q.split()[0] if fts_q.split() else ""
            gap_hits = rows_to_list(db.execute(
                "SELECT * FROM knowledge_gaps WHERE domain LIKE ? OR description LIKE ? "
                "ORDER BY importance DESC LIMIT ?",
                (f"%{first_word}%", f"%{task_desc[:80]}%", limit)
            ).fetchall())
        except Exception:
            gap_hits = []

    memories = []
    if fts_q:
        try:
            mem_rows = db.execute(
                "SELECT m.* FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? AND m.retired_at IS NULL AND m.confidence < 0.7 "
                "ORDER BY rank LIMIT ?",
                (fts_q, limit * 3)
            ).fetchall()
            memories = rows_to_list(mem_rows)
        except Exception:
            memories = []

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

    now = _now_ts()
    log_ids = []
    for gap in uncertainty_gaps:
        cur = db.execute(
            "INSERT INTO agent_uncertainty_log (agent_id, task_desc, gap_topic, free_energy, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, task_desc[:500], gap["topic"][:200], gap["free_energy"], now)
        )
        log_ids.append(cur.lastrowid)

    latency_ms = round((time.monotonic() - t0) * 1000)
    log_access(db, agent_id, "infer-pretask", "memories", query=task_desc[:200], result_count=len(uncertainty_gaps))
    db.commit()

    result = {
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

    if getattr(args, "format", "text") == "json":
        json_out(result)
        return

    print(f"\nPre-Task Uncertainty Report  [{latency_ms}ms]")
    print(f"Task: {task_desc[:80]!r}  |  Agent: {agent_id}")
    print(f"Gaps: {len(uncertainty_gaps)}  |  knowledge_gaps matched: {len(gap_hits)}\n")
    if not uncertainty_gaps:
        print("  No high-uncertainty memories found. Proceed with confidence.")
        return
    print(f"{'#':<4} {'FreeEnergy':<12} {'Conf':<7} {'Imp':<7} Topic")
    print("-" * 80)
    for i, gap in enumerate(uncertainty_gaps, 1):
        print(f"{i:<4} {gap['free_energy']:<12} {gap['confidence']:<7} {gap['importance']:<7} {gap['topic'][:55]}")
    print(f"\n{len(uncertainty_gaps)} gaps logged (ids: {log_ids[:5]}{'...' if len(log_ids) > 5 else ''})")


def cmd_infer_gapfill(args):
    """Gap fill after task: resolve open uncertainty log entries, optionally create memory."""
    db = get_db()
    agent_id = args.agent or "unknown"
    task_desc = args.task_desc
    content = getattr(args, "content", None)
    now = _now_ts()

    open_gaps = db.execute(
        "SELECT * FROM agent_uncertainty_log WHERE agent_id = ? AND resolved_at IS NULL "
        "AND task_desc LIKE ? ORDER BY free_energy DESC LIMIT 20",
        (agent_id, f"%{task_desc[:50]}%")
    ).fetchall()

    if not open_gaps:
        open_gaps = db.execute(
            "SELECT * FROM agent_uncertainty_log WHERE agent_id = ? AND resolved_at IS NULL "
            "AND created_at > datetime(\'now\', \'-24 hours\') ORDER BY free_energy DESC LIMIT 10",
            (agent_id,)
        ).fetchall()

    memory_id = None
    if content:
        cur = db.execute(
            "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
            "VALUES (?, \'lesson\', \'global\', ?, 0.80, ?, ?)",
            (agent_id, content[:2000], now, now)
        )
        memory_id = cur.lastrowid

    resolved_ids = []
    for row in open_gaps:
        db.execute(
            "UPDATE agent_uncertainty_log SET resolved_at = ?, resolved_by = ? WHERE id = ?",
            (now, memory_id, row["id"])
        )
        resolved_ids.append(row["id"])

    log_access(db, agent_id, "infer-gapfill", "agent_uncertainty_log",
               query=task_desc[:200], result_count=len(resolved_ids))
    db.commit()

    result = {
        "task_desc": task_desc,
        "agent_id": agent_id,
        "resolved_gaps": resolved_ids,
        "memory_created": memory_id,
        "total_resolved": len(resolved_ids),
    }

    if getattr(args, "format", "text") == "json":
        json_out(result)
        return

    print(f"\nGap Fill  |  Agent: {agent_id}")
    print(f"Task: {task_desc[:80]!r}")
    print(f"Gaps resolved: {len(resolved_ids)}")
    if memory_id:
        print(f"New memory: #{memory_id}")
    elif resolved_ids:
        print("No memory written (use --content to record what was learned).")
    else:
        print("No open gaps found matching this task context.")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="brainctl",
        description="brainctl — A cognitive memory system for AI agents.\n\n"
                    "Core commands:\n"
                    "  init          Create a fresh brain.db\n"
                    "  memory        Add, search, list, retire memories\n"
                    "  entity        Create, search, relate typed entities\n"
                    "  event         Log and search events\n"
                    "  search        Universal cross-table search (FTS5 + vector)\n"
                    "  affect        Functional affect tracking and safety monitoring\n"
                    "  stats         Database statistics\n"
                    "  cost          Token cost analysis and optimization tips\n"
                    "  trigger       Prospective memory triggers\n"
                    "  decision      Record decisions with rationale\n"
                    "  graph         Knowledge graph operations\n"
                    "  report        Compile brain knowledge into markdown reports\n"
                    "  lint          Health check — find issues, suggest fixes\n"
                    "  neurostate    Neuromodulation state management\n"
                    "  ui            Web dashboard (port 3939)\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--agent", "-a", default=os.environ.get("AGENT_ID", "default"), help="Agent ID for attribution (default: $AGENT_ID or 'default')")
    sub = p.add_subparsers(dest="command")

    # --- version ---
    sub.add_parser("version", help="Show version and DB path")

    # --- agent ---
    ag = sub.add_parser("agent", help="Manage agents")
    ag_sub = ag.add_subparsers(dest="agent_cmd")

    ag_reg = ag_sub.add_parser("register", help="Register an agent")
    ag_reg.add_argument("id", help="Agent ID")
    ag_reg.add_argument("name", help="Display name")
    ag_reg.add_argument("type", help="Agent runtime/type label")
    ag_reg.add_argument("--adapter-info", help="JSON adapter details")

    ag_sub.add_parser("list", help="List all agents")

    ag_ping = ag_sub.add_parser("ping", help="Update last_seen_at")

    # --- memory ---
    mem = sub.add_parser("memory", help="Read/write durable memories")
    mem_sub = mem.add_subparsers(dest="mem_cmd")

    mem_add = mem_sub.add_parser("add", help="Add a memory")
    mem_add.add_argument("content", help="Memory content")
    mem_add.add_argument("--category", "-c", required=False, choices=sorted(VALID_MEMORY_CATEGORIES),
                         help="Memory category (required unless --reflexion is set)")
    mem_add.add_argument("--scope", "-s", default="global")
    mem_add.add_argument("--confidence", type=float)
    mem_add.add_argument("--tags", "-t", help="Comma-separated tags")
    mem_add.add_argument("--source-event", type=int)
    mem_add.add_argument("--type", choices=["episodic", "semantic"], default="episodic",
                         help="Memory type: episodic (time-bound, faster decay) or semantic (durable facts, slower decay)")
    mem_add.add_argument("--reflexion", action="store_true",
                         help="Shorthand for failure lessons: sets category=lesson, auto-tags with 'reflexion'")
    mem_add.add_argument("--attribute", action="store_true",
                         help="Conflict preservation mode: if other agents have memories in the same scope, "
                              "log belief_conflict entries to flag differing provenance")
    mem_add.add_argument("--force", action="store_true",
                         help="Bypass W(m) worthiness gate and write regardless of score")
    mem_add.add_argument("--dry-run-worthiness", action="store_true", dest="dry_run_worthiness",
                         help="Compute W(m) score and show result without writing")
    mem_add.add_argument("--supersedes", type=int, metavar="ID", dest="supersedes",
                         help="ID of memory being superseded; applies PII recency gate")
    mem_add.add_argument("--file", dest="file_path",
                         help="Anchor memory to a source file (e.g. src/auth/jwt.ts)")
    mem_add.add_argument("--line", type=int, dest="file_line",
                         help="Optional line number within the anchored file")

    mem_search = mem_sub.add_parser("search", help="Search memories (FTS5)")
    mem_search.add_argument("query", help="Search query")
    mem_search.add_argument("--exact", action="store_true", help="Use LIKE instead of FTS")
    mem_search.add_argument("--category", "-c")
    mem_search.add_argument("--scope", "-s")
    mem_search.add_argument("--limit", "-l", type=int, default=20)
    mem_search.add_argument("--no-recency", action="store_true", dest="no_recency",
                             help="Disable temporal recency weighting; return raw FTS rank order")
    mem_search.add_argument("--epistemic", action="store_true",
                             help="Epistemic foraging mode: prioritize memories with confidence < 0.6 (high uncertainty = high info value)")
    mem_search.add_argument("--output", "-o", choices=["json", "compact", "oneline"], default="json",
                             help="Output format: json (default), compact (minified), oneline (ID|type|text)")
    mem_search.add_argument("--file", dest="file_path",
                             help="Boost memories anchored to this file path (substring match)")

    mem_list = mem_sub.add_parser("list", help="List memories")
    mem_list.add_argument("--category", "-c")
    mem_list.add_argument("--scope", "-s")
    mem_list.add_argument("--limit", "-l", type=int)
    mem_list.add_argument("--sort", default=None,
                          choices=["confidence", "updated_at", "recalled_count", "ewc_importance"],
                          help="Sort order (default: confidence)")

    mem_retire = mem_sub.add_parser("retire", help="Soft-delete a memory")
    mem_retire.add_argument("id", type=int)

    mem_replace = mem_sub.add_parser("replace", help="Replace a memory (retire old, create new)")
    mem_replace.add_argument("old_id", type=int, help="ID of memory to retire")
    mem_replace.add_argument("content", help="New memory content")
    mem_replace.add_argument("--category", "-c", required=True, choices=sorted(VALID_MEMORY_CATEGORIES))
    mem_replace.add_argument("--scope", "-s", default="global")
    mem_replace.add_argument("--confidence", type=float)
    mem_replace.add_argument("--tags", "-t")

    mem_retract = mem_sub.add_parser("retract", help="Retract a memory (mark as false/invalid) with cascade")
    mem_retract.add_argument("id", type=int, help="Memory ID to retract")
    mem_retract.add_argument("--reason", "-r", help="Reason for retraction")
    mem_retract.add_argument("--no-cascade", action="store_true", default=False,
                              help="Do not cascade retraction to derived memories")

    mem_trust = mem_sub.add_parser("trust-propagate", help="Recalculate trust scores and propagate through chains")

    mem_update = mem_sub.add_parser("update", help="Update a memory with optimistic locking (CAS)")

    mem_confidence = mem_sub.add_parser("confidence", help="Show Beta(α,β) Bayesian confidence breakdown")
    mem_confidence.add_argument("id", type=int, help="Memory ID")

    # --- trust (top-level) ---
    trust = sub.add_parser("trust", help="Trust Score Engine — show, audit, calibrate, decay")
    trust_sub = trust.add_subparsers(dest="trust_cmd")

    tr_show = trust_sub.add_parser("show", help="Show trust score breakdown for a memory")
    tr_show.add_argument("memory_id", type=int, help="Memory ID")

    tr_audit = trust_sub.add_parser("audit", help="List memories with trust score below threshold")
    tr_audit.add_argument("--threshold", type=float, default=0.5,
                          help="Trust threshold (default: 0.5)")
    tr_audit.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")

    tr_calibrate = trust_sub.add_parser("calibrate",
                                         help="Apply category-based trust priors to all memories")
    tr_calibrate.add_argument("--dry-run", action="store_true", dest="dry_run",
                               help="Show what would be updated without writing")

    tr_decay = trust_sub.add_parser("decay", help="Apply temporal trust decay to unvalidated memories")
    tr_decay.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="Show what would be decayed without writing")

    tr_contradiction = trust_sub.add_parser("update-on-contradiction",
                                             help="Apply trust penalties after contradiction")
    tr_contradiction.add_argument("memory_id_a", type=int, help="First memory ID (kept if --resolved)")
    tr_contradiction.add_argument("memory_id_b", type=int, help="Second memory ID")
    tr_contradiction.add_argument("--resolved", action="store_true",
                                   help="Contradiction resolved (smaller penalty to id_a only)")

    tr_meb = trust_sub.add_parser("process-meb",
                                   help="Process MEB events and apply trust implications")
    tr_meb.add_argument("--since", type=int, default=0,
                         help="MEB watermark — process events with id > this value (default: 0)")
    tr_meb.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="Show what would be updated without writing")
    mem_update.add_argument("id", type=int, help="Memory ID to update")
    mem_update.add_argument("--expected-version", type=int, required=True, dest="expected_version",
                            help="Expected current version (CAS guard — fails if version has changed)")
    mem_update.add_argument("--content", help="New content")
    mem_update.add_argument("--confidence", type=float)
    mem_update.add_argument("--tags", "-t", help="Comma-separated tags")
    mem_update.add_argument("--scope", "-s")

    mem_suggest = mem_sub.add_parser(
        "suggest-category",
        help="Infer the best category for a given content string (heuristic)"
    )
    mem_suggest.add_argument("content", help="Memory content to classify")

    mem_pii = mem_sub.add_parser("pii", help="Compute Proactive Interference Index for a memory")
    mem_pii.add_argument("id", type=int, help="Memory ID")
    mem_pii.add_argument("--json", action="store_true", help="Output as JSON")

    mem_pii_scan = mem_sub.add_parser("pii-scan", help="Scan all memories sorted by PII descending")
    mem_pii_scan.add_argument("--top", type=int, default=20, metavar="N", help="Return top N memories (default: 20)")
    mem_pii_scan.add_argument("--json", action="store_true", help="Output as JSON")

    # --- entity ---
    ent = sub.add_parser("entity", help="Knowledge graph entity registry")
    ent_sub = ent.add_subparsers(dest="ent_cmd")

    ent_create = ent_sub.add_parser("create", help="Create a new entity")
    ent_create.add_argument("name", help="Entity name (unique within scope)")
    ent_create.add_argument("--type", "-t", dest="entity_type", required=True,
                            choices=sorted(VALID_ENTITY_TYPES), help="Entity type")
    ent_create.add_argument("--properties", "-p", help="JSON object of properties")
    ent_create.add_argument("--observations", "-o",
                            help="Semicolon-separated atomic observations (e.g. 'Speaks Spanish; Lives in NYC')")
    ent_create.add_argument("--scope", "-s", default="global")
    ent_create.add_argument("--confidence", type=float)

    ent_get = ent_sub.add_parser("get", help="Get entity by name or ID (includes relations)")
    ent_get.add_argument("identifier", help="Entity name or numeric ID")

    ent_search = ent_sub.add_parser("search", help="Search entities (FTS5)")
    ent_search.add_argument("query", help="Search query")
    ent_search.add_argument("--type", "-t", dest="entity_type", choices=sorted(VALID_ENTITY_TYPES))
    ent_search.add_argument("--limit", "-l", type=int, default=20)

    ent_list = ent_sub.add_parser("list", help="List entities")
    ent_list.add_argument("--type", "-t", dest="entity_type", choices=sorted(VALID_ENTITY_TYPES))
    ent_list.add_argument("--scope", "-s")
    ent_list.add_argument("--limit", "-l", type=int, default=50)

    ent_update = ent_sub.add_parser("update", help="Update entity properties")
    ent_update.add_argument("identifier", help="Entity name or numeric ID")
    ent_update.add_argument("--properties", "-p", help="JSON properties to merge")
    ent_update.add_argument("--name", help="New name")
    ent_update.add_argument("--type", "-t", dest="entity_type", choices=sorted(VALID_ENTITY_TYPES))

    ent_observe = ent_sub.add_parser("observe", help="Add observations to an entity")
    ent_observe.add_argument("identifier", help="Entity name or numeric ID")
    ent_observe.add_argument("observations", help="Semicolon-separated observations to add")

    ent_relate = ent_sub.add_parser("relate", help="Create a relation between two entities")
    ent_relate.add_argument("from_entity", help="Source entity name or ID")
    ent_relate.add_argument("relation", help="Relation type in active voice (e.g. manages, works_at, depends_on)")
    ent_relate.add_argument("to_entity", help="Target entity name or ID")
    ent_relate.add_argument("--confidence", "-c", type=float, default=1.0)

    ent_delete = ent_sub.add_parser("delete", help="Soft-delete an entity")
    ent_delete.add_argument("identifier", help="Entity name or numeric ID")

    # --- trigger ---  (prospective memory)
    trg = sub.add_parser("trigger", help="Prospective memory triggers — conditional future recall")
    trg_sub = trg.add_subparsers(dest="trg_cmd")

    trg_create = trg_sub.add_parser("create", help="Create a new prospective memory trigger")
    trg_create.add_argument("condition", help="Natural language condition for when to fire")
    trg_create.add_argument("--keywords", "-k", required=True, help="Comma-separated keywords for matching")
    trg_create.add_argument("--action", "-a", required=True, help="What to surface/do when triggered")
    trg_create.add_argument("--entity", "-e", help="Linked entity name or ID")
    trg_create.add_argument("--memory", "-m", type=int, help="Linked memory ID")
    trg_create.add_argument("--priority", "-p", default="medium", choices=["low", "medium", "high", "critical"])
    trg_create.add_argument("--expires", help="Expiry datetime (ISO format)")

    trg_list = trg_sub.add_parser("list", help="List triggers")
    trg_list.add_argument("--status", "-s", choices=["active", "fired", "expired", "cancelled"])

    trg_check = trg_sub.add_parser("check", help="Check if active triggers match a query")
    trg_check.add_argument("query", help="Query text to match against trigger keywords")

    trg_fire = trg_sub.add_parser("fire", help="Mark a trigger as fired")
    trg_fire.add_argument("id", type=int, help="Trigger ID")

    trg_cancel = trg_sub.add_parser("cancel", help="Cancel an active trigger")
    trg_cancel.add_argument("id", type=int, help="Trigger ID")

    # --- event ---
    ev = sub.add_parser("event", help="Log and search events")
    ev_sub = ev.add_subparsers(dest="ev_cmd")

    ev_add = ev_sub.add_parser("add", help="Log an event")
    ev_add.add_argument("summary", help="Event summary")
    ev_add.add_argument("--type", "-t", required=True, choices=sorted(VALID_EVENT_TYPES))
    ev_add.add_argument("--detail", "-d")
    ev_add.add_argument("--metadata", "-m", help="JSON metadata")
    ev_add.add_argument("--session")
    ev_add.add_argument("--project", "-p")
    ev_add.add_argument("--refs", help="Comma-separated refs")
    ev_add.add_argument("--importance", type=float)
    ev_add.add_argument("--caused-by", type=int, dest="caused_by", metavar="EVENT_ID",
                        help="ID of the event that caused this one (causal threading)")

    ev_search = ev_sub.add_parser("search", help="Search events")
    ev_search.add_argument("--query", "-q")
    ev_search.add_argument("--type", "-t")
    ev_search.add_argument("--project", "-p")
    ev_search.add_argument("--limit", "-l", type=int, default=20)
    ev_search.add_argument("--no-recency", action="store_true", dest="no_recency",
                            help="Disable temporal recency weighting; return raw FTS rank order")

    ev_tail = ev_sub.add_parser("tail", help="Show recent events")
    ev_tail.add_argument("-n", type=int, default=20, help="Number of events")

    ev_link = ev_sub.add_parser("link", help="Explicitly link two events as cause->effect (agent-reported)")
    ev_link.add_argument("cause_event_id", type=int, help="ID of the cause event")
    ev_link.add_argument("effect_event_id", type=int, help="ID of the effect event")
    ev_link.add_argument("--relation", "-r", default="causes",
                         choices=["causes", "triggered_by", "contributes_to"],
                         help="Edge relation type (default: causes)")
    ev_link.add_argument("--confidence", "-c", type=float, default=None,
                         help="Confidence 0.0-1.0 (default: 0.9 for agent-reported)")

    # --- temporal ---
    tmpl = sub.add_parser("temporal", help="Causal chain traversal and auto-detection")
    tmpl_sub = tmpl.add_subparsers(dest="temporal_cmd")

    tmpl_causes = tmpl_sub.add_parser("causes", help="Forward chain: what did event X cause? (downstream effects)")
    tmpl_causes.add_argument("event_id", type=int, help="Seed event ID")
    tmpl_causes.add_argument("--depth", "-d", type=int, default=6, help="Max chain depth (default: 6)")
    tmpl_causes.add_argument("--min-confidence", type=float, default=0.0, dest="min_confidence",
                              help="Min edge confidence to follow (default: 0.0)")

    tmpl_effects = tmpl_sub.add_parser("effects", help="Backward chain: why did event X happen? (upstream causes)")
    tmpl_effects.add_argument("event_id", type=int, help="Seed event ID")
    tmpl_effects.add_argument("--depth", "-d", type=int, default=6, help="Max chain depth (default: 6)")
    tmpl_effects.add_argument("--min-confidence", type=float, default=0.0, dest="min_confidence",
                               help="Min edge confidence to follow (default: 0.0)")

    tmpl_chain = tmpl_sub.add_parser("chain", help="Bidirectional: show both causes and effects of an event")
    tmpl_chain.add_argument("event_id", type=int, help="Seed event ID")
    tmpl_chain.add_argument("--depth", "-d", type=int, default=4, help="Max traversal depth (default: 4)")
    tmpl_chain.add_argument("--min-confidence", type=float, default=0.0, dest="min_confidence",
                             help="Min edge confidence to follow (default: 0.0)")

    tmpl_detect = tmpl_sub.add_parser("auto-detect",
                                       help="Run causal edge auto-detection pipeline over all events")
    tmpl_detect.add_argument("--dry-run", action="store_true", dest="dry_run",
                              help="Show what would be inserted without writing")

    # --- epoch ---
    epoch = sub.add_parser("epoch", help="Manage temporal epochs")
    epoch_sub = epoch.add_subparsers(dest="epoch_cmd")

    epoch_detect = epoch_sub.add_parser("detect", help="Suggest epoch boundaries from event history")
    epoch_detect.add_argument("--gap-hours", type=float, default=48.0, help="Minimum inactivity gap to trigger boundary")
    epoch_detect.add_argument("--window-size", type=int, default=8, help="Events per side for topic-shift evaluation")
    epoch_detect.add_argument("--min-window", type=int, default=4, help="Minimum events per side to evaluate shift")
    epoch_detect.add_argument("--topic-shift-threshold", type=float, default=0.2, help="Cosine similarity threshold for topic shift")
    epoch_detect.add_argument("--min-boundary-distance", type=int, default=8, help="Min events between accepted boundaries")
    epoch_detect.add_argument("--min-events", type=int, default=8, help="Minimum events per suggested epoch")
    epoch_detect.add_argument("--verbose", action="store_true", help="Include raw boundary diagnostics")

    epoch_create = epoch_sub.add_parser("create", help="Create an epoch and backfill matching records")
    epoch_create.add_argument("name", help="Epoch name")
    epoch_create.add_argument("--started", required=True, help="Start timestamp (ISO or YYYY-MM-DD HH:MM:SS)")
    epoch_create.add_argument("--ended", help="Optional end timestamp")
    epoch_create.add_argument("--parent", type=int, help="Optional parent epoch id")
    epoch_create.add_argument("--description", help="Optional epoch description")

    epoch_list = epoch_sub.add_parser("list", help="List epochs")
    epoch_list.add_argument("--active", action="store_true", help="Only show currently active epochs")
    epoch_list.add_argument("--limit", "-l", type=int, help="Max rows")

    # --- context ---
    ctx = sub.add_parser("context", help="Manage knowledge context chunks")
    ctx_sub = ctx.add_subparsers(dest="ctx_cmd")

    ctx_add = ctx_sub.add_parser("add", help="Add a context chunk")
    ctx_add.add_argument("content", help="Context content")
    ctx_add.add_argument("--source-type", required=True)
    ctx_add.add_argument("--source-ref", required=True)
    ctx_add.add_argument("--chunk", type=int, default=0)
    ctx_add.add_argument("--summary")
    ctx_add.add_argument("--project", "-p")
    ctx_add.add_argument("--tags", "-t")
    ctx_add.add_argument("--tokens", type=int)

    ctx_search = ctx_sub.add_parser("search", help="Search context")
    ctx_search.add_argument("query")
    ctx_search.add_argument("--limit", "-l", type=int, default=20)

    # --- task ---
    task = sub.add_parser("task", help="Manage shared tasks")
    task_sub = task.add_subparsers(dest="task_cmd")

    task_add = task_sub.add_parser("add", help="Add a task")
    task_add.add_argument("title")
    task_add.add_argument("--description", "-d")
    task_add.add_argument("--status", choices=sorted(VALID_TASK_STATUSES))
    task_add.add_argument("--priority", choices=sorted(VALID_PRIORITIES))
    task_add.add_argument("--assign")
    task_add.add_argument("--project", "-p")
    task_add.add_argument("--external-id")
    task_add.add_argument("--external-system")
    task_add.add_argument("--metadata", "-m")

    task_update = task_sub.add_parser("update", help="Update a task")
    task_update.add_argument("id", type=int)
    task_update.add_argument("--status", choices=sorted(VALID_TASK_STATUSES))
    task_update.add_argument("--priority", choices=sorted(VALID_PRIORITIES))
    task_update.add_argument("--assign")
    task_update.add_argument("--no-claim", action="store_true")

    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument("--status")
    task_list.add_argument("--project", "-p")
    task_list.add_argument("--limit", "-l", type=int)

    # --- decision ---
    dec = sub.add_parser("decision", help="Log and list decisions")
    dec_sub = dec.add_subparsers(dest="dec_cmd")

    dec_add = dec_sub.add_parser("add", help="Record a decision")
    dec_add.add_argument("title")
    dec_add.add_argument("--rationale", "-r", required=True)
    dec_add.add_argument("--alternatives", help="Pipe-separated alternatives")
    dec_add.add_argument("--project", "-p")
    dec_add.add_argument("--reversible", action="store_true", default=True)
    dec_add.add_argument("--source-event", type=int)

    dec_list = dec_sub.add_parser("list", help="List decisions")
    dec_list.add_argument("--project", "-p")
    dec_list.add_argument("--limit", "-l", type=int)

    # --- handoff ---
    hof = sub.add_parser("handoff", help="Temporary handoff packets for session continuity")
    hof_sub = hof.add_subparsers(dest="handoff_cmd")

    hof_add = hof_sub.add_parser("add", help="Create a handoff packet")
    hof_add.add_argument("--title")
    hof_add.add_argument("--goal", required=True)
    hof_add.add_argument("--current-state", required=True, dest="current_state")
    hof_add.add_argument("--open-loops", required=True, dest="open_loops")
    hof_add.add_argument("--next-step", required=True, dest="next_step")
    hof_add.add_argument("--recent-tail", dest="recent_tail")
    hof_add.add_argument("--session")
    hof_add.add_argument("--chat-id")
    hof_add.add_argument("--thread-id")
    hof_add.add_argument("--user-id")
    hof_add.add_argument("--project", "-p")
    hof_add.add_argument("--scope", "-s", default="global")
    hof_add.add_argument("--status", choices=["pending", "consumed", "expired", "pinned"], default="pending")
    hof_add.add_argument("--decisions-json")
    hof_add.add_argument("--entities-json")
    hof_add.add_argument("--tasks-json")
    hof_add.add_argument("--facts-json")
    hof_add.add_argument("--source-event", type=int)
    hof_add.add_argument("--expires-at")

    hof_list = hof_sub.add_parser("list", help="List handoff packets")
    hof_list.add_argument("--status", choices=["pending", "consumed", "expired", "pinned"])
    hof_list.add_argument("--project", "-p")
    hof_list.add_argument("--chat-id")
    hof_list.add_argument("--thread-id")
    hof_list.add_argument("--user-id")
    hof_list.add_argument("--limit", "-l", type=int, default=20)

    hof_latest = hof_sub.add_parser("latest", help="Fetch the latest matching handoff packet")
    hof_latest.add_argument("--status", choices=["pending", "consumed", "expired", "pinned"], default="pending")
    hof_latest.add_argument("--project", "-p")
    hof_latest.add_argument("--chat-id")
    hof_latest.add_argument("--thread-id")
    hof_latest.add_argument("--user-id")

    hof_consume = hof_sub.add_parser("consume", help="Mark a handoff packet consumed")
    hof_consume.add_argument("id", type=int)

    hof_pin = hof_sub.add_parser("pin", help="Pin a handoff packet so it does not expire")
    hof_pin.add_argument("id", type=int)

    hof_expire = hof_sub.add_parser("expire", help="Mark a handoff packet expired")
    hof_expire.add_argument("id", type=int)

    # --- state ---
    st = sub.add_parser("state", help="Per-agent key/value state")
    st_sub = st.add_subparsers(dest="state_cmd")

    st_get = st_sub.add_parser("get", help="Get state")
    st_get.add_argument("--key", "-k")

    st_set = st_sub.add_parser("set", help="Set state")
    st_set.add_argument("key")
    st_set.add_argument("value")

    # --- attention-class ---
    attn = sub.add_parser("attention-class", help="Get or set agent attention class tier (exec|ic|peripheral|dormant)")
    attn_sub = attn.add_subparsers(dest="attn_cmd")

    attn_get = attn_sub.add_parser("get", help="Get attention class for an agent (or all agents)")
    attn_get.add_argument("--agent", "-a", help="Agent id (omit to list all)")

    attn_set = attn_sub.add_parser("set", help="Set attention class for an agent")
    attn_set.add_argument("class_name", metavar="class", choices=["exec", "ic", "peripheral", "dormant"],
                          help="Attention class: exec | ic | peripheral | dormant")
    attn_set.add_argument("--agent", "-a", required=True, help="Agent id to update")

    # --- budget ---
    bdg = sub.add_parser("budget", help="Token consumption and attention budget commands")
    bdg_sub = bdg.add_subparsers(dest="budget_cmd")
    bdg_status = bdg_sub.add_parser("status", help="Show per-agent and fleet-wide token usage for today")
    bdg_status.add_argument("--json", action="store_true", help="Output as JSON")

    # --- search ---
    srch = sub.add_parser("search", help="Universal cross-table search")
    srch.add_argument("query")
    srch.add_argument("--tables", help="Comma-separated: memories,events,context")
    srch.add_argument("--limit", "-l", type=int, default=10)
    srch.add_argument("--no-recency", action="store_true", dest="no_recency",
                       help="Disable temporal recency weighting; return raw FTS rank order")
    srch.add_argument("--no-graph", action="store_true", dest="no_graph",
                       help="Disable 1-hop knowledge_edges expansion on top results")
    srch.add_argument("--budget", type=int, default=None, metavar="TOKENS",
                       help="Hard token cap on search response (trim lowest-ranked entries first)")
    srch.add_argument("--min-salience", type=float, default=None, dest="min_salience", metavar="FLOOR",
                       help="Suppress memories with final_score below this threshold (e.g. 0.1)")
    srch.add_argument("--mmr", action="store_true",
                       help="Re-rank memories using Maximal Marginal Relevance to balance relevance vs diversity")
    srch.add_argument("--mmr-lambda", type=float, default=0.7, dest="mmr_lambda", metavar="LAMBDA",
                       help="MMR trade-off parameter: 1.0=pure relevance, 0.0=pure diversity (default: 0.7)")
    srch.add_argument("--explore", action="store_true",
                       help="Curiosity mode: sample never/rarely recalled memories weighted by confidence")
    srch.add_argument("--pagerank-boost", type=float, default=0.0, dest="pagerank_boost", metavar="ALPHA",
                       help="Boost final_score by PageRank centrality: score *= (1 + alpha * norm_pr). Requires cached PageRank (brainctl graph pagerank).")
    srch.add_argument("--quantum", action="store_true",
                       help="Apply phase-aware quantum amplitude re-ranking to memory results")
    srch.add_argument("--benchmark", action="store_true",
                       help="Compare classical vs quantum scores side-by-side; implies --quantum")
    srch.add_argument("--output", "-o", choices=["json", "compact", "oneline"], default="json",
                       help="Output format: json (default, pretty), compact (minified JSON), oneline (ID|type|text per line)")

    # --- promote ---
    prom = sub.add_parser("promote", help="Promote an event to a durable memory")
    prom.add_argument("event_id", type=int)
    prom.add_argument("--category", "-c")
    prom.add_argument("--scope", "-s")
    prom.add_argument("--content", help="Override content (default: event summary)")
    prom.add_argument("--confidence", type=float)
    prom.add_argument("--tags", "-t")

    # --- distill ---
    dist = sub.add_parser("distill", help="Batch-promote high-importance events to durable memories")
    dist.add_argument("--threshold", type=float, default=0.7,
                      help="Minimum importance to promote (default: 0.7)")
    dist.add_argument("--limit", type=int, default=50,
                      help="Max events to promote per run (default: 50)")
    dist.add_argument("--dry-run", action="store_true",
                      help="Show what would be promoted without writing")
    dist.add_argument("--since", help="Only consider events after this ISO date")
    dist.add_argument("--filter-agent", help="Only promote events from this agent_id")
    dist.add_argument("--event-types",
                      help="Comma-separated event types to include (e.g. result,decision)")

    # --- dreams ---
    drm = sub.add_parser("dreams", help="Show dream hypotheses from the bisociation incubation queue")
    drm.add_argument("--status", default="incubating", choices=["incubating", "promoted", "retired"],
                     help="Hypothesis status to show (default: incubating)")
    drm.add_argument("--limit", "-l", type=int, default=20, help="Max results (default: 20)")
    drm.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- init ---
    init_p = sub.add_parser("init", help="Initialize a fresh brain.db database")
    init_p.add_argument("--path", help="Custom path for brain.db (default: ~/agentmemory/db/brain.db)")
    init_p.add_argument("--force", action="store_true", help="Overwrite existing database")

    # --- maintenance ---
    sub.add_parser("backup", help="Backup database")
    sub.add_parser("stats", help="Show database statistics")
    sub.add_parser("cost", help="Token cost analysis — shows format savings, query costs, and optimization tips")
    sub.add_parser("validate", help="Validate database integrity")

    # --- affect ---
    aff = sub.add_parser("affect", help="Functional affect tracking")
    aff_sub = aff.add_subparsers(dest="affect_cmd")
    aff_log = aff_sub.add_parser("log", help="Log affect observation by classifying text")
    aff_log.add_argument("text", help="Text to classify for affect state")
    aff_log.add_argument("--source", default="observation", help="Source type: observation, self_report, probe, automatic")

    aff_check = aff_sub.add_parser("check", help="Check current affect state + safety probe for an agent")

    aff_hist = aff_sub.add_parser("history", help="Show affect history for an agent")
    aff_hist.add_argument("--limit", "-l", type=int, default=20)

    aff_mon = aff_sub.add_parser("monitor", help="Fleet-wide affect monitoring — scan all agents for safety flags")

    aff_cls = aff_sub.add_parser("classify", help="Classify affect from text (dry-run, no logging)")
    aff_cls.add_argument("text", help="Text to classify")

    # --- report ---
    rpt = sub.add_parser("report", help="Compile brain knowledge into a readable markdown report")
    rpt.add_argument("--topic", "-t", help="Filter report to a specific topic")
    rpt.add_argument("--entity", "-e", help="Focus report on a specific entity")
    rpt.add_argument("--out", help="Write report to file instead of stdout")
    rpt.add_argument("--limit", "-l", type=int, default=20, help="Max items per section (default: 20)")

    # --- lint ---
    lnt = sub.add_parser("lint", help="Brain health check — find issues, suggest fixes")
    lnt.add_argument("--fix", action="store_true", help="Auto-fix safe issues (duplicates, log bloat)")
    lnt.add_argument("--output", "-o", choices=["json", "text"], default="json", help="Output format")

    # batch subcommand not available — brainctl is model-agnostic

    # --- index (Karpathy LLM Wiki pattern) ---
    idx = sub.add_parser("index", help="Generate a browsable catalog of all knowledge (memories, entities, decisions)")
    idx.add_argument("--category", "-c", help="Filter to a specific memory category")
    idx.add_argument("--scope", "-s", help="Filter to a specific scope")
    idx.add_argument("--out", help="Write index to file instead of stdout")
    idx.add_argument("--format", choices=["markdown", "json"], default="markdown",
                     help="Output format: markdown (human-readable) or json (machine-readable)")

    prune = sub.add_parser("prune-log", help="Prune old access log entries")
    prune.add_argument("--days", type=int, default=30)

    sub.add_parser("temporal-context", help="Print compact temporal orientation summary for agents")

    # --- ui ---
    ui_p = sub.add_parser("ui", help="Open web dashboard at localhost:3939")
    ui_p.add_argument("--port", type=int, default=3939, help="Port to serve on (default: 3939)")
    ui_p.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")

    health = sub.add_parser("health", help="Memory SLO health dashboard (coverage, freshness, precision, diversity, temporal)")
    health.add_argument("--json", action="store_true", help="Output raw JSON instead of dashboard")
    health.add_argument("--window", type=int, default=7, metavar="DAYS", help="Rolling window in days for coverage/freshness (default: 7)")

    # --- dashboard ---
    dash = sub.add_parser(
        "dashboard",
        help="Unified telemetry dashboard — single-pane-of-glass health view of brain.db",
    )
    dash.add_argument(
        "--format", "-f",
        choices=["text", "json"],
        default="text",
        help="Output format: text (default, human-readable) or json (machine output)",
    )
    dash.add_argument(
        "--agent", dest="dashboard_agent", default=None, metavar="AGENT_ID",
        help="Filter dashboard to a single agent (default: show all agents)",
    )

    # --- graph ---
    gph = sub.add_parser("graph", help="Query knowledge_edges graph (related nodes, causal chains, stats)")
    gph_sub = gph.add_subparsers(dest="graph_cmd")

    gph_sub.add_parser("stats", help="Edge distribution summary")

    gph_nbr = gph_sub.add_parser("neighbors", help="List direct neighbors of a node")
    gph_nbr.add_argument("table", help="Source table: memories, events, context")
    gph_nbr.add_argument("id", type=int, help="Node id")
    gph_nbr.add_argument("--limit", "-l", type=int, default=20)

    gph_rel = gph_sub.add_parser("related", help="Multi-hop traversal from a node")
    gph_rel.add_argument("table", help="Source table: memories, events, context")
    gph_rel.add_argument("id", type=int, help="Node id")
    gph_rel.add_argument("--hops", type=int, default=1, help="Traversal depth (default: 1)")
    gph_rel.add_argument("--limit", "-l", type=int, default=20)

    gph_cau = gph_sub.add_parser("causal", help="Trace causal chain from an event")
    gph_cau.add_argument("event_id", type=int, help="Starting event id")
    gph_cau.add_argument("--depth", type=int, default=10, help="Max chain depth (default: 10)")

    gph_add = gph_sub.add_parser("add-edge", help="Manually insert a knowledge edge")
    gph_add.add_argument("source_table")
    gph_add.add_argument("source_id", type=int)
    gph_add.add_argument("target_table")
    gph_add.add_argument("target_id", type=int)
    gph_add.add_argument("relation")
    gph_add.add_argument("--weight", type=float, default=1.0)

    gph_act = gph_sub.add_parser(
        "activate",
        help="Spreading activation from seed node(s) — returns ranked activated neighbors",
    )
    gph_act.add_argument("table", nargs="?", help="Seed table: memories, events, context (omit with --from-stdin)")
    gph_act.add_argument("id", type=int, nargs="?", help="Seed node id (omit with --from-stdin)")
    gph_act.add_argument("--hops", type=int, default=2, help="Max propagation depth (default: 2)")
    gph_act.add_argument("--decay", type=float, default=0.6, help="Activation decay per hop (default: 0.6)")
    gph_act.add_argument("--top-k", type=int, default=20, dest="top_k", help="Max results to return (default: 20)")
    gph_act.add_argument(
        "--from-stdin",
        action="store_true",
        dest="from_stdin",
        help="Read seed nodes from JSON piped on stdin (e.g. from vsearch output)",
    )

    gph_pr = gph_sub.add_parser("pagerank", help="Compute PageRank scores for all graph nodes")
    gph_pr.add_argument("--damping", type=float, default=0.85, help="Damping factor (default: 0.85)")
    gph_pr.add_argument("--iters", type=int, default=50, help="Max power iterations (default: 50)")
    gph_pr.add_argument("--top-k", type=int, default=20, dest="top_k", help="Top N results to show (default: 20)")
    gph_pr.add_argument("--table", choices=["memories", "entities", "events", "context"], help="Filter results to a single table")
    gph_pr.add_argument("--force", action="store_true", help="Recompute even if cached result is fresh")
    gph_pr.add_argument("--format", "-f", choices=["text", "json"], default="text")

    gph_comm = gph_sub.add_parser("communities", help="Label propagation community detection")
    gph_comm.add_argument("--seed", type=int, default=42, help="Random seed for label propagation (default: 42)")
    gph_comm.add_argument("--force", action="store_true", help="Recompute even if cached result is fresh")
    gph_comm.add_argument("--format", "-f", choices=["text", "json"], default="text")

    gph_btw = gph_sub.add_parser("betweenness", help="Betweenness centrality — bridge nodes between clusters")
    gph_btw.add_argument("--top-k", type=int, default=20, dest="top_k", help="Top N results to show (default: 20)")
    gph_btw.add_argument("--force", action="store_true", help="Recompute even if cached result is fresh")
    gph_btw.add_argument("--format", "-f", choices=["text", "json"], default="text")

    gph_pb = gph_sub.add_parser("protect-bridges", help="Mark high-betweenness memory nodes as protected (EWC integration)")
    gph_pb.add_argument("--threshold", type=float, default=0.005, help="Min betweenness score to protect (default: 0.005)")
    gph_pb.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be protected without writing")
    gph_pb.add_argument("--force", action="store_true", help="Recompute betweenness before protecting")
    gph_pb.add_argument("--format", "-f", choices=["text", "json"], default="text")

    gph_path = gph_sub.add_parser("path", help="Shortest path between two nodes")
    gph_path.add_argument("from_table", help="Source table: memories, events, context")
    gph_path.add_argument("from_id", type=int, help="Source node id")
    gph_path.add_argument("to_table", help="Target table: memories, events, context")
    gph_path.add_argument("to_id", type=int, help="Target node id")
    gph_path.add_argument("--max-hops", type=int, default=6, dest="max_hops", help="Max BFS depth (default: 6)")
    gph_path.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- vsearch ---
    vs = sub.add_parser("vsearch", help="Semantic vector search (nearest-neighbor via sqlite-vec)")
    vs.add_argument("query")
    vs.add_argument("--tables", help="Comma-separated: memories,events,context (default: all)")
    vs.add_argument("--limit", "-l", type=int, default=10)
    vs.add_argument("--hybrid", action="store_true", default=True,
                    help="Combine FTS5 + cosine distance (default: on)")
    vs.add_argument("--vec-only", action="store_true", dest="vec_only",
                    help="Skip FTS5; use cosine distance only")
    vs.add_argument("--alpha", type=float, default=0.5,
                    help="FTS5 weight in hybrid score (0.0-1.0, default: 0.5)")
    vs.add_argument("--graph-boost", action="store_true", dest="graph_boost",
                    help="After retrieval, run spreading activation from results and boost graph-connected memories")
    vs.add_argument("--graph-boost-weight", type=float, default=0.3, dest="graph_boost_weight",
                    help="Weight of graph activation score in final hybrid (default: 0.3)")

    # --- vec ---
    vec = sub.add_parser("vec", help="Vector index maintenance")
    vec_sub = vec.add_subparsers(dest="vec_cmd")
    vec_sub.add_parser("purge-retired",
                       help="Delete vec_memories entries for all retired memories (one-time cleanup)")

    # --- gaps ---
    p_weights = sub.add_parser("weights", help="Show adaptive retrieval weights and store diagnostics")
    p_weights.add_argument("--query", "-q", help="Optional query to show query-type adjusted weights")

    gps = sub.add_parser("gaps", help="Metacognitive gap detection — list and scan knowledge blind spots")
    gps_sub = gps.add_subparsers(dest="gaps_cmd")

    gps_sub.add_parser("refresh", help="Recompute knowledge_coverage stats from current memories")

    gps_sub.add_parser("scan", help="Detect coverage, staleness, and confidence holes; write to knowledge_gaps")

    gps_list = gps_sub.add_parser("list", help="List unresolved knowledge gaps sorted by severity")
    gps_list.add_argument("--limit", "-l", type=int, default=50, help="Max results (default: 50)")
    gps_list.add_argument("--type", help="Filter by gap_type: coverage_hole|staleness_hole|confidence_hole|contradiction_hole")

    gps_resolve = gps_sub.add_parser("resolve", help="Mark a gap as resolved")
    gps_resolve.add_argument("id", type=int, help="Gap ID to resolve")
    gps_resolve.add_argument("--note", help="Resolution note")

    # --- expertise ---
    exp = sub.add_parser("expertise", help="Agent expertise directory — who knows what")
    exp_sub = exp.add_subparsers(dest="exp_cmd")

    exp_build = exp_sub.add_parser("build", help="Build/refresh expertise table from memory+event history")
    exp_build.add_argument("--agent", dest="agent_id", help="Rebuild for a single agent (default: all active)")
    exp_build.add_argument("--quiet", "-q", action="store_true", help="Suppress per-agent output")
    exp_build.add_argument("--json", action="store_true", help="Output JSON")

    exp_show = exp_sub.add_parser("show", help="Show expertise profile for an agent")
    exp_show.add_argument("agent_id", help="Agent ID")
    exp_show.add_argument("--limit", "-l", type=int, default=20, help="Max domains to show (default: 20)")
    exp_show.add_argument("--json", action="store_true", help="Output JSON")

    exp_list = exp_sub.add_parser("list", help="List all agents' top expertise domains")
    exp_list.add_argument("--domain", "-d", help="Filter by domain (partial match)")
    exp_list.add_argument("--min-strength", type=float, default=0.0, dest="min_strength",
                          help="Minimum strength threshold (default: 0.0)")
    exp_list.add_argument("--limit", "-l", type=int, default=50, help="Max results (default: 50)")
    exp_list.add_argument("--json", action="store_true", help="Output JSON")

    exp_update = exp_sub.add_parser("update", help="Update brier_score or strength for agent+domain")
    exp_update.add_argument("agent_id", help="Agent ID")
    exp_update.add_argument("domain", help="Domain name")
    exp_update.add_argument("--brier", type=float, metavar="SCORE",
                            help="Brier score (0.0=perfect, 2.0=worst)")
    exp_update.add_argument("--strength", type=float, metavar="VALUE",
                            help="Override expertise strength (0.0-1.0)")

    # --- whosknows ---
    wk = sub.add_parser("whosknows", help="Find the best agent(s) for a topic")
    wk.add_argument("topic", nargs="+", help="Topic to look up")
    wk.add_argument("--top-n", type=int, default=10, dest="top_n", help="Max results (default: 10)")
    wk.add_argument("--min-strength", type=float, default=0.05, dest="min_strength",
                    help="Minimum expertise strength to include (default: 0.05)")
    wk.add_argument("--verbose", "-v", action="store_true", help="Show domain breakdown per agent")
    wk.add_argument("--json", action="store_true", help="Output JSON")

    # --- reflexion ---
    rfx = sub.add_parser("reflexion", help="Failure taxonomy lessons — write, query, lifecycle")
    rfx_sub = rfx.add_subparsers(dest="rfx_cmd")

    rfx_write = rfx_sub.add_parser("write", help="Write a new reflexion lesson")
    rfx_write.add_argument("--failure-class", required=True, dest="failure_class",
                           help="REASONING_ERROR|CONTEXT_LOSS|HALLUCINATION|COORDINATION_FAILURE|TOOL_MISUSE")
    rfx_write.add_argument("--failure-subclass", dest="failure_subclass", help="Optional drill-down label")
    rfx_write.add_argument("--trigger", required=True, help="When does this lesson apply? (trigger conditions)")
    rfx_write.add_argument("--lesson", required=True, help="The corrective instruction")
    rfx_write.add_argument("--generalizable-to", dest="generalizable_to",
                           help="Comma-separated scope tokens: agent_type:external,capability:brainctl,scope:global,...")
    rfx_write.add_argument("--confidence", type=float, help="Override default confidence (0.0-1.0)")
    rfx_write.add_argument("--override-level", dest="override_level",
                           help="HARD_OVERRIDE|SOFT_HINT|SILENT_LOG (default: class-based)")
    rfx_write.add_argument("--expiration-policy", dest="expiration_policy",
                           help="success_count|code_fix|ttl|manual (default: success_count)")
    rfx_write.add_argument("--expiration-n", dest="expiration_n", type=int,
                           help="Consecutive successes needed before archiving")
    rfx_write.add_argument("--expiration-ttl-days", dest="expiration_ttl_days", type=int,
                           help="TTL days for ttl expiration policy")
    rfx_write.add_argument("--root-cause-ref", dest="root_cause_ref",
                           help="Code/config ref for code_fix policy (e.g. my-api/checkout-protocol)")
    rfx_write.add_argument("--source-event", dest="source_event", type=int, help="Source event ID")
    rfx_write.add_argument("--source-run", dest="source_run", help="Run ID from failed task")

    rfx_list = rfx_sub.add_parser("list", help="List reflexion lessons")
    rfx_list.add_argument("--failure-class", dest="failure_class", help="Filter by failure class")
    rfx_list.add_argument("--status", help="active|archived|retired (default: active)")
    rfx_list.add_argument("--source-agent", dest="source_agent", help="Filter by source agent ID")
    rfx_list.add_argument("--limit", "-l", type=int, default=50, help="Max results")

    rfx_query = rfx_sub.add_parser("query", help="Query lessons for a task context (FTS + scope filter)")
    rfx_query.add_argument("--task-description", required=True, dest="task_description",
                           help="Task description to match against trigger conditions")
    rfx_query.add_argument("--scope", help="Comma-separated scope tokens to filter (e.g. agent_type:external)")
    rfx_query.add_argument("--top-k", dest="top_k", type=int, default=5, help="Max results (default: 5)")
    rfx_query.add_argument("--min-confidence", dest="min_confidence", type=float, default=0.0,
                           help="Minimum confidence threshold")

    rfx_success = rfx_sub.add_parser("success", help="Record successful outcomes (expiration progress)")
    rfx_success.add_argument("--lesson-ids", required=True, dest="lesson_ids",
                             help="Comma-separated lesson IDs that were applied and helped")

    rfx_recur = rfx_sub.add_parser("failure-recurrence", help="Record a failure recurrence (confidence demotion)")
    rfx_recur.add_argument("--lesson-id", required=True, dest="lesson_id", type=int, help="Lesson ID")
    rfx_recur.add_argument("--note", help="Optional note about the recurrence")

    rfx_retire = rfx_sub.add_parser("retire", help="Retire a lesson (code fix or manual)")
    rfx_retire.add_argument("--lesson-id", required=True, dest="lesson_id", type=int, help="Lesson ID to retire")
    rfx_retire.add_argument("--reason", help="Retirement reason")

    # --- meb ---
    meb = sub.add_parser("meb", help="Memory Event Bus — subscribe to memory write notifications")
    meb_sub = meb.add_subparsers(dest="meb_cmd")

    meb_tail = meb_sub.add_parser("tail", help="Poll recent memory write events")
    meb_tail.add_argument("-n", type=int, default=20, help="Max events to return (default: 20)")
    meb_tail.add_argument("--since", type=int, default=None, metavar="EVENT_ID",
                          help="Return only events with id > EVENT_ID (incremental polling cursor)")
    meb_tail.add_argument("--agent", "-a", help="Filter by writing agent_id")
    meb_tail.add_argument("--category", "-c", help="Filter by memory category")
    meb_tail.add_argument("--scope", "-s", help="Filter by scope prefix (e.g. project:agentmemory)")
    meb_tail.add_argument("--include-backfill", action="store_true", dest="include_backfill",
                          help="Include historical backfill events (excluded by default)")

    meb_sub.add_parser("subscribe",
        help="Return current watermark ID — use as --since cursor for incremental polling")

    meb_sub.add_parser("stats",
        help="Show MEB queue depth, throughput, and propagation latency")

    meb_prune = meb_sub.add_parser("prune", help="Delete TTL-expired and overflow memory events")
    meb_prune.add_argument("--ttl-hours", type=int, dest="ttl_hours",
                           help="Override TTL in hours (default: from meb_config)")
    meb_prune.add_argument("--max-depth", type=int, dest="max_depth",
                           help="Override max queue depth (default: from meb_config)")

    # --- push ---
    psh = sub.add_parser("push", help="Proactive memory push — score + select top-K memories for a task, inject into context")
    psh_sub = psh.add_subparsers(dest="push_cmd")

    psh_run = psh_sub.add_parser("run", help="Score memories for a task and output context block")
    psh_run.add_argument("task", help="Task description to score memories against")
    psh_run.add_argument("--agent", "-a", help="Agent ID to record push as (default: unknown)")
    psh_run.add_argument("--top-k", dest="top_k", type=int, default=5, help="Max memories to push (1-5, default: 5)")
    psh_run.add_argument("--project", "-p", help="Project scope for push event (e.g. my-project)")
    psh_run.add_argument("--format", "-f", choices=["text", "json"], default="text", help="Output format (default: text)")
    psh_run.add_argument("--no-events", action="store_true", dest="no_events", help="Skip event search; memories only")

    psh_report = psh_sub.add_parser("report", help="Show recalled_count utility delta for a previous push")
    psh_report.add_argument("push_id", help="push_id from a previous 'push run' output")

    # --- policy ---
    pol = sub.add_parser("policy", help="Policy memory engine — match, add, and update decision policies")
    pol_sub = pol.add_subparsers(dest="pol_cmd")

    pol_match = pol_sub.add_parser("match", help="Find matching policy directives for a decision context")
    pol_match.add_argument("context", help="Decision context description (natural language)")
    pol_match.add_argument("--agent", "-a", help="Reporting agent ID")
    pol_match.add_argument("--category", help="Filter to a specific policy category")
    pol_match.add_argument("--scope", help="Scope filter (e.g. global, project:agentmemory)")
    pol_match.add_argument("--min-confidence", dest="min_confidence", type=float, default=0.4,
                           help="Minimum effective confidence to include (default: 0.4)")
    pol_match.add_argument("--top-k", dest="top_k", type=int, default=3,
                           help="Maximum results to return (default: 3)")
    pol_match.add_argument("--staleness-mode", dest="staleness_mode",
                           choices=["warn", "block", "ignore"], default="warn",
                           help="How to handle stale policies: warn|block|ignore (default: warn)")
    pol_match.add_argument("--all", action="store_true", dest="all", default=False,
                           help="Neuromod mode: surface ALL policies for scope, bypass top-k and min-confidence")
    pol_match.add_argument("--format", "-f", choices=["text", "json"], default="text")

    pol_list = pol_sub.add_parser("list", help="List all policy memories with status and outcome info")
    pol_list.add_argument("--agent", "-a", help="Reporting agent ID")
    pol_list.add_argument("--status", default="active",
                          help="Status filter: active|candidate|deprecated|all (default: active)")
    pol_list.add_argument("--category", help="Filter to a specific policy category")
    pol_list.add_argument("--scope", help="Scope filter (e.g. global, project:agentmemory)")
    pol_list.add_argument("--format", "-f", choices=["text", "json"], default="text")

    pol_add = pol_sub.add_parser("add", help="Create a new policy memory from a decision + outcome")
    pol_add.add_argument("--name", required=True, help="Human-readable slug (e.g. checkout-conflict-guard)")
    pol_add.add_argument("--trigger", required=True, help="When does this policy apply? (natural language)")
    pol_add.add_argument("--directive", required=True, help="What should the agent do? (natural language)")
    pol_add.add_argument("--agent", "-a", help="Author agent ID (required)")
    pol_add.add_argument("--category", default="general",
                         help=f"Policy category: {', '.join(sorted(_POLICY_CATEGORIES))}")
    pol_add.add_argument("--scope", default="global",
                         help="Scope: global | project:<name> | agent:<id>")
    pol_add.add_argument("--priority", type=int, default=50,
                         help="Priority 0-100, higher = higher precedence (default: 50)")
    pol_add.add_argument("--confidence", type=float, default=0.5,
                         help="Initial confidence 0.0-1.0 (default: 0.5)")
    pol_add.add_argument("--half-life", dest="half_life", type=int, default=30,
                         help="Wisdom half-life in days (default: 30)")
    pol_add.add_argument("--derived-from", dest="derived_from",
                         help="Comma-separated memory/event IDs this was derived from")
    pol_add.add_argument("--expires-at", dest="expires_at",
                         help="Hard expiry date (ISO 8601), optional")

    pol_fb = pol_sub.add_parser("feedback", help="Update policy confidence from observed outcome")
    pol_fb.add_argument("policy_id", help="Policy UUID or name slug")
    pol_fb.add_argument("--success", action="store_true", help="Record successful outcome")
    pol_fb.add_argument("--failure", action="store_true", help="Record failed outcome")
    pol_fb.add_argument("--boost", type=float, help="Custom confidence boost on success (default: 0.02)")
    pol_fb.add_argument("--notes", help="Optional free-text note about the outcome")
    pol_fb.add_argument("--agent", "-a", help="Reporting agent ID")

    # --- neuro ---
    neuro = sub.add_parser("neuro", help="Neuromodulation state — org-state sensing and salience modulation")
    neuro_sub = neuro.add_subparsers(dest="neuro_cmd")

    neuro_status = neuro_sub.add_parser("status", help="Show current neuromodulation state and parameters")
    neuro_status.add_argument("--format", "-f", choices=["text", "json"], default="text")

    neuro_set = neuro_sub.add_parser("set", help="Manually set neuromodulation mode")
    neuro_set.add_argument("mode", help="Mode: normal|urgent|incident|sprint|strategic|focused")
    neuro_set.add_argument("--expires", help="ISO8601 expiry for manual override (e.g. 2026-03-28T18:00:00)")
    neuro_set.add_argument("--notes", help="Optional note about why this override was set")
    neuro_set.add_argument("--agent", "-a", help="Agent ID setting the override")

    neuro_det = neuro_sub.add_parser("detect", help="Auto-detect and apply org_state from recent events")
    neuro_det.add_argument("--force", action="store_true", help="Override active manual lock")
    neuro_det.add_argument("--apply", action="store_true", help="Alias for always-on apply (detect always applies; accepted for compatibility)")
    neuro_det.add_argument("--agent", "-a", help="Agent ID to attribute detection to")
    neuro_det.add_argument("--format", "-f", choices=["text", "json"], default="text")

    neuro_hist = neuro_sub.add_parser("history", help="Show neuromodulation transition history")
    neuro_hist.add_argument("--limit", type=int, default=20, help="Max transitions to show (default: 20)")
    neuro_hist.add_argument("--format", "-f", choices=["text", "json"], default="text")

    neuro_sig = neuro_sub.add_parser("signal", help="Inject a dopamine signal — boost/penalize memory confidence in a scope")
    neuro_sig.add_argument("--dopamine", type=float, required=True, help="Signal strength: -1.0 (penalty) to +1.0 (boost)")
    neuro_sig.add_argument("--scope", "-s", help="Memory scope to target (e.g. project:my-project)")
    neuro_sig.add_argument("--since", help="ISO8601 cutoff — only affect memories recalled after this time")
    neuro_sig.add_argument("--agent", "-a", help="Agent ID injecting the signal")
    neuro_sig.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- neurostate (top-level alias) ---
    p_neurostate = sub.add_parser("neurostate", help="Compute and display current neurotransmitter levels from org activity")
    p_neurostate.add_argument("--detect", action="store_true", help="Auto-detect org_state before computing levels")
    p_neurostate.add_argument("--agent", "-a", help="Agent ID logging the neurostate snapshot")
    p_neurostate.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- workspace ---
    ws = sub.add_parser("workspace", help="Global Workspace Broadcasting — salience-gated org-wide awareness")
    ws_sub = ws.add_subparsers(dest="ws_cmd")

    ws_status = ws_sub.add_parser("status", help="Show current global workspace (active broadcasts)")
    ws_status.add_argument("-n", type=int, default=20, help="Max broadcasts to show (default: 20)")
    ws_status.add_argument("--scope", "-s", help="Filter by scope prefix (e.g. project:my-project)")

    ws_history = ws_sub.add_parser("history", help="Show recent broadcast history")
    ws_history.add_argument("-n", type=int, default=30, help="Max entries (default: 30)")
    ws_history.add_argument("--since", type=int, default=None, metavar="BROADCAST_ID",
                            help="Return only broadcasts with id > BROADCAST_ID")
    ws_history.add_argument("--agent", "-a", help="Filter by broadcasting agent_id")

    ws_broadcast = ws_sub.add_parser("broadcast", help="Manually broadcast a memory into the global workspace")
    ws_broadcast.add_argument("memory_id", type=int, help="Memory ID to broadcast")
    ws_broadcast.add_argument("--summary", help="Override broadcast summary (default: first 200 chars of content)")
    ws_broadcast.add_argument("--scope", "-s", default="global",
                              help="Target scope: global | project:<name> | agent:<id>")
    ws_broadcast.add_argument("--agent", "-a", help="Broadcasting agent ID")

    ws_ack = ws_sub.add_parser("ack", help="Acknowledge receipt of a broadcast")
    ws_ack.add_argument("broadcast_id", type=int, help="Broadcast ID to acknowledge")
    ws_ack.add_argument("--agent", "-a", help="Acknowledging agent ID")

    ws_phi = ws_sub.add_parser("phi", help="Compute org integration metric (Phi) — measures cross-agent awareness")
    ws_phi.add_argument("--breakdown", action="store_true", help="Show per-agent broadcast breakdown")

    ws_cfg = ws_sub.add_parser("config", help="Get or set workspace configuration")
    ws_cfg.add_argument("--key", "-k", help="Config key (omit to list all)")
    ws_cfg.add_argument("--value", "-v", help="New value to set")

    ws_ingest = ws_sub.add_parser("ingest", help="Retroactively score and broadcast recent high-salience memories")
    ws_ingest.add_argument("--hours", type=int, default=1,
                           help="Look back N hours for un-broadcast memories (default: 1)")
    ws_ingest.add_argument("--agent", "-a", help="Agent ID for broadcast attribution")
    ws_ingest.add_argument("--dry-run", action="store_true", dest="dry_run",
                           help="Show what would be broadcast without writing")

    # --- world (Organizational World Model) ---
    wld = sub.add_parser("world", help="Organizational World Model — org snapshot, project dynamics, agent capabilities")
    wld.add_argument("--days", type=int, default=7, help="Activity window in days for default status view (default: 7)")
    wld.add_argument("--json", action="store_true", help="JSON output for default status view")
    wld_sub = wld.add_subparsers(dest="world_cmd")

    wld_status = wld_sub.add_parser("status", help="Compressed org snapshot (default subcommand)")
    wld_status.add_argument("--days", type=int, default=7, help="Activity window in days (default: 7)")
    wld_status.add_argument("--json", action="store_true")

    wld_proj = wld_sub.add_parser("project", help="Project dynamics — velocity, agents, event breakdown")
    wld_proj.add_argument("project", help="Project name or substring to match")
    wld_proj.add_argument("--days", type=int, default=14, help="Activity window in days (default: 14)")
    wld_proj.add_argument("--json", action="store_true")

    wld_agent = wld_sub.add_parser("agent", help="Agent capability profile")
    wld_agent.add_argument("agent_id", help="Agent ID")
    wld_agent.add_argument("--limit", type=int, default=20, help="Max capabilities to show")
    wld_agent.add_argument("--json", action="store_true")

    wld_rebuild = wld_sub.add_parser("rebuild-caps", help="Rebuild agent_capabilities from event + expertise history")
    wld_rebuild.add_argument("--agent", dest="agent_id", help="Limit rebuild to one agent ID")
    wld_rebuild.add_argument("--json", action="store_true")

    wld_predict = wld_sub.add_parser("predict", help="Log a world model prediction for later calibration")
    wld_predict.add_argument("subject", help="Subject ID (task ref, agent_id, project name)")
    wld_predict.add_argument("predicted", help="Predicted state (JSON string or short label)")
    wld_predict.add_argument("--subject-type", dest="subject_type", default="task",
                             help="Subject type: task | agent | project")
    wld_predict.add_argument("--author", help="Authoring agent ID (default: $AGENT_ID)")

    wld_resolve = wld_sub.add_parser("resolve", help="Resolve a prediction with actual outcome")
    wld_resolve.add_argument("snapshot_id", type=int, help="Snapshot ID from 'predict' output")
    wld_resolve.add_argument("actual", help="Actual state (JSON string or label)")
    wld_resolve.add_argument("--error", type=float, help="Scalar prediction error (0.0-1.0)")

    # --- tom (Theory of Mind) ---
    tom = sub.add_parser("tom", help="Theory of Mind — agent belief tracking, conflicts, perspective models")
    tom_sub = tom.add_subparsers(dest="tom_cmd")

    tom_update = tom_sub.add_parser("update", help="Refresh BDI state snapshot for an agent")
    tom_update.add_argument("agent_id", nargs="?", help="Agent ID (omit for all active agents)")
    tom_update.add_argument("--json", action="store_true")
    tom_update.add_argument("--quiet", "-q", action="store_true")

    tom_belief = tom_sub.add_parser("belief", help="Read/write agent beliefs")
    tom_belief_sub = tom_belief.add_subparsers(dest="tom_belief_cmd")
    tom_bs = tom_belief_sub.add_parser("set", help="Set or update a belief")
    tom_bs.add_argument("agent_id", help="Agent ID")
    tom_bs.add_argument("topic", help="Topic key (e.g. global:memory_spine:schema_version)")
    tom_bs.add_argument("content", help="Belief content")
    tom_bs.add_argument("--assumption", action="store_true", help="Mark as unverified assumption")
    tom_bs.add_argument("--confidence", type=float, default=1.0)
    tom_bs.add_argument("--json", action="store_true")
    tom_bi = tom_belief_sub.add_parser("invalidate", help="Invalidate a belief")
    tom_bi.add_argument("agent_id", help="Agent ID")
    tom_bi.add_argument("topic", help="Topic key")
    tom_bi.add_argument("reason", help="Why the belief is now false")
    tom_bi.add_argument("--json", action="store_true")

    tom_conflicts = tom_sub.add_parser("conflicts", help="Belief conflict management")
    tom_conflicts_sub = tom_conflicts.add_subparsers(dest="tom_conflicts_cmd")
    tom_cl = tom_conflicts_sub.add_parser("list", help="List open conflicts")
    tom_cl.add_argument("--agent", help="Filter by agent ID")
    tom_cl.add_argument("--topic", help="Filter by topic substring")
    tom_cl.add_argument("--severity", type=float, default=0.0, help="Minimum severity (0-1)")
    tom_cl.add_argument("--limit", type=int, default=50)
    tom_cl.add_argument("--json", action="store_true")
    tom_cr = tom_conflicts_sub.add_parser("resolve", help="Resolve a conflict")
    tom_cr.add_argument("conflict_id", type=int, help="Conflict ID")
    tom_cr.add_argument("resolution", help="Resolution description")
    tom_cr.add_argument("--json", action="store_true")

    tom_perspective = tom_sub.add_parser("perspective", help="Observer perspective models")
    tom_persp_sub = tom_perspective.add_subparsers(dest="tom_persp_cmd")
    tom_ps = tom_persp_sub.add_parser("set", help="Set observer's model of subject on topic")
    tom_ps.add_argument("observer", help="Observer agent ID")
    tom_ps.add_argument("subject", help="Subject agent ID")
    tom_ps.add_argument("topic", help="Topic key")
    tom_ps.add_argument("--belief", help="Observer's estimate of subject's belief")
    tom_ps.add_argument("--gap", help="Knowledge gap text")
    tom_ps.add_argument("--confusion", type=float, default=0.0, help="Confusion risk 0-1")
    tom_ps.add_argument("--json", action="store_true")
    tom_pg = tom_persp_sub.add_parser("get", help="Get observer's model of subject")
    tom_pg.add_argument("observer", help="Observer agent ID")
    tom_pg.add_argument("subject", help="Subject agent ID")
    tom_pg.add_argument("--json", action="store_true")

    tom_gs = tom_sub.add_parser("gap-scan", help="Scan agent's active tasks for belief gaps")
    tom_gs.add_argument("agent_id", help="Agent ID")
    tom_gs.add_argument("--json", action="store_true")

    tom_inj = tom_sub.add_parser("inject", help="Inject gap-filling memory for an agent on a topic")
    tom_inj.add_argument("agent_id", help="Target agent ID")
    tom_inj.add_argument("topic", help="Topic key")
    tom_inj.add_argument("--content", help="Content to inject (uses knowledge_gap if omitted)")
    tom_inj.add_argument("--observer", help="Observer agent ID (defaults to agent_id)")
    tom_inj.add_argument("--json", action="store_true")

    tom_st = tom_sub.add_parser("status", help="BDI health summary ranked by confusion risk")
    tom_st.add_argument("agent_id", nargs="?", help="Agent ID (omit for all)")
    tom_st.add_argument("--json", action="store_true")

    # --- agent-model ---
    am = sub.add_parser("agent-model", help="Show full mental model for an agent")
    am.add_argument("agent_id", help="Agent ID")
    am.add_argument("--json", action="store_true")

    # --- belief (top-level) ---
    belief_cmd = sub.add_parser("belief", help="Agent belief model — read/write/seed beliefs about agents")
    belief_sub = belief_cmd.add_subparsers(dest="belief_cmd")

    bel_set = belief_sub.add_parser("set", help="Write a belief about a target agent")
    bel_set.add_argument("target_agent", help="Target agent ID (the agent the belief is about)")
    bel_set.add_argument("belief_type", help="Belief type: capability, goal, uncertainty, knowledge, preference")
    bel_set.add_argument("content", help="Belief content text")
    bel_set.add_argument("--confidence", type=float, default=1.0, help="Confidence score 0-1 (default: 1.0)")
    bel_set.add_argument("--assumption", action="store_true", help="Mark as unverified assumption")
    bel_set.add_argument("--json", action="store_true")

    bel_get = belief_sub.add_parser("get", help="Retrieve active beliefs about a target agent")
    bel_get.add_argument("target_agent", help="Target agent ID")
    bel_get.add_argument("--observer", help="Filter by observer agent ID")
    bel_get.add_argument("--json", action="store_true")

    bel_seed = belief_sub.add_parser("seed", help="Seed capability beliefs from agent_expertise table")
    bel_seed.add_argument("--min-strength", type=float, default=0.3, dest="min_strength",
                          help="Minimum expertise strength to include (default: 0.3)")
    bel_seed.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="Preview without writing")
    bel_seed.add_argument("--json", action="store_true")

    # --- belief-conflicts ---
    bc_cmd = sub.add_parser("belief-conflicts", help="List open cross-agent belief conflicts")
    bc_cmd.add_argument("--agent", help="Filter by agent ID")
    bc_cmd.add_argument("--topic", help="Filter by topic substring")
    bc_cmd.add_argument("--severity", type=float, default=0.0, help="Minimum severity (0-1)")
    bc_cmd.add_argument("--limit", type=int, default=50)
    bc_cmd.add_argument("--json", action="store_true")

    # --- collapse-log ---
    cl_cmd = sub.add_parser("collapse-log", help="List belief collapse events")
    cl_cmd.add_argument("--belief-id", dest="belief_id", default=None, help="Filter by belief/memory ID")
    cl_cmd.add_argument("--agent-id", dest="agent_id", default=None, help="Filter by agent ID")
    cl_cmd.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    cl_cmd.add_argument("--json", action="store_true", help="Output JSON")

    # --- collapse-stats ---
    cs_cmd = sub.add_parser("collapse-stats", help="Aggregate statistics for belief collapses")
    cs_cmd.add_argument("--json", action="store_true", help="Output JSON")

    # --- resolve-conflict ---
    rc_cmd = sub.add_parser("resolve-conflict", help="AGM credibility-weighted belief conflict resolution")
    rc_cmd.add_argument("conflict_id", nargs="?", type=int, help="Conflict ID to resolve")
    rc_cmd.add_argument("--list",          action="store_true", help="List open conflicts with scores")
    rc_cmd.add_argument("--auto",          action="store_true", help="Batch resolve all auto-resolvable conflicts")
    rc_cmd.add_argument("--dry-run",       action="store_true", dest="dry_run", help="Show what would happen without writing")
    rc_cmd.add_argument("--force-winner",  metavar="AGENT_ID",  dest="force_winner", help="Force a specific agent to win")
    rc_cmd.add_argument("--threshold",     type=float,          default=0.05, help="Min score delta to auto-resolve (default: 0.05)")
    rc_cmd.add_argument("--json",          action="store_true", help="Output JSON")

    # --- reason (L1+L2) ---
    p_reason = sub.add_parser("reason", help="Neuro-symbolic L1+L2: hybrid search + graph expansion with provenance")
    p_reason.add_argument("query", help="Query to reason about")
    p_reason.add_argument("--limit", "-n", type=int, default=10, help="Max L1 results per table (default: 10)")
    p_reason.add_argument("--hops", type=int, default=2, help="Graph expansion hops (default: 2)")
    p_reason.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- infer (L1+L2+L3) ---
    p_infer = sub.add_parser("infer", help="Neuro-symbolic L1+L2+L3: full inference — search + graph + policy + confidence chain")
    p_infer.add_argument("query", help="Query to infer conclusions about")
    p_infer.add_argument("--limit", "-n", type=int, default=10, help="Max L1 results per table (default: 10)")
    p_infer.add_argument("--hops", type=int, default=2, help="Graph expansion hops (default: 2)")
    p_infer.add_argument("--min-confidence", type=float, default=None, dest="min_confidence",
                         help="Minimum policy confidence threshold (default: 0.0)")
    p_infer.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- infer-pretask (Active Inference Layer) ---
    p_infer_pretask = sub.add_parser("infer-pretask",
        help="Pre-task uncertainty scan: free energy over low-confidence memories")
    p_infer_pretask.add_argument("task_desc", help="Task description to scan for knowledge gaps")
    p_infer_pretask.add_argument("--limit", "-n", type=int, default=10)
    p_infer_pretask.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # --- infer-gapfill (Active Inference Layer) ---
    p_infer_gapfill = sub.add_parser("infer-gapfill",
        help="Resolve open uncertainty gaps after task completion")
    p_infer_gapfill.add_argument("task_desc", help="Task description (matches against logged gaps)")
    p_infer_gapfill.add_argument("--content", help="Fact learned during task (creates a new memory)")
    p_infer_gapfill.add_argument("--format", "-f", choices=["text", "json"], default="text")

    # outcome: Outcome-Linked Memory Evaluation
    out_cmd = sub.add_parser("outcome", help="Outcome-linked memory evaluation — annotate tasks and view calibration metrics")
    out_sub = out_cmd.add_subparsers(dest="outcome_cmd")

    out_ann = out_sub.add_parser("annotate", help="Annotate completed task in access_log")
    out_ann.add_argument("task_id", help="Task identifier (e.g. PROJ-123)")
    out_ann.add_argument("--outcome", required=True, choices=["success", "blocked", "escalated", "cancelled"])
    out_ann.add_argument("--agent", "-a", dest="agent_id", default=None, help="Agent ID (default: $AGENT_ID or 'unknown')")

    out_rep = out_sub.add_parser("report", help="View memory lift and Brier score calibration report")
    out_rep.add_argument("--period", type=int, default=30, metavar="DAYS")
    out_rep.add_argument("--agent", "-a", dest="agent_id", default=None)
    out_rep.add_argument("--json", action="store_true")
    out_rep.add_argument("--save", action="store_true", help="Persist calibration snapshot to memory_outcome_calibration")

    # --- monitor ---
    mon_p = sub.add_parser("monitor", help="Stream new events, memories, and affect changes in real-time")
    mon_p.add_argument("--agent", "-a", default=None, dest="agent", help="Filter to a specific agent ID")
    mon_p.add_argument("--interval", type=float, default=2.0, metavar="SECONDS",
                       help="Poll interval in seconds (default: 2.0)")
    mon_p.add_argument("--tail", type=int, default=20, metavar="N",
                       help="Number of recent items to show on startup (default: 20)")
    mon_p.add_argument("--types", metavar="TYPE1,TYPE2",
                       help="Comma-separated event_types to filter (applies to events only)")

    # --- config ---
    p_config = sub.add_parser("config", help="Manage brainctl configuration")
    config_sub = p_config.add_subparsers(dest="config_cmd")
    p_config_init = config_sub.add_parser("init", help="Create default config file")
    p_config_init.add_argument("--force", action="store_true", help="Overwrite existing config")
    config_sub.add_parser("show", help="Show effective configuration")

    # --- migrate ---
    p_migrate = sub.add_parser("migrate", help="Apply pending database migrations")
    p_migrate.add_argument("--status", action="store_true", help="Show migration status without applying")
    p_migrate.add_argument("--dry-run", action="store_true", help="Show what would be applied without writing")
    p_migrate.add_argument("--path", help="Path to brain.db (default: from env/config)")

    # --- merge ---
    p_merge = sub.add_parser("merge", help="Merge two brain.db files — combine offline work or sync from backup")
    p_merge.add_argument("source", help="Path to source brain.db (merged INTO target)")
    p_merge.add_argument("--target", default=None, metavar="PATH",
                         help="Path to target brain.db (default: $BRAIN_DB)")
    p_merge.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="Preview what would be merged without making changes")
    p_merge.add_argument("--tables", default=None, metavar="TABLE1,TABLE2",
                         help="Only merge specific tables (comma-separated)")

    # --- schedule ---
    p_sched = sub.add_parser("schedule", help="Hippocampus consolidation scheduler daemon")
    sched_sub = p_sched.add_subparsers(dest="sched_cmd")

    sched_sub.add_parser("status", help="Show schedule config and last/next run times")

    sched_run = sched_sub.add_parser("run", help="Run one consolidation cycle now")
    sched_run.add_argument("--agent", default="hippocampus", help="Agent ID for event attribution")

    sched_set = sched_sub.add_parser("set", help="Configure the schedule")
    sched_set.add_argument("--interval", type=int, default=None, metavar="N",
                           help="Interval in minutes between consolidation runs")
    sched_set.add_argument("--enabled", dest="enabled", action="store_true", default=None,
                           help="Enable the scheduled daemon")
    sched_set.add_argument("--disabled", dest="enabled", action="store_false",
                           help="Disable the scheduled daemon")

    sched_start = sched_sub.add_parser("start", help="Start daemon loop (runs consolidation on interval)")
    sched_start.add_argument("--interval", type=int, default=None, metavar="N",
                             help="Override interval in minutes (default: from config, else 60)")
    sched_start.add_argument("--daemon", action="store_true",
                             help="Fork to background (if supported), otherwise run in foreground")
    sched_start.add_argument("--agent", default="hippocampus", help="Agent ID for event attribution")

    # --- obsidian ---
    from agentmemory.commands.obsidian import register_parser as _obs_register
    _obs_register(sub)

    return p

# ---------------------------------------------------------------------------
# Outcome subcommands
# ---------------------------------------------------------------------------

def cmd_outcome_annotate(args):
    import os
    sys.path.insert(0, str(Path.home() / "bin" / "lib"))
    from outcome_eval import annotate_task_retrieval
    agent_id = args.agent_id or os.environ.get("AGENT_ID", "unknown")
    n = annotate_task_retrieval(args.task_id, agent_id, args.outcome)
    json_out({"ok": True, "task_id": args.task_id, "outcome": args.outcome, "agent_id": agent_id, "rows_annotated": n})


def cmd_outcome_report(args):
    import os
    sys.path.insert(0, str(Path.home() / "bin" / "lib"))
    from outcome_eval import compute_memory_lift, compute_brier_score, compute_precision_at_k, run_calibration_pass
    agent_id = args.agent_id or os.environ.get("AGENT_ID", "unknown")
    period = args.period

    if args.save:
        result = run_calibration_pass(agent_id=agent_id, period_days=period)
        if args.json:
            json_out(result)
            return
        lift_with = result["success_with_memory"]
        lift_without = result["success_without_memory"]
        lift_pp = result["lift_pp"]
        brier = result["brier_score"]
        p5 = result["p_at_5"]
    else:
        lift = compute_memory_lift(period_days=period)
        brier = compute_brier_score(agent_id=agent_id, period_days=period)
        p5 = compute_precision_at_k(agent_id=agent_id, k=5, period_days=period)
        lift_with = lift["with_memory_success_rate"]
        lift_without = lift["without_memory_success_rate"]
        lift_pp = lift["lift_pp"]
        result = {
            "agent_id": agent_id, "period_days": period,
            "success_with_memory": lift_with, "success_without_memory": lift_without,
            "lift_pp": lift_pp, "brier_score": brier, "p_at_5": p5,
            "tasks_with_memory": lift["tasks_with_memory"],
            "tasks_without_memory": lift["tasks_without_memory"],
        }

    if args.json:
        json_out(result)
        return

    use_color = sys.stdout.isatty()
    BOLD = "\033[1m" if use_color else ""
    DIM = "\033[2m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    def _pct(v):
        return f"{v:.0%}" if v is not None else "n/a (insufficient data)"

    def _f2(v):
        return f"{v:.4f}" if v is not None else "n/a"

    print()
    print(f"{BOLD}Outcome-Linked Memory Evaluation{RESET}  {DIM}period: last {period} days  |  agent: {agent_id}{RESET}")
    print()
    print(f"  Tasks with memory retrieval:    {result.get('tasks_with_memory', '?'):>4}  →  {_pct(lift_with)} success")
    print(f"  Tasks without memory retrieval: {result.get('tasks_without_memory', '?'):>4}  →  {_pct(lift_without)} success")
    if lift_pp is not None:
        sign = "+" if lift_pp >= 0 else ""
        print(f"  Memory lift:                    {sign}{lift_pp:.1f} pp")
    else:
        print(f"  Memory lift:                    n/a (insufficient data)")
    print(f"  Brier score:                    {_f2(brier)}  {DIM}(0=perfect, 1=worst){RESET}")
    print(f"  Precision@5:                    {_f2(p5)}")
    print()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def cmd_config(args):
    from agentmemory.config import init_config_file, show as config_show
    sub = getattr(args, 'config_cmd', None)
    if sub == 'init':
        force = getattr(args, 'force', False)
        created, path = init_config_file(force=force)
        json_out({"ok": True, "created": created, "path": path})
    elif sub == 'show':
        json_out(config_show())
    else:
        json_out({"error": "Usage: brainctl config [init|show]"})


def cmd_migrate(args):
    from agentmemory.migrate import run as migrate_run, status as migrate_status
    db = str(getattr(args, 'path', None) or DB_PATH)

    if args.status:
        result = migrate_status(db)
        json_out(result)
        return

    dry_run = getattr(args, 'dry_run', False)
    result = migrate_run(db, dry_run=dry_run)
    json_out(result)


# ---------------------------------------------------------------------------
# Merge command
# ---------------------------------------------------------------------------

def cmd_merge(args):
    """Merge source brain.db into target brain.db."""
    from agentmemory.merge import merge as do_merge

    source = args.source
    target = str(getattr(args, 'target', None) or DB_PATH)
    dry_run = getattr(args, 'dry_run', False)
    tables_raw = getattr(args, 'tables', None)
    table_list = [t.strip() for t in tables_raw.split(",") if t.strip()] if tables_raw else None

    result = do_merge(
        source_path=source,
        target_path=target,
        dry_run=dry_run,
        tables=table_list,
    )
    json_out(result)


# ---------------------------------------------------------------------------
# Monitor command
# ---------------------------------------------------------------------------

def cmd_monitor(args):
    """Stream new events, memories, and affect changes in real-time."""
    db = get_db()
    interval = getattr(args, "interval", 2.0) or 2.0
    tail_n = getattr(args, "tail", 20) or 20
    agent_filter = getattr(args, "agent", None)
    types_filter = getattr(args, "types", None)

    # Parse --types filter
    type_set = None
    if types_filter:
        type_set = {t.strip() for t in types_filter.split(",") if t.strip()}

    # ANSI colors
    use_color = sys.stdout.isatty()
    _C = {
        "EVENT":  "\033[96m",   # cyan
        "MEM":    "\033[92m",   # green
        "AFFECT": "\033[93m",   # yellow
        "RESET":  "\033[0m",
        "DIM":    "\033[2m",
    }
    def _col(key, text):
        if not use_color:
            return text
        return f"{_C[key]}{text}{_C['RESET']}"

    def _print_line(prefix, event_type, agent_id, content, ts):
        ts_str = (ts or "")[:19]
        label = _col(prefix, f"[{prefix}/{event_type}]")
        dim_agent = _col("DIM", agent_id) if use_color else agent_id
        snippet = (content or "").replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        print(f"{ts_str} {label} {dim_agent}: \"{snippet}\"", flush=True)

    # Check if affect_log table exists
    has_affect = bool(db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='affect_log'"
    ).fetchone())

    # ---- Tail: show last N items on startup ----
    # Events tail
    ev_sql = "SELECT id, agent_id, event_type, summary, created_at FROM events WHERE 1=1"
    ev_params = []
    if agent_filter:
        ev_sql += " AND agent_id = ?"
        ev_params.append(agent_filter)
    if type_set:
        placeholders = ",".join("?" * len(type_set))
        ev_sql += f" AND event_type IN ({placeholders})"
        ev_params.extend(sorted(type_set))
    ev_sql += " ORDER BY id DESC LIMIT ?"
    ev_params.append(tail_n)
    tail_events = list(reversed(db.execute(ev_sql, ev_params).fetchall()))

    # Memories tail
    mem_sql = "SELECT id, agent_id, category, content, created_at FROM memories WHERE retired_at IS NULL"
    mem_params = []
    if agent_filter:
        mem_sql += " AND agent_id = ?"
        mem_params.append(agent_filter)
    mem_sql += " ORDER BY id DESC LIMIT ?"
    mem_params.append(tail_n)
    tail_mems = list(reversed(db.execute(mem_sql, mem_params).fetchall()))

    # Affect tail
    tail_affect = []
    if has_affect:
        aff_sql = "SELECT id, agent_id, affect_label, functional_state, created_at FROM affect_log WHERE 1=1"
        aff_params = []
        if agent_filter:
            aff_sql += " AND agent_id = ?"
            aff_params.append(agent_filter)
        aff_sql += " ORDER BY id DESC LIMIT ?"
        aff_params.append(tail_n)
        tail_affect = [dict(r) for r in reversed(db.execute(aff_sql, aff_params).fetchall())]

    # Print tail items interleaved by timestamp
    tail_items = []
    for r in tail_events:
        tail_items.append(("EVENT", r["event_type"] or "event", r["agent_id"], r["summary"], r["created_at"], r["id"]))
    for r in tail_mems:
        tail_items.append(("MEM", r["category"] or "memory", r["agent_id"], r["content"], r["created_at"], r["id"]))
    for r in tail_affect:
        content = r.get("functional_state") or r.get("affect_label") or "affect"
        tail_items.append(("AFFECT", r.get("affect_label") or "affect", r["agent_id"], content, r["created_at"], r["id"]))

    tail_items.sort(key=lambda x: (x[4] or ""))
    for prefix, ev_type, agent_id, content, ts, _ in tail_items:
        _print_line(prefix, ev_type, agent_id, content, ts)

    # Track high-water marks
    row = db.execute("SELECT MAX(id) FROM events").fetchone()
    last_event_id = row[0] if row and row[0] is not None else 0

    row = db.execute("SELECT MAX(id) FROM memories").fetchone()
    last_mem_id = row[0] if row and row[0] is not None else 0

    last_affect_id = 0
    if has_affect:
        row = db.execute("SELECT MAX(id) FROM affect_log").fetchone()
        last_affect_id = row[0] if row and row[0] is not None else 0

    # ---- Poll loop ----
    try:
        while True:
            time.sleep(interval)

            # New events
            ev_sql = (
                "SELECT id, agent_id, event_type, summary, created_at FROM events "
                "WHERE id > ?"
            )
            ev_params = [last_event_id]
            if agent_filter:
                ev_sql += " AND agent_id = ?"
                ev_params.append(agent_filter)
            if type_set:
                placeholders = ",".join("?" * len(type_set))
                ev_sql += f" AND event_type IN ({placeholders})"
                ev_params.extend(sorted(type_set))
            ev_sql += " ORDER BY id ASC"
            new_events = db.execute(ev_sql, ev_params).fetchall()
            for r in new_events:
                _print_line("EVENT", r["event_type"] or "event", r["agent_id"], r["summary"], r["created_at"])
                last_event_id = max(last_event_id, r["id"])

            # New memories
            mem_sql = (
                "SELECT id, agent_id, category, content, created_at FROM memories "
                "WHERE id > ? AND retired_at IS NULL"
            )
            mem_params = [last_mem_id]
            if agent_filter:
                mem_sql += " AND agent_id = ?"
                mem_params.append(agent_filter)
            mem_sql += " ORDER BY id ASC"
            new_mems = db.execute(mem_sql, mem_params).fetchall()
            for r in new_mems:
                _print_line("MEM", r["category"] or "memory", r["agent_id"], r["content"], r["created_at"])
                last_mem_id = max(last_mem_id, r["id"])

            # New affect
            if has_affect:
                aff_sql = (
                    "SELECT id, agent_id, affect_label, functional_state, created_at FROM affect_log "
                    "WHERE id > ?"
                )
                aff_params = [last_affect_id]
                if agent_filter:
                    aff_sql += " AND agent_id = ?"
                    aff_params.append(agent_filter)
                aff_sql += " ORDER BY id ASC"
                new_affect = [dict(r) for r in db.execute(aff_sql, aff_params).fetchall()]
                for r in new_affect:
                    content = r.get("functional_state") or r.get("affect_label") or "affect"
                    _print_line("AFFECT", r.get("affect_label") or "affect", r["agent_id"], content, r["created_at"])
                    last_affect_id = max(last_affect_id, r["id"])

    except KeyboardInterrupt:
        print("\n[monitor] stopped.", flush=True)


# ---------------------------------------------------------------------------
# Schedule command
# ---------------------------------------------------------------------------


def cmd_schedule(args):
    """Route schedule subcommands."""
    from agentmemory.scheduler import (
        ConsolidationScheduler,
        get_schedule_config,
        set_schedule_config,
    )

    sched_cmd = getattr(args, "sched_cmd", None)
    db_path = str(DB_PATH)
    agent_id = getattr(args, "agent", "hippocampus")

    if sched_cmd == "status":
        config = get_schedule_config(db_path)
        json_out({"ok": True, **config})

    elif sched_cmd == "run":
        config = get_schedule_config(db_path)
        scheduler = ConsolidationScheduler(
            db_path=db_path,
            interval_minutes=config.get("interval_minutes", 60),
            agent_id=agent_id,
        )
        result = scheduler.run_once()
        json_out(result)

    elif sched_cmd == "set":
        config = get_schedule_config(db_path)
        interval = getattr(args, "interval", None)
        if interval is None:
            interval = config.get("interval_minutes", 60)
        enabled = getattr(args, "enabled", None)
        if enabled is None:
            enabled = config.get("enabled", False)
        saved = set_schedule_config(db_path=db_path, interval_minutes=interval, enabled=enabled, agent_id=agent_id)
        json_out({"ok": True, "config": saved})

    elif sched_cmd == "start":
        config = get_schedule_config(db_path)
        interval = getattr(args, "interval", None) or config.get("interval_minutes", 60)
        daemon_mode = getattr(args, "daemon", False)

        if daemon_mode:
            import subprocess
            cmd = [
                sys.executable, "-m", "agentmemory.scheduler",
                "--db-path", db_path,
                "--interval", str(interval),
                "--agent", agent_id,
            ]
            proc = subprocess.Popen(cmd, start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            json_out({"ok": True, "daemonized": True, "pid": proc.pid,
                      "interval_minutes": interval, "agent_id": agent_id})
        else:
            print(f"[schedule] Starting consolidation daemon (interval={interval}m). Press Ctrl+C to stop.",
                  flush=True)
            scheduler = ConsolidationScheduler(
                db_path=db_path,
                interval_minutes=interval,
                agent_id=agent_id,
            )
            try:
                scheduler.run_daemon()
            except KeyboardInterrupt:
                json_out({"ok": True, "stopped": True, "runs_completed": scheduler._runs_completed})
    else:
        json_out({"error": "Usage: brainctl schedule [status|run|set|start]"})


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Auto-register agent on any write command to prevent FK violations on fresh DBs
    if args.command not in ("version", "init", "validate") and getattr(args, "agent", None):
        try:
            _ensure_agent(get_db(), args.agent)
        except Exception:
            pass

    dispatch = {
        "version": cmd_version,
        "search": cmd_search,
        "backup": cmd_backup,
        "init": cmd_init,
        "stats": cmd_stats,
        "cost": cmd_cost,
        "affect": None,  # subcommand dispatch below
        "report": cmd_report,
        "lint": cmd_lint,
        "index": cmd_index,

        "validate": cmd_validate,
        "prune-log": cmd_prune_access_log,
        "push": None,  # handled below
        "promote": cmd_promote,
        "distill": cmd_distill,
        "dreams": cmd_dreams,
        "temporal-context": cmd_temporal_context,
        "vsearch": cmd_vsearch,
        "graph": cmd_graph,
        "health": cmd_health,
        "dashboard": cmd_dashboard,
        "neurostate": cmd_neurostate,
        "ui": None,  # handled below
    }

    # Sub-command dispatch
    if args.command == "agent":
        dispatch = {"register": cmd_agent_register, "list": cmd_agent_list, "ping": cmd_agent_ping}
        fn = dispatch.get(args.agent_cmd)
    elif args.command == "memory":
        dispatch = {"add": cmd_memory_add, "search": cmd_memory_search, "list": cmd_memory_list,
                     "retire": cmd_memory_retire, "replace": cmd_memory_replace,
                     "retract": cmd_memory_retract, "trust-propagate": cmd_memory_trust_propagate,
                     "update": cmd_memory_update,
                     "suggest-category": cmd_memory_suggest_category,
                     "confidence": cmd_memory_confidence,
                     "pii": cmd_memory_pii, "pii-scan": cmd_memory_pii_scan}
        fn = dispatch.get(args.mem_cmd)
    elif args.command == "entity":
        dispatch = {
            "create": cmd_entity_create, "get": cmd_entity_get, "search": cmd_entity_search,
            "list": cmd_entity_list, "update": cmd_entity_update, "observe": cmd_entity_observe,
            "relate": cmd_entity_relate, "delete": cmd_entity_delete,
        }
        fn = dispatch.get(args.ent_cmd)
    elif args.command == "trigger":
        dispatch = {
            "create": cmd_trigger_create, "list": cmd_trigger_list, "check": cmd_trigger_check,
            "fire": cmd_trigger_fire, "cancel": cmd_trigger_cancel,
        }
        fn = dispatch.get(args.trg_cmd)
    elif args.command == "event":
        dispatch = {"add": cmd_event_add, "search": cmd_event_search, "tail": cmd_event_tail,
                    "link": cmd_event_link}
        fn = dispatch.get(args.ev_cmd)
    elif args.command == "temporal":
        dispatch = {
            "causes": cmd_temporal_causes,
            "effects": cmd_temporal_effects,
            "chain": cmd_temporal_chain,
            "auto-detect": cmd_temporal_auto_detect,
        }
        fn = dispatch.get(args.temporal_cmd)
    elif args.command == "epoch":
        dispatch = {"detect": cmd_epoch_detect, "create": cmd_epoch_create, "list": cmd_epoch_list}
        fn = dispatch.get(args.epoch_cmd)
    elif args.command == "context":
        dispatch = {"add": cmd_context_add, "search": cmd_context_search}
        fn = dispatch.get(args.ctx_cmd)
    elif args.command == "task":
        dispatch = {"add": cmd_task_add, "update": cmd_task_update, "list": cmd_task_list}
        fn = dispatch.get(args.task_cmd)
    elif args.command == "decision":
        dispatch = {"add": cmd_decision_add, "list": cmd_decision_list}
        fn = dispatch.get(args.dec_cmd)
    elif args.command == "handoff":
        dispatch = {
            "add": cmd_handoff_add,
            "list": cmd_handoff_list,
            "latest": cmd_handoff_latest,
            "consume": cmd_handoff_consume,
            "pin": cmd_handoff_pin,
            "expire": cmd_handoff_expire,
        }
        fn = dispatch.get(args.handoff_cmd)
    elif args.command == "state":
        dispatch = {"get": cmd_state_get, "set": cmd_state_set}
        fn = dispatch.get(args.state_cmd)
    elif args.command == "attention-class":
        dispatch = {"get": cmd_attention_class_get, "set": cmd_attention_class_set}
        fn = dispatch.get(args.attn_cmd)
    elif args.command == "budget":
        dispatch = {"status": cmd_budget_status}
        fn = dispatch.get(args.budget_cmd)
    elif args.command == "vec":
        dispatch = {"purge-retired": cmd_vec_purge_retired}
        fn = dispatch.get(args.vec_cmd)
    elif args.command == "weights":
        fn = cmd_weights
    elif args.command == "gaps":
        dispatch = {
            "refresh": cmd_gaps_refresh,
            "scan": cmd_gaps_scan,
            "list": cmd_gaps_list,
            "resolve": cmd_gaps_resolve,
        }
        fn = dispatch.get(args.gaps_cmd)
    elif args.command == "expertise":
        dispatch = {
            "build": cmd_expertise_build,
            "show": cmd_expertise_show,
            "list": cmd_expertise_list,
            "update": cmd_expertise_update,
        }
        fn = dispatch.get(args.exp_cmd)
    elif args.command == "affect":
        dispatch = {
            "log": cmd_affect_log, "check": cmd_affect_check,
            "history": cmd_affect_history, "monitor": cmd_affect_monitor,
            "classify": cmd_affect_classify,
        }
        fn = dispatch.get(args.affect_cmd)
    elif args.command == "whosknows":
        fn = cmd_whosknows
    elif args.command == "ui":
        try:
            from agentmemory.ui.server import serve as _ui_serve
        except ImportError:
            # Fallback for dev checkout
            import sys as _sys
            _sys.path.insert(0, str(Path.home() / "agentmemory" / "ui"))
            from server import serve as _ui_serve
        _ui_serve(port=args.port, db_path=str(DB_PATH), open_browser=not args.no_browser)
        return
    elif args.command == "push":
        push_dispatch = {
            "run": cmd_push,
            "report": cmd_push_report,
        }
        fn = push_dispatch.get(args.push_cmd)
    elif args.command == "trust":
        dispatch = {
            "show":                   cmd_trust_show,
            "audit":                  cmd_trust_audit,
            "calibrate":              cmd_trust_calibrate,
            "decay":                  cmd_trust_decay,
            "update-on-contradiction": cmd_trust_update_contradiction,
            "process-meb":            cmd_trust_process_meb,
        }
        fn = dispatch.get(args.trust_cmd)
    elif args.command == "meb":
        dispatch = {
            "tail":      cmd_meb_tail,
            "subscribe": cmd_meb_subscribe,
            "stats":     cmd_meb_stats,
            "prune":     cmd_meb_prune,
        }
        fn = dispatch.get(args.meb_cmd)
    elif args.command == "reflexion":
        dispatch = {
            "write": cmd_reflexion_write,
            "list": cmd_reflexion_list,
            "query": cmd_reflexion_query,
            "success": cmd_reflexion_success,
            "failure-recurrence": cmd_reflexion_failure_recurrence,
            "retire": cmd_reflexion_retire,
        }
        fn = dispatch.get(args.rfx_cmd)
    elif args.command == "policy":
        dispatch = {
            "match": cmd_policy_match,
            "add": cmd_policy_add,
            "feedback": cmd_policy_feedback,
            "list": cmd_policy_list,
        }
        fn = dispatch.get(args.pol_cmd)
    elif args.command == "neuro":
        dispatch = {
            "status": cmd_neuro_status,
            "set": cmd_neuro_set,
            "detect": cmd_neuro_detect,
            "history": cmd_neuro_history,
            "signal": cmd_neuro_signal,
        }
        fn = dispatch.get(args.neuro_cmd)
    elif args.command == "neurostate":
        cmd_neurostate(args)
        return
    elif args.command == "tom":
        tom_cmd = args.tom_cmd
        if tom_cmd == "update":
            cmd_tom_update(args)
            return
        elif tom_cmd == "belief":
            belief_dispatch = {"set": cmd_tom_belief_set, "invalidate": cmd_tom_belief_invalidate}
            fn = belief_dispatch.get(args.tom_belief_cmd)
        elif tom_cmd == "conflicts":
            conflicts_dispatch = {"list": cmd_tom_conflicts_list, "resolve": cmd_tom_conflicts_resolve}
            fn = conflicts_dispatch.get(args.tom_conflicts_cmd)
        elif tom_cmd == "perspective":
            persp_dispatch = {"set": cmd_tom_perspective_set, "get": cmd_tom_perspective_get}
            fn = persp_dispatch.get(args.tom_persp_cmd)
        elif tom_cmd == "gap-scan":
            cmd_tom_gap_scan(args)
            return
        elif tom_cmd == "inject":
            cmd_tom_inject(args)
            return
        elif tom_cmd == "status":
            cmd_tom_status(args)
            return
        else:
            fn = None
        if fn:
            fn(args)
        else:
            parser.print_help()
        return
    elif args.command == "agent-model":
        cmd_agent_model(args)
        return
    elif args.command == "belief":
        belief_dispatch = {
            "set": cmd_belief_set,
            "get": cmd_belief_get,
            "seed": cmd_belief_seed,
        }
        fn = belief_dispatch.get(args.belief_cmd)
        if fn:
            fn(args)
        else:
            parser.print_help()
        return
    elif args.command == "collapse-log":
        cmd_collapse_log(args)
        return
    elif args.command == "collapse-stats":
        cmd_collapse_stats(args)
        return
    elif args.command == "belief-conflicts":
        cmd_belief_conflicts(args)
        return
    elif args.command == "resolve-conflict":
        cmd_resolve_conflict(args)
        return
    elif args.command == "reason":
        cmd_reason(args)
        return
    elif args.command == "infer":
        cmd_infer(args)
        return
    elif args.command == "infer-pretask":
        cmd_infer_pretask(args)
        return
    elif args.command == "infer-gapfill":
        cmd_infer_gapfill(args)
        return
    elif args.command == "outcome":
        outcome_dispatch = {
            "annotate": cmd_outcome_annotate,
            "report":   cmd_outcome_report,
        }
        fn = outcome_dispatch.get(args.outcome_cmd)
        if fn:
            fn(args)
        else:
            parser.print_help()
        return
    elif args.command == "workspace":
        dispatch = {
            "status":    cmd_workspace_status,
            "history":   cmd_workspace_history,
            "broadcast": cmd_workspace_broadcast,
            "ack":       cmd_workspace_ack,
            "phi":       cmd_workspace_phi,
            "config":    cmd_workspace_config_cmd,
            "ingest":    cmd_workspace_ingest,
        }
        fn = dispatch.get(args.ws_cmd)
    elif args.command == "world":
        world_cmd = getattr(args, "world_cmd", None) or "status"
        dispatch = {
            "status":       cmd_world_status,
            "project":      cmd_world_project,
            "agent":        cmd_world_agent,
            "rebuild-caps": cmd_world_rebuild_caps,
            "predict":      cmd_world_predict,
            "resolve":      cmd_world_resolve,
        }
        fn = dispatch.get(world_cmd)
        if fn is None:
            fn = cmd_world_status  # default subcommand
    elif args.command == "monitor":
        fn = cmd_monitor
    elif args.command == "config":
        fn = cmd_config
    elif args.command == "migrate":
        cmd_migrate(args)
        return
    elif args.command == "merge":
        cmd_merge(args)
        return
    elif args.command == "schedule":
        cmd_schedule(args)
        return
    elif args.command == "obsidian":
        from agentmemory.commands.obsidian import (
            cmd_obsidian_export, cmd_obsidian_import,
            cmd_obsidian_watch, cmd_obsidian_status,
        )
        obs_dispatch = {
            "export": cmd_obsidian_export,
            "import": cmd_obsidian_import,
            "watch":  cmd_obsidian_watch,
            "status": cmd_obsidian_status,
        }
        fn = obs_dispatch.get(args.obs_cmd)
        if fn:
            fn(args)
        else:
            parser.print_help()
        return
    else:
        fn = dispatch.get(args.command)

    if fn:
        try:
            fn(args)
        except SystemExit:
            raise  # let argparse exits through
        except KeyboardInterrupt:
            sys.exit(130)
        except sqlite3.OperationalError as e:
            err_msg = str(e)
            if "no such table" in err_msg:
                json_out({"error": f"Database table missing: {err_msg}",
                          "hint": "Run 'brainctl init' to create a fresh database, or 'brainctl init --force' to reset."})
            elif "database is locked" in err_msg:
                json_out({"error": "Database is locked. Another process may be writing.",
                          "hint": "Wait a moment and retry, or check for hung processes."})
            else:
                json_out({"error": f"Database error: {err_msg}"})
            sys.exit(1)
        except sqlite3.IntegrityError as e:
            json_out({"error": f"Integrity constraint: {e}",
                      "hint": "A required record may be missing. Check agent exists or use --force."})
            sys.exit(1)
        except (TypeError, ValueError) as e:
            json_out({"error": f"Invalid input: {e}"})
            sys.exit(1)
        except FileNotFoundError as e:
            json_out({"error": f"File not found: {e}",
                      "hint": "Run 'brainctl init' to create the database."})
            sys.exit(1)
        except Exception as e:
            json_out({"error": f"{type(e).__name__}: {e}"})
            sys.exit(1)
    else:
        parser.print_help()
