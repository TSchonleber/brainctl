"""Regression tests for the 2.9.1 fixes found by the whitepaper audit."""
from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from agentmemory.brain import Brain

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_SCHEMA = REPO_ROOT / "src" / "agentmemory" / "db" / "init_schema.sql"
_NOW = "strftime('%Y-%m-%dT%H:%M:%S','now')"


def _schema_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(INIT_SCHEMA.read_text())
    for aid in ("a", "b", "hippocampus"):
        conn.execute(
            "INSERT INTO agents (id, display_name, agent_type) VALUES (?, ?, 'test')",
            (aid, aid),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. Conflict resolution used a column the schema doesn't have
# ---------------------------------------------------------------------------

def _seed_conflict(conn, *, a_content, b_content, a_conf=0.9, b_conf=0.9):
    conn.execute(
        "INSERT INTO memories (agent_id, category, content, confidence, created_at, updated_at) "
        f"VALUES ('a', 'project', ?, ?, {_NOW}, {_NOW})",
        (a_content, a_conf),
    )
    conn.execute(
        "INSERT INTO memories (agent_id, category, content, confidence, created_at, updated_at) "
        f"VALUES ('b', 'project', ?, ?, {_NOW}, {_NOW})",
        (b_content, b_conf),
    )
    cur = conn.execute(
        "INSERT INTO belief_conflicts (topic, agent_a_id, agent_b_id, belief_a, belief_b) "
        "VALUES ('db', 'a', 'b', ?, ?)",
        (a_content, b_content),
    )
    conn.commit()
    return cur.lastrowid


def test_list_conflicts_runs_on_real_schema(tmp_path):
    from agentmemory.lib.belief_revision import list_conflicts

    db = tmp_path / "brain.db"
    conn = _schema_db(db)
    _seed_conflict(conn, a_content="db is postgres", b_content="db is mysql")
    conn.close()

    rows = list_conflicts(str(db))
    assert len(rows) == 1
    assert rows[0]["requires_supervisor_intervention"] == 0


def test_resolve_conflict_escalates_on_real_schema(tmp_path):
    from agentmemory.lib.belief_revision import resolve_conflict

    db = tmp_path / "brain.db"
    conn = _schema_db(db)
    cid = _seed_conflict(conn, a_content="db is postgres", b_content="db is mysql")
    conn.close()

    # Identical credibility on both sides -> too close to call -> escalate,
    # which writes the supervisor flag.
    result = resolve_conflict(cid, db_path=str(db))
    assert result["action"] == "escalate"
    conn = sqlite3.connect(str(db))
    flag = conn.execute(
        "SELECT requires_supervisor_intervention FROM belief_conflicts WHERE id=?", (cid,)
    ).fetchone()[0]
    conn.close()
    assert flag == 1


def test_belief_revision_honours_brain_db(tmp_path, monkeypatch):
    from agentmemory.lib.belief_revision import list_conflicts

    db = tmp_path / "brain.db"
    _schema_db(db).close()
    monkeypatch.setenv("BRAINCTL_DB", str(db))
    assert list_conflicts() == []


# ---------------------------------------------------------------------------
# 2. Dream REM bridge inserted into columns knowledge_edges doesn't have
# ---------------------------------------------------------------------------

def _vec(values):
    return struct.pack(f"{len(values)}f", *values)


def test_rem_phase_writes_bridge_edge(tmp_path):
    from agentmemory.dream import run_rem_phase

    conn = _schema_db(tmp_path / "brain.db")
    ids = []
    for content in ("connected one", "connected two", "isolated near one"):
        cur = conn.execute(
            "INSERT INTO memories (agent_id, category, content, created_at, updated_at) "
            f"VALUES ('a', 'project', ?, {_NOW}, {_NOW})",
            (content,),
        )
        ids.append(cur.lastrowid)
    c1, c2, iso = ids
    conn.execute(
        "INSERT INTO knowledge_edges (source_table, source_id, target_table, target_id, relation_type) "
        "VALUES ('memories', ?, 'memories', ?, 'related')",
        (c1, c2),
    )
    vecs = {c1: [1.0, 0.0, 0.0, 0.0], c2: [0.0, 1.0, 0.0, 0.0], iso: [0.99, 0.05, 0.0, 0.0]}
    for mid, v in vecs.items():
        conn.execute(
            "INSERT INTO embeddings (source_table, source_id, model, dimensions, vector) "
            "VALUES ('memories', ?, 'test', 4, ?)",
            (mid, _vec(v)),
        )
    conn.commit()

    stats = run_rem_phase(conn, agent_id="hippocampus", bridge_threshold=0.5)
    assert stats["isolated_bridge_discovery"]["bridges_created"] == 1
    edge = conn.execute(
        "SELECT source_id, target_id, weight, agent_id FROM knowledge_edges "
        "WHERE relation_type='rem_bridge'"
    ).fetchone()
    assert (edge["source_id"], edge["target_id"]) == (min(c1, iso), max(c1, iso))
    assert 0.5 <= edge["weight"] <= 1.0
    assert edge["agent_id"] == "hippocampus"
    conn.close()


# ---------------------------------------------------------------------------
# 3. MCP supersede seeded Beta(N, 1) instead of the symmetric Beta(N, N)
# ---------------------------------------------------------------------------

def test_mcp_supersede_seeds_symmetric_prior(tmp_path, monkeypatch):
    mcp_server = pytest.importorskip("agentmemory.mcp_server")

    db = tmp_path / "brain.db"
    conn = _schema_db(db)
    # A recalled, confident incumbent so PII > 0.2 and alpha_floor > 1.
    cur = conn.execute(
        "INSERT INTO memories (agent_id, category, content, confidence, recalled_count, alpha, beta, "
        "created_at, updated_at) "
        f"VALUES ('a', 'environment', 'deploy target is fly.io', 0.95, 50, 40, 1, {_NOW}, {_NOW})"
    )
    old_id = cur.lastrowid
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "DB_PATH", db)
    monkeypatch.delenv("BRAIN_DB", raising=False)
    monkeypatch.delenv("BRAINCTL_HOME", raising=False)

    out = mcp_server.tool_memory_add(
        agent_id="a", content="deploy target is render.com", category="environment",
        supersedes_id=old_id, force=True,
    )
    assert out.get("ok"), out
    conn = sqlite3.connect(str(db))
    alpha, beta = conn.execute(
        "SELECT alpha, beta FROM memories WHERE id=?", (out["memory_id"],)
    ).fetchone()
    conn.close()
    floor = out["pii_gate"]["alpha_floor"] if "pii_gate" in out else alpha
    assert alpha == beta == float(floor)


