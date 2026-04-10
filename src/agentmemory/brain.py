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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_ts() -> str:
    return _utc_now_iso()


class Brain:
    """Simple interface to brainctl's memory system."""
    
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
            logging.getLogger(__name__).warning("agent auto-register failed: %s", exc)
        return conn
    
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
                logging.getLogger(__name__).warning(
                    "vec.index_memory failed for memory %s: %s", mid, exc
                )
        db.close()
        return mid
    
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search memories by content. Returns list of dicts."""
        db = self._db()
        # Simple LIKE search (works without FTS5)
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
