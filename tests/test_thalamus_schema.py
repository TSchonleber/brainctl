"""Tests for thalamus Phase 1 schema (migration 050)."""
import sqlite3
import pytest


def test_migration_050_creates_all_tables(tmp_path):
    """Apply migration 050 and assert all 5 thalamus tables exist."""
    db_path = str(tmp_path / "brain.db")
    conn = sqlite3.connect(db_path)
    
    # Read and apply migration 050
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql"
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)
    
    # Assert all 5 tables exist
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'thalamic_%'"
    ).fetchall()}
    assert "thalamic_relays" in tables
    assert "thalamic_gate" in tables
    assert "thalamic_mode" in tables
    assert "thalamic_salience" in tables
    assert "thalamic_bursts" in tables
    
    conn.close()


def test_thalamic_mode_seed_row_exists(tmp_path):
    """Assert thalamic_mode has the seed row with id=1."""
    db_path = str(tmp_path / "brain.db")
    conn = sqlite3.connect(db_path)
    
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql"
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)
    
    row = conn.execute("SELECT * FROM thalamic_mode WHERE id=1").fetchone()
    assert row is not None
    assert row[1] == "wake_focused"  # mode column
    
    conn.close()


def test_key_indexes_exist(tmp_path):
    """Assert key indexes on thalamic tables were created."""
    db_path = str(tmp_path / "brain.db")
    conn = sqlite3.connect(db_path)
    
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql"
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)
    
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()}
    assert "idx_relays_sector" in indexes
    assert "idx_gate_sector" in indexes
    assert "idx_salience_recent" in indexes
    
    conn.close()


def test_foreign_key_constraint_on_gate(tmp_path):
    """Assert thalamic_gate has foreign key to thalamic_relays."""
    db_path = str(tmp_path / "brain.db")
    conn = sqlite3.connect(db_path)
    
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql"
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)
    
    # Check foreign key is enforced
    fk_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='thalamic_gate'"
    ).fetchone()[0]
    assert "FOREIGN KEY" in fk_sql
    assert "thalamic_relays" in fk_sql
    
    conn.close()


def test_check_constraints_on_mode(tmp_path):
    """Assert thalamic_mode has CHECK constraint on mode enum."""
    db_path = str(tmp_path / "brain.db")
    conn = sqlite3.connect(db_path)
    
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql"
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)
    
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='thalamic_mode'"
    ).fetchone()[0]
    assert "CHECK" in table_sql
    assert "wake_focused" in table_sql
    
    conn.close()
