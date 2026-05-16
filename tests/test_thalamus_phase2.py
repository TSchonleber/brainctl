"""Smoke tests for thalamus Phase 2 write tools + shadow consult."""
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_thalamus import (
    tool_thalamus_gate_set,
    tool_thalamus_burst,
    tool_thalamus_mode_set,
    tool_thalamus_shadow_stats,
    tool_thalamus_relay_create,
)
from agentmemory.thalamus_shadow import consult_for_write, _sector_for_scope_category


class _NoCloseConn:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply migrations 050 + 053 to an in-memory DB."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    for migration in (
        "/Users/r4vager/agentmemory/db/migrations/050_thalamus.sql",
        "/Users/r4vager/agentmemory/db/migrations/053_thalamus_shadow.sql",
    ):
        with open(migration) as f:
            conn.executescript(f.read())


def _patched_module(conn):
    """Mock the open_db helper so tools see our in-memory conn."""
    import agentmemory.mcp_tools_thalamus as m
    wrapped = _NoCloseConn(conn)
    m.open_db = lambda x: wrapped  # type: ignore[assignment]
    return m


def test_thalamus_mode_set_switches_mode_and_dials():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    _patched_module(conn)

    r = tool_thalamus_mode_set(mode="wake_exploratory", set_by="test", arousal=0.8, acetylcholine=0.7)
    assert r["ok"] is True
    assert r["mode"]["mode"] == "wake_exploratory"
    assert r["mode"]["arousal"] == 0.8
    assert r["mode"]["set_by"] == "test"

    # Invalid mode rejected
    bad = tool_thalamus_mode_set(mode="not_a_mode", set_by="test")
    assert bad["ok"] is False
    assert "must be one of" in bad["error"]


def test_thalamus_gate_set_requires_existing_channel():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    _patched_module(conn)

    # No relay yet → gate_set fails
    miss = tool_thalamus_gate_set(channel_id="missing", suppression=0.5)
    assert miss["ok"] is False
    assert "no thalamic_gate row" in miss["error"]

    # Create the relay, then set the gate
    tool_thalamus_relay_create(
        channel_id="test/chan",
        sector="memory_recall",
        driver_source="test",
        target="t",
        transport="higher_order",
    )
    ok = tool_thalamus_gate_set(channel_id="test/chan", suppression=0.6, topdown_bias=0.4, bias_source="test")
    assert ok["ok"] is True
    assert ok["gate"]["suppression"] == 0.6
    assert ok["gate"]["topdown_bias"] == 0.4
    assert ok["gate"]["bias_source"] == "test"


def test_thalamus_burst_records_event_and_disarms():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    _patched_module(conn)

    tool_thalamus_relay_create(
        channel_id="burst/chan", sector="memory_recall",
        driver_source="t", target="t", transport="higher_order",
    )
    tool_thalamus_gate_set(channel_id="burst/chan", armed_for_burst=True)

    r = tool_thalamus_burst(channel_id="burst/chan", payload_ref="memory:1", reason="novelty", salience=0.9)
    assert r["ok"] is True
    assert r["burst"]["salience"] == 0.9
    # armed_for_burst should be reset to 0 after firing
    armed = conn.execute("SELECT armed_for_burst FROM thalamic_gate WHERE channel_id='burst/chan'").fetchone()[0]
    assert armed == 0

    # Invalid reason rejected
    bad = tool_thalamus_burst(channel_id="burst/chan", reason="nonsense")
    assert bad["ok"] is False


def test_shadow_consult_classifies_sectors():
    # Pure sector classification — no DB needed
    assert _sector_for_scope_category("project:wallet", "environment") == "pii_sensitive"
    assert _sector_for_scope_category("global", "decision") == "belief"
    assert _sector_for_scope_category("global", "consolidation") == "consolidation"
    assert _sector_for_scope_category("global", "user") == "sensory_external"
    assert _sector_for_scope_category("global", "convention") == "memory_recall"


def test_shadow_consult_records_decisions():
    import tempfile, os
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    try:
        conn = sqlite3.connect(tmpf.name)
        conn.row_factory = sqlite3.Row
        _apply_migrations(conn)
        conn.close()

        # Default state (no suppression) → pass
        d = consult_for_write(scope="global", category="convention", surprise_score=0.5, db_path=tmpf.name)
        assert d is not None
        assert d["decision"] == "pass"
        assert d["sector"] == "memory_recall"

        # Seed a high-suppression gate row and re-consult
        conn = sqlite3.connect(tmpf.name)
        conn.execute("INSERT INTO thalamic_gate (channel_id, sector, suppression) VALUES ('x', 'memory_recall', 0.9)")
        conn.commit()
        conn.close()
        d = consult_for_write(scope="global", category="convention", surprise_score=0.5, db_path=tmpf.name)
        assert d is not None
        assert d["decision"] == "tier_downgrade"
        assert d["suppression"] == 0.9
    finally:
        os.unlink(tmpf.name)


def test_shadow_stats_summary():
    import tempfile, os
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    try:
        conn = sqlite3.connect(tmpf.name)
        conn.row_factory = sqlite3.Row
        _apply_migrations(conn)
        conn.close()

        # Generate a few shadow decisions
        for _ in range(3):
            consult_for_write(scope="global", category="convention", surprise_score=0.5, db_path=tmpf.name)
        # Seed a suppressed gate then consult once more — should record tier_downgrade
        conn = sqlite3.connect(tmpf.name)
        conn.execute("INSERT INTO thalamic_gate (channel_id, sector, suppression) VALUES ('y', 'memory_recall', 0.9)")
        conn.commit()
        conn.close()
        consult_for_write(scope="global", category="convention", surprise_score=0.5, db_path=tmpf.name)

        # Inspect via shadow_stats — mock the module's open_db
        conn = sqlite3.connect(tmpf.name)
        conn.row_factory = sqlite3.Row
        _patched_module(conn)
        r = tool_thalamus_shadow_stats(days=7)
        assert r["ok"] is True
        assert r["total_decisions"] == 4
        decisions = {d["decision"]: d["n"] for d in r["by_decision"]}
        assert decisions.get("pass", 0) == 3
        assert decisions.get("tier_downgrade", 0) == 1
        assert r["divergence_rate"] == 0.25
    finally:
        os.unlink(tmpf.name)
