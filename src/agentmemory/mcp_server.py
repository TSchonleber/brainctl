#!/usr/bin/env python3
"""
brainctl-mcp — MCP server wrapping brain.db operations

Exposes brainctl commands as MCP tools over stdio transport.
Same database, same logic, structured JSON protocol.

Usage:
  brainctl-mcp                  # stdio transport (for Claude Desktop, VS Code, etc.)
  brainctl-mcp --list-tools     # print available tools and exit
  brainctl-mcp --doctor         # diagnose installation and configuration
  brainctl-mcp --doctor --json  # also output JSON results
"""

import json
import logging
import os
import re
import sqlite3
import struct
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentmemory.paths import get_db_path
logger = logging.getLogger(__name__)
from mcp.server import Server

# Extension tool modules — each exports TOOLS: list[Tool] and DISPATCH: dict
try:
    from agentmemory import (
        mcp_tools_agents,
        mcp_tools_analytics,
        mcp_tools_belief_merge,
        mcp_tools_beliefs,
        mcp_tools_consolidation,
        mcp_tools_expertise,
        mcp_tools_federation,
        mcp_tools_health,
        mcp_tools_knowledge,
        mcp_tools_lifecycle,
        mcp_tools_meb,
        mcp_tools_merge,
        mcp_tools_neuro,
        mcp_tools_policy,
        mcp_tools_reasoning,
        mcp_tools_reconcile,
        mcp_tools_reflexion,
        mcp_tools_scheduler,
        mcp_tools_telemetry,
        mcp_tools_temporal,
        mcp_tools_tom,
        mcp_tools_trust,
        mcp_tools_usage,
        mcp_tools_workspace,
        mcp_tools_world,
    )
    _EXT_MODULES = [
        mcp_tools_agents,
        mcp_tools_analytics,
        mcp_tools_belief_merge,
        mcp_tools_beliefs,
        mcp_tools_consolidation,
        mcp_tools_expertise,
        mcp_tools_federation,
        mcp_tools_health,
        mcp_tools_knowledge,
        mcp_tools_lifecycle,
        mcp_tools_meb,
        mcp_tools_merge,
        mcp_tools_neuro,
        mcp_tools_policy,
        mcp_tools_reasoning,
        mcp_tools_reconcile,
        mcp_tools_reflexion,
        mcp_tools_scheduler,
        mcp_tools_telemetry,
        mcp_tools_temporal,
        mcp_tools_tom,
        mcp_tools_trust,
        mcp_tools_usage,
        mcp_tools_workspace,
        mcp_tools_world,
    ]
except ImportError as _e:
    logger.warning("Some extension tool modules failed to import: %s", _e)
    _EXT_MODULES = []
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Query intent classification — route queries to appropriate tables
try:
    sys.path.insert(0, str(Path.home() / "agentmemory" / "bin"))
    from intent_classifier import classify_intent as _classify_intent
    _INTENT_AVAILABLE = True
except Exception:
    _INTENT_AVAILABLE = False

# Built-in lightweight intent classifier fallback
class _BuiltinIntentResult:
    __slots__ = ("intent", "confidence", "matched_rule", "format_hint", "tables")
    def __init__(self, intent, confidence, matched_rule, format_hint, tables):
        self.intent = intent
        self.confidence = confidence
        self.matched_rule = matched_rule
        self.format_hint = format_hint
        self.tables = tables

def _builtin_classify_intent(query):
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

# Quantum amplitude scorer (optional re-ranking)
try:
    sys.path.insert(0, str(Path.home() / "bin" / "lib"))
    from quantum_retrieval import quantum_rerank as _quantum_rerank
    _QUANTUM_AVAILABLE = True
except Exception:
    _QUANTUM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants (same as brainctl)
# ---------------------------------------------------------------------------

DB_PATH = get_db_path()

def _find_vec_dylib():
    """Auto-discover the sqlite-vec loadable extension path."""
    try:
        import sqlite_vec
        return sqlite_vec.loadable_path()
    except (ImportError, AttributeError):
        pass
    import glob
    for pattern in ['/opt/homebrew/lib/python*/site-packages/sqlite_vec/vec0.*',
                    '/usr/lib/python*/site-packages/sqlite_vec/vec0.*']:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None

VEC_DYLIB = _find_vec_dylib()
OLLAMA_EMBED_URL = os.environ.get("BRAINCTL_OLLAMA_URL", "http://localhost:11434/api/embed")
EMBED_MODEL = os.environ.get("BRAINCTL_EMBED_MODEL", "nomic-embed-text")
DIMENSIONS = int(os.environ.get("BRAINCTL_EMBED_DIMENSIONS", "768"))

VALID_MEMORY_CATEGORIES = [
    "convention", "decision", "environment", "identity",
    "integration", "lesson", "preference", "project", "user"
]

VALID_EVENT_TYPES = [
    "artifact", "decision", "error", "handoff", "memory_promoted",
    "memory_retired", "observation", "result", "session_end",
    "session_start", "stale_context", "task_update", "warning"
]

