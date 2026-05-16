"""Smoke tests for thalamus MCP tools (Phase 1 read-only)."""
import sqlite3
import sys
from pathlib import Path

# Add src to path for imports
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_thalamus import (
    tool_thalamus_status,
    tool_thalamus_salience,
    tool_thalamus_relay_create,
)


def _apply_migration_to_memory_db(conn: sqlite3.Connection) -> None:
    """Apply migration 050 to an in-memory DB."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    migration_path = "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql"
    with open(migration_path) as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)


def test_thalamus_status_empty_db():
    """Test thalamus_status returns valid dict against empty DB."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration_to_memory_db(conn)
    
    # Mock the DB path to use in-memory
    import agentmemory.mcp_tools_thalamus as thalamus_module
    original_open_db = thalamus_module.open_db
    thalamus_module.open_db = lambda x: conn
    
    try:
        result = tool_thalamus_status()
        assert result["ok"] is True
        assert "mode" in result
        assert result["mode"]["mode"] == "wake_focused"
        assert result["relay_count"] == 0
        assert result["gate_count"] == 0
    finally:
        thalamus_module.open_db = original_open_db
        conn.close()


def test_thalamus_status_seeded_db():
    """Test thalamus_status returns valid dict against seeded DB."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration_to_memory_db(conn)
    
    # Seed a relay
    conn.execute(
        """
        INSERT INTO thalamic_relays (channel_id, sector, driver_source, target, transport)
        VALUES ('test-channel', 'memory_recall', 'test', 'memory_search', 'higher_order')
        """
    )
    conn.execute(
        """
        INSERT INTO thalamic_gate (channel_id, sector)
        VALUES ('test-channel', 'memory_recall')
        """
    )
    conn.commit()
    
    import agentmemory.mcp_tools_thalamus as thalamus_module
    original_open_db = thalamus_module.open_db
    thalamus_module.open_db = lambda x: conn
    
    try:
        result = tool_thalamus_status()
        assert result["ok"] is True
        assert result["relay_count"] == 1
        assert result["gate_count"] == 1
    finally:
        thalamus_module.open_db = original_open_db
        conn.close()


def test_thalamus_salience_basic():
    """Test thalamus_salience with synthetic candidates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration_to_memory_db(conn)
    
    import agentmemory.mcp_tools_thalamus as thalamus_module
    original_open_db = thalamus_module.open_db
    thalamus_module.open_db = lambda x: conn
    
    try:
        candidates = [
            {"content": "test memory 1", "novelty": 0.8},
            {"content": "test memory 2", "novelty": 0.5},
        ]
        result = tool_thalamus_salience(candidates, agent_id="test-agent")
        
        assert isinstance(result, list)
        assert len(result) == 2
        assert all("candidate_id" in r for r in result)
        assert all("integrated" in r for r in result)
        assert result[0]["integrated"] >= result[1]["integrated"]  # sorted by integrated desc
    finally:
        thalamus_module.open_db = original_open_db
        conn.close()


class _NoCloseConn:
    """Wraps a sqlite3.Connection but makes close() a no-op so a single
    in-memory DB can survive multiple tool invocations during a test."""
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_thalamus_relay_create_idempotent():
    """Test thalamus_relay_create is idempotent (same channel_id = 1 row)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration_to_memory_db(conn)
    wrapped = _NoCloseConn(conn)

    import agentmemory.mcp_tools_thalamus as thalamus_module
    original_open_db = thalamus_module.open_db
    thalamus_module.open_db = lambda x: wrapped
    
    try:
        # First call
        result1 = tool_thalamus_relay_create(
            channel_id="test-idempotent",
            sector="memory_recall",
            driver_source="test",
            target="memory_search",
            transport="higher_order",
        )
        assert result1["ok"] is True
        
        # Second call with same channel_id
        result2 = tool_thalamus_relay_create(
            channel_id="test-idempotent",
            sector="memory_recall",
            driver_source="test",
            target="memory_search",
            transport="higher_order",
        )
        assert result2["ok"] is True
        
        # Assert only 1 row exists
        count = conn.execute("SELECT COUNT(*) FROM thalamic_relays WHERE channel_id='test-idempotent'").fetchone()[0]
        assert count == 1
    finally:
        thalamus_module.open_db = original_open_db
        conn.close()


def test_thalamus_relay_create_invalid_transport():
    """Test thalamus_relay_create validates transport enum."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migration_to_memory_db(conn)
    
    import agentmemory.mcp_tools_thalamus as thalamus_module
    original_open_db = thalamus_module.open_db
    thalamus_module.open_db = lambda x: conn
    
    try:
        result = tool_thalamus_relay_create(
            channel_id="test-invalid",
            sector="memory_recall",
            driver_source="test",
            target="memory_search",
            transport="invalid_transport",  # invalid
        )
        assert result["ok"] is False
        assert "transport must be" in result["error"]
    finally:
        thalamus_module.open_db = original_open_db
        conn.close()
