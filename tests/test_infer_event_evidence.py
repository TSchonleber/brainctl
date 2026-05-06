"""Issue #97-4: ``infer`` was reporting "No evidence found" / tier
``L1-gap`` even when L1 retrieval found event hits, because
``_reason_l3_infer`` only folded ``l1_memories`` into ``all_evidence``
and dropped ``l1_events`` entirely. ``provenance.l1_results`` counted
events, so the response self-contradicted: provenance said yes, the
conclusion said no.

This test pins the corrected behavior: when only events match, infer
must include them in evidence and surface a substantive conclusion.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_server as mcp_server
import agentmemory.mcp_tools_reasoning as reasoning


@pytest.fixture
def brain_with_events(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    brain = Brain(db_path=str(db), agent_id="infer-agent")
    monkeypatch.setattr(mcp_server, "DB_PATH", db)
    # Patch the reasoning module's DB resolver to also point at this DB
    monkeypatch.setenv("BRAIN_DB", str(db))
    return brain, db


def test_infer_includes_events_in_evidence(brain_with_events):
    brain, _ = brain_with_events
    # Seed: events only, no memories with this content
    brain.log("Kelly village deployment failed at step 3", event_type="error",
              importance=0.8, project="kelly")
    brain.log("Kelly village rollback succeeded", event_type="result",
              importance=0.7, project="kelly")
    brain.log("Kelly village staging green", event_type="observation",
              importance=0.6, project="kelly")

    out = reasoning.tool_infer(
        agent_id="infer-agent",
        query="kelly village deployment",
        limit=10,
        hops=1,
    )
    assert out["ok"] is True, f"infer call failed: {out}"

    prov = out["provenance"]
    assert prov["l1_results"] >= 1, "Setup precondition: events should be found by L1"

    inference = out["inference"]
    evidence = out["evidence"]

    assert evidence, (
        "Regression: l1 found events but they were dropped from evidence. "
        f"provenance={prov} inference={inference}"
    )
    # And the conclusion must not be the empty-evidence sentinel
    assert inference["tier"] != "L1-gap", (
        f"Regression: tier reported L1-gap despite l1_results={prov['l1_results']}"
    )
    assert "No evidence found" not in inference["conclusion"], (
        f"Regression: empty-evidence conclusion despite L1 hits: {inference}"
    )


def test_infer_still_returns_l1_gap_when_no_hits(brain_with_events):
    """Sanity check: a genuine miss still surfaces as L1-gap."""
    brain, _ = brain_with_events
    out = reasoning.tool_infer(
        agent_id="infer-agent",
        query="zzqqxxnonsense",
        limit=10,
        hops=1,
    )
    assert out["ok"] is True
    assert out["provenance"]["l1_results"] == 0
    assert out["inference"]["tier"] == "L1-gap"
    assert "No evidence found" in out["inference"]["conclusion"]
