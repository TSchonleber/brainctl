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
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

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

    def remember(self, content: str, category: str = "general", tags: Optional[Union[str, List[str]]] = None, confidence: float = 1.0) -> int:
        """Add a memory. Returns memory ID."""
        db = self._db()
        tags_json = json.dumps(tags.split(",")) if isinstance(tags, str) else (json.dumps(tags) if tags else None)
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
        except sqlite3.OperationalError:
            pass  # FTS5 table missing — fall back to LIKE
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
        except sqlite3.OperationalError:
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
            matched = [k for k in kws if k in query_lower]
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
