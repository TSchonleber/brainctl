"""Tests for mcp_tools_vta_snc — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_075 = REPO_ROOT / "db" / "migrations" / "075_vta_snc.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_075.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_vta_snc as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_pathways(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM vta_pathway_links").fetchone()[0]
        assert n == 6
        state = conn.execute(
            "SELECT tonic_da, burst_budget, total_firings, pathology_flag FROM vta_state"
        ).fetchone()
        assert state == (0.5, 1.0, 0, "none")
    finally:
        conn.close()


def test_status_returns_state_and_aggregates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_vta_status()
    assert out["ok"] is True
    assert out["state"]["tonic_da"] == 0.5
    assert out["aggregate_24h"]["n"] == 0


def test_fire_updates_state_and_depletes_budget(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_vta_fire(burst_magnitude=0.5, source_kind="bg_td_positive",
                            target_pathway="nigrostriatal")
    assert out["ok"] is True
    # 0.5 × 0.1 = 0.05 depletion → 1.0 → 0.95
    assert abs(out["new_burst_budget"] - 0.95) < 1e-9
    status = mod.tool_vta_status()
    assert status["state"]["total_firings"] == 1


def test_fire_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_vta_fire(burst_magnitude=1.5, source_kind="novelty")
    assert "error" in mod.tool_vta_fire(burst_magnitude=0.5, source_kind="bogus")


def test_set_tonic_updates_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_vta_set_tonic(tonic_da=0.25, pathology_flag="low_da", reason="test")
    assert out["ok"] is True
    assert out["state"]["tonic_da"] == 0.25
    assert out["state"]["pathology_flag"] == "low_da"


def test_set_tonic_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_vta_set_tonic(tonic_da=1.5)
    assert "error" in mod.tool_vta_set_tonic(pathology_flag="bogus")
    assert "error" in mod.tool_vta_set_tonic()


def test_pathways_filter(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    all_p = mod.tool_vta_pathways()
    assert len(all_p["pathway_links"]) == 6
    meso = mod.tool_vta_pathways(pathway="mesolimbic")
    assert len(meso["pathway_links"]) == 2
    # All mesolimbic links target nucleus_accumbens or amygdala
    targets = {p["target_subsystem"] for p in meso["pathway_links"]}
    assert targets == {"nucleus_accumbens", "amygdala"}


def test_history_filters(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_vta_fire(burst_magnitude=0.3, source_kind="bg_td_positive", target_pathway="nigrostriatal")
    mod.tool_vta_fire(burst_magnitude=0.5, source_kind="novelty", target_pathway="mesolimbic")
    mod.tool_vta_fire(burst_magnitude=0.2, source_kind="bg_td_positive", target_pathway="mesocortical")
    all_h = mod.tool_vta_history(limit=10)
    assert len(all_h["history"]) == 3
    td = mod.tool_vta_history(limit=10, source_kind="bg_td_positive")
    assert len(td["history"]) == 2
    meso = mod.tool_vta_history(limit=10, target_pathway="mesolimbic")
    assert len(meso["history"]) == 1
