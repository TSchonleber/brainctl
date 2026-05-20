"""Tests for mcp_tools_memory_aging — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_078 = REPO_ROOT / "db" / "migrations" / "078_memory_aging.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_078.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_memory_aging as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_with_defaults(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        state = conn.execute(
            "SELECT capture_window_hours, demotion_tier, enforcement_mode FROM memory_aging_state"
        ).fetchone()
        assert state == (24, "unconsolidated", "shadow")
    finally:
        conn.close()


def test_tag_creates_with_deadline(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_memory_tag(memory_id=42)
    assert out["ok"] is True
    assert out["preexisting"] is False
    assert out["tag"]["status"] == "tagged"


def test_tag_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    first = mod.tool_memory_tag(memory_id=42, window_hours=12)
    second = mod.tool_memory_tag(memory_id=42, window_hours=99)  # ignored
    assert first["tag"]["id"] == second["tag"]["id"]
    assert second["preexisting"] is True


def test_capture_flips_tag(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_memory_tag(memory_id=42)
    out = mod.tool_memory_capture(memory_id=42, capture_kind="recall", agent_id="a1")
    assert out["ok"] is True
    assert out["flipped_to_captured"] is True
    tag = mod.tool_memory_tag_get(memory_id=42)
    assert tag["tag"]["status"] == "captured"
    assert tag["tag"]["capture_count"] == 1


def test_capture_increments_count_after_first(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_memory_tag(memory_id=42)
    mod.tool_memory_capture(memory_id=42)
    mod.tool_memory_capture(memory_id=42)
    tag = mod.tool_memory_tag_get(memory_id=42)
    assert tag["tag"]["capture_count"] == 2
    assert tag["tag"]["status"] == "captured"  # still captured


def test_capture_validates_kind(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_memory_capture(memory_id=1, capture_kind="bogus")
    assert "error" in out


def test_sweep_shadow_counts_only(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Force-expire by creating a tag and then setting deadline in the past
    mod.tool_memory_tag(memory_id=99)
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    try:
        conn.execute(
            "UPDATE memory_tags SET capture_deadline = datetime('now', '-1 hour') WHERE memory_id = 99"
        )
        conn.commit()
    finally:
        conn.close()
    out = mod.tool_memory_aging_sweep()
    assert out["swept_count"] == 1
    assert out["demoted_count"] == 0  # shadow
    # Tag should still be 'tagged' in shadow mode
    tag = mod.tool_memory_tag_get(memory_id=99)
    assert tag["tag"]["status"] == "tagged"


def test_sweep_enforce_demotes(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_memory_aging_set(enforcement_mode="enforce")
    mod.tool_memory_tag(memory_id=99)
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    try:
        conn.execute(
            "UPDATE memory_tags SET capture_deadline = datetime('now', '-1 hour') WHERE memory_id = 99"
        )
        conn.commit()
    finally:
        conn.close()
    out = mod.tool_memory_aging_sweep()
    assert out["swept_count"] == 1
    assert out["demoted_count"] == 1
    tag = mod.tool_memory_tag_get(memory_id=99)
    assert tag["tag"]["status"] == "expired"


def test_set_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_memory_aging_set(capture_window_hours=0)
    assert "error" in mod.tool_memory_aging_set(demotion_tier="bogus")
    assert "error" in mod.tool_memory_aging_set(enforcement_mode="bogus")
    assert "error" in mod.tool_memory_aging_set()


def test_status_aggregates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_memory_tag(memory_id=1)
    mod.tool_memory_tag(memory_id=2)
    mod.tool_memory_capture(memory_id=1)
    out = mod.tool_memory_aging_status()
    assert out["ok"] is True
    by_status = {row["status"]: row["n"] for row in out["tags_by_status"]}
    assert by_status.get("captured") == 1
    assert by_status.get("tagged") == 1
    assert out["state"]["total_tags"] >= 2
