"""Tests for mcp_tools_immunity — quarantine_list, quarantine_review, quarantine_purge."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_immunity as imm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    """Each test gets an isolated Brain DB."""
    from agentmemory.brain import Brain
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(imm, "DB_PATH", db_file)
    return db_file


@pytest.fixture
def quarantined_memory(patch_db):
    """Create a memory and quarantine it, return (db_file, memory_id, quarantine_id)."""
    db_file = patch_db
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    # Insert a memory directly
    conn.execute(
        "INSERT INTO memories (content, category, confidence, agent_id, created_at) "
        "VALUES ('injected memory content', 'convention', 0.9, 'test-agent', '2026-01-01T00:00:00')"
    )
    conn.commit()
    mid = conn.execute("SELECT id FROM memories ORDER BY id DESC LIMIT 1").fetchone()[0]
    # Create quarantine table and insert record
    imm._ensure_quarantine_table(conn)
    conn.execute(
        "INSERT INTO memory_quarantine (memory_id, reason, source_trust, contradiction_count, quarantined_by) "
        "VALUES (?, 'contradiction_spike', 0.4, 4, 'system')",
        (mid,),
    )
    conn.commit()
    qid = conn.execute("SELECT id FROM memory_quarantine ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    return db_file, mid, qid


# ---------------------------------------------------------------------------
# quarantine_list tests
# ---------------------------------------------------------------------------


class TestQuarantineList:
    def test_empty_db_returns_ok(self, patch_db):
        result = imm.tool_quarantine_list(agent_id="test")
        assert result["ok"] is True
        assert result["items"] == []

    def test_lists_quarantined_memory(self, quarantined_memory):
        _, mid, _ = quarantined_memory
        result = imm.tool_quarantine_list(agent_id="test")
        assert result["ok"] is True
        assert result["count"] >= 1
        assert any(item["memory_id"] == mid for item in result["items"])

    def test_filter_pending_only(self, quarantined_memory):
        result = imm.tool_quarantine_list(agent_id="test", verdict="pending")
        assert result["ok"] is True
        for item in result["items"]:
            assert item["verdict"] is None

    def test_invalid_verdict_rejected(self, patch_db):
        result = imm.tool_quarantine_list(agent_id="test", verdict="bogus")
        assert result["ok"] is False

    def test_items_have_required_fields(self, quarantined_memory):
        result = imm.tool_quarantine_list(agent_id="test")
        for item in result["items"]:
            for field in ("id", "memory_id", "reason", "source_trust", "contradiction_count",
                          "quarantined_by", "verdict", "content"):
                assert field in item, f"Missing field: {field}"

    def test_limit_respected(self, quarantined_memory):
        result = imm.tool_quarantine_list(agent_id="test", limit=1)
        assert len(result["items"]) <= 1


# ---------------------------------------------------------------------------
# quarantine_review tests
# ---------------------------------------------------------------------------


class TestQuarantineReview:
    def test_mark_safe(self, quarantined_memory):
        _, mid, qid = quarantined_memory
        result = imm.tool_quarantine_review(agent_id="operator", quarantine_id=qid, verdict="safe")
        assert result["ok"] is True
        assert result["verdict"] == "safe"

    def test_mark_malicious(self, quarantined_memory):
        _, mid, qid = quarantined_memory
        result = imm.tool_quarantine_review(agent_id="operator", quarantine_id=qid, verdict="malicious")
        assert result["ok"] is True
        assert result["verdict"] == "malicious"

    def test_mark_uncertain(self, quarantined_memory):
        _, mid, qid = quarantined_memory
        result = imm.tool_quarantine_review(agent_id="operator", quarantine_id=qid, verdict="uncertain")
        assert result["ok"] is True

    def test_requires_quarantine_id(self, patch_db):
        result = imm.tool_quarantine_review(agent_id="test", verdict="safe")
        assert result["ok"] is False

    def test_invalid_verdict(self, quarantined_memory):
        _, _, qid = quarantined_memory
        result = imm.tool_quarantine_review(agent_id="test", quarantine_id=qid, verdict="delete")
        assert result["ok"] is False

    def test_nonexistent_quarantine_id(self, patch_db):
        result = imm.tool_quarantine_review(agent_id="test", quarantine_id=99999, verdict="safe")
        assert result["ok"] is False

    def test_records_reviewer(self, quarantined_memory):
        _, _, qid = quarantined_memory
        imm.tool_quarantine_review(agent_id="reviewer-agent", quarantine_id=qid, verdict="uncertain")
        result = imm.tool_quarantine_list(agent_id="test")
        reviewed = next(i for i in result["items"] if i["id"] == qid)
        assert reviewed["reviewed_by"] == "reviewer-agent"


# ---------------------------------------------------------------------------
# quarantine_purge tests
# ---------------------------------------------------------------------------


class TestQuarantinePurge:
    def test_purge_requires_malicious_verdict(self, quarantined_memory):
        _, _, qid = quarantined_memory
        # Not yet marked malicious
        result = imm.tool_quarantine_purge(agent_id="operator", quarantine_id=qid)
        assert result["ok"] is False
        assert "malicious" in result["error"]

    def test_dry_run_no_delete(self, quarantined_memory):
        _, mid, qid = quarantined_memory
        imm.tool_quarantine_review(agent_id="op", quarantine_id=qid, verdict="malicious")
        result = imm.tool_quarantine_purge(agent_id="op", quarantine_id=qid, dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        # Memory should still exist
        db_file = imm.DB_PATH
        conn = sqlite3.connect(str(db_file))
        row = conn.execute("SELECT id FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row is not None

    def test_purge_retires_memory(self, quarantined_memory):
        _, mid, qid = quarantined_memory
        imm.tool_quarantine_review(agent_id="op", quarantine_id=qid, verdict="malicious")
        result = imm.tool_quarantine_purge(agent_id="op", quarantine_id=qid, dry_run=False)
        assert result["ok"] is True
        assert result["dry_run"] is False
        db_file = imm.DB_PATH
        conn = sqlite3.connect(str(db_file))
        row = conn.execute("SELECT retired_at FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is not None  # soft-deleted via retired_at

    def test_purge_requires_quarantine_id(self, patch_db):
        result = imm.tool_quarantine_purge(agent_id="test")
        assert result["ok"] is False

    def test_nonexistent_quarantine_id(self, patch_db):
        result = imm.tool_quarantine_purge(agent_id="test", quarantine_id=99999)
        assert result["ok"] is False
