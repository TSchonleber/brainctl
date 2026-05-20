"""Tests for mcp_tools_workspace_bandwidth — Phase 1."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_072 = REPO_ROOT / "db" / "migrations" / "072_workspace_bandwidth.sql"


def _bootstrap(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT
        );
        """
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_072.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_workspace_bandwidth as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_applies_with_defaults(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        state = conn.execute(
            "SELECT bandwidth_limit, enforcement_mode, epoch_count FROM workspace_bandwidth_state"
        ).fetchone()
        assert state == (4, "shadow", 0)
        sv = conn.execute("SELECT version FROM schema_version WHERE version=72").fetchone()
        assert sv == (72,)
    finally:
        conn.close()


def test_status_returns_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_workspace_bandwidth_status()
    assert out["ok"] is True
    assert out["state"]["bandwidth_limit"] == 4
    assert out["state"]["enforcement_mode"] == "shadow"
    assert out["last_5_epochs"] == []


def test_admit_increments_counter(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    for i in range(3):
        out = mod.tool_workspace_bandwidth_admit()
        assert out["ok"] is True
        assert out["admitted"] is True
        assert out["epoch_count"] == i + 1
        assert out["saturated"] is False


def test_admit_saturates_above_limit_shadow_mode(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    for _ in range(4):
        mod.tool_workspace_bandwidth_admit()
    # 5th admit: still admitted in shadow mode but `saturated=True`
    out = mod.tool_workspace_bandwidth_admit()
    assert out["admitted"] is True
    assert out["saturated"] is True


def test_admit_rejects_in_enforce_mode(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_workspace_bandwidth_set(enforcement_mode="enforce")
    for _ in range(4):
        admitted = mod.tool_workspace_bandwidth_admit()
        assert admitted["admitted"] is True
    rejected = mod.tool_workspace_bandwidth_admit()
    assert rejected["admitted"] is False
    assert rejected["rejected"] is True


def test_set_updates_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_workspace_bandwidth_set(
        bandwidth_limit=10, epoch_duration_seconds=30, enforcement_mode="enforce"
    )
    assert out["ok"] is True
    assert out["state"]["bandwidth_limit"] == 10
    assert out["state"]["epoch_duration_seconds"] == 30
    assert out["state"]["enforcement_mode"] == "enforce"


def test_set_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_workspace_bandwidth_set(bandwidth_limit=0)
    assert "error" in mod.tool_workspace_bandwidth_set(epoch_duration_seconds=-1)
    assert "error" in mod.tool_workspace_bandwidth_set(enforcement_mode="bogus")
    assert "error" in mod.tool_workspace_bandwidth_set()  # nothing passed


def test_rotation_creates_epoch_row(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Set duration to 1s, admit twice, sleep 2s, status should rotate.
    mod.tool_workspace_bandwidth_set(epoch_duration_seconds=1)
    mod.tool_workspace_bandwidth_admit()
    mod.tool_workspace_bandwidth_admit()
    time.sleep(1.2)
    out = mod.tool_workspace_bandwidth_status()
    assert out["rotation"].get("rotated") is True
    assert out["rotation"]["admitted"] == 2
    history = mod.tool_workspace_bandwidth_epochs_history(limit=10)
    assert len(history["epochs"]) == 1
    assert history["epochs"][0]["admitted_count"] == 2


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_workspace_bandwidth_set(epoch_duration_seconds=1, bandwidth_limit=2)
    for _ in range(3):
        mod.tool_workspace_bandwidth_admit()
    time.sleep(1.2)
    mod.tool_workspace_bandwidth_status()  # rotate
    high_sat = mod.tool_workspace_bandwidth_epochs_history(limit=10, min_saturation=1.0)
    assert len(high_sat["epochs"]) == 1
    assert high_sat["epochs"][0]["saturation"] >= 1.0
