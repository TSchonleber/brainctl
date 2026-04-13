"""
brainctl — Python API for agent memory.

Quick start:
    from agentmemory import Brain

    brain = Brain()                               # uses $BRAIN_DB or ~/agentmemory/db/brain.db
    brain = Brain("/path/to/brain.db")            # custom path

    brain.remember("User prefers dark mode")
    brain.search("preferences")
    brain.entity("Chief", "person", observations=["Founder", "Builder"])
    brain.log("Deployed v2.0")
    brain.affect("I'm excited about this!")
    brain.stats()

    # Session continuity
    brain.handoff("finish API integration", "auth module done", "rate limiting", "add retry logic")
    packet = brain.resume()  # fetch + consume latest handoff

    # Prospective memory
    brain.trigger("deploy failure", "deploy,failure,rollback", "check rollback procedure")
    matches = brain.check_triggers("the deploy failed")

    # Diagnostics
    brain.doctor()

    # Drop-in session bookends (one call to start, one to finish)
    context = brain.orient()          # returns handoff + recent events + active triggers
    brain.wrap_up("summary of work")  # logs session_end + creates handoff
"""

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agentmemory.affect import classify_affect
from agentmemory.paths import get_db_path

try:
    from agentmemory import vec as _vec
    _VEC_AVAILABLE = True
except ImportError:
    _vec = None  # type: ignore[assignment]
    _VEC_AVAILABLE = False

_INIT_SQL_PATH = Path(__file__).parent / "db" / "init_schema.sql"
_log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_ts() -> str:
    return _utc_now_iso()


def _safe_fts(query: str) -> str:
    """Sanitize a query string for FTS5 MATCH syntax."""
    safe = re.sub(r'[^\w\s]', ' ', query).strip()
    return " OR ".join(safe.split()) if safe else ""


