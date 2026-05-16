"""Tests for hippocampal subfields Phase 1."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_hippocampal_subfields import (
    tool_hippocampus_dg_separate, tool_hippocampus_dg_check,
    tool_hippocampus_ca3_complete, tool_hippocampus_subfields_status,
    _decision_for_distance, VALID_DECISIONS,
)


class _NoCloseConn:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


def _setup_tempdb() -> str:
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    conn = sqlite3.connect(tmpf.name)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    with open(str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "059_hippocampal_subfields.sql")) as f:
        conn.executescript(f.read())
    conn.close()
    return tmpf.name


def _patched(conn):
    import agentmemory.mcp_tools_hippocampal_subfields as m
    m.open_db = lambda x: _NoCloseConn(conn)  # type: ignore[assignment]
    return m


def test_migration_creates_both_tables():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'hippocampus_%'"
        ).fetchall()}
        assert tables == {"hippocampus_pattern_separations", "hippocampus_completion_traces"}
        conn.close()
    finally:
        os.unlink(db)


def test_dg_decision_thresholds():
    # similarity ≥0.97 → deduplicate; ≥0.85 → separate; otherwise passthrough
    d, tag = _decision_for_distance(0.02)
    assert d == "deduplicate"
    assert tag is None
    d, tag = _decision_for_distance(0.10)
    assert d == "separate"
    assert tag is not None
    d, tag = _decision_for_distance(0.50)
    assert d == "passthrough"
    assert tag is None


def test_dg_check_is_read_only():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        r = tool_hippocampus_dg_check(cosine_distance=0.20)
        assert r["ok"] is True
        assert r["decision"] in VALID_DECISIONS
        # No rows written
        count = conn.execute("SELECT COUNT(*) FROM hippocampus_pattern_separations").fetchone()[0]
        assert count == 0
        conn.close()
    finally:
        os.unlink(db)


def test_dg_separate_auto_decision_when_omitted():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        r = tool_hippocampus_dg_separate(
            memory_id=1, nearest_neighbor_id=2,
            cosine_distance=0.10, scope="x", agent_id="a",
        )
        assert r["ok"] is True
        assert r["decision"] == "separate"
        assert r["separation_tag"] is not None
        conn.close()
    finally:
        os.unlink(db)


def test_ca3_complete_logs_trace():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        r = tool_hippocampus_ca3_complete(
            query_hash="abc", completed_to_memory_id=42, distance=0.15, rank=1,
        )
        assert r["ok"] is True
        assert r["trace_id"] == 1
        conn.close()
    finally:
        os.unlink(db)
