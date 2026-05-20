"""Tests for mcp_tools_connectome — Phase 1."""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_073 = REPO_ROOT / "db" / "migrations" / "073_connectome.sql"


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
        conn.executescript(MIGRATION_073.read_text())
        conn.commit()
    finally:
        conn.close()


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _apply(db)
    from agentmemory import mcp_tools_connectome as mod
    monkeypatch.setattr(mod, "DB_PATH", db)
    return mod


def test_migration_seeds_known_topology(tmp_path):
    db = tmp_path / "brain.db"
    _apply(db)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM connectome_nodes").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM connectome_edges").fetchone()[0]
        assert n >= 20  # 22 seeded
        assert e >= 15  # 18 seeded
        # bg_modulators should be a high-degree hub (matches biology)
        hub = conn.execute(
            """
            SELECT n.name,
                   (SELECT COUNT(*) FROM connectome_edges
                      WHERE source_id = n.id OR target_id = n.id) AS deg
              FROM connectome_nodes n WHERE n.name = 'bg_modulators'
            """
        ).fetchone()
        assert hub[1] >= 3
    finally:
        conn.close()


def test_status_returns_summary(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_connectome_status()
    assert out["ok"] is True
    assert out["node_count"] >= 20
    assert out["edge_count"] >= 15
    assert any(t["edge_type"] == "writes_to" for t in out["edges_by_type"])
    assert any(c["category"] == "subsystem" for c in out["nodes_by_category"])
    # top_degree[0] should be a real hub
    assert out["top_degree"][0]["total_degree"] >= 3


def test_node_get_returns_neighbors(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_connectome_node_get(name="basal_ganglia")
    assert out["ok"] is True
    assert out["node"]["name"] == "basal_ganglia"
    assert out["out_degree"] >= 1
    # Outgoing edges should include a write to bg_td_events
    target_names = {e["target"] for e in out["outgoing"]}
    assert "bg_td_events" in target_names


def test_node_get_unknown(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_connectome_node_get(name="not-a-node")
    assert "error" in out


def test_register_node_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    first = mod.tool_connectome_register_node(
        name="vta_snc", category="subsystem",
        description="dopamine source (research avenue 7)",
    )
    # Second call without description → COALESCE preserves first description
    second = mod.tool_connectome_register_node(
        name="vta_snc", category="subsystem",
    )
    assert first["ok"] and second["ok"]
    assert first["node"]["id"] == second["node"]["id"]
    assert "research avenue 7" in second["node"]["description"]


def test_register_node_validates_category(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_connectome_register_node(name="x", category="bad-cat")
    assert "error" in out


def test_register_edge_idempotent(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    # Register nodes if not present (they may already be seeded)
    mod.tool_connectome_register_node(name="vta_snc", category="subsystem")
    first = mod.tool_connectome_register_edge(
        source="vta_snc", target="bg_modulators", edge_type="writes_to",
        weight=0.9, evidence_source="docs:avenue 7",
    )
    second = mod.tool_connectome_register_edge(
        source="vta_snc", target="bg_modulators", edge_type="writes_to",
        weight=1.0,
    )
    assert first["ok"] and second["ok"]
    # Verify the second call updated the weight
    out = mod.tool_connectome_node_get(name="vta_snc")
    bgmod_edges = [e for e in out["outgoing"] if e["target"] == "bg_modulators"]
    assert len(bgmod_edges) == 1
    assert bgmod_edges[0]["weight"] == 1.0


def test_register_edge_rejects_unknown_node(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_connectome_register_edge(
        source="nonexistent", target="bg_modulators", edge_type="writes_to",
    )
    assert "error" in out


def test_register_edge_validates(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_connectome_register_edge(
        source="basal_ganglia", target="bg_modulators", edge_type="bogus",
    )
    assert "error" in mod.tool_connectome_register_edge(
        source="basal_ganglia", target="bg_modulators", edge_type="writes_to",
        weight=1.5,
    )


def test_neighbors_bfs_depth_1(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    out = mod.tool_connectome_neighbors(name="basal_ganglia", direction="out", depth=1)
    assert out["ok"] is True
    names = {n["name"] for n in out["nodes"]}
    assert "bg_td_events" in names or "bg_modulators" in names
    # depth-1 should not include 2-hop reachable nodes from BG
    # (unless they happen to be direct outgoing — varies by seed)


def test_neighbors_validates_direction_and_depth(tmp_path, monkeypatch):
    mod = _make_db(tmp_path, monkeypatch)
    assert "error" in mod.tool_connectome_neighbors(name="basal_ganglia", direction="invalid")
    assert "error" in mod.tool_connectome_neighbors(name="basal_ganglia", depth=0)
    assert "error" in mod.tool_connectome_neighbors(name="basal_ganglia", depth=99)
