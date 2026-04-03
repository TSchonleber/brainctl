"""
brainctl — Python API for agent memory.

Quick start:
    from brainctl import Brain
    
    brain = Brain()                          # uses ~/brainctl/brain.db
    brain = Brain("/path/to/brain.db")       # custom path
    
    brain.remember("User prefers dark mode") # add a memory
    brain.search("preferences")              # search memories
    brain.entity("Chief", "person",          # create entity
        observations=["Founder", "Builder"])
    brain.log("Deployed v2.0")               # log an event
    brain.affect("I'm excited about this!")  # classify affect
    brain.stats()                            # database stats
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentmemory.affect import classify_affect

_INIT_SQL_PATH = Path(__file__).parent.parent.parent / "db" / "init_schema.sql"


class Brain:
    """Simple interface to brainctl's memory system."""
    
    def __init__(self, db_path: Optional[str] = None, agent_id: str = "default") -> None:
        if db_path is None:
            db_path = os.environ.get("BRAIN_DB", str(Path.home() / "brainctl" / "brain.db"))
        self.db_path = Path(db_path)
        self.agent_id = agent_id
        
        if not self.db_path.exists():
            self._init_db()
    
    def _init_db(self) -> None:
        """Create a fresh brain.db with core schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL DEFAULT 'default',
                category TEXT NOT NULL DEFAULT 'general',
                scope TEXT NOT NULL DEFAULT 'global',
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                tags TEXT,
                retired_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL DEFAULT 'default',
                event_type TEXT NOT NULL DEFAULT 'observation',
                summary TEXT NOT NULL,
                detail TEXT,
                project TEXT,
                importance REAL NOT NULL DEFAULT 0.5,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                properties TEXT NOT NULL DEFAULT '{}',
                observations TEXT NOT NULL DEFAULT '[]',
                agent_id TEXT NOT NULL DEFAULT 'default',
                confidence REAL NOT NULL DEFAULT 1.0,
                scope TEXT NOT NULL DEFAULT 'global',
                retired_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS knowledge_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                target_table TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                agent_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                rationale TEXT NOT NULL,
                project TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS memory_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL DEFAULT 'default',
                trigger_condition TEXT NOT NULL,
                trigger_keywords TEXT NOT NULL,
                action TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'active',
                fired_at TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS affect_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                valence REAL NOT NULL DEFAULT 0.0,
                arousal REAL NOT NULL DEFAULT 0.0,
                dominance REAL NOT NULL DEFAULT 0.0,
                affect_label TEXT,
                cluster TEXT,
                functional_state TEXT,
                safety_flag TEXT,
                trigger TEXT,
                source TEXT DEFAULT 'observation',
                metadata TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_affect_agent_time ON affect_log(agent_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_affect_safety ON affect_log(safety_flag) WHERE safety_flag IS NOT NULL;
        """)
        conn.close()
    
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
    
    def remember(self, content: str, category: str = "general", tags: Optional[Union[str, List[str]]] = None, confidence: float = 1.0) -> int:
        """Add a memory. Returns memory ID."""
        db = self._db()
        tags_json = json.dumps(tags.split(",")) if isinstance(tags, str) else (json.dumps(tags) if tags else None)
        cur = db.execute(
            "INSERT INTO memories (agent_id, category, content, confidence, tags) VALUES (?,?,?,?,?)",
            (self.agent_id, category, content, confidence, tags_json)
        )
        db.commit()
        mid = cur.lastrowid
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
        db.execute("UPDATE memories SET retired_at = datetime('now') WHERE id = ?", (memory_id,))
        db.commit()
        db.close()
    
    def log(self, summary: str, event_type: str = "observation", project: Optional[str] = None, importance: float = 0.5) -> int:
        """Log an event. Returns event ID."""
        db = self._db()
        cur = db.execute(
            "INSERT INTO events (agent_id, event_type, summary, project, importance) VALUES (?,?,?,?,?)",
            (self.agent_id, event_type, summary, project, importance)
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
        cur = db.execute(
            "INSERT INTO entities (name, entity_type, properties, observations, agent_id) VALUES (?,?,?,?,?)",
            (name, entity_type, props, obs, self.agent_id)
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
            "INSERT OR IGNORE INTO knowledge_edges (source_table, source_id, target_table, target_id, relation_type, agent_id) "
            "VALUES ('entities', ?, 'entities', ?, ?, ?)",
            (from_row["id"], to_row["id"], relation, self.agent_id)
        )
        db.commit()
        db.close()
    
    def decide(self, title: str, rationale: str, project: Optional[str] = None) -> int:
        """Record a decision."""
        db = self._db()
        cur = db.execute(
            "INSERT INTO decisions (agent_id, title, rationale, project) VALUES (?,?,?,?)",
            (self.agent_id, title, rationale, project)
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
            except:
                stats[tbl] = 0
        try:
            stats["active_memories"] = db.execute(
                "SELECT count(*) FROM memories WHERE retired_at IS NULL"
            ).fetchone()[0]
        except:
            stats["active_memories"] = 0
        db.close()
        return stats

    def affect(self, text: str) -> Dict[str, Any]:
        """Classify affect from text. Returns VAD scores and labels."""
        return classify_affect(text)

    def affect_log(self, text: str, source: str = "observation") -> Dict[str, Any]:
        """Classify affect from text and store in affect_log table. Returns the affect result with stored ID."""
        result = classify_affect(text)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
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