_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Brain:
    """Python interface to brainctl's memory system.

    Covers core operations (remember, search, entities, events, decisions),
    session continuity (handoff/resume), prospective memory (triggers),
    diagnostics (doctor), and optional vector search (vsearch).
    """

    def __init__(self, db_path: Optional[str] = None, agent_id: str = "default") -> None:
        if db_path is None:
            db_path = str(get_db_path())
        self.db_path = Path(db_path)
        self.agent_id = agent_id

        # Connection lifecycle (Phase 1.1 refactor):
        # One long-lived sqlite3 connection per Brain instance, lazily created on
        # first use and reused across all public method calls. Opened with
        # ``check_same_thread=False`` so it can be shared across threads, guarded
        # by a single ``threading.RLock`` that every public method acquires.
        # Choice rationale: the workload is mostly read-heavy with light writes
        # and the SQLite C library serializes access per-connection anyway — a
        # single lock + single connection is simpler than per-thread connections
        # and avoids per-call PRAGMA + agent-upsert overhead (~15-18 SQL stmts).
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._closed = False

        if not self.db_path.exists():
            self._init_db()

    def _init_db(self) -> None:
        """Create a fresh brain.db with the canonical production schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not _INIT_SQL_PATH.exists():
            raise FileNotFoundError(f"init_schema.sql not found at {_INIT_SQL_PATH}")

        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(_INIT_SQL_PATH.read_text())
        conn.execute(
            "INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('enabled', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('ignition_threshold', '0.7')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('urgent_threshold', '0.9')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('governor_max_per_hour', '5')"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO neuromodulation_state (
                id, org_state, dopamine_signal, arousal_level,
                confidence_boost_rate, confidence_decay_rate, retrieval_breadth_multiplier,
                focus_level, temporal_lambda, context_window_depth
            ) VALUES (1, 'normal', 0.0, 0.3, 0.1, 0.02, 1.0, 0.3, 0.03, 50)
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO agents (
                id, display_name, agent_type, status, created_at, updated_at
            ) VALUES (?, ?, 'api', 'active', ?, ?)
            """,
            (self.agent_id, self.agent_id, _now_ts(), _now_ts()),
        )
        conn.commit()
        conn.close()
        # Secure file permissions — only owner can read/write
        import stat
        self.db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        _log.info("brain.db created at %s", self.db_path)

    def _open_shared_conn(self) -> sqlite3.Connection:
        """Open a new shared connection and run one-time setup (PRAGMAs + agent upsert)."""
        conn = sqlite3.connect(
            str(self.db_path), timeout=10, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO agents (
                    id, display_name, agent_type, status, created_at, updated_at
                ) VALUES (?, ?, 'api', 'active', ?, ?)
                """,
                (self.agent_id, self.agent_id, _now_ts(), _now_ts()),
            )
            conn.commit()
        except Exception as exc:
            _log.warning("agent auto-register failed: %s", exc)
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared per-instance connection, opening lazily on first use.

        Safe to call from multiple threads — caller is expected to hold ``self._lock``
        for the duration of any query sequence that must be consistent. Simple
        one-shot ``execute()`` calls are already serialized at the sqlite3 C layer.

        If ``close()`` was previously called, this transparently reopens a fresh
        connection so long-lived callers don't break.
        """
        with self._lock:
            if self._conn is None:
                self._conn = self._open_shared_conn()
                self._closed = False
            return self._conn

    def _db(self) -> sqlite3.Connection:
        """Return a connection for legacy / external callers.

        Historically this opened and returned a fresh short-lived connection that
        the caller was expected to ``close()``. Preserved for backward compat with
        external code (CLI integrations, tests) that still manages its own
        connection lifetime. **Brain's own public methods use ``_get_conn()``**
        and must not route through here.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO agents (
                    id, display_name, agent_type, status, created_at, updated_at
                ) VALUES (?, ?, 'api', 'active', ?, ?)
                """,
                (self.agent_id, self.agent_id, _now_ts(), _now_ts()),
            )
            conn.commit()
        except Exception as exc:
            _log.warning("agent auto-register failed: %s", exc)
        return conn

    def _run(self, fn):
        """Execute ``fn(conn)`` against the shared connection under the lock.

        Wraps the common pattern:
            with self._lock:
                conn = self._get_conn()
                try:
                    return fn(conn)
                except sqlite3.OperationalError as exc:
                    if 'no such table' in str(exc):
                        # db file was tampered with behind our back — lazy reinit once
                        self._reset_conn()
                        self._init_db()
                        conn = self._get_conn()
                        return fn(conn)
                    raise
        """
        with self._lock:
            conn = self._get_conn()
            try:
                return fn(conn)
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    # Stale shared connection — drop it and try once more.
                    # If the db file was deleted entirely, recreate schema.
                    self._reset_conn()
                    if not self.db_path.exists():
                        self._init_db()
                    conn = self._get_conn()
                    return fn(conn)
                raise

    def _reset_conn(self) -> None:
        """Close and clear the shared connection (without marking as user-closed)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------------------------------------------------------------------
    # Lifecycle: close + context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the shared connection. Idempotent — safe to call multiple times.

        After ``close()``, subsequent public method calls will lazily reopen a
        fresh connection, so long-lived callers can safely ignore lifecycle.
        """
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as exc:
                    _log.debug("Brain.close: connection close raised %s", exc)
                self._conn = None
            self._closed = True

    def __enter__(self) -> "Brain":
        # Eagerly open so the context manager shape is obvious.
        self._get_conn()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup if user forgets to close.
        try:
            if getattr(self, "_conn", None) is not None:
                self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Core: remember, search, forget
    # ------------------------------------------------------------------

    def remember(self, content: str, category: str = "general", tags: Optional[Union[str, List[str]]] = None, confidence: float = 1.0) -> int:
        """Add a memory. Returns memory ID."""
        tags_json = json.dumps(tags.split(",")) if isinstance(tags, str) else (json.dumps(tags) if tags else None)
        now = _now_ts()
        with self._lock:
            db = self._get_conn()
            cur = db.execute(
                "INSERT INTO memories (agent_id, category, content, confidence, tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.agent_id, category, content, confidence, tags_json, now, now)
            )
            db.commit()
            mid = cur.lastrowid
            if _VEC_AVAILABLE:
                try:
                    # vec.index_memory opens its own connection to the same DB;
                    # WAL mode handles concurrent write access cleanly, and it
                    # does not contend with our RLock because it's a separate
                    # sqlite3 connection object. Leave untouched — the async
                    # embedding rework is tracked separately as Phase 1.2.
                    _vec.index_memory(db, mid, content)
                except Exception as exc:
                    _log.warning("vec.index_memory failed for memory %s: %s", mid, exc)
        return mid

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search memories using FTS5 full-text search with porter stemming.

        Falls back to LIKE search if FTS5 table is unavailable (older DBs).
        """
        if not query or not query.strip():
            return []
        with self._lock:
            db = self._get_conn()
            try:
                fts_q = _safe_fts(query)
                if fts_q:
                    rows = db.execute(
                        "SELECT m.id, m.content, m.category, m.confidence, m.created_at "
                        "FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
                        "WHERE memories_fts MATCH ? AND m.retired_at IS NULL "
                        "ORDER BY fts.rank LIMIT ?",
                        (fts_q, limit)
                    ).fetchall()
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass  # FTS5 table missing — fall back to LIKE
            rows = db.execute(
                "SELECT id, content, category, confidence, created_at FROM memories "
                "WHERE content LIKE ? AND retired_at IS NULL ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def forget(self, memory_id: int) -> None:
        """Soft-delete a memory."""
        now = _now_ts()
        with self._lock:
            db = self._get_conn()
            db.execute("UPDATE memories SET retired_at = ?, updated_at = ? WHERE id = ?", (now, now, memory_id))
            db.commit()

    # ------------------------------------------------------------------
    # Events, entities, decisions
    # ------------------------------------------------------------------

    def log(self, summary: str, event_type: str = "observation", project: Optional[str] = None, importance: float = 0.5) -> int:
        """Log an event. Returns event ID."""
        now = _now_ts()
        with self._lock:
            db = self._get_conn()
            cur = db.execute(
                "INSERT INTO events (agent_id, event_type, summary, project, importance, created_at) VALUES (?,?,?,?,?,?)",
                (self.agent_id, event_type, summary, project, importance, now)
            )
            db.commit()
            return cur.lastrowid

    def entity(self, name: str, entity_type: str, properties: Optional[Dict[str, Any]] = None, observations: Optional[List[str]] = None) -> int:
        """Create or get an entity. Returns entity ID."""
        with self._lock:
            db = self._get_conn()
            existing = db.execute(
                "SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (name,)
            ).fetchone()
            if existing:
                return existing["id"]

            props = json.dumps(properties) if properties else "{}"
            obs = json.dumps(observations) if observations else "[]"
            now = _now_ts()
            cur = db.execute(
                "INSERT INTO entities (name, entity_type, properties, observations, agent_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (name, entity_type, props, obs, self.agent_id, now, now)
            )
            db.commit()
            return cur.lastrowid

    def relate(self, from_entity: str, relation: str, to_entity: str) -> None:
        """Create a relation between two entities by name."""
        with self._lock:
            db = self._get_conn()
            from_row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (from_entity,)).fetchone()
            to_row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (to_entity,)).fetchone()
            if not from_row or not to_row:
                raise ValueError(f"Entity not found: {from_entity if not from_row else to_entity}")
            db.execute(
                "INSERT OR IGNORE INTO knowledge_edges (source_table, source_id, target_table, target_id, relation_type, agent_id, created_at) "
                "VALUES ('entities', ?, 'entities', ?, ?, ?, ?)",
                (from_row["id"], to_row["id"], relation, self.agent_id, _now_ts())
            )
            db.commit()

    def decide(self, title: str, rationale: str, project: Optional[str] = None) -> int:
        """Record a decision."""
        with self._lock:
            db = self._get_conn()
            cur = db.execute(
                "INSERT INTO decisions (agent_id, title, rationale, project, created_at) VALUES (?,?,?,?,?)",
                (self.agent_id, title, rationale, project, _now_ts())
            )
            db.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Session continuity: handoff / resume
    # ------------------------------------------------------------------

    def handoff(self, goal: str, current_state: str, open_loops: str, next_step: str,
                project: Optional[str] = None, title: Optional[str] = None) -> int:
        """Create a handoff packet for session continuity. Returns packet ID.

        Use before ending a session to preserve working context for the next agent.
        """
        for name, val in [("goal", goal), ("current_state", current_state),
                          ("open_loops", open_loops), ("next_step", next_step)]:
            if not val or not val.strip():
                raise ValueError(f"{name} must be a non-empty string")
        now = _now_ts()
        with self._lock:
            db = self._get_conn()
            cur = db.execute(
                "INSERT INTO handoff_packets (agent_id, goal, current_state, open_loops, next_step, "
                "project, title, status, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'global', ?, ?)",
                (self.agent_id, goal, current_state, open_loops, next_step,
                 project, title, now, now)
            )
            db.commit()
            return cur.lastrowid

    def resume(self, project: Optional[str] = None) -> Dict[str, Any]:
        """Fetch and auto-consume the latest pending handoff. Returns {} if none."""
        with self._lock:
            db = self._get_conn()
            q = ("SELECT * FROM handoff_packets WHERE agent_id = ? AND status = 'pending'")
            params: list = [self.agent_id]
            if project:
                q += " AND project = ?"
                params.append(project)
            q += " ORDER BY created_at DESC LIMIT 1"
            row = db.execute(q, params).fetchone()
            if not row:
                return {}
            packet = dict(row)
            now = _now_ts()
            db.execute(
                "UPDATE handoff_packets SET status = 'consumed', consumed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, packet["id"])
            )
            db.commit()
            packet["status"] = "consumed"
            return packet

    # ------------------------------------------------------------------
    # Drop-in session bookends: orient / wrap_up
    # ------------------------------------------------------------------

    def orient(self, project: Optional[str] = None, query: Optional[str] = None) -> Dict[str, Any]:
        """One-call session start. Returns everything an agent needs to begin working.

        Gathers: pending handoff, recent events, active triggers, and optionally
        searches for relevant memories. Call this at the start of every session.

        Returns dict with keys: handoff, recent_events, triggers, memories, stats.
        """
        now = _now_ts()
        result: Dict[str, Any] = {"agent_id": self.agent_id}
        with self._lock:
            db = self._get_conn()

            # 1. Check for pending handoff (don't consume yet — agent decides)
            try:
                hq = "SELECT id, goal, current_state, open_loops, next_step, project, title, created_at FROM handoff_packets WHERE agent_id = ? AND status = 'pending'"
                hp: list = [self.agent_id]
                if project:
                    hq += " AND project = ?"
                    hp.append(project)
                hq += " ORDER BY created_at DESC LIMIT 1"
                hrow = db.execute(hq, hp).fetchone()
                result["handoff"] = dict(hrow) if hrow else None
            except sqlite3.OperationalError:
                result["handoff"] = None

            # 2. Recent events (last 10)
            try:
                eq = "SELECT id, event_type, summary, project, created_at FROM events WHERE agent_id = ?"
                ep: list = [self.agent_id]
                if project:
                    eq += " AND project = ?"
                    ep.append(project)
                eq += " ORDER BY created_at DESC LIMIT 10"
                result["recent_events"] = [dict(r) for r in db.execute(eq, ep).fetchall()]
            except sqlite3.OperationalError:
                result["recent_events"] = []

            # 3. Active triggers
            try:
                # Expire overdue
                db.execute(
                    "UPDATE memory_triggers SET status = 'expired' "
                    "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
                    (now,)
                )
                db.commit()
                trows = db.execute(
                    "SELECT id, trigger_condition, trigger_keywords, action, priority "
                    "FROM memory_triggers WHERE status = 'active' AND agent_id = ? "
                    "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                    "WHEN 'medium' THEN 2 ELSE 3 END",
                    (self.agent_id,)
                ).fetchall()
                result["triggers"] = [dict(r) for r in trows]
            except sqlite3.OperationalError:
                result["triggers"] = []

            # 4. Search for relevant memories (if query or project given)
            search_q = query or project
            if search_q:
                try:
                    fts_q = _safe_fts(search_q)
                    if fts_q:
                        mrows = db.execute(
                            "SELECT m.id, m.content, m.category, m.confidence, m.created_at "
                            "FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
                            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL "
                            "ORDER BY fts.rank LIMIT 10",
                            (fts_q,)
                        ).fetchall()
                        result["memories"] = [dict(r) for r in mrows]
                    else:
                        result["memories"] = []
                except sqlite3.OperationalError:
                    result["memories"] = []
            else:
                result["memories"] = []

            # 5. Quick stats
            try:
                result["stats"] = {
                    "active_memories": db.execute(
                        "SELECT count(*) FROM memories WHERE retired_at IS NULL"
                    ).fetchone()[0],
                    "total_events": db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "total_entities": db.execute("SELECT count(*) FROM entities").fetchone()[0],
                }
            except Exception:
                result["stats"] = {}

            # Log session start — reentrant under our RLock, uses same shared conn.
            self.log("Session started", event_type="session_start", project=project)

        return result

    def wrap_up(self, summary: str, goal: Optional[str] = None,
                open_loops: Optional[str] = None, next_step: Optional[str] = None,
                project: Optional[str] = None) -> Dict[str, Any]:
        """One-call session end. Logs session_end event and creates a handoff.

        Args:
            summary: What was accomplished this session.
            goal: Ongoing goal (defaults to summary).
            open_loops: Unfinished work (defaults to "none").
            next_step: What should happen next (defaults to "continue from summary").
            project: Optional project scope.

        Returns dict with keys: event_id, handoff_id.
        """
        event_id = self.log(
            f"Session ended: {summary}",
            event_type="session_end",
            project=project,
            importance=0.7,
        )
        handoff_id = self.handoff(
            goal=goal or summary,
            current_state=summary,
            open_loops=open_loops or "none noted",
            next_step=next_step or f"Continue from: {summary}",
            project=project,
        )
        return {"event_id": event_id, "handoff_id": handoff_id}

    # ------------------------------------------------------------------
    # Prospective memory: triggers
    # ------------------------------------------------------------------

    def trigger(self, condition: str, keywords: str, action: str,
                priority: str = "medium", expires: Optional[str] = None) -> int:
        """Create a prospective memory trigger. Returns trigger ID.

        Args:
            condition: Human-readable description of when this should fire.
            keywords: Comma-separated keywords to match against.
            action: What to do when the trigger fires.
            priority: One of critical, high, medium, low.
            expires: Optional ISO datetime when the trigger expires.
        """
        if priority not in _PRIORITY_ORDER:
            raise ValueError(f"priority must be one of {list(_PRIORITY_ORDER)}")
        with self._lock:
            db = self._get_conn()
            cur = db.execute(
                "INSERT INTO memory_triggers (agent_id, trigger_condition, trigger_keywords, "
                "action, priority, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.agent_id, condition, keywords, action, priority, expires, _now_ts())
            )
            db.commit()
            return cur.lastrowid

    def check_triggers(self, query: str) -> List[Dict[str, Any]]:
        """Check if any active triggers match a query string.

        Returns list of matched triggers sorted by priority (critical first).
        """
        now = _now_ts()
        with self._lock:
            db = self._get_conn()
            # Expire overdue triggers
            try:
                db.execute(
                    "UPDATE memory_triggers SET status = 'expired' "
                    "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
                    (now,)
                )
                db.commit()
            except sqlite3.OperationalError:
                return []
            rows = db.execute(
                "SELECT * FROM memory_triggers WHERE status = 'active' AND agent_id = ?",
                (self.agent_id,)
            ).fetchall()
            query_lower = query.lower()
            matches = []
            for row in rows:
                kws = [k.strip().lower() for k in (row["trigger_keywords"] or "").split(",") if k.strip()]
                matched = [k for k in kws if k in query_lower]
                if matched:
                    m = dict(row)
                    m["matched_keywords"] = matched
                    matches.append(m)
            matches.sort(key=lambda m: _PRIORITY_ORDER.get(m.get("priority", "medium"), 2))
            return matches

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def doctor(self) -> Dict[str, Any]:
        """Run diagnostic checks on the brain database.

        Returns a dict with ok, healthy, issues list, and stats.
        """
        issues: List[str] = []
        active_memories = 0
        fts_ok = False
        with self._lock:
            db = self._get_conn()

            # Check core tables
            required = ["memories", "events", "entities", "decisions", "agents",
                         "handoff_packets", "memory_triggers", "affect_log", "knowledge_edges"]
            existing_tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            for tbl in required:
                if tbl not in existing_tables:
                    issues.append(f"Missing table: {tbl}")

            # FTS5
            fts_ok = "memories_fts" in existing_tables
            if not fts_ok:
                issues.append("Missing FTS5 table: memories_fts (search will use LIKE fallback)")

            # Integrity check
            try:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    issues.append(f"Integrity check failed: {integrity}")
            except Exception as e:
                issues.append(f"Integrity check error: {e}")

            # Counts
            try:
                active_memories = db.execute(
                    "SELECT count(*) FROM memories WHERE retired_at IS NULL"
                ).fetchone()[0]
            except Exception:
                pass

            # Orphan memories (agent_id not in agents table)
            try:
                orphans = db.execute(
                    "SELECT count(*) FROM memories WHERE agent_id NOT IN (SELECT id FROM agents)"
                ).fetchone()[0]
                if orphans > 0:
                    issues.append(f"{orphans} orphaned memories (agent_id not in agents table)")
            except Exception:
                pass

        # DB file size
        db_size_mb = round(self.db_path.stat().st_size / (1024 * 1024), 2) if self.db_path.exists() else 0.0

        healthy = len(issues) == 0
        return {
            "ok": True,
            "healthy": healthy,
            "issues": issues,
            "active_memories": active_memories,
            "fts5_available": fts_ok,
            "vec_available": _VEC_AVAILABLE,
            "db_size_mb": db_size_mb,
            "db_path": str(self.db_path),
        }

    def stats(self) -> Dict[str, int]:
        """Get database statistics."""
        stats: Dict[str, int] = {}
        with self._lock:
            db = self._get_conn()
            for tbl in ["memories", "events", "entities", "decisions", "knowledge_edges", "affect_log"]:
                try:
                    stats[tbl] = db.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
                except Exception:
                    stats[tbl] = 0
            try:
                stats["active_memories"] = db.execute(
                    "SELECT count(*) FROM memories WHERE retired_at IS NULL"
                ).fetchone()[0]
            except Exception:
                stats["active_memories"] = 0
        return stats

    # ------------------------------------------------------------------
    # Vector search (optional — requires sqlite-vec + Ollama)
    # ------------------------------------------------------------------

    def think(
        self,
        query: str,
        seed_limit: int = 5,
        hops: int = 2,
        decay: float = 0.6,
        top_k: int = 20,
    ) -> Dict[str, Any]:
        """Spreading-activation recall — distinct from semantic search.

        Searches the FTS index for `query` to pick seed memories, then
        traverses knowledge_edges outward with decaying activation. Returns
        a dict with `seeds` and `activated` (ranked by activation).

        Use `search()` to find what you remember about a topic.
        Use `think()` to find what your memory associates with that topic.
        """
        from agentmemory.dream import think_from_query
        with self._lock:
            db = self._get_conn()
            return think_from_query(
                db, query, seed_limit=seed_limit, hops=hops, decay=decay, top_k=top_k
            )

    def vsearch(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Vector similarity search. Returns [] if sqlite-vec is unavailable.

        Requires: pip install brainctl[vec] and Ollama running locally.
        """
        if not _VEC_AVAILABLE or _vec is None:
            return []
        with self._lock:
            db = self._get_conn()
            try:
                return _vec.vec_search(db, query, k=limit)
            except Exception as exc:
                _log.debug("vsearch failed: %s", exc)
                return []

    # ------------------------------------------------------------------
    # Consolidation (simplified single-pass)
    # ------------------------------------------------------------------

    def consolidate(self, limit: int = 50, min_priority: float = 0.1) -> Dict[str, Any]:
        """Run a single consolidation pass: promote high-replay episodic memories to semantic.

        Promotes episodic memories with replay_priority >= min_priority, ripple_tags >= 3,
        and confidence >= 0.7 to semantic memory_type. Resets replay_priority after processing.
        """
        with self._lock:
            db = self._get_conn()
            try:
                rows = db.execute(
                    "SELECT id, memory_type, ripple_tags, confidence FROM memories "
                    "WHERE retired_at IS NULL AND replay_priority >= ? "
                    "ORDER BY replay_priority DESC LIMIT ?",
                    (min_priority, limit)
                ).fetchall()
            except sqlite3.OperationalError:
                return {"ok": False, "error": "replay_priority column not available (run brainctl migrate)"}

            processed = 0
            promoted = 0
            now = _now_ts()
            for row in rows:
                processed += 1
                if (row["memory_type"] == "episodic"
                        and (row["ripple_tags"] or 0) >= 3
                        and (row["confidence"] or 0) >= 0.7):
                    db.execute(
                        "UPDATE memories SET memory_type = 'semantic', updated_at = ? WHERE id = ?",
                        (now, row["id"])
                    )
                    promoted += 1
                db.execute(
                    "UPDATE memories SET replay_priority = 0.0 WHERE id = ?",
                    (row["id"],)
                )
            db.commit()
            return {"ok": True, "processed": processed, "promoted": promoted}

    # ------------------------------------------------------------------
    # Tier stats (D-MEM write-tier distribution)
    # ------------------------------------------------------------------

    def tier_stats(self) -> Dict[str, Any]:
        """Show write-tier distribution (full/construct) for this agent."""
        with self._lock:
            db = self._get_conn()
            try:
                rows = db.execute(
                    "SELECT write_tier, count(*) as cnt FROM memories "
                    "WHERE retired_at IS NULL AND agent_id = ? GROUP BY write_tier",
                    (self.agent_id,)
                ).fetchall()
            except sqlite3.OperationalError:
                return {"ok": False, "error": "write_tier column not available (run brainctl migrate)"}
            total = sum(r["cnt"] for r in rows)
            tiers = {r["write_tier"]: r["cnt"] for r in rows}
            return {"ok": True, "total": total, "tiers": tiers}

    # ------------------------------------------------------------------
    # Affect
    # ------------------------------------------------------------------

    def affect(self, text: str) -> Dict[str, Any]:
        """Classify affect from text. Returns VAD scores and labels."""
        return classify_affect(text)

    def affect_log(self, text: str, source: str = "observation") -> Dict[str, Any]:
        """Classify affect from text and store in affect_log table. Returns the affect result with stored ID."""
        result = classify_affect(text)
        now = _now_ts()
        with self._lock:
            db = self._get_conn()
            cur = db.execute(
                "INSERT INTO affect_log (agent_id, valence, arousal, dominance, affect_label, "
                "cluster, functional_state, safety_flag, trigger, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.agent_id,
                    result.get("valence", 0.0),
                    result.get("arousal", 0.0),
                    result.get("dominance", 0.0),
                    result.get("affect_label"),
                    result.get("cluster"),
                    result.get("functional_state"),
                    result.get("safety_flag"),
                    text,
                    source,
                    now,
                ),
            )
            db.commit()
            result["id"] = cur.lastrowid
            result["source"] = source
            result["created_at"] = now
        return result


