"""Tests for mcp_tools_hippocampus_ca1 — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_071 = REPO_ROOT / "db" / "migrations" / "071_hippocampus_ca1_subiculum.sql"


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
        conn.executescript(MIGRATION_071.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_hippocampus_ca1 as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_applies_with_seed_state(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        state = conn.execute(
            "SELECT recent_match_rate, recent_novelty_rate, total_comparisons FROM hippocampus_ca1_state"
        ).fetchone()
        assert state == (0.5, 0.5, 0)
        sv = conn.execute("SELECT version FROM schema_version WHERE version=71").fetchone()
        assert sv == (71,)
    finally:
        conn.close()


def test_ca1_compare_match_classification(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Identical hashes → match_score=1.0 → 'match'
    out = mod.tool_ca1_compare(
        ec_input_hash="abc123def456",
        ca3_output_hash="abc123def456",
        agent_id="a1",
    )
    assert out["ok"] is True
    assert out["match_score"] == 1.0
    assert out["novelty_score"] == 0.0
    assert out["classification"] == "match"


def test_ca1_compare_mismatch_classification(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Fully disjoint hashes → match_score < 0.15 → 'mismatch'
    out = mod.tool_ca1_compare(
        ec_input_hash="aaaaaaaaaaaa",
        ca3_output_hash="bbbbbbbbbbbb",
        agent_id="a1",
    )
    assert out["ok"] is True
    assert out["match_score"] == 0.0
    assert out["classification"] == "mismatch"


def test_ca1_compare_partial_classification(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # 6/12 chars match → 0.5 → 'ambiguous'
    out = mod.tool_ca1_compare(
        ec_input_hash="aaaaaa______",
        ca3_output_hash="aaaaaa000000",
    )
    assert out["ok"] is True
    assert abs(out["match_score"] - 0.5) < 1e-9
    assert out["classification"] == "ambiguous"


def test_ca1_status_reflects_comparisons(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_ca1_compare(ec_input_hash="aaa", ca3_output_hash="aaa", agent_id="a1")
    mod.tool_ca1_compare(ec_input_hash="bbb", ca3_output_hash="ccc", agent_id="a1")
    status = mod.tool_ca1_status(agent_id="a1")
    assert status["ok"] is True
    assert status["aggregate_24h"]["n"] == 2
    assert status["aggregate_24h"]["n_match"] == 1
    assert status["aggregate_24h"]["n_mismatch"] == 1
    assert status["state"]["total_comparisons"] == 2


def test_subiculum_output_writes(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_subiculum_output(
        target_channel="workspace_broadcast", memory_id=42,
        output_strength=0.8, agent_id="a1",
    )
    assert out["ok"] is True
    assert out["target_channel"] == "workspace_broadcast"


def test_subiculum_output_validates_target(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_subiculum_output(target_channel="nope")
    assert "error" in out


def test_subiculum_output_validates_strength(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_subiculum_output(
        target_channel="cortex_general", output_strength=1.5,
    )
    assert "error" in out


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_ca1_compare(ec_input_hash="aaa", ca3_output_hash="aaa", agent_id="a1")
    mod.tool_ca1_compare(ec_input_hash="aaa", ca3_output_hash="bbb", agent_id="a2")
    mod.tool_subiculum_output(target_channel="workspace_broadcast", agent_id="a1")
    mod.tool_subiculum_output(target_channel="thalamus_relay", agent_id="a1")
    all_h = mod.tool_ca1_subiculum_history(limit=10)
    assert len(all_h["comparisons"]) == 2
    assert len(all_h["outputs"]) == 2
    a1 = mod.tool_ca1_subiculum_history(limit=10, agent_id="a1")
    assert len(a1["comparisons"]) == 1
    assert len(a1["outputs"]) == 2
    matches = mod.tool_ca1_subiculum_history(limit=10, classification="match")
    assert len(matches["comparisons"]) == 1
    workspace = mod.tool_ca1_subiculum_history(limit=10, target_channel="workspace_broadcast")
    assert len(workspace["outputs"]) == 1