VALID_ENTITY_TYPES = [
    "agent", "concept", "document", "event", "location",
    "organization", "other", "person", "project", "service", "tool"
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_ts() -> str:
    return _utc_now_iso()


def get_db() -> sqlite3.Connection:
    global DB_PATH
    if os.environ.get("BRAIN_DB") or os.environ.get("BRAINCTL_HOME"):
        DB_PATH = get_db_path()
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_agent(conn, agent_id: str) -> None:
    if not agent_id:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at)
        VALUES (?, ?, 'mcp', 'active', ?, ?)
        """,
        (agent_id, agent_id, _now_ts(), _now_ts()),
    )


def log_access(conn, agent_id, action, target_table=None, target_id=None, query=None, result_count=None):
    conn.execute(
        "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count) VALUES (?,?,?,?,?,?)",
        (agent_id, action, target_table, target_id, query, result_count)
    )


def _embed_safe(text: str):
    try:
        payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(OLLAMA_EMBED_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
            return struct.pack(f"{len(vec)}f", *vec)
    except Exception:
        return None


def _get_vec_db():
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


def _require_owned_handoff(db, agent_id: str, handoff_id: int):
    return db.execute(
        "SELECT id, status, agent_id FROM handoff_packets WHERE id = ? AND agent_id = ?",
        (handoff_id, agent_id),
    ).fetchone()


def _safe_fts(query: str) -> str:
    """Sanitize query for FTS5."""
    safe = re.sub(r'[^\w\s]', ' ', query).strip()
    return " OR ".join(safe.split()) if safe else ""


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _compute_pii_mcp(db, memory_id: int) -> float:
    """Compute Proactive Interference Index for MCP write-path gate."""
    import math as _math
    _PII_TW = {"permanent": 1.00, "long": 0.80, "medium": 0.50, "short": 0.30, "ephemeral": 0.15}
    row = db.execute(
        "SELECT alpha, beta, recalled_count, temporal_class FROM memories "
        "WHERE id = ? AND retired_at IS NULL", (memory_id,)
    ).fetchone()
    if not row:
        return 0.0
    alpha = float(row["alpha"] or 1.0)
    beta  = float(row["beta"]  or 1.0)
    recalled = int(row["recalled_count"] or 0)
    max_row = db.execute("SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL").fetchone()
    max_recalled = int(max_row[0] or 1) or 1
    bayesian_strength = alpha / (alpha + beta)
    recall_weight = _math.log(1 + recalled) / _math.log(1 + max_recalled)
    temporal_weight = _PII_TW.get(row["temporal_class"] or "medium", 0.50)
    return min(1.0, max(0.0, bayesian_strength * recall_weight * temporal_weight))


def _surprise_score_mcp(db, content: str, blob=None):
    """Compute surprise score for a candidate memory (MCP path).

    Returns (surprise: float, method: str) where surprise in [0, 1].
    1.0 = maximally novel, 0.0 = exact duplicate.
    """
    # Method 1: Cosine similarity via embeddings
    if blob:
        try:
            db_vec = _get_vec_db()
            if db_vec:
                try:
                    rows = db_vec.execute(
                        "SELECT rowid FROM vec_memories WHERE embedding MATCH ? AND k=?",
                        (blob, 5)
                    ).fetchall()
                    if rows:
                        cand_n = len(blob) // 4
                        cand_vec = list(struct.unpack(f"{cand_n}f", blob[:cand_n * 4]))
                        max_sim = 0.0
                        for row in rows:
                            e = db_vec.execute(
                                "SELECT vector FROM embeddings WHERE source_table='memories' AND source_id=?",
                                (row[0] if isinstance(row, tuple) else row["rowid"],)
                            ).fetchone()
                            if e:
                                v_bytes = bytes(e[0] if isinstance(e, tuple) else e["vector"])
                                n2 = len(v_bytes) // 4
                                v2 = list(struct.unpack(f"{n2}f", v_bytes[:n2 * 4]))
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
            pass

    # Method 2: FTS5 word overlap
    try:
        words = set(content.lower().split())
        if not words:
            return 1.0, "empty"
        query_words = list(words)[:20]
        fts_query = _safe_fts(" ".join(query_words))
        if not fts_query:
            return 1.0, "fts5_no_query"
        rows = db.execute(
            "SELECT content FROM memories WHERE retired_at IS NULL AND content LIKE ? LIMIT 5",
            (f"%{' '.join(query_words[:5])}%",)
        ).fetchall()
        if not rows:
            return 1.0, "fts5_no_matches"
        max_overlap = 0.0
        for row in rows:
            existing_words = set(row["content"].lower().split())
            if not existing_words:
                continue
            intersection = words & existing_words
            union = words | existing_words
            overlap = len(intersection) / len(union) if union else 0.0
            max_overlap = max(max_overlap, overlap)
        if max_overlap > 0.9:
            surprise = 0.1 + (1.0 - max_overlap) * 2.0
        elif max_overlap < 0.1:
            surprise = 0.9 + (0.1 - max_overlap)
        else:
            surprise = 1.0 - max_overlap
        return round(max(0.0, min(1.0, surprise)), 4), "fts5"
    except Exception:
        return 0.7, "fts5_error"


# Source trust weights for the W(m) gate multiplier (issue #24).
# Lower-trust sources must carry higher surprise to pass the same worthiness threshold.
_SOURCE_TRUST_WEIGHTS: dict[str, float] = {
    "human_verified": 1.0,
    "mcp_tool":       0.85,
    "llm_inference":  0.7,
    "external_doc":   0.5,
}
_VALID_SOURCES = tuple(_SOURCE_TRUST_WEIGHTS)


def tool_memory_add(agent_id: str, content: str, category: str, scope: str = "global",
                    confidence: float = 1.0, tags: str = None, memory_type: str = "episodic",
                    force: bool = False, supersedes_id: int = None,
                    source: str = "mcp_tool") -> dict:
    """Add a memory with W(m) worthiness gate and PII recency gate.

    Args:
        source: Origin of the memory content. Controls trust_score at write time.
                One of: 'human_verified', 'mcp_tool', 'llm_inference', 'external_doc'.
                Lower-trust sources face a higher effective worthiness bar.
    """
    if category not in VALID_MEMORY_CATEGORIES:
        return {"ok": False, "error": f"Invalid category: {category}. Must be one of: {', '.join(VALID_MEMORY_CATEGORIES)}"}
    if not (0.0 <= confidence <= 1.0):
        return {"ok": False, "error": "confidence must be between 0.0 and 1.0"}
    if memory_type not in ("episodic", "semantic"):
        return {"ok": False, "error": "memory_type must be 'episodic' or 'semantic'"}
    if scope != "global" and not scope.startswith("project:") and not scope.startswith("agent:"):
        return {"ok": False, "error": "scope must be 'global', 'project:<name>', or 'agent:<id>'"}
    if source not in _SOURCE_TRUST_WEIGHTS:
        return {"ok": False, "error": f"Invalid source: {source}. Must be one of: {', '.join(_VALID_SOURCES)}"}

    # Resolve source trust weight — lower trust requires higher novelty to pass the gate.
    source_trust = _SOURCE_TRUST_WEIGHTS[source]

    db = get_db()
    ensure_agent(db, agent_id)
    tags_json = json.dumps(tags.split(",")) if tags else None

    # Surprise scoring — lightweight novelty check
    blob = None
    try:
        blob = _embed_safe(content)
    except Exception:
        pass
    try:
        surprise, surprise_method = _surprise_score_mcp(db, content, blob=blob)
    except Exception:
        surprise, surprise_method = 0.7, "error"

    # Arousal-precision coupling (Free Energy Principle: arousal = global precision gain)
    # Grounded in McGaugh 2004 emotional modulation of memory consolidation
    _arousal_gain = 1.0
    try:
        from agentmemory.affect import classify_affect, arousal_write_boost
        _affect = classify_affect(content)
        _arousal_gain = arousal_write_boost(_affect.get("arousal", 0.0))
    except Exception:
        pass

    # Valence-gated encoding (McGaugh 2004 + Hamann 2001):
    # Negative valence (threat/aversion) suppresses memory propagation.
    # Positive valence (reward/approach) facilitates propagation.
    # Read agent's most recent affect_log valence and scale W(m) accordingly.
    _valence_scale = 1.0
    try:
        _valence_row = db.execute(
            "SELECT valence FROM affect_log WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (agent_id,)
        ).fetchone()
        if _valence_row:
            _v = float(_valence_row["valence"] or 0.0)
            if _v < -0.5:
                _valence_scale = 0.7   # suppress: high-stress encoding is attenuated
            elif _v > 0.5:
                _valence_scale = 1.15  # propagate: positive state boosts encoding
    except Exception:
        pass

    # Lightweight W(m) pre-check: worthiness = surprise * importance * source_trust * (1 - redundancy) * arousal * valence
    importance_estimate = confidence
    _pre_redundancy = 0.5 if (surprise is not None and surprise < 0.2) else 0.0
    _pre_worthiness = (surprise or 0.7) * importance_estimate * source_trust * (1.0 - _pre_redundancy) * _arousal_gain * _valence_scale
    if _pre_worthiness < 0.3 and not force:
        try:
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
                "VALUES (?, 'observation', ?, ?, ?)",
                (agent_id,
                 f"Memory rejected by W(m) gate: {content[:60]}",
                 json.dumps({
                     "content_preview": content[:120],
                     "surprise": surprise,
                     "surprise_method": surprise_method,
                     "importance_estimate": round(importance_estimate, 4),
                     "valence_scale": round(_valence_scale, 4),
                     "pre_worthiness": round(_pre_worthiness, 4),
                     "source": source,
                     "source_trust": source_trust,
                     "category": category,
                     "scope": scope,
                 }),
                 _now_ts())
            )
            db.commit()
        except Exception:
            pass
        db.close()
        return {
            "ok": False,
            "rejected": True,
            "surprise_score": surprise,
            "surprise_method": surprise_method,
            "pre_worthiness": round(_pre_worthiness, 4),
            "source": source,
            "source_trust": source_trust,
            "reason": "Low surprise/worthiness — memory is too similar to existing content.",
            "hint": "Pass force=true to bypass the gate.",
        }

    # W(m) worthiness gate — deeper semantic check using write_decision.py
    worthiness_score = None
    worthiness_reason = ""
    worthiness_components = {}
    try:
        if blob and not force:
            import importlib.util as _ilu
            _wdpath = str(Path(__file__).parent / "lib" / "write_decision.py")
            _spec = _ilu.spec_from_file_location("write_decision", _wdpath)
            _wd = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_wd)

            vdb_gate = _get_vec_db()
            if vdb_gate:
                try:
                    worthiness_score, worthiness_reason, worthiness_components = _wd.gate_write(
                        candidate_blob=blob,
                        confidence=confidence,
                        temporal_class=None,
                        category=category,
                        scope=scope,
                        db_vec=vdb_gate,
                        force=False,
                        arousal_gain=_arousal_gain,
                    )
                finally:
                    vdb_gate.close()
    except Exception as exc:
        logger.debug("W(m) gate failed (non-fatal): %s", exc)

    if worthiness_reason and not force:
        # Log rejection event
        try:
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
                "VALUES (?, 'write_rejected', ?, ?, ?)",
                (agent_id,
                 f"W(m) gate rejected: {worthiness_reason} (score={worthiness_score})",
                 json.dumps({
                     "content_preview": content[:120],
                     "category": category,
                     "scope": scope,
                     "score": worthiness_score,
                     "reason": worthiness_reason,
                     "source": source,
                     "source_trust": source_trust,
                 }),
                 _now_ts())
            )
            db.commit()
        except Exception:
            pass
        db.close()
        return {
            "ok": False,
            "rejected": True,
            "worthiness_score": worthiness_score,
            "rejection_reason": worthiness_reason,
            "components": worthiness_components,
            "hint": "Pass force=true to bypass the gate.",
        }

    # PII recency gate — transparent write-path check
    import math as _math
    alpha_floor = 1
    pii_gate_info = {}
    if supersedes_id:
        incumbent_pii = _compute_pii_mcp(db, supersedes_id)
        _PII_TIERS_MCP = [(0.70, "CRYSTALLIZED"), (0.40, "ENTRENCHED"), (0.20, "ESTABLISHED"), (0.00, "OPEN")]
        tier = next((lbl for thr, lbl in _PII_TIERS_MCP if incumbent_pii >= thr), "OPEN")
        alpha_floor = 1 + _math.ceil(max(0.0, incumbent_pii - 0.20) * 0.5 * 5)
        pii_gate_info = {
            "supersedes_id": supersedes_id,
            "incumbent_pii": round(incumbent_pii, 4),
            "incumbent_tier": tier,
            "alpha_floor": alpha_floor,
        }

    created_at = _now_ts()
    cur = db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, tags, memory_type, "
        "supersedes_id, alpha, trust_score, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, category, scope, content, confidence, tags_json, memory_type,
         supersedes_id, float(alpha_floor), source_trust, created_at, created_at)
    )
    mid = cur.lastrowid

    # Record gated_from_memory_id if column exists (migration 025)
    if supersedes_id:
        try:
            db.execute("UPDATE memories SET gated_from_memory_id = ? WHERE id = ?", (supersedes_id, mid))
        except Exception:
            pass

    log_access(db, agent_id, "write", "memories", mid)
    # Embed on write — reuse blob computed above for the gate
    embedded = False
    try:
        if not blob:
            blob = _embed_safe(content)
        if blob:
            vdb = _get_vec_db()
            if vdb:
                vdb.execute("INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?,?)", (mid, blob))
                vdb.execute(
                    "INSERT OR IGNORE INTO embeddings (source_table, source_id, model, dimensions, vector) VALUES (?,?,?,?,?)",
                    ("memories", mid, EMBED_MODEL, 768, blob)
                )
                vdb.commit(); vdb.close()
                embedded = True
    except Exception:
        pass
    db.commit(); db.close()
    result = {"ok": True, "memory_id": mid, "embedded": embedded, "worthiness_score": worthiness_score,
              "surprise_score": surprise, "surprise_method": surprise_method,
              "source": source, "trust_score": source_trust}
    if _valence_scale != 1.0:
        result["valence_scale"] = round(_valence_scale, 4)
    if pii_gate_info:
        result["pii_gate"] = pii_gate_info
    return result


_SEMANTIC_CONFIDENCE_BONUS = 1.1  # CLS: semantic memories get a mild ranking boost when no type filter is applied


def tool_memory_search(agent_id: str, query: str, category: str = None,
                       scope: str = None, limit: int = 20,
                       memory_type: str = None,
                       pagerank_boost: float = 0.0,
                       borrow_from: str = None) -> dict:
    if memory_type and memory_type not in ("episodic", "semantic"):
        return {"ok": False, "error": "memory_type must be 'episodic' or 'semantic'"}
    db = get_db()
    fts_q = _safe_fts(query)
    if not fts_q:
        return {"ok": False, "error": "Empty query"}

    # Theta-gamma slot cap — enforce 7*tier max slots per retrieval cycle.
    # Tier 1 (default) → 7 slots, tier 2 → 14, tier 3 → 21.
    # Mirrors the theta-nested gamma coupling constraint (Lisman & Jensen 2013).
    tier_row = db.execute(
        "SELECT attention_budget_tier FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    tier = (tier_row[0] if tier_row and tier_row[0] else 1)
    max_slots = 7 * tier
    limit = min(limit, max_slots)

    conditions = ["m.retired_at IS NULL"]
    params = [fts_q]
    if borrow_from:
        # Cross-agent borrow: restrict to the other agent's globally-scoped memories
        conditions.append("m.agent_id = ?")
        params.append(borrow_from)
        conditions.append("m.scope = 'global'")
    if category:
        conditions.append("m.category = ?")
        params.append(category)
    if scope:
        conditions.append("m.scope = ?")
        params.append(scope)
    if memory_type:
        # CLS: explicit type filter — caller wants only episodic or only semantic
        conditions.append("m.memory_type = ?")
        params.append(memory_type)
    params.append(limit)
    where = " AND ".join(conditions)

    rows = db.execute(
        f"SELECT m.* FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
        f"WHERE memories_fts MATCH ? AND {where} ORDER BY rank LIMIT ?", params
    ).fetchall()
    results = rows_to_list(rows)

    # CLS semantic bonus: when no type filter is set, apply a mild confidence
    # multiplier to semantic memories so they score slightly above equivalent
    # episodic memories. Semantic memories are higher-quality consolidated
    # representations and should rank ahead when confidence is otherwise equal.
    if not memory_type:
        for r in results:
            if r.get("memory_type") == "semantic":
                r["confidence"] = min(1.0, (r.get("confidence") or 1.0) * _SEMANTIC_CONFIDENCE_BONUS)

    # Quantum amplitude re-ranking — transparent to callers
    if _QUANTUM_AVAILABLE and results:
        try:
            results = _quantum_rerank(results, db_path=str(DB_PATH))
        except Exception:
            pass

    # SR/PageRank re-ranking — boost results by cached graph centrality score.
    # pagerank_boost=0 (default) leaves ranking unchanged.
    # pagerank_boost=1.0 weights FTS rank and PageRank equally.
    # Implements Millidge 2025: Personalized PageRank == Successor Representation.
    if pagerank_boost > 0.0 and results:
        pr_keys = [f"pagerank_memories_{r['id']}" for r in results]
        key_placeholders = ",".join("?" * len(pr_keys))
        pr_rows = db.execute(
            f"SELECT key, value FROM agent_state WHERE key IN ({key_placeholders})",
            pr_keys,
        ).fetchall()
        pr_scores = {}
        for row in pr_rows:
            try:
                pr_scores[row["key"]] = json.loads(row["value"]).get("score", 0.0)
            except Exception:
                pass
        for i, r in enumerate(results):
            pr = pr_scores.get(f"pagerank_memories_{r['id']}", 0.0)
            # Combine FTS rank position (inverted) with PageRank score
            fts_rank = 1.0 - (i / max(len(results), 1))
            r["_sr_score"] = fts_rank + pagerank_boost * pr
        results.sort(key=lambda r: -r.get("_sr_score", 0.0))
        for r in results:
            r.pop("_sr_score", None)

    if borrow_from:
        log_access(db, agent_id, "borrow", "memories", query=f"{query} [from:{borrow_from}]", result_count=len(results))
    else:
        log_access(db, agent_id, "search", "memories", query=query, result_count=len(results))

    # Ebbinghaus retrieval strengthening — each recalled memory gets a confidence
    # boost via apply_recall_boost (diminishing-returns Bayesian formula).
    # 60-second cooldown prevents runaway inflation on rapid repeated searches.
    if results:
        try:
            from agentmemory.hippocampus import apply_recall_boost as _recall_boost
            from datetime import timedelta as _td
            _sixty_secs_ago = (datetime.now() - _td(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
            for r in results:
                mid = r.get("id")
                if not mid:
                    continue
                last_recalled = r.get("last_recalled_at")
                if last_recalled and last_recalled > _sixty_secs_ago:
                    continue  # cooldown: boosted within last 60s
                _recall_boost(db, mid)
        except Exception:
            pass

    db.commit(); db.close()
    result = {"ok": True, "count": len(results), "memories": results,
              "slot_cap": max_slots, "tier": tier}
    if borrow_from:
        result["borrowed_from"] = borrow_from
    return result


_LABILE_RESCUE_THRESHOLD = 0.8   # importance >= this triggers retroactive labile tagging
_LABILE_RESCUE_WINDOW_HOURS = 2  # look back this many hours for memories to rescue
_LABILE_DURATION_HOURS = 2       # labile window extends this far past event write time


def tool_event_add(agent_id: str, summary: str, event_type: str, detail: str = None,
                   project: str = None, importance: float = 0.5) -> dict:
    if event_type not in VALID_EVENT_TYPES:
        return {"ok": False, "error": f"Invalid event_type: {event_type}. Must be one of: {', '.join(VALID_EVENT_TYPES)}"}
    if not (0.0 <= importance <= 1.0):
        return {"ok": False, "error": "importance must be between 0.0 and 1.0"}
    db = get_db()
    ensure_agent(db, agent_id)
    now_ts = _now_ts()
    cur = db.execute(
        "INSERT INTO events (agent_id, event_type, summary, detail, project, importance, created_at) VALUES (?,?,?,?,?,?,?)",
        (agent_id, event_type, summary, detail, project, importance, now_ts)
    )
    eid = cur.lastrowid
    log_access(db, agent_id, "write", "events", eid)

    labile_rescued = 0
    if importance >= _LABILE_RESCUE_THRESHOLD:
        # Behavioral tagging retroactive rescue (Redondo & Morris 2011):
        # A high-salience event retroactively stabilizes memories written
        # in the preceding window by extending their decay immunity.
        from datetime import timedelta
        try:
            now_dt = datetime.fromisoformat(now_ts)
        except Exception:
            now_dt = datetime.utcnow()
        window_start = (now_dt - timedelta(hours=_LABILE_RESCUE_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
        labile_until = (now_dt + timedelta(hours=_LABILE_DURATION_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
        rescued = db.execute(
            """
            UPDATE memories
            SET labile_until = ?, labile_agent_id = ?
            WHERE agent_id = ?
              AND created_at >= ?
              AND created_at <= ?
              AND retired_at IS NULL
              AND (labile_until IS NULL OR labile_until < ?)
            """,
            (labile_until, agent_id, agent_id, window_start, now_ts, labile_until),
        )
        labile_rescued = rescued.rowcount

    db.commit(); db.close()
    result = {"ok": True, "event_id": eid, "created_at": now_ts}
    if labile_rescued:
        result["labile_rescued"] = labile_rescued

    # Proactive surfacing — auto-check triggers against the new event summary.
    # Agents don't need to call trigger_check manually; any trigger that matches
    # the event content fires here and is returned in the response payload.
    try:
        trigger_result = tool_trigger_check(agent_id, summary)
        if trigger_result.get("ok") and trigger_result.get("count", 0) > 0:
            result["triggered"] = trigger_result["triggers"]
    except Exception:
        pass

    return result


def tool_event_search(agent_id: str, query: str = None, event_type: str = None,
                      project: str = None, limit: int = 20) -> dict:
    db = get_db()
    if query:
        fts_q = _safe_fts(query)
        if not fts_q:
            return {"ok": False, "error": "Empty query"}
        rows = db.execute(
            "SELECT e.* FROM events_fts fts JOIN events e ON e.id = fts.rowid "
            "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?", (fts_q, limit)
        ).fetchall()
    elif event_type:
        rows = db.execute(
            "SELECT * FROM events WHERE event_type = ? ORDER BY created_at DESC LIMIT ?",
            (event_type, limit)
        ).fetchall()
    elif project:
        rows = db.execute(
            "SELECT * FROM events WHERE project = ? ORDER BY created_at DESC LIMIT ?",
            (project, limit)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    results = rows_to_list(rows)
    log_access(db, agent_id, "search", "events", query=query, result_count=len(results))
    db.commit(); db.close()
    return {"ok": True, "count": len(results), "events": results}


def tool_entity_create(agent_id: str, name: str, entity_type: str, properties: str = None,
                       observations: str = None, scope: str = "global") -> dict:
    if entity_type not in VALID_ENTITY_TYPES:
        return {"ok": False, "error": f"Invalid entity_type: {entity_type}. Must be one of: {', '.join(VALID_ENTITY_TYPES)}"}
    db = get_db()
    ensure_agent(db, agent_id)
    props_json = "{}"
    if properties:
        try:
            props_json = json.dumps(json.loads(properties))
        except json.JSONDecodeError:
            return {"ok": False, "error": "properties must be valid JSON"}

    obs_json = "[]"
    if observations:
        obs_list = [o.strip() for o in observations.split(";") if o.strip()]
        obs_json = json.dumps(obs_list)

    existing = db.execute(
        "SELECT id FROM entities WHERE name = ? AND scope = ? AND retired_at IS NULL", (name, scope)
    ).fetchone()
    if existing:
        return {"ok": False, "error": f"Entity '{name}' already exists (id={existing['id']})"}

    cur = db.execute(
        "INSERT INTO entities (name, entity_type, properties, observations, agent_id, scope) VALUES (?,?,?,?,?,?)",
        (name, entity_type, props_json, obs_json, agent_id, scope)
    )
    eid = cur.lastrowid
    log_access(db, agent_id, "write", "entities", eid)
    # Embed
    embedded = False
    try:
        obs = json.loads(obs_json)
        blob = _embed_safe(f"{name} ({entity_type}): {' '.join(obs)}")
        if blob:
            vdb = _get_vec_db()
            if vdb:
                vdb.execute("INSERT OR REPLACE INTO vec_entities(rowid, embedding) VALUES (?,?)", (eid, blob))
                vdb.commit(); vdb.close()
                embedded = True
    except Exception:
        pass
    db.commit(); db.close()
    return {"ok": True, "entity_id": eid, "name": name, "embedded": embedded}


def tool_entity_get(agent_id: str, identifier: str) -> dict:
    db = get_db()
    if identifier.isdigit():
        row = db.execute("SELECT * FROM entities WHERE id = ? AND retired_at IS NULL", (int(identifier),)).fetchone()
    else:
        row = db.execute("SELECT * FROM entities WHERE name = ? AND retired_at IS NULL", (identifier,)).fetchone()
    if not row:
        return {"ok": False, "error": f"Entity not found: {identifier}"}

    entity = dict(row)
    entity["properties"] = json.loads(entity["properties"])
    entity["observations"] = json.loads(entity["observations"])

    edges = db.execute(
        "SELECT * FROM knowledge_edges WHERE (source_table='entities' AND source_id=?) OR (target_table='entities' AND target_id=?)",
        (entity["id"], entity["id"])
    ).fetchall()
    relations = []
    for e in edges:
        e = dict(e)
        if e["source_table"] == "entities" and e["source_id"] == entity["id"]:
            other = db.execute("SELECT name FROM entities WHERE id=?", (e["target_id"],)).fetchone() if e["target_table"] == "entities" else None
            relations.append({"direction": "outgoing", "relation": e["relation_type"], "target_id": e["target_id"], "target_name": other["name"] if other else None})
        else:
            other = db.execute("SELECT name FROM entities WHERE id=?", (e["source_id"],)).fetchone() if e["source_table"] == "entities" else None
            relations.append({"direction": "incoming", "relation": e["relation_type"], "source_id": e["source_id"], "source_name": other["name"] if other else None})
    entity["relations"] = relations
    log_access(db, agent_id, "read", "entities", entity["id"])
    db.commit(); db.close()
    return entity


def tool_entity_search(agent_id: str, query: str, entity_type: str = None, limit: int = 20) -> dict:
    db = get_db()
    fts_q = _safe_fts(query)
    if not fts_q:
        return {"ok": False, "error": "Empty query"}
    if entity_type:
        rows = db.execute(
            "SELECT e.* FROM entities_fts fts JOIN entities e ON e.id=fts.rowid WHERE entities_fts MATCH ? AND e.entity_type=? AND e.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_q, entity_type, limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT e.* FROM entities_fts fts JOIN entities e ON e.id=fts.rowid WHERE entities_fts MATCH ? AND e.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_q, limit)
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["properties"] = json.loads(d["properties"])
        d["observations"] = json.loads(d["observations"])
        results.append(d)
    log_access(db, agent_id, "search", "entities", query=query, result_count=len(results))
    db.commit(); db.close()
    return {"ok": True, "count": len(results), "entities": results}


def tool_entity_observe(agent_id: str, identifier: str, observations: str) -> dict:
    db = get_db()
    if identifier.isdigit():
        row = db.execute("SELECT * FROM entities WHERE id=? AND retired_at IS NULL", (int(identifier),)).fetchone()
    else:
        row = db.execute("SELECT * FROM entities WHERE name=? AND retired_at IS NULL", (identifier,)).fetchone()
    if not row:
        return {"ok": False, "error": f"Entity not found: {identifier}"}
    eid = row["id"]
    current = json.loads(row["observations"])
    new_obs = [o.strip() for o in observations.split(";") if o.strip()]
    added = [o for o in new_obs if o not in current]
    current.extend(added)
    db.execute("UPDATE entities SET observations=?, updated_at=? WHERE id=?", (json.dumps(current), _now_ts(), eid))
    log_access(db, agent_id, "write", "entities", eid)
    db.commit(); db.close()
    return {"ok": True, "entity_id": eid, "added": added, "total_observations": len(current)}


def tool_trigger_create(agent_id: str, condition: str, keywords: str, action: str,
                        entity: str = None, memory_id: int = None,
                        priority: str = "medium", expires: str = None) -> dict:
    db = get_db()
    ensure_agent(db, agent_id)
    entity_id = None
    if entity:
        if entity.isdigit():
            r = db.execute("SELECT id FROM entities WHERE id=? AND retired_at IS NULL", (int(entity),)).fetchone()
        else:
            r = db.execute("SELECT id FROM entities WHERE name=? AND retired_at IS NULL", (entity,)).fetchone()
        if not r:
            return {"ok": False, "error": f"Entity not found: {entity}"}
        entity_id = r["id"]
    cur = db.execute(
        "INSERT INTO memory_triggers (agent_id, trigger_condition, trigger_keywords, action, entity_id, memory_id, priority, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, condition, keywords, action, entity_id, memory_id, priority, expires)
    )
    tid = cur.lastrowid
    log_access(db, agent_id, "write", "memory_triggers", tid)
    db.commit(); db.close()
    return {"ok": True, "trigger_id": tid, "condition": condition, "keywords": keywords}


def tool_trigger_list(agent_id: str, status: str = None) -> dict:
    db = get_db()
    if status:
        rows = db.execute("SELECT * FROM memory_triggers WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM memory_triggers ORDER BY created_at DESC").fetchall()
    db.close()
    return {"ok": True, "triggers": [dict(r) for r in rows]}


def tool_trigger_check(agent_id: str, query: str) -> dict:
    db = get_db()
    # Expire overdue
    db.execute("UPDATE memory_triggers SET status='expired' WHERE status='active' AND expires_at IS NOT NULL AND expires_at < ?", (_now_ts(),))
    rows = db.execute("SELECT * FROM memory_triggers WHERE status='active'").fetchall()
    query_lower = query.lower()
    query_words = set(query_lower.split())
    matches = []
    for row in rows:
        kw_list = [k.strip().lower() for k in row["trigger_keywords"].split(",") if k.strip()]
        matched_kw = [kw for kw in kw_list if kw in query_lower or kw in query_words]
        if matched_kw:
            t = dict(row)
            t["matched_keywords"] = matched_kw
            matches.append(t)
    prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    matches.sort(key=lambda t: prio_order.get(t.get("priority", "medium"), 2))
    db.commit(); db.close()
    return {"ok": True, "query": query, "matched_triggers": matches, "count": len(matches)}


def tool_trigger_update(agent_id: str, trigger_id: int, condition: str = None,
                        keywords: str = None, action: str = None,
                        priority: str = None, status: str = None,
                        expires: str = None) -> dict:
    """Update fields on an existing trigger."""
    db = get_db()
    row = db.execute("SELECT * FROM memory_triggers WHERE id=?", (trigger_id,)).fetchone()
    if not row:
        db.close()
        return {"ok": False, "error": f"Trigger {trigger_id} not found"}
    updates = []
    params = []
    if condition is not None:
        updates.append("trigger_condition=?"); params.append(condition)
    if keywords is not None:
        updates.append("trigger_keywords=?"); params.append(keywords)
    if action is not None:
        updates.append("action=?"); params.append(action)
    if priority is not None:
        if priority not in ("low", "medium", "high", "critical"):
            db.close()
            return {"ok": False, "error": f"Invalid priority: {priority}"}
        updates.append("priority=?"); params.append(priority)
    if status is not None:
        if status not in ("active", "fired", "expired", "cancelled"):
            db.close()
            return {"ok": False, "error": f"Invalid status: {status}"}
        updates.append("status=?"); params.append(status)
    if expires is not None:
        updates.append("expires_at=?"); params.append(expires)
    if not updates:
        db.close()
        return {"ok": False, "error": "No fields to update"}
    params.append(trigger_id)
    db.execute(f"UPDATE memory_triggers SET {', '.join(updates)} WHERE id=?", params)
    log_access(db, agent_id, "write", "memory_triggers", trigger_id)
    db.commit(); db.close()
    return {"ok": True, "trigger_id": trigger_id, "updated_fields": [u.split("=")[0] for u in updates]}


def tool_trigger_delete(agent_id: str, trigger_id: int) -> dict:
    """Cancel a trigger."""
    db = get_db()
    row = db.execute("SELECT * FROM memory_triggers WHERE id=?", (trigger_id,)).fetchone()
    if not row:
        db.close()
        return {"ok": False, "error": f"Trigger {trigger_id} not found"}
    db.execute("UPDATE memory_triggers SET status='cancelled' WHERE id=?", (trigger_id,))
    log_access(db, agent_id, "write", "memory_triggers", trigger_id)
    db.commit(); db.close()
    return {"ok": True, "trigger_id": trigger_id, "status": "cancelled"}


def tool_entity_relate(agent_id: str, from_entity: str, relation: str, to_entity: str) -> dict:
    db = get_db()
    for name_val, label in [(from_entity, "from"), (to_entity, "to")]:
        if name_val.isdigit():
            r = db.execute("SELECT id FROM entities WHERE id=? AND retired_at IS NULL", (int(name_val),)).fetchone()
        else:
            r = db.execute("SELECT id FROM entities WHERE name=? AND retired_at IS NULL", (name_val,)).fetchone()
        if not r:
            return {"ok": False, "error": f"{label} entity not found: {name_val}"}
        if label == "from":
            from_id = r["id"]
        else:
            to_id = r["id"]
    try:
        db.execute(
            "INSERT INTO knowledge_edges (source_table, source_id, target_table, target_id, relation_type, weight, agent_id) VALUES ('entities',?,'entities',?,?,1.0,?)",
            (from_id, to_id, relation, agent_id)
        )
    except sqlite3.IntegrityError:
        return {"ok": False, "error": f"Relation '{relation}' already exists"}
    log_access(db, agent_id, "write", "knowledge_edges")
    db.commit(); db.close()
    return {"ok": True, "from_id": from_id, "to_id": to_id, "relation": relation}


def tool_decision_add(agent_id: str, title: str, rationale: str, project: str = None) -> dict:
    db = get_db()
    ensure_agent(db, agent_id)
    cur = db.execute(
        "INSERT INTO decisions (agent_id, title, rationale, project) VALUES (?,?,?,?)",
        (agent_id, title, rationale, project)
    )
    did = cur.lastrowid
    log_access(db, agent_id, "write", "decisions", did)
    db.commit(); db.close()
    return {"ok": True, "decision_id": did}


def tool_handoff_add(agent_id: str, goal: str, current_state: str, open_loops: str,
                     next_step: str, title: str = None, session_id: str = None,
                     chat_id: str = None, thread_id: str = None, user_id: str = None,
                     project: str = None, scope: str = "global", status: str = "pending",
                     recent_tail: str = None, decisions_json: str = None,
                     entities_json: str = None, tasks_json: str = None,
                     facts_json: str = None, source_event_id: int = None,
                     expires_at: str = None, **kw) -> dict:
    validated = _validate_handoff_fields(
        agent_id=agent_id, goal=goal, current_state=current_state, open_loops=open_loops,
        next_step=next_step, title=title, session_id=session_id, chat_id=chat_id,
        thread_id=thread_id, user_id=user_id, project=project, scope=scope, status=status,
        recent_tail=recent_tail, decisions_json=decisions_json, entities_json=entities_json,
        tasks_json=tasks_json, facts_json=facts_json, source_event_id=source_event_id,
        expires_at=expires_at,
    )
    db = get_db()
    ensure_agent(db, validated["agent_id"])
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
            validated["agent_id"], validated["session_id"], validated["chat_id"], validated["thread_id"], validated["user_id"], validated["project"], validated["scope"], validated["status"],
            validated["title"], validated["goal"], validated["current_state"], validated["open_loops"], validated["next_step"], validated["recent_tail"],
            validated["decisions_json"], validated["entities_json"], validated["tasks_json"], validated["facts_json"],
            validated["source_event_id"], validated["expires_at"], now, now,
        ),
    )
    handoff_id = cursor.lastrowid
    log_access(db, agent_id, "write", "handoff_packets", handoff_id)
    db.commit(); db.close()
    return {"ok": True, "handoff_id": handoff_id, "status": validated["status"]}


def tool_handoff_latest(agent_id: str, status: str = "pending", project: str = None,
                        chat_id: str = None, thread_id: str = None, user_id: str = None,
                        **kw) -> dict:
    validated = _validate_handoff_fields(
        agent_id=agent_id, project=project, chat_id=chat_id,
        thread_id=thread_id, user_id=user_id, status=status,
    )
    db = get_db()
    candidates = []
    if validated["chat_id"] and validated["thread_id"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE chat_id = ? AND thread_id = ? AND status = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (validated["chat_id"], validated["thread_id"], validated["status"], validated["agent_id"]),
        ))
    if validated["chat_id"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE chat_id = ? AND status = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (validated["chat_id"], validated["status"], validated["agent_id"]),
        ))
    if validated["project"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE project = ? AND status = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (validated["project"], validated["status"], validated["agent_id"]),
        ))
    if validated["user_id"]:
        candidates.append((
            "SELECT * FROM handoff_packets WHERE user_id = ? AND agent_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (validated["user_id"], validated["agent_id"], validated["status"]),
        ))
    candidates.append((
        "SELECT * FROM handoff_packets WHERE agent_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
        (validated["agent_id"], validated["status"]),
    ))
    row = None
    for sql, params in candidates:
        row = db.execute(sql, params).fetchone()
        if row:
            break
    db.close()
    return row_to_dict(row) or {}


def tool_handoff_consume(agent_id: str, handoff_id: int, **kw) -> dict:
    validated = _validate_handoff_fields(agent_id=agent_id)
    handoff_id = _optional_int(handoff_id, "handoff_id")
    db = get_db()
    row = _require_owned_handoff(db, validated["agent_id"], handoff_id)
    if not row:
        db.close()
        return {"ok": False, "error": f"handoff {handoff_id} not found for agent {validated['agent_id']}"}
    now = _now_ts()
    db.execute(
        "UPDATE handoff_packets SET status = 'consumed', consumed_at = ?, updated_at = ? WHERE id = ?",
        (now, now, handoff_id),
    )
    log_access(db, agent_id, "write", "handoff_packets", handoff_id)
    db.commit(); db.close()
    return {"ok": True, "handoff_id": handoff_id, "status": "consumed", "consumed_at": now}


def tool_handoff_pin(agent_id: str, handoff_id: int, **kw) -> dict:
    validated = _validate_handoff_fields(agent_id=agent_id)
    handoff_id = _optional_int(handoff_id, "handoff_id")
    db = get_db()
    row = _require_owned_handoff(db, validated["agent_id"], handoff_id)
    if not row:
        db.close()
        return {"ok": False, "error": f"handoff {handoff_id} not found for agent {validated['agent_id']}"}
    now = _now_ts()
    db.execute(
        "UPDATE handoff_packets SET status = 'pinned', expires_at = NULL, updated_at = ? WHERE id = ?",
        (now, handoff_id),
    )
    log_access(db, agent_id, "write", "handoff_packets", handoff_id)
    db.commit(); db.close()
    return {"ok": True, "handoff_id": handoff_id, "status": "pinned"}


def tool_handoff_expire(agent_id: str, handoff_id: int, **kw) -> dict:
    validated = _validate_handoff_fields(agent_id=agent_id)
    handoff_id = _optional_int(handoff_id, "handoff_id")
    db = get_db()
    row = _require_owned_handoff(db, validated["agent_id"], handoff_id)
    if not row:
        db.close()
        return {"ok": False, "error": f"handoff {handoff_id} not found for agent {validated['agent_id']}"}
    now = _now_ts()
    db.execute(
        "UPDATE handoff_packets SET status = 'expired', updated_at = ? WHERE id = ?",
        (now, handoff_id),
    )
    log_access(db, agent_id, "write", "handoff_packets", handoff_id)
    db.commit(); db.close()
    return {"ok": True, "handoff_id": handoff_id, "status": "expired"}


def tool_search(agent_id: str, query: str, limit: int = 20) -> dict:
    """Cross-table search: memories + events + entities. Intent-aware routing."""
    db = get_db()
    fts_q = _safe_fts(query)
    if not fts_q:
        return {"ok": False, "error": "Empty query"}

    # Classify intent and route to appropriate tables
    intent_meta = {}
    intent_tables = {"memories", "events", "entities"}  # default: all three
    ir = None
    if _INTENT_AVAILABLE:
        try:
            ir = _classify_intent(query)
        except Exception:
            ir = _builtin_classify_intent(query)
    else:
        ir = _builtin_classify_intent(query)

    if ir:
        intent_meta = {
            "intent": ir.intent,
            "intent_confidence": ir.confidence,
            "format_hint": ir.format_hint,
        }
        # Map intent tables to MCP table set (entities replaces context in MCP)
        _routed = set(ir.tables)
        intent_tables = set()
        if "memories" in _routed:
            intent_tables.add("memories")
        if "events" in _routed:
            intent_tables.add("events")
        # entity_lookup intent: include entities; also include for all intents by default
        if ir.intent == "entity_lookup" or "context" in _routed:
            intent_tables.add("entities")
        if not intent_tables:
            intent_tables = {"memories", "events", "entities"}

    results = []

    if "memories" in intent_tables:
        memories = rows_to_list(db.execute(
            "SELECT m.id, 'memory' as type, m.content as text, m.category, m.confidence, m.created_at "
            "FROM memories_fts fts JOIN memories m ON m.id=fts.rowid "
            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_q, limit)
        ).fetchall())
        # Quantum amplitude re-ranking — transparent to callers
        if _QUANTUM_AVAILABLE and memories:
            try:
                memories = _quantum_rerank(memories, db_path=str(DB_PATH))
            except Exception:
                pass
        results.extend(memories)

    if "events" in intent_tables:
        events = rows_to_list(db.execute(
            "SELECT e.id, 'event' as type, e.summary as text, e.event_type as category, e.importance as confidence, e.created_at "
            "FROM events_fts fts JOIN events e ON e.id=fts.rowid "
            "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_q, limit)
        ).fetchall())
        results.extend(events)

    if "entities" in intent_tables:
        entities = rows_to_list(db.execute(
            "SELECT e.id, 'entity' as type, e.name as text, e.entity_type as category, e.confidence, e.created_at "
            "FROM entities_fts fts JOIN entities e ON e.id=fts.rowid "
            "WHERE entities_fts MATCH ? AND e.retired_at IS NULL ORDER BY rank LIMIT ?",
            (fts_q, limit)
        ).fetchall())
        results.extend(entities)

    log_access(db, agent_id, "search", query=query, result_count=len(results))
    db.commit(); db.close()
    return {"ok": True, "count": len(results), "results": results, **intent_meta}


def tool_pagerank(table: str = None, damping: float = 0.85, iterations: int = 20,
                  top_k: int = 20, force: bool = False) -> dict:
    """Compute PageRank over knowledge_edges graph. Returns top-k nodes by score.

    Results are cached in agent_state for 24h. Uses iterative power method (no NetworkX).
    """
    from datetime import datetime as _dt
    db = get_db()

    # Check cache
    if not force:
        row = db.execute(
            "SELECT value, updated_at FROM agent_state WHERE agent_id='system' AND key='graph_pagerank'"
        ).fetchone()
        if row:
            try:
                age_hours = (_dt.now() - _dt.fromisoformat(row["updated_at"])).total_seconds() / 3600
                if age_hours < 24:
                    raw = json.loads(row["value"])
                    scores = {(parts[0], int(parts[1])): v
                              for x, v in raw.items()
                              for parts in [x.split("|", 1)]}
                    if table:
                        scores = {k: v for k, v in scores.items() if k[0] == table}
                    top = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
                    results = [{"table": t, "id": i, "pagerank": round(s, 6)} for (t, i), s in top]
                    db.close()
                    return {"ok": True, "cached": True, "node_count": len(scores), "top_k": results}
            except Exception:
                pass

    # Load edges
    rows = db.execute(
        "SELECT source_table, source_id, target_table, target_id, weight FROM knowledge_edges"
    ).fetchall()
    nodes = set()
    adj = {}  # node -> [(neighbor, weight)]
    for src_tbl, src_id, tgt_tbl, tgt_id, w in rows:
        u = (src_tbl, int(src_id))
        v = (tgt_tbl, int(tgt_id))
        nodes.add(u); nodes.add(v)
        adj.setdefault(u, []).append((v, w or 1.0))
        adj.setdefault(v, []).append((u, w or 1.0))

    if not nodes:
        db.close()
        return {"ok": True, "node_count": 0, "top_k": []}

    node_list = list(nodes)
    n = len(node_list)
    idx = {node: i for i, node in enumerate(node_list)}

    # Compute out-weights
    out_weight = [0.0] * n
    for node in node_list:
        i = idx[node]
        for _, w in adj.get(node, []):
            out_weight[i] += w

    # Power iteration PageRank
    pr = [1.0 / n] * n
    for _ in range(iterations):
        new_pr = [(1.0 - damping) / n] * n
        for node in node_list:
            i = idx[node]
            total_out = out_weight[i]
            if total_out == 0:
                continue
            contrib = damping * pr[i] / total_out
            for neighbor, w in adj.get(node, []):
                j = idx.get(neighbor)
                if j is not None:
                    new_pr[j] += contrib * w
        pr = new_pr

    all_scores = {node_list[i]: pr[i] for i in range(n)}

    # Cache in agent_state
    now = _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cached = {f"{tbl}|{nid}": v for (tbl, nid), v in all_scores.items()}
    db.execute(
        "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) "
        "VALUES ('system', 'graph_pagerank', ?, ?)",
        (json.dumps(cached), now)
    )
    # Also store per-node keys
    for (tbl, nid), score in all_scores.items():
        db.execute(
            "INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at) "
            "VALUES ('system', ?, ?, ?)",
            (f"pagerank_{tbl}_{nid}", json.dumps({"score": round(score, 8), "table": tbl, "id": nid}), now)
        )
    db.commit()

    # Filter and return top-k
    if table:
        filtered = {k: v for k, v in all_scores.items() if k[0] == table}
    else:
        filtered = all_scores
    top = sorted(filtered.items(), key=lambda x: -x[1])[:top_k]
    results = [{"table": t, "id": i, "pagerank": round(s, 6)} for (t, i), s in top]
    db.close()
    return {"ok": True, "cached": False, "node_count": len(filtered),
            "total_nodes": len(all_scores), "table_filter": table, "top_k": results}


def tool_stats() -> dict:
    db = get_db()
    stats = {}
    for tbl, col in [("agents", "id"), ("memories", "id"), ("events", "id"), ("entities", "id"),
                      ("decisions", "id"), ("context", "id"), ("knowledge_edges", "id")]:
        try:
            stats[tbl] = db.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        except Exception:
            stats[tbl] = 0
    stats["active_memories"] = db.execute("SELECT count(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
    stats["active_entities"] = db.execute("SELECT count(*) FROM entities WHERE retired_at IS NULL").fetchone()[0]
    db.close()
    return stats


def tool_belief_collapse(
    agent_id: str,
    action: str = "stats",
    belief_id: str | None = None,
    trigger_type: str | None = None,
    trigger_id: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> dict:
    """Belief Collapse Mechanics: measurement operators + collapse event logging."""
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path.home() / "agentmemory"))
    try:
        from collapse_mechanics import (
            force_collapse, list_collapse_events, collapse_stats,
            evaluate_coherence, is_superposed, check_and_collapse_on_time,
        )
    except ImportError as e:
        return {"error": f"collapse_mechanics import failed: {e}"}

    if action == "collapse":
        if not belief_id:
            return {"error": "belief_id required for collapse action"}
        if not trigger_type:
            return {"error": "trigger_type required for collapse action"}
        valid_triggers = ("task_checkout", "direct_query", "evidence_threshold", "time_decoherence")
        if trigger_type not in valid_triggers:
            return {"error": f"Unknown trigger_type: {trigger_type}. Use: {', '.join(valid_triggers)}"}
        collapsed = force_collapse(
            db_path=None,
            agent_id=agent_id,
            belief_id=belief_id,
            trigger_type=trigger_type,
            trigger_id=trigger_id or query,
        )
        return {"ok": True, "belief_id": belief_id, "collapsed_to": collapsed, "trigger": trigger_type}

    elif action == "log":
        events = list_collapse_events(belief_id=belief_id, limit=limit)
        return {"ok": True, "count": len(events), "events": events}

    elif action == "stats":
        return collapse_stats()

    elif action == "check":
        if not belief_id:
            return {"error": "belief_id required for check action"}
        coherence = evaluate_coherence(belief_id)
        superposed = is_superposed(belief_id)
        auto_collapsed = None
        if superposed and coherence < 0.1:
            auto_collapsed = check_and_collapse_on_time(belief_id, agent_id, threshold=0.1)
        return {
            "ok": True,
            "belief_id": belief_id,
            "coherence_score": coherence,
            "is_superposed": superposed,
            "auto_collapsed": auto_collapsed,
        }

    return {"error": f"Unknown action: {action}. Use: collapse, log, stats, check"}


def tool_access_log_annotate(
    agent_id: str,
    task_id: str,
    outcome: str,
) -> dict:
    """Annotate access_log rows for a completed task with its outcome."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path.home() / "bin" / "lib"))
    try:
        from outcome_eval import annotate_task_retrieval
    except ImportError as e:
        return {"error": f"outcome_eval import failed: {e}"}
    try:
        n = annotate_task_retrieval(task_id=task_id, agent_id=agent_id, outcome=outcome)
        return {"ok": True, "task_id": task_id, "outcome": outcome, "agent_id": agent_id, "rows_annotated": n}
    except ValueError as e:
        return {"error": str(e)}