# ----------------------------------------------------------------------
# Module-level Brain factory with per-(db_path, agent_id) cache.
# ----------------------------------------------------------------------
#
# Recommended entry point for multi-threaded callers. Returns the same
# Brain instance for the same (absolute-path, agent_id) pair so callers
# share a single connection instead of racing to open their own. The
# cache is guarded by a module-level lock. Thread-safety of the returned
# Brain itself is handled by its internal RLock.

_BRAIN_CACHE: Dict[Tuple[str, str], "Brain"] = {}
_BRAIN_CACHE_LOCK = threading.Lock()


def get_brain(db_path: Optional[str] = None, agent_id: str = "default") -> "Brain":
    """Return a cached Brain for the given (db_path, agent_id).

    Repeat calls with the same arguments return the **same** instance, so
    multi-threaded callers share a single connection. Path normalization
    uses the resolved absolute path, so relative / symlinked / tilde-
    expanded variants all hit the same cache slot.

    This is the recommended entry point when multiple threads or multiple
    callers in the same process need memory access. Prefer this over
    constructing ``Brain(...)`` directly.
    """
    if db_path is None:
        db_path = str(get_db_path())
    abs_path = str(Path(db_path).expanduser().resolve())
    key = (abs_path, agent_id)
    with _BRAIN_CACHE_LOCK:
        brain = _BRAIN_CACHE.get(key)
        if brain is None or brain._closed:
            brain = Brain(db_path=abs_path, agent_id=agent_id)
            _BRAIN_CACHE[key] = brain
        return brain


def _clear_brain_cache() -> None:
    """Test hook: drop all cached Brain instances (closing each)."""
    with _BRAIN_CACHE_LOCK:
        for b in list(_BRAIN_CACHE.values()):
            try:
                b.close()
            except Exception:
                pass
        _BRAIN_CACHE.clear()
