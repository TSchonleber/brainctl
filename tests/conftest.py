"""Shared fixtures for brainctl test suite."""
import sys
import os
import sqlite3
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROD_DB = Path(__file__).resolve().parent.parent / "db" / "brain.db"

from agentmemory.brain import Brain


@pytest.fixture
def brain(tmp_path):
    """Return a Brain instance backed by a temp DB file."""
    db_file = tmp_path / "brain.db"
    return Brain(db_path=str(db_file), agent_id="test-agent")


@pytest.fixture
def brain_with_data(brain):
    """Brain pre-loaded with sample memories, entities, events."""
    brain.remember("User prefers dark mode", category="preference", confidence=0.9)
    brain.remember("Project uses Python 3.12", category="project", confidence=1.0)
    brain.remember("Deploy to staging first", category="lesson", confidence=0.8)
    brain.entity("Alice", "person", observations=["Engineer", "Likes coffee"])
    brain.entity("BrainProject", "project", observations=["Memory system"])
    brain.relate("Alice", "works_on", "BrainProject")
    brain.log("Started dev session", event_type="session", project="brain")
    brain.log("Deployed v1.0", event_type="deploy", project="brain")
    return brain


@pytest.fixture
def cli_db(tmp_path):
    """Create an empty DB with the full production schema for CLI tests.

    Copies the production DB structure (including FTS tables) by cloning
    the real DB and then deleting all data rows.
    """
    import shutil
    db_file = tmp_path / "brain.db"
    if PROD_DB.exists():
        shutil.copy2(str(PROD_DB), str(db_file))
        # Delete all data to get a clean slate
        conn = sqlite3.connect(str(db_file))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
        ).fetchall()]
        for t in tables:
            try:
                conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        # Insert test agents to satisfy FK constraints
        for aid in ('tester', 'fmt', 'unknown', 'default'):
            conn.execute(
                "INSERT OR IGNORE INTO agents (id, display_name, agent_type) VALUES (?, ?, 'test')",
                (aid, aid)
            )
        conn.commit()
        conn.close()
    else:
        # Fallback: create minimal schema via Brain + extra tables
        b = Brain(db_path=str(db_file), agent_id="default")
        conn = sqlite3.connect(str(db_file))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS context (id INTEGER PRIMARY KEY, agent_id TEXT, content TEXT, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, agent_id TEXT, title TEXT, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, ref TEXT, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS access_log (id INTEGER PRIMARY KEY, agent_id TEXT, action TEXT, target_table TEXT, target_id INTEGER, query TEXT, result_count INTEGER, tokens_consumed INTEGER, created_at TEXT DEFAULT (datetime('now')));
        """)
        conn.close()
    return db_file
