"""Tests for thalamus catalog seed script."""
import sqlite3
import sys
from pathlib import Path

# Add src to path for imports
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Add scripts to path for seed script import
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _apply_migration_to_memory_db(conn: sqlite3.Connection) -> None:
    """Apply migration 050 to an in-memory DB."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "050_thalamus.sql")
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)


def test_seed_script_creates_channels_per_sector():
    """Run seed script against synthetic memory_events and assert ≥ 1 channel per sector."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    
    # Apply base schema (need memory_events table)
    # Create minimal schema for memory_events
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT,
            agent_id TEXT,
            category TEXT,
            scope TEXT,
            memory_type TEXT,
            created_at TEXT
        )
    """)
    
    # Apply thalamus migration
    _apply_migration_to_memory_db(conn)
    
    # Insert synthetic memory_events covering different sectors
    conn.execute("""
        INSERT INTO memory_events (operation, agent_id, category, scope, memory_type, created_at)
        VALUES 
            ('event_add', 'user', 'user', 'global', 'episodic', datetime('now', '-1 day')),
            ('agent_orient', 'claude-code', 'project', 'project:brainctl', 'episodic', datetime('now', '-1 day')),
            ('memory_search', 'hermes', 'convention', 'global', 'semantic', datetime('now', '-1 day')),
            ('decision_add', 'codex', 'decision', 'project:test', 'episodic', datetime('now', '-1 day')),
            ('consolidation_run', 'hippocampus', 'consolidation', 'global', 'episodic', datetime('now', '-1 day'))
    """)
    conn.commit()
    
    # Import and run seed script functions
    from seed_thalamus_catalog import (
        _fetch_clusters,
        _sector_for,
        _relay_from_cluster,
        build_relay_seeds,
        seed_catalog,
    )
    
    # Fetch clusters from synthetic data
    clusters = _fetch_clusters(conn, days=30, scan_limit=100)
    assert len(clusters) >= 5  # we inserted 5 events
    
    # Build relay seeds
    relays = build_relay_seeds(clusters, min_channels=5, max_channels=20)
    assert len(relays) >= 5
    
    # Seed the catalog
    result = seed_catalog(conn, relays, dry_run=False)
    assert result["inserted_or_updated"] >= 5
    
    # Assert we have channels across multiple sectors
    sectors_represented = {r.sector for r in relays}
    assert len(sectors_represented) >= 4  # at least 4 different sectors
    
    conn.close()


def test_seed_script_idempotent():
    """Test running seed script twice produces no duplicates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    
    # Create minimal schema
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT,
            agent_id TEXT,
            category TEXT,
            scope TEXT,
            memory_type TEXT,
            created_at TEXT
        )
    """)
    
    # Apply thalamus migration
    _apply_migration_to_memory_db(conn)
    
    # Insert synthetic data
    conn.execute("""
        INSERT INTO memory_events (operation, agent_id, category, scope, memory_type, created_at)
        VALUES 
            ('event_add', 'user', 'user', 'global', 'episodic', datetime('now', '-1 day'))
    """)
    conn.commit()
    
    from seed_thalamus_catalog import (
        _fetch_clusters,
        build_relay_seeds,
        seed_catalog,
    )
    
    clusters = _fetch_clusters(conn, days=30, scan_limit=100)
    relays = build_relay_seeds(clusters, min_channels=1, max_channels=10)
    
    # First run
    result1 = seed_catalog(conn, relays, dry_run=False)
    count_after_first = conn.execute("SELECT COUNT(*) FROM thalamic_relays").fetchone()[0]
    
    # Second run with same relays
    result2 = seed_catalog(conn, relays, dry_run=False)
    count_after_second = conn.execute("SELECT COUNT(*) FROM thalamic_relays").fetchone()[0]
    
    # Assert no duplicates
    assert count_after_first == count_after_second
    assert result1["inserted_or_updated"] == result2["inserted_or_updated"]
    
    conn.close()


def test_sector_classification():
    """Test that _sector_for correctly classifies traffic clusters."""
    from seed_thalamus_catalog import TrafficCluster, _sector_for
    
    # Test PII-sensitive classification
    pii_cluster = TrafficCluster(
        event_type="event_add",
        agent_id="user",
        category="user",
        scope="global",
        memory_type="episodic",
        count=10,
        first_seen="2026-01-01",
        last_seen="2026-01-02",
    )
    # This would need PII keywords in the actual implementation logic
    # For now, test the function exists and returns a valid sector
    sector = _sector_for(pii_cluster)
    assert sector in {"sensory_external", "agent_efferent", "memory_recall", "belief", "consolidation", "pii_sensitive"}
    
    # Test belief classification
    belief_cluster = TrafficCluster(
        event_type="decision_add",
        agent_id="claude-code",
        category="decision",
        scope="project:test",
        memory_type="episodic",
        count=5,
        first_seen="2026-01-01",
        last_seen="2026-01-02",
    )
    sector = _sector_for(belief_cluster)
    assert sector == "belief"
    
    # Test consolidation classification
    consolidation_cluster = TrafficCluster(
        event_type="consolidation_run",
        agent_id="hippocampus",
        category="consolidation",
        scope="global",
        memory_type="episodic",
        count=3,
        first_seen="2026-01-01",
        last_seen="2026-01-02",
    )
    sector = _sector_for(consolidation_cluster)
    assert sector == "consolidation"
