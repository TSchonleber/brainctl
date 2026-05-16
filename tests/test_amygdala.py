"""Tests for amygdala Phase 1: valence tags + reconsolidation + extinction."""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_amygdala import (
    tool_amygdala_status, tool_amygdala_tag, tool_amygdala_query_valence,
    tool_amygdala_extinguish, _saturating_update, _MAX_SINGLE_UPDATE,
    VALID_TARGET_KINDS,
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
    with open(str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "058_amygdala.sql")) as f:
        conn.executescript(f.read())
    conn.close()
    return tmpf.name


def _patched(conn):
    import agentmemory.mcp_tools_amygdala as m
    m.open_db = lambda x: _NoCloseConn(conn)  # type: ignore[assignment]
    return m


def test_migration_creates_three_tables():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'amygdala_%'"
        ).fetchall()}
        assert tables == {
            "amygdala_valence_tags",
            "amygdala_valence_events",
            "amygdala_extinction_gates",
        }
        conn.close()
    finally:
        os.unlink(db)


def test_saturating_update_caps_single_event():
    # No matter how extreme the delta, the single update is capped at ±0.5
    assert _saturating_update(0.0, 100.0, 1.0) <= _MAX_SINGLE_UPDATE + 1e-9
    assert _saturating_update(0.0, -100.0, 1.0) >= -_MAX_SINGLE_UPDATE - 1e-9
    # Final value is bounded to [-1, 1]
    assert _saturating_update(0.95, 100.0, 1.0) <= 1.0
    assert _saturating_update(-0.95, -100.0, 1.0) >= -1.0


def test_tag_validates_target_kind():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        bad = tool_amygdala_tag(target_kind="bogus", target_id="x", valence=-0.5)
        assert bad["ok"] is False
        assert "target_kind" in bad["error"]
        conn.close()
    finally:
        os.unlink(db)


def test_tag_one_shot_with_saturation():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        r = tool_amygdala_tag(
            target_kind="entity", target_id="test-target",
            valence=-1.0, arousal=1.0, reason="extreme aversive event",
        )
        assert r["ok"] is True
        # Even at max delta the single-event change is bounded
        assert abs(r["new_valence"]) <= _MAX_SINGLE_UPDATE + 1e-9
        conn.close()
    finally:
        os.unlink(db)


def test_query_opens_labile_window_and_tag_uses_higher_lr():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        tool_amygdala_tag(target_kind="agent", target_id="agent-X",
                          valence=-0.5, arousal=0.8)
        # First tag uses default learning rate (0.1)
        r1 = tool_amygdala_tag(target_kind="agent", target_id="agent-X",
                               valence=-0.5, arousal=0.8)
        assert r1["learning_rate"] == 0.1
        assert r1["was_labile"] is False
        # Query opens the labile window
        q = tool_amygdala_query_valence(target_kind="agent", target_id="agent-X")
        assert q["labile_for_seconds"] == 3600
        # Subsequent tag should be labile with elevated LR
        r2 = tool_amygdala_tag(target_kind="agent", target_id="agent-X",
                               valence=-0.5, arousal=0.8)
        assert r2["was_labile"] is True
        assert r2["learning_rate"] == 0.4
        conn.close()
    finally:
        os.unlink(db)


def test_extinction_gate_reduces_effective_valence():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        # Build a strongly aversive tag
        for _ in range(5):
            tool_amygdala_tag(target_kind="entity", target_id="snake",
                              valence=-0.9, arousal=0.95)
        # Query without context — no gates active
        before = tool_amygdala_query_valence(target_kind="entity", target_id="snake")
        # Install extinction gate for the "at_zoo" context
        e = tool_amygdala_extinguish(
            target_kind="entity", target_id="snake",
            context_hash="ctx:at_zoo", suppression_level=0.8,
        )
        assert e["ok"] is True
        assert e["suppression_level"] == 0.8
        # Query in that context — effective should be reduced
        after = tool_amygdala_query_valence(
            target_kind="entity", target_id="snake",
            context_hash="ctx:at_zoo",
        )
        assert after["max_suppression"] == 0.8
        # Effective = raw × (1 - 0.8) = raw × 0.2
        assert abs(after["effective_valence"] - 0.2 * after["raw_valence"]) < 1e-9
        # The raw valence is UNCHANGED — extinction is overlay not erasure
        assert abs(after["raw_valence"] - before["raw_valence"]) < 1e-9
        conn.close()
    finally:
        os.unlink(db)


def test_extinction_strengthens_with_repeated_safe_exposures():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        tool_amygdala_tag(target_kind="entity", target_id="x", valence=-0.7, arousal=0.9)
        # First exposure at suppression 0.3
        e1 = tool_amygdala_extinguish(target_kind="entity", target_id="x",
                                       context_hash="c1", suppression_level=0.3)
        assert e1["suppression_level"] == 0.3
        assert e1["n_safe_exposures"] == 1
        # Next exposure at suppression 0.6 — should max() and increment
        e2 = tool_amygdala_extinguish(target_kind="entity", target_id="x",
                                       context_hash="c1", suppression_level=0.6)
        assert e2["suppression_level"] == 0.6
        assert e2["n_safe_exposures"] == 2
        # Even a weaker exposure doesn't reduce: max() rule
        e3 = tool_amygdala_extinguish(target_kind="entity", target_id="x",
                                       context_hash="c1", suppression_level=0.4)
        assert e3["suppression_level"] == 0.6
        assert e3["n_safe_exposures"] == 3
        conn.close()
    finally:
        os.unlink(db)


def test_status_aggregates_kinds_and_extinctions():
    db = _setup_tempdb()
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _patched(conn)
        tool_amygdala_tag(target_kind="entity", target_id="e1", valence=-0.5, arousal=0.8)
        tool_amygdala_tag(target_kind="agent", target_id="a1", valence=0.7, arousal=0.6)
        tool_amygdala_extinguish(target_kind="entity", target_id="e1",
                                  context_hash="ctx", suppression_level=0.5)
        s = tool_amygdala_status(top_n=10)
        assert s["ok"] is True
        kinds_seen = {r["target_kind"] for r in s["tag_counts"]}
        assert kinds_seen == {"entity", "agent"}
        assert len(s["recent_events"]) >= 2
        assert any(r["target_kind"] == "entity" for r in s["extinction_summary"])
        conn.close()
    finally:
        os.unlink(db)


def test_valid_target_kinds_set():
    assert VALID_TARGET_KINDS == {"entity", "agent", "context"}
