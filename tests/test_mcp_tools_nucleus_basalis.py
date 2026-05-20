"""Tests for mcp_tools_nucleus_basalis — Phase 1.

Covers:
  - Migration applies + seeds populate as expected
  - nb_status returns sensible defaults on a fresh DB
  - nb_register_target is idempotent
  - nb_fire round-trip writes a firing + updates nb_state
  - nb_attend_sector convenience wrapper resolves the sector
  - nb_signal_history filters and paginates correctly
  - Validation: invalid channel_kind / firing mode rejected
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_068 = REPO_ROOT / "db" / "migrations" / "068_nucleus_basalis.sql"


def _bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema_version + bg_modulators so migration 068 applies."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT
        );
        CREATE TABLE IF NOT EXISTS bg_modulators (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            tonic_da REAL NOT NULL DEFAULT 0.5,
            lc_ne REAL NOT NULL DEFAULT 0.5,
            serotonin REAL NOT NULL DEFAULT 0.5,
            set_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );
        INSERT OR IGNORE INTO bg_modulators (id) VALUES (1);
        """
    )


def _apply_migration(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap_schema(conn)
        conn.executescript(MIGRATION_068.read_text())
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def nb_db(tmp_path, monkeypatch):
    """Build a fresh DB with migration 068 applied and point the module
    at it."""
    db = tmp_path / "brain.db"
    _apply_migration(db)
    # Late import so the monkeypatch sticks before DB_PATH module-level
    # capture would matter.
    from agentmemory import mcp_tools_nucleus_basalis as nb_mod
    monkeypatch.setattr(nb_mod, "DB_PATH", db)
    return nb_mod


def test_migration_applies_and_seeds(tmp_path):
    db = tmp_path / "brain.db"
    _apply_migration(db)
    conn = sqlite3.connect(str(db))
    try:
        # 4 seeded thalamic sectors
        targets = conn.execute(
            "SELECT name, channel_kind, default_ach_gain FROM nb_attention_targets ORDER BY id"
        ).fetchall()
        names = [t[0] for t in targets]
        assert names == ["cognitive", "episodic", "semantic", "pii_sensitive"]
        assert all(t[1] == "thalamic_sector" for t in targets)
        # Single nb_state row
        state = conn.execute("SELECT id, mode, ach_reservoir FROM nb_state").fetchone()
        assert state == (1, "tonic_mid", 0.5)
        # bg_modulators gained the acetylcholine column
        ach = conn.execute("SELECT acetylcholine FROM bg_modulators WHERE id = 1").fetchone()
        assert ach[0] == 0.5
        # schema_version row
        sv = conn.execute("SELECT version FROM schema_version WHERE version=68").fetchone()
        assert sv == (68,)
    finally:
        conn.close()


def test_nb_status_empty_db(nb_db):
    out = nb_db.tool_nb_status()
    assert out["ok"] is True
    assert out["state"]["mode"] == "tonic_mid"
    assert out["recent_24h"]["n"] == 0
    assert out["last_firings"] == []
    assert out["registered_targets"] == 4  # seeded sectors


def test_nb_register_target_idempotent(nb_db):
    first = nb_db.tool_nb_register_target(
        name="agents.research", channel_kind="agent_scope",
        default_ach_gain=0.12, description="research-bot scope",
    )
    second = nb_db.tool_nb_register_target(
        name="agents.research", channel_kind="agent_scope",
        default_ach_gain=0.12,
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["target"]["id"] == second["target"]["id"]
    # Second call should preserve description from the first via COALESCE.
    assert second["target"]["description"] == "research-bot scope"


def test_nb_register_target_validates_channel_kind(nb_db):
    out = nb_db.tool_nb_register_target(
        name="bogus", channel_kind="not-a-real-kind"
    )
    assert "error" in out


def test_nb_fire_round_trip_updates_state(nb_db):
    out = nb_db.tool_nb_fire(
        target_name="cognitive",
        attention_magnitude=0.8,
        agent_id="test-agent",
        mode="phasic",
    )
    assert out["ok"] is True
    # default_ach_gain for cognitive = 0.15 → 0.15 × 0.8 = 0.12
    assert out["ach_delta_applied"] == pytest.approx(0.12, abs=1e-4)
    # State should now reflect the firing
    status = nb_db.tool_nb_status(agent_id="test-agent")
    assert status["state"]["last_phasic_at"] is not None
    assert status["state"]["last_attended_target_id"] is not None
    assert status["recent_24h"]["n"] == 1


def test_nb_fire_rejects_unregistered_target(nb_db):
    out = nb_db.tool_nb_fire(
        target_name="nope-not-real", attention_magnitude=0.5
    )
    assert "error" in out


def test_nb_fire_validates_mode(nb_db):
    out = nb_db.tool_nb_fire(
        target_name="cognitive", attention_magnitude=0.5, mode="invalid-mode"
    )
    assert "error" in out


def test_nb_attend_sector_resolves_and_fires(nb_db):
    out = nb_db.tool_nb_attend_sector(
        sector_name="pii_sensitive",
        attention_magnitude=1.0,
        agent_id="pii-test",
    )
    assert out["ok"] is True
    # pii_sensitive default_ach_gain = 0.20 → 0.20 × 1.0 = 0.20
    assert out["ach_delta_applied"] == pytest.approx(0.20, abs=1e-4)
    assert out["mode"] == "phasic"


def test_nb_attend_sector_rejects_non_thalamic(nb_db):
    # Register an agent_scope target with a name that LOOKS sector-y.
    nb_db.tool_nb_register_target(
        name="not-a-sector", channel_kind="agent_scope",
        default_ach_gain=0.1,
    )
    out = nb_db.tool_nb_attend_sector(
        sector_name="not-a-sector", attention_magnitude=0.5
    )
    assert "error" in out


def test_nb_signal_history_filters_and_limits(nb_db):
    # Three firings, two for one agent
    for i in range(3):
        nb_db.tool_nb_fire(
            target_name="cognitive",
            attention_magnitude=0.5,
            agent_id="agent-a" if i < 2 else "agent-b",
        )
    all_history = nb_db.tool_nb_signal_history(limit=10)
    assert len(all_history["history"]) == 3
    a_only = nb_db.tool_nb_signal_history(limit=10, agent_id="agent-a")
    assert len(a_only["history"]) == 2
    limit_one = nb_db.tool_nb_signal_history(limit=1)
    assert len(limit_one["history"]) == 1
