"""Tests for mcp_tools_claustrum — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_079 = REPO_ROOT / "db" / "migrations" / "079_claustrum.sql"


def _bootstrap(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);"
    )


def _apply(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        _bootstrap(conn)
        conn.executescript(MIGRATION_079.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_claustrum as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_modalities(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM claustrum_modality_catalog").fetchone()[0]
        assert n == 9
        state = conn.execute(
            "SELECT binding_window_seconds, min_modalities_for_binding, enforcement_mode FROM claustrum_state"
        ).fetchone()
        assert state == (60, 2, "shadow")
    finally:
        conn.close()


def test_status_returns_state(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_claustrum_status()
    assert out["ok"] is True
    assert len(out["modality_catalog"]) == 9


def test_record_binding_with_list(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_claustrum_record_binding(
        memory_id=42, modalities=["fts", "vector", "hybrid_rrf"], agent_id="a1",
    )
    assert out["ok"] is True
    assert out["binding_id"] is not None
    # All 3 weights = 1.0 → mean=1.0; count_factor = 3/4 = 0.75 → strength = 0.75
    assert abs(out["binding_strength"] - 0.75) < 1e-9


def test_record_binding_with_csv(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_claustrum_record_binding(
        memory_id=42, modalities="fts,vector",
    )
    assert out["ok"] is True
    # 2 modalities × weight 1.0 → mean=1.0; count_factor=2/4=0.5 → strength 0.5
    assert abs(out["binding_strength"] - 0.5) < 1e-9


def test_record_binding_rejects_below_min(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_claustrum_record_binding(memory_id=1, modalities=["fts"])
    assert "error" in out


def test_record_binding_rejects_unknown_modality(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_claustrum_record_binding(memory_id=1, modalities=["fts", "nope"])
    assert "error" in out


def test_register_modality_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    first = mod.tool_claustrum_register_modality(name="my_custom", weight=0.6)
    second = mod.tool_claustrum_register_modality(name="my_custom", weight=0.8)
    assert first["ok"] and second["ok"]
    assert second["modality"]["weight"] == 0.8


def test_register_modality_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_claustrum_register_modality(name="x", weight=1.5)


def test_memory_bindings_history(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    mod.tool_claustrum_record_binding(memory_id=99, modalities=["fts", "vector"])
    mod.tool_claustrum_record_binding(memory_id=99, modalities=["fts", "ca3_completion"])
    out = mod.tool_claustrum_memory_bindings(memory_id=99)
    assert out["ok"] is True
    assert len(out["bindings"]) == 2


def test_set_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_claustrum_set(binding_window_seconds=0)
    assert "error" in mod.tool_claustrum_set(min_modalities_for_binding=1)
    assert "error" in mod.tool_claustrum_set(enforcement_mode="bogus")
    out = mod.tool_claustrum_set(min_modalities_for_binding=3, enforcement_mode="enforce")
    assert out["ok"] is True
    assert out["state"]["min_modalities_for_binding"] == 3
