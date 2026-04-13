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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentmemory import _gates
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


class GateRejected(Exception):
    """Raised by Brain.remember(..., strict=True) when the W(m) gate rejects.

    The :class:`~agentmemory._gates.GateDecision` is attached on
    ``.decision`` so callers can introspect the rejection.
    """

    def __init__(self, decision) -> None:  # type: ignore[no-untyped-def]
        super().__init__(decision.reason if decision is not None else "gate rejected")
        self.decision = decision


def _safe_fts(query: str) -> str:
    """Sanitize a query string for FTS5 MATCH.

    Rules:

    * Preserve quoted phrases: ``"release notes"`` stays a phrase.
    * Preserve prefix matching: ``api*`` stays a prefix term.
    * Preserve bare ``AND`` / ``OR`` / ``NOT`` uppercase operators.
    * Drop any other FTS5 metacharacter (parens, colons, carets, etc.).
    * Unbalanced quotes are dropped rather than raising.
    * Degenerate input (only punctuation) returns ``""`` — the caller
      must already fall through to LIKE in that case.
    """
    if not query or not query.strip():
        return ""

    # 1. Extract quoted phrases verbatim so they survive tokenization.
    phrases: list[str] = []

    def _grab_phrase(match: "re.Match[str]") -> str:
        inner = match.group(1)
        # Strip FTS5-dangerous chars from inside the phrase but keep words.
        inner_clean = re.sub(r'[^\w\s\-]', ' ', inner)
        inner_clean = re.sub(r'\s+', ' ', inner_clean).strip()
        if not inner_clean:
            return " "
        phrases.append(inner_clean)
        return f" \x00PHRASE{len(phrases) - 1}\x00 "

    stripped = re.sub(r'"([^"]*)"', _grab_phrase, query)
    # Any remaining bare quote is unbalanced — drop it.
    stripped = stripped.replace('"', ' ')

    # 2. Tokenize the rest. Preserve alphanum, underscore, hyphen, and
    #    a trailing asterisk (prefix operator).
    tokens: list[str] = []
    for raw in stripped.split():
        if raw.startswith("\x00PHRASE") and raw.endswith("\x00"):
            idx = int(raw[len("\x00PHRASE"):-1])
            tokens.append(f'"{phrases[idx]}"')
            continue

        # Bare boolean operators pass through untouched.
        if raw in ("AND", "OR", "NOT"):
            tokens.append(raw)
            continue

        # Detect trailing prefix asterisk.
        has_prefix = raw.endswith("*")
        core = raw[:-1] if has_prefix else raw

        # Strip dangerous FTS5 operators and anything that isn't a word char.
        core = re.sub(r'[^\w\-]', '', core)
        if not core:
            continue
        # A lone hyphen isn't a valid token.
        core = core.strip('-')
        if not core:
            continue

        tokens.append(f"{core}*" if has_prefix else core)

    if not tokens:
        return ""

    # 3. If we have only one token, return it as-is so FTS5 can stem.
    if len(tokens) == 1:
        return tokens[0]

    # 4. Otherwise OR the tokens together (same default behaviour as
    #    before for bag-of-words queries) while keeping phrases atomic.
    return " OR ".join(tokens)


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

    def _db(self) -> sqlite3.Connection:
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

    # ------------------------------------------------------------------
    # Core: remember, search, forget
    # ------------------------------------------------------------------

    def _embed_for_gate(self, text: str) -> Optional[bytes]:
        """Best-effort embedding helper for the W(m) gate.

        Returns None when sqlite-vec isn't wired up or the embedder is
        unreachable. The gate gracefully falls back to word-overlap
        surprise scoring when this returns None, so the Python API path
        still runs without Ollama.
        """
        if not _VEC_AVAILABLE or _vec is None:
            return None
        try:
            return _vec.embed_text(text)
        except Exception as exc:
            _log.debug("vec.embed_text unavailable, falling back to word-overlap surprise: %s", exc)
            return None

    def _get_vec_db(self) -> Optional[sqlite3.Connection]:
        """Return a sqlite-vec-enabled connection, or None.

        Passed to :func:`agentmemory._gates.evaluate_write` so the W(m)
        gate can query ``vec_memories``. When sqlite-vec isn't available
        the gate automatically falls back to word-overlap surprise.
        """
        if not _VEC_AVAILABLE or _vec is None:
            return None
        try:
            dylib = _vec._find_vec_dylib()
        except Exception:
            return None
        if not dylib:
            return None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.enable_load_extension(True)
            conn.load_extension(dylib)
            conn.enable_load_extension(False)
            return conn
        except Exception as exc:
            _log.debug("Brain._get_vec_db failed: %s", exc)
            return None

    def remember(
        self,
        content: str,
        category: str = "general",
        tags: Optional[Union[str, List[str]]] = None,
        confidence: float = 1.0,
        *,
        bypass_gate: bool = False,
        strict: bool = False,
        supersedes_id: Optional[int] = None,
        source: str = "python_api",
    ) -> Optional[int]:
        """Add a memory. Returns the memory id, or ``None`` if the gate rejected.

        Every call runs through the shared write gate in
        :mod:`agentmemory._gates`, which mirrors the MCP write path:
        reconsolidation lability check, surprise scoring, arousal/valence
        modulation, a pre-worthiness floor, and the full W(m) semantic
        gate (when embeddings are available).

        Args:
            content: The memory text.
            category: Memory category (identity, decision, lesson, ...).
            tags: Comma-separated string or list of tags.
            confidence: Caller's confidence in the memory, 0-1.
            bypass_gate: Skip the gate entirely. Intended for migration
                tools and tests; never expose to end users.
            strict: When True, a gate rejection raises :class:`GateRejected`
                instead of returning ``None``.
            supersedes_id: If this write is meant to overwrite an existing
                memory (e.g. during reconsolidation), pass its id so the
                lability window is enforced.
            source: Trust bucket for the writer. Valid values are the keys
                of :data:`agentmemory._gates.SOURCE_TRUST_WEIGHTS`.

        Raises:
            GateRejected: When ``strict=True`` and the write was rejected.
        """
        db = self._db()

        if not bypass_gate:
            decision = _gates.evaluate_write(
                db,
                agent_id=self.agent_id,
                content=content,
                category=category,
                scope="global",
                confidence=confidence,
                source=source,
                force=False,
                supersedes_id=supersedes_id,
                embed_fn=self._embed_for_gate,
                get_vec_db_fn=self._get_vec_db,
            )
            if not decision.accepted:
                self._log_gate_rejection(db, content, category, decision)
                db.close()
                _log.warning(
                    "Brain.remember rejected by W(m) gate: %s", decision.reason
                )
                if strict:
                    raise GateRejected(decision)
                return None

        tags_json = (
            json.dumps(tags.split(",")) if isinstance(tags, str)
            else (json.dumps(tags) if tags else None)
        )
        now = _now_ts()
        cur = db.execute(
            "INSERT INTO memories (agent_id, category, content, confidence, tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (self.agent_id, category, content, confidence, tags_json, now, now)
        )
        db.commit()
        mid = cur.lastrowid
        if _VEC_AVAILABLE:
            try:
                _vec.index_memory(db, mid, content)
            except Exception as exc:
                _log.warning("vec.index_memory failed for memory %s: %s", mid, exc)
        db.close()
        return mid

    def _log_gate_rejection(self, db: sqlite3.Connection, content: str, category: str, decision) -> None:
        """Record a ``write_rejected`` event for observability.

        Mirrors the event mcp_server emits so downstream diagnostics see
        rejections from either path. Failure to log is non-fatal.
        """
        try:
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) "
                "VALUES (?, 'write_rejected', ?, ?, ?)",
                (
                    self.agent_id,
                    f"Brain.remember rejected: {decision.reason[:120]}",
                    json.dumps({
                        "content_preview": content[:120],
                        "category": category,
                        "surprise": decision.surprise,
                        "surprise_method": decision.surprise_method,
                        "pre_worthiness": decision.pre_worthiness,
                        "score": decision.score,
                        "reason": decision.reason,
                        "source": "python_api",
                    }),
                    _now_ts(),
                ),
            )
            db.commit()
        except Exception as exc:
            _log.debug("Failed to log write_rejected event: %s", exc)

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search memories using FTS5 full-text search with porter stemming.

        Falls back to LIKE search if FTS5 table is unavailable (older DBs).
        """
        if not query or not query.strip():
            return []
        db = self._db()
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
                results = [dict(r) for r in rows]
                db.close()
                return results
        except sqlite3.OperationalError as exc:
            _log.warning(
                "FTS5 search failed at brain.py:search(), falling back to LIKE: %s",
                exc,
            )
        # Fallback: LIKE search
        rows = db.execute(
            "SELECT id, content, category, confidence, created_at FROM memories "
            "WHERE content LIKE ? AND retired_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit)
        ).fetchall()
        results = [dict(r) for r in rows]
        db.close()
        return results

    def forget(self, memory_id: int) -> None:
        """Soft-delete a memory."""
        db = self._db()
        now = _now_ts()
        db.execute("UPDATE memories SET retired_at = ?, updated_at = ? WHERE id = ?", (now, now, memory_id))
        db.commit()
        db.close()

    # ------------------------------------------------------------------
    # Events, entities, decisions
    # ------------------------------------------------------------------

    def log(self, summary: str, event_type: str = "observation", project: Optional[str] = None, importance: float = 0.5) -> int:
        """Log an event. Returns event ID."""
        db = self._db()
        now = _now_ts()
        cur = db.execute(
            "INSERT INTO events (agent_id, event_type, summary, project, importance, created_at) VALUES (?,?,?,?,?,?)",
            (self.agent_id, event_type, summary, project, importance, now)
        )
        db.commit()
        eid = cur.lastrowid
        db.close()
        return eid

    def entity(self, name: str, entity_type: str, properties: Optional[Dict[str, Any]] = None, observations: Optional[List[str]] = None) -> int:
        """Create or get an entity. Returns entity ID."""
        db = self._db()
        existing = db.execute(
            "SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (name,)
        ).fetchone()
        if existing:
            db.close()
            return existing["id"]

        props = json.dumps(properties) if properties else "{}"
        obs = json.dumps(observations) if observations else "[]"
        now = _now_ts()
        cur = db.execute(
            "INSERT INTO entities (name, entity_type, properties, observations, agent_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (name, entity_type, props, obs, self.agent_id, now, now)
        )
        db.commit()
        eid = cur.lastrowid
        db.close()
        return eid

    def relate(self, from_entity: str, relation: str, to_entity: str) -> None:
        """Create a relation between two entities by name."""
        db = self._db()
        from_row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (from_entity,)).fetchone()
        to_row = db.execute("SELECT id FROM entities WHERE name = ? AND retired_at IS NULL", (to_entity,)).fetchone()
        if not from_row or not to_row:
            db.close()
            raise ValueError(f"Entity not found: {from_entity if not from_row else to_entity}")
        db.execute(
            "INSERT OR IGNORE INTO knowledge_edges (source_table, source_id, target_table, target_id, relation_type, agent_id, created_at) "
            "VALUES ('entities', ?, 'entities', ?, ?, ?, ?)",
            (from_row["id"], to_row["id"], relation, self.agent_id, _now_ts())
        )
        db.commit()
        db.close()

    def decide(self, title: str, rationale: str, project: Optional[str] = None) -> int:
        """Record a decision."""
        db = self._db()
        cur = db.execute(
            "INSERT INTO decisions (agent_id, title, rationale, project, created_at) VALUES (?,?,?,?,?)",
            (self.agent_id, title, rationale, project, _now_ts())
        )
        db.commit()
        did = cur.lastrowid
        db.close()
        return did

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
        db = self._db()
        now = _now_ts()
        cur = db.execute(
            "INSERT INTO handoff_packets (agent_id, goal, current_state, open_loops, next_step, "
            "project, title, status, scope, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'global', ?, ?)",
            (self.agent_id, goal, current_state, open_loops, next_step,
             project, title, now, now)
        )
        db.commit()
        hid = cur.lastrowid
        db.close()
        return hid

    def resume(self, project: Optional[str] = None) -> Dict[str, Any]:
        """Fetch and auto-consume the latest pending handoff. Returns {} if none."""
        db = self._db()
        q = ("SELECT * FROM handoff_packets WHERE agent_id = ? AND status = 'pending'")
        params: list = [self.agent_id]
        if project:
            q += " AND project = ?"
            params.append(project)
        q += " ORDER BY created_at DESC LIMIT 1"
        row = db.execute(q, params).fetchone()
        if not row:
            db.close()
            return {}
        packet = dict(row)
        now = _now_ts()
        db.execute(
            "UPDATE handoff_packets SET status = 'consumed', consumed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, packet["id"])
        )
        db.commit()
        db.close()
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
        db = self._db()
        now = _now_ts()
        result: Dict[str, Any] = {"agent_id": self.agent_id}

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
        except sqlite3.OperationalError as exc:
            _log.warning("orient(): handoff lookup failed: %s", exc)
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
        except sqlite3.OperationalError as exc:
            _log.warning("orient(): recent events lookup failed: %s", exc)
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
        except sqlite3.OperationalError as exc:
            _log.warning("orient(): trigger lookup failed: %s", exc)
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
            except sqlite3.OperationalError as exc:
                _log.warning(
                    "orient(): FTS5 search failed, returning empty: %s", exc
                )
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

        db.close()

        # Log session start
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
        db = self._db()
        cur = db.execute(
            "INSERT INTO memory_triggers (agent_id, trigger_condition, trigger_keywords, "
            "action, priority, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.agent_id, condition, keywords, action, priority, expires, _now_ts())
        )
        db.commit()
        tid = cur.lastrowid
        db.close()
        return tid

    def check_triggers(self, query: str) -> List[Dict[str, Any]]:
        """Check if any active triggers match a query string.

        Returns list of matched triggers sorted by priority (critical first).
        """
        db = self._db()
        now = _now_ts()
        # Expire overdue triggers
        try:
            db.execute(
                "UPDATE memory_triggers SET status = 'expired' "
                "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
                (now,)
            )
            db.commit()
        except sqlite3.OperationalError as exc:
            _log.warning(
                "check_triggers(): expiring overdue triggers failed: %s", exc
            )
            db.close()
            return []
        rows = db.execute(
            "SELECT * FROM memory_triggers WHERE status = 'active' AND agent_id = ?",
            (self.agent_id,)
        ).fetchall()
        query_lower = query.lower()
        matches = []
        for row in rows:
            kws = [k.strip().lower() for k in (row["trigger_keywords"] or "").split(",") if k.strip()]
            # Word-boundary match — prevents false positives like "deploy"
            # matching "redeploy" or "re" matching "research".
            matched = [
                k for k in kws
                if re.search(rf'\b{re.escape(k)}\b', query_lower)
            ]
            if matched:
                m = dict(row)
                m["matched_keywords"] = matched
                matches.append(m)
        matches.sort(key=lambda m: _PRIORITY_ORDER.get(m.get("priority", "medium"), 2))
        db.close()
        return matches

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def doctor(self) -> Dict[str, Any]:
        """Run diagnostic checks on the brain database.

        Returns a dict with ok, healthy, issues list, and stats.
        """
        issues: List[str] = []
        db = self._db()

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
        active_memories = 0
        try:
            active_memories = db.execute(
                "SELECT count(*) FROM memories WHERE retired_at IS NULL"
            ).fetchone()[0]
        except Exception:
            pass

        # Orphan memories (agent_id not in agents table)
        orphans = 0
        try:
            orphans = db.execute(
                "SELECT count(*) FROM memories WHERE agent_id NOT IN (SELECT id FROM agents)"
            ).fetchone()[0]
            if orphans > 0:
                issues.append(f"{orphans} orphaned memories (agent_id not in agents table)")
        except Exception:
            pass

        db.close()

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
        db = self._db()
        stats: Dict[str, int] = {}
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
        db.close()
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
        db = self._db()
        try:
            return think_from_query(
                db, query, seed_limit=seed_limit, hops=hops, decay=decay, top_k=top_k
            )
        finally:
            db.close()

    def vsearch(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Vector similarity search. Returns [] if sqlite-vec is unavailable.

        Requires: pip install brainctl[vec] and Ollama running locally.
        """
        if not _VEC_AVAILABLE or _vec is None:
            return []
        db = self._db()
        try:
            results = _vec.vec_search(db, query, k=limit)
        except Exception as exc:
            _log.debug("vsearch failed: %s", exc)
            results = []
        db.close()
        return results

    # ------------------------------------------------------------------
    # Consolidation (simplified single-pass)
    # ------------------------------------------------------------------

    def consolidate(self, limit: int = 50, min_priority: float = 0.1) -> Dict[str, Any]:
        """Run a single consolidation pass: promote high-replay episodic memories to semantic.

        Promotes episodic memories with replay_priority >= min_priority, ripple_tags >= 3,
        and confidence >= 0.7 to semantic memory_type. Resets replay_priority after processing.
        """
        db = self._db()
        try:
            rows = db.execute(
                "SELECT id, memory_type, ripple_tags, confidence FROM memories "
                "WHERE retired_at IS NULL AND replay_priority >= ? "
                "ORDER BY replay_priority DESC LIMIT ?",
                (min_priority, limit)
            ).fetchall()
        except sqlite3.OperationalError:
            db.close()
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
        db.close()
        return {"ok": True, "processed": processed, "promoted": promoted}

    # ------------------------------------------------------------------
    # Tier stats (D-MEM write-tier distribution)
    # ------------------------------------------------------------------

    def tier_stats(self) -> Dict[str, Any]:
        """Show write-tier distribution (full/construct) for this agent."""
        db = self._db()
        try:
            rows = db.execute(
                "SELECT write_tier, count(*) as cnt FROM memories "
                "WHERE retired_at IS NULL AND agent_id = ? GROUP BY write_tier",
                (self.agent_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            db.close()
            return {"ok": False, "error": "write_tier column not available (run brainctl migrate)"}
        total = sum(r["cnt"] for r in rows)
        tiers = {r["write_tier"]: r["cnt"] for r in rows}
        db.close()
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
        db = self._db()
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
        db.close()
        return result