def tool_resolve_conflict(
    agent_id: str,
    conflict_id: int | None = None,
    list_conflicts: bool = False,
    auto: bool = False,
    dry_run: bool = False,
    force_winner: str | None = None,
    threshold: float = 0.05,
) -> dict:
    """AGM credibility-weighted belief conflict resolution."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path.home() / "bin" / "lib"))
    try:
        from belief_revision import (
            resolve_conflict as _resolve,
            list_conflicts as _list_fn,
            auto_resolve as _auto,
        )
    except ImportError as e:
        return {"error": f"belief_revision import failed: {e}"}

    db_path = str(DB_PATH)

    if list_conflicts:
        conflicts = _list_fn(db_path)
        return {"ok": True, "open_conflicts": len(conflicts), "conflicts": conflicts}

    if auto:
        results = _auto(db_path=db_path, threshold=threshold, dry_run=dry_run)
        resolved  = [r for r in results if not r.get("escalated") and not r.get("error")]
        escalated = [r for r in results if r.get("escalated")]
        errors    = [r for r in results if r.get("error")]
        return {
            "ok": True,
            "total": len(results),
            "resolved": len(resolved),
            "escalated": len(escalated),
            "errors": len(errors),
            "dry_run": dry_run,
            "results": results,
        }

    if conflict_id is None:
        return {"error": "Provide conflict_id, list_conflicts=true, or auto=true"}

    return _resolve(
        conflict_id=conflict_id,
        db_path=db_path,
        dry_run=dry_run,
        force_winner_id=force_winner,
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="memory_add",
        description=(
            "Add a durable memory to brain.db. Passes through a W(m) worthiness gate "
            "that rejects low-novelty or redundant writes. Pass force=true to bypass. "
            "Use for facts, lessons, conventions, preferences that should persist. "
            "Set source to reflect the origin of the content — lower-trust sources face "
            "a higher worthiness bar and record a lower trust_score on the memory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory content"},
                "category": {"type": "string", "enum": VALID_MEMORY_CATEGORIES, "description": "Memory category"},
                "scope": {"type": "string", "description": "Scope: 'global', 'project:<name>', or 'agent:<id>'", "default": "global"},
                "confidence": {"type": "number", "description": "Confidence 0.0-1.0", "default": 1.0},
                "tags": {"type": "string", "description": "Comma-separated tags"},
                "memory_type": {"type": "string", "enum": ["episodic", "semantic"], "default": "episodic"},
                "force": {"type": "boolean", "description": "Bypass W(m) worthiness gate", "default": False},
                "supersedes_id": {"type": "integer", "description": "ID of memory being superseded; triggers PII recency gate"},
                "source": {
                    "type": "string",
                    "enum": list(_SOURCE_TRUST_WEIGHTS),
                    "default": "mcp_tool",
                    "description": (
                        "Origin of the memory content. Controls trust_score at write time. "
                        "'human_verified'=1.0, 'mcp_tool'=0.85, 'llm_inference'=0.7, 'external_doc'=0.5. "
                        "Lower-trust sources face a higher effective W(m) worthiness bar."
                    ),
                },
            },
            "required": ["content", "category"],
        },
    ),
    Tool(
        name="memory_search",
        description="Search memories in brain.db using full-text search. Returns matching memories ranked by relevance. Result count is capped at 7 × agent attention_budget_tier (theta-gamma coupling). Set memory_type to filter to one CLS store; unset applies a 1.1x semantic confidence bonus. Set pagerank_boost > 0 for SR-style retrieval (Millidge 2025: PageRank == Successor Representation).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "category": {"type": "string", "enum": VALID_MEMORY_CATEGORIES},
                "scope": {"type": "string"},
                "limit": {"type": "integer", "default": 20, "description": "Max results; capped by agent tier (7 × tier)"},
                "memory_type": {"type": "string", "enum": ["episodic", "semantic"], "description": "Filter to one CLS store. Unset = both stores, semantic gets 1.1x confidence bonus."},
                "pagerank_boost": {"type": "number", "default": 0.0, "description": "Re-rank by graph centrality (0=FTS-only, 1=equal FTS+PageRank). Requires prior pagerank run. Implements SR retrieval."},
                "borrow_from": {"type": "string", "description": "Agent ID to borrow from. When set, searches only that agent's scope='global' memories and logs the cross-agent access in access_log."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="event_add",
        description="Log an event to brain.db. Events are timestamped records of what happened. If importance >= 0.8, retroactively tags memories written in the prior 2 hours with a labile window (behavioral tagging rescue). Auto-checks all agent triggers against the event summary — any matching triggers are returned in the response as 'triggered' list.",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event summary"},
                "event_type": {"type": "string", "enum": VALID_EVENT_TYPES},
                "detail": {"type": "string", "description": "Longer description"},
                "project": {"type": "string"},
                "importance": {"type": "number", "default": 0.5, "description": "0.0-1.0"},
            },
            "required": ["summary", "event_type"],
        },
    ),
    Tool(
        name="event_search",
        description="Search events in brain.db. Can search by text, type, or project.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text search query"},
                "event_type": {"type": "string", "enum": VALID_EVENT_TYPES},
                "project": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="entity_create",
        description="Create a typed entity in the knowledge graph. Entities are people, projects, tools, concepts, etc. with properties and observations.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique entity name"},
                "entity_type": {"type": "string", "enum": VALID_ENTITY_TYPES},
                "properties": {"type": "string", "description": "JSON object of structured properties"},
                "observations": {"type": "string", "description": "Semicolon-separated atomic facts about the entity"},
                "scope": {"type": "string", "default": "global"},
            },
            "required": ["name", "entity_type"],
        },
    ),
    Tool(
        name="entity_get",
        description="Get an entity by name or ID, including all its relations to other entities.",
        inputSchema={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Entity name or numeric ID"},
            },
            "required": ["identifier"],
        },
    ),
    Tool(
        name="entity_search",
        description="Search entities by name, type, properties, or observations using full-text search.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "entity_type": {"type": "string", "enum": VALID_ENTITY_TYPES},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="entity_observe",
        description="Add new observations (atomic facts) to an existing entity.",
        inputSchema={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Entity name or numeric ID"},
                "observations": {"type": "string", "description": "Semicolon-separated new observations"},
            },
            "required": ["identifier", "observations"],
        },
    ),
    Tool(
        name="entity_relate",
        description="Create a directed relation between two entities. Use active voice for relation type (e.g. 'manages', 'founded', 'depends_on').",
        inputSchema={
            "type": "object",
            "properties": {
                "from_entity": {"type": "string", "description": "Source entity name or ID"},
                "relation": {"type": "string", "description": "Relation type in active voice"},
                "to_entity": {"type": "string", "description": "Target entity name or ID"},
            },
            "required": ["from_entity", "relation", "to_entity"],
        },
    ),
    Tool(
        name="trigger_create",
        description="Create a prospective memory trigger. When future queries match the keywords, the trigger's action will be surfaced automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "condition": {"type": "string", "description": "Natural language condition for when to fire"},
                "keywords": {"type": "string", "description": "Comma-separated keywords for matching"},
                "action": {"type": "string", "description": "What to surface/do when triggered"},
                "entity": {"type": "string", "description": "Optional linked entity name or ID"},
                "memory_id": {"type": "integer", "description": "Optional linked memory ID"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"], "default": "medium"},
                "expires": {"type": "string", "description": "Optional expiry datetime (ISO format)"},
            },
            "required": ["condition", "keywords", "action"],
        },
    ),
    Tool(
        name="trigger_list",
        description="List prospective memory triggers, optionally filtered by status.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "fired", "expired", "cancelled"]},
            },
        },
    ),
    Tool(
        name="trigger_check",
        description="Check if any active prospective memory triggers match a query. Returns matching triggers with their actions.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query text to match against trigger keywords"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="trigger_update",
        description="Update fields on an existing prospective memory trigger.",
        inputSchema={
            "type": "object",
            "properties": {
                "trigger_id": {"type": "integer", "description": "Trigger ID to update"},
                "condition": {"type": "string", "description": "New trigger condition"},
                "keywords": {"type": "string", "description": "New comma-separated keywords"},
                "action": {"type": "string", "description": "New action text"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "status": {"type": "string", "enum": ["active", "fired", "expired", "cancelled"]},
                "expires": {"type": "string", "description": "New expiry datetime (ISO format)"},
            },
            "required": ["trigger_id"],
        },
    ),
    Tool(
        name="trigger_delete",
        description="Cancel/delete a prospective memory trigger by ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "trigger_id": {"type": "integer", "description": "Trigger ID to cancel"},
            },
            "required": ["trigger_id"],
        },
    ),
    Tool(
        name="decision_add",
        description="Record a decision with its rationale. Useful for tracking why choices were made.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Decision title"},
                "rationale": {"type": "string", "description": "Why this decision was made"},
                "project": {"type": "string"},
            },
            "required": ["title", "rationale"],
        },
    ),
    Tool(
        name="handoff_add",
        description="Create a structured handoff packet for continuity across session resets.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "current_state": {"type": "string"},
                "open_loops": {"type": "string"},
                "next_step": {"type": "string"},
                "session_id": {"type": "string"},
                "chat_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "user_id": {"type": "string"},
                "project": {"type": "string"},
                "scope": {"type": "string", "default": "global"},
                "status": {"type": "string", "enum": ["pending", "consumed", "expired", "pinned"], "default": "pending"},
                "recent_tail": {"type": "string"},
                "decisions_json": {"type": "string"},
                "entities_json": {"type": "string"},
                "tasks_json": {"type": "string"},
                "facts_json": {"type": "string"},
                "source_event_id": {"type": "integer"},
                "expires_at": {"type": "string"}
            },
            "required": ["goal", "current_state", "open_loops", "next_step"],
        },
    ),
    Tool(
        name="handoff_latest",
        description="Fetch the latest matching handoff packet, preferring thread, then chat, then project, then user scope.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "consumed", "expired", "pinned"], "default": "pending"},
                "project": {"type": "string"},
                "chat_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "user_id": {"type": "string"}
            },
        },
    ),
    Tool(
        name="handoff_consume",
        description="Mark a handoff packet consumed after successful restore.",
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "integer", "description": "Handoff packet ID"}
            },
            "required": ["handoff_id"],
        },
    ),
    Tool(
        name="handoff_pin",
        description="Pin a handoff packet so it is preserved intentionally.",
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "integer", "description": "Handoff packet ID"}
            },
            "required": ["handoff_id"],
        },
    ),
    Tool(
        name="handoff_expire",
        description="Mark a handoff packet expired so it is no longer used for resume.",
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "integer", "description": "Handoff packet ID"}
            },
            "required": ["handoff_id"],
        },
    ),
    Tool(
        name="search",
        description="Cross-table search across memories, events, and entities simultaneously. Best for broad queries.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="pagerank",
        description=(
            "Compute PageRank centrality scores over the knowledge_edges graph. "
            "Returns top-k most central nodes. Results cached for 24h. "
            "Use --table filter to restrict to memories, entities, events, or context. "
            "Pass force=true to recompute even if cached."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "table": {"type": "string", "enum": ["memories", "entities", "events", "context"],
                           "description": "Filter results to a single table type"},
                "damping": {"type": "number", "default": 0.85, "description": "Damping factor (default: 0.85)"},
                "iterations": {"type": "integer", "default": 20, "description": "Power iterations (default: 20)"},
                "top_k": {"type": "integer", "default": 20, "description": "Top N results (default: 20)"},
                "force": {"type": "boolean", "default": False, "description": "Recompute even if cached"},
            },
        },
    ),
    Tool(
        name="stats",
        description="Get brain.db statistics: counts for memories, events, entities, decisions, agents, edges.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="resolve_conflict",
        description=(
            "AGM credibility-weighted belief conflict resolution. "
            "List open conflicts, resolve a single conflict by ID, or batch-resolve all auto-resolvable ones. "
            "Uses Bayesian mean × recency × trust × expertise to pick the winner. "
            "Retracts the losing memory and inserts a supersedes edge. "
            "Escalates to Hermes when scores are too close or a permanent memory is involved."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "conflict_id":     {"type": "integer", "description": "Conflict ID to resolve (omit for --list or --auto)"},
                "list_conflicts":  {"type": "boolean", "description": "List all open conflicts", "default": False},
                "auto":            {"type": "boolean", "description": "Batch-resolve all auto-resolvable conflicts", "default": False},
                "dry_run":         {"type": "boolean", "description": "Simulate without writing changes", "default": False},
                "force_winner":    {"type": "string",  "description": "Agent ID to force as winner"},
                "threshold":       {"type": "number",  "description": "Min score delta to auto-resolve (default: 0.05)", "default": 0.05},
            },
        },
    ),
    Tool(
        name="belief_collapse",
        description=(
            "Belief Collapse Mechanics: measurement operators + collapse event logging. "
            "Actions: 'collapse' (force-collapse a superposed belief), 'log' (list collapse events), "
            "'stats' (aggregate trigger statistics), 'check' (evaluate coherence + auto-collapse if decoherent). "
            "Trigger types: task_checkout | direct_query | evidence_threshold | time_decoherence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action":       {"type": "string", "enum": ["collapse", "log", "stats", "check"],
                                 "description": "collapse | log | stats | check", "default": "stats"},
                "belief_id":    {"type": "string", "description": "UUID of belief (agent_beliefs.id) or memory (memories.id)"},
                "trigger_type": {"type": "string",
                                 "enum": ["task_checkout", "direct_query", "evidence_threshold", "time_decoherence"],
                                 "description": "Collapse trigger type (required for action=collapse)"},
                "trigger_id":   {"type": "string", "description": "ID of triggering task/query/evidence"},
                "query":        {"type": "string", "description": "Query string (for direct_query trigger)"},
                "limit":        {"type": "integer", "description": "Max events for log action", "default": 20},
            },
            "required": ["action"],
        },
    ),
    Tool(
        name="access_log_annotate",
        description=(
            "Outcome-Linked Memory Evaluation: annotate all access_log rows "
            "from a completed task with its outcome. Call at task completion to close the "
            "feedback loop between memory retrieval and task success. "
            "Outcomes: success | blocked | escalated | cancelled."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID performing the annotation"},
                "task_id":  {"type": "string", "description": "Task identifier (e.g. issue ID or run ID)"},
                "outcome":  {"type": "string", "enum": ["success", "blocked", "escalated", "cancelled"],
                             "description": "Task outcome"},
            },
            "required": ["task_id", "outcome"],
        },
    ),
    # --- Affect tools ---
    Tool(
        name="affect_classify",
        description=(
            "Classify functional affect (emotion) from text using local lexical analysis. "
            "Zero LLM cost, ~1ms. Returns valence/arousal/dominance, emotion label, "
            "cluster, functional state, and safety flags. Does NOT log to database."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID (optional, for context)"},
                "text":     {"type": "string", "description": "Text to classify affect from"},
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="affect_log",
        description=(
            "Classify affect from text AND store the result in the affect_log table. "
            "Use this to track agent emotional state over time. Returns the classification "
            "plus the inserted row ID."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID"},
                "text":     {"type": "string", "description": "Text to classify and log"},
                "source":   {"type": "string", "description": "Source label (default: observation)",
                             "default": "observation"},
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="affect_check",
        description=(
            "Check current affect state for an agent. Returns the latest affect_log entry, "
            "recent affect velocity (rate of change), and any active safety flags. "
            "Use this to monitor agent wellbeing before critical tasks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to check (required)"},
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="affect_monitor",
        description=(
            "Fleet-wide affect scan across all agents. Returns mean VAD coordinates, "
            "cluster distribution, safety alerts, and overall fleet health status. "
            "Use for supervisory oversight of multi-agent systems."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID (ignored, fleet-wide scan)"},
            },
        },
    ),
]

# Merge extension module tools into the master list
for _m in _EXT_MODULES:
    TOOLS.extend(_m.TOOLS)

# ---------------------------------------------------------------------------
# Affect tools
# ---------------------------------------------------------------------------

def tool_affect_classify(agent_id="mcp-client", text="", **kw):
    from agentmemory.affect import classify_affect
    return classify_affect(text)

def tool_affect_log(agent_id="mcp-client", text="", source="observation", **kw):
    from agentmemory.affect import classify_affect
    result = classify_affect(text)
    db = get_db()
    metadata_json = json.dumps({
        "emotions": result.get("emotions", []),
        "safety_flags": result.get("safety_flags", []),
    })
    db.execute(
        "INSERT INTO affect_log (agent_id, valence, arousal, dominance, affect_label, "
        "cluster, functional_state, safety_flag, trigger, source, metadata, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, result["valence"], result["arousal"], result["dominance"],
         result["affect_label"], result["cluster"], result["functional_state"],
         result.get("safety_flag"), text, source, metadata_json, _now_ts())
    )
    db.commit()
    return result

def tool_affect_check(agent_id="mcp-client", **kw):
    from agentmemory.affect import SAFETY_PATTERNS, affect_velocity
    db = get_db()
    row = db.execute(
        "SELECT * FROM affect_log WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
        (agent_id,)
    ).fetchone()
    if not row:
        return {"agent": agent_id, "status": "no_data"}
    current = dict(row)
    history_rows = db.execute(
        "SELECT valence, arousal, dominance FROM affect_log WHERE agent_id=? ORDER BY created_at DESC LIMIT 10",
        (agent_id,)
    ).fetchall()
    history = list(reversed([dict(r) for r in history_rows]))
    velocity = affect_velocity(history)
    v, a, d = current["valence"], current["arousal"], current["dominance"]
    flags = []
    for name, pattern in SAFETY_PATTERNS.items():
        try:
            if pattern["conditions"](v, a, d):
                flags.append({"pattern": name, "severity": pattern["severity"], "description": pattern["description"]})
        except Exception:
            pass
    return {
        "agent": agent_id,
        "current": {"valence": v, "arousal": a, "dominance": d,
                     "affect_label": current["affect_label"], "cluster": current["cluster"],
                     "functional_state": current["functional_state"]},
        "velocity": velocity,
        "safety_flags": flags,
        "status": "critical" if any(f["severity"] == "critical" for f in flags)
                  else "warning" if flags else "healthy",
    }

def tool_affect_monitor(agent_id="mcp-client", **kw):
    from agentmemory.affect import SAFETY_PATTERNS, fleet_affect_summary
    db = get_db()
    rows = db.execute("""
        SELECT a.agent_id, a.valence, a.arousal, a.dominance,
               a.affect_label, a.cluster, a.functional_state, a.safety_flag
        FROM affect_log a INNER JOIN (
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
                    flags.append({"pattern": name, "severity": pattern["severity"], "description": pattern["description"]})
            except Exception:
                pass
        state["safety_flags"] = flags
        agent_states[state["agent_id"]] = state
    return fleet_affect_summary(agent_states)

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("brainctl")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # Inject agent_id from arguments or default
    agent_id = arguments.pop("agent_id", "mcp-client")

    dispatch = {
        "memory_add": tool_memory_add,
        "memory_search": tool_memory_search,
        "event_add": tool_event_add,
        "event_search": tool_event_search,
        "entity_create": tool_entity_create,
        "entity_get": tool_entity_get,
        "entity_search": tool_entity_search,
        "entity_observe": tool_entity_observe,
        "entity_relate": tool_entity_relate,
        "trigger_create": tool_trigger_create,
        "trigger_list": tool_trigger_list,
        "trigger_check": tool_trigger_check,
        "trigger_update": tool_trigger_update,
        "trigger_delete": tool_trigger_delete,
        "decision_add": tool_decision_add,
        "handoff_add": tool_handoff_add,
        "handoff_latest": tool_handoff_latest,
        "handoff_consume": tool_handoff_consume,
        "handoff_pin": tool_handoff_pin,
        "handoff_expire": tool_handoff_expire,
        "search": tool_search,
        "stats": tool_stats,
        "resolve_conflict": tool_resolve_conflict,
        "belief_collapse": tool_belief_collapse,
        "access_log_annotate": tool_access_log_annotate,
        "affect_classify": tool_affect_classify,
        "affect_log": tool_affect_log,
        "affect_check": tool_affect_check,
        "affect_monitor": tool_affect_monitor,
    }

    # Merge extension module dispatchers
    for _m in _EXT_MODULES:
        dispatch.update(_m.DISPATCH)

    fn = dispatch.get(name)
    if not fn:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    try:
        if name == "stats":
            result = fn()
        else:
            result = fn(agent_id=agent_id, **arguments)
    except Exception as e:
        result = {"error": str(e)}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


def _run_doctor() -> None:
    """Diagnose brainctl-mcp installation and configuration."""
    import sys as _sys

    checks = []
    all_ok = True

    def check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        nonlocal all_ok
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        checks.append({"check": name, "status": status, "detail": detail, "fix": fix})
        symbol = "✓" if ok else "✗"
        print(f"  {symbol} {name}: {detail}", file=_sys.stderr)
        if not ok and fix:
            print(f"    → {fix}", file=_sys.stderr)

    print("brainctl-mcp doctor\n", file=_sys.stderr)

    # 1. Python version
    v = _sys.version_info
    py_ok = v >= (3, 11)
    check("Python version", py_ok,
          f"{v.major}.{v.minor}.{v.micro}",
          "brainctl requires Python 3.11+. Upgrade your Python installation.")

    # 2. MCP package importable
    try:
        import mcp  # noqa: F401
        check("mcp package", True, "installed")
    except ImportError:
        check("mcp package", False, "not found",
              "pip install brainctl[mcp]")

    # 3. DB file exists
    db_exists = DB_PATH.exists()
    check("brain.db", db_exists,
          str(DB_PATH) + (" (exists)" if db_exists else " (missing)"),
          f"brainctl init --path {DB_PATH}  OR  set BRAIN_DB env var")

    # 4. DB readable and valid SQLite
    if db_exists:
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            conn.execute("SELECT count(*) FROM memories").fetchone()
            conn.close()
            check("DB readable", True, "memories table accessible")
        except Exception as exc:
            check("DB readable", False, str(exc),
                  "Database may be corrupted. Try: brainctl migrate  or  brainctl init --path <new-path>")

    # 5. BRAINCTL_HOME / BRAIN_DB env
    import os
    brain_db_env = os.environ.get("BRAIN_DB", "")
    brainctl_home_env = os.environ.get("BRAINCTL_HOME", "")
    if brain_db_env:
        check("BRAIN_DB env", True, brain_db_env)
    elif brainctl_home_env:
        check("BRAINCTL_HOME env", True, brainctl_home_env)
    else:
        check("DB path config", True, f"using default: {DB_PATH}")

    # 6. Ollama reachable (optional — only warn, not fail)
    ollama_ok = False
    try:
        import urllib.request
        req = urllib.request.Request(OLLAMA_EMBED_URL.replace("/api/embed", ""),
                                      method="GET")
        urllib.request.urlopen(req, timeout=2)
        ollama_ok = True
    except Exception:
        pass
    # Ollama is optional — only info-level
    symbol = "✓" if ollama_ok else "i"
    detail = "reachable" if ollama_ok else f"not reachable at {OLLAMA_EMBED_URL}"
    hint = "" if ollama_ok else "Vector search will be disabled. Start Ollama or set BRAINCTL_OLLAMA_URL."
    print(f"  {symbol} Ollama (optional): {detail}", file=_sys.stderr)
    if hint:
        print(f"    → {hint}", file=_sys.stderr)
    checks.append({"check": "Ollama (optional)", "status": "OK" if ollama_ok else "INFO",
                   "detail": detail, "fix": hint})

    # 7. sqlite-vec extension (optional)
    vec_ok = VEC_DYLIB is not None
    check_sym = "✓" if vec_ok else "i"
    print(f"  {check_sym} sqlite-vec (optional): {'found at ' + str(VEC_DYLIB) if vec_ok else 'not found'}",
          file=_sys.stderr)
    if not vec_ok:
        print("    → pip install brainctl[vec]  to enable vector search", file=_sys.stderr)
    checks.append({"check": "sqlite-vec (optional)", "status": "OK" if vec_ok else "INFO",
                   "detail": "found" if vec_ok else "not found",
                   "fix": "pip install brainctl[vec]"})

    print(file=_sys.stderr)
    if all_ok:
        print("All checks passed. brainctl-mcp is ready.", file=_sys.stderr)
    else:
        print("Some checks failed. Fix the issues above, then run brainctl-mcp.", file=_sys.stderr)

    # Output JSON for programmatic use if --json flag present
    if "--json" in _sys.argv:
        import json
        print(json.dumps({"ok": all_ok, "checks": checks}, indent=2))

    _sys.exit(0 if all_ok else 1)


async def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("\nFlags:")
        print("  --list-tools    Print all available tools and exit")
        print("  --doctor        Diagnose installation and configuration")
        print("  --doctor --json Also output JSON results")
        return

    if "--list-tools" in sys.argv:
        for t in TOOLS:
            print(f"  {t.name}: {t.description[:80]}")
        return

    if "--doctor" in sys.argv:
        _run_doctor()
        return

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run():
    """Synchronous entry point for pyproject.toml console_scripts."""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