# ---------------------------------------------------------------------------
# 4. Brain.remember skipped DG pattern separation and its bookkeeping
# ---------------------------------------------------------------------------

def test_brain_remember_reinforces_exact_duplicate(tmp_path):
    brain = Brain(db_path=str(tmp_path / "brain.db"), agent_id="tester")
    mid1 = brain.remember("The sky is blue", category="lesson")
    mid2 = brain.remember("The sky is blue", category="lesson")
    assert mid1 == mid2
    db = brain._get_conn()
    row = db.execute("SELECT recalled_count, alpha FROM memories WHERE id=?", (mid1,)).fetchone()
    assert row["recalled_count"] == 1
    assert row["alpha"] == 2.0
    assert db.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NULL").fetchone()[0] == 1
    brain.close()


def test_brain_remember_force_skips_dedup(tmp_path):
    brain = Brain(db_path=str(tmp_path / "brain.db"), agent_id="tester")
    mid1 = brain.remember("The sky is blue", category="lesson")
    mid2 = brain.remember("The sky is blue", category="lesson", force=True)
    assert mid1 != mid2
    brain.close()


def test_brain_remember_separates_key_token_difference(tmp_path):
    brain = Brain(db_path=str(tmp_path / "brain.db"), agent_id="tester")
    mid1 = brain.remember("Standup moved to 10am on Tuesday", category="project")
    mid2 = brain.remember("Standup moved to 11am on Tuesday", category="project")
    assert mid1 != mid2
    db = brain._get_conn()
    edge = db.execute(
        "SELECT source_id, target_id FROM knowledge_edges WHERE relation_type='distinct_from'"
    ).fetchone()
    assert edge is not None and {edge["source_id"], edge["target_id"]} == {mid1, mid2}
    brain.close()


# ---------------------------------------------------------------------------
# 5. Modules only loaded from hard-coded home paths
# ---------------------------------------------------------------------------

def test_search_helpers_load_in_tree():
    import agentmemory._impl as _impl
    from agentmemory.lib import intent_classifier, salience_routing

    assert _impl._INTENT_AVAILABLE and _impl._SAL_AVAILABLE and _impl._QUANTUM_AVAILABLE
    assert _impl._classify_intent is intent_classifier.classify_intent
    assert _impl._sal is salience_routing


def test_write_decision_loads_without_home_override(tmp_path, monkeypatch):
    from agentmemory import _gates

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    mod = _gates.load_write_decision_module()
    assert mod is not None and hasattr(mod, "gate_write")


def test_cli_write_gate_uses_shared_loader():
    src = (REPO_ROOT / "src" / "agentmemory" / "_impl.py").read_text()
    assert '"agentmemory" / "bin" / "lib" / "write_decision.py"' not in src
    assert "load_write_decision_module()" in src


def test_outcome_eval_honours_brain_db(tmp_path, monkeypatch):
    from agentmemory.lib import outcome_eval

    db = tmp_path / "brain.db"
    _schema_db(db).close()
    monkeypatch.setenv("BRAINCTL_DB", str(db))
    conn = outcome_eval._get_db()
    try:
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == db
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. LoCoMo category labels
# ---------------------------------------------------------------------------

LOCOMO_LABELS = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def test_locomo_category_labels_match_dataset_convention():
    from tests.bench import locomo_eval
    from tests.bench.locomo import runner

    assert locomo_eval.CATEGORY_LABELS == LOCOMO_LABELS
    assert runner.CATEGORY_LABELS == LOCOMO_LABELS


def test_locomo_baseline_slices_match_dataset_counts():
    data = json.loads((REPO_ROOT / "tests" / "bench" / "locomo" / "locomo10.json").read_text())
    counts: dict[str, int] = {}
    for conv in data:
        for qa in conv["qa"]:
            if qa.get("evidence"):
                label = LOCOMO_LABELS[qa["category"]]
                counts[label] = counts.get(label, 0) + 1
    baseline = json.loads((REPO_ROOT / "tests" / "bench" / "baselines" / "locomo.json").read_text())
    assert {k: v["count"] for k, v in baseline["by_category"].items()} == counts
