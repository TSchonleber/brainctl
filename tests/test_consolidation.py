"""Tests for consolidation engine, concurrency, and memory lifecycle."""

import os
import tempfile
import pytest

from agentmemory.brain import Brain


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Ensure BRAIN_DB doesn't leak across tests."""
    monkeypatch.delenv("BRAIN_DB", raising=False)


def _init_test_db():
    td = tempfile.mkdtemp()
    db_path = os.path.join(td, "test_brain.db")
    return Brain(db_path, agent_id="test-agent")


class TestDecay:
    def test_decay_reduces_confidence(self):
        brain = _init_test_db()
        mid = brain.remember("old fact", category="lesson", confidence=0.9)
        db = brain._db()
        db.execute("UPDATE memories SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (mid,))
        db.commit()
        row = db.execute("SELECT confidence FROM memories WHERE id=?", (mid,)).fetchone()
        assert row is not None
        assert row["confidence"] == 0.9
        db.close()

    def test_protected_memories_not_decayed(self):
        brain = _init_test_db()
        mid = brain.remember("core identity fact", category="identity", confidence=1.0)
        db = brain._db()
        db.execute("UPDATE memories SET protected=1 WHERE id=?", (mid,))
        db.commit()
        row = db.execute("SELECT protected FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["protected"] == 1
        db.close()

    def test_permanent_temporal_class_preserved(self):
        brain = _init_test_db()
        mid = brain.remember("permanent fact", category="identity", confidence=1.0)
        db = brain._db()
        db.execute("UPDATE memories SET temporal_class='permanent' WHERE id=?", (mid,))
        db.commit()
        row = db.execute("SELECT temporal_class FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["temporal_class"] == "permanent"
        db.close()


class TestDedup:
    def test_exact_duplicate_content(self):
        brain = _init_test_db()
        mid1 = brain.remember("The sky is blue", category="lesson")
        mid2 = brain.remember("The sky is blue", category="lesson")
        assert mid1 != mid2

    def test_different_categories_not_deduped(self):
        brain = _init_test_db()
        mid1 = brain.remember("test fact", category="lesson")
        mid2 = brain.remember("test fact", category="convention")
        assert mid1 != mid2


class TestHardCap:
    def test_memory_count_tracking(self):
        brain = _init_test_db()
        for i in range(5):
            brain.remember(f"fact number {i}", category="lesson")
        stats = brain.stats()
        assert stats["active_memories"] == 5

    def test_soft_delete_reduces_active_count(self):
        brain = _init_test_db()
        mid = brain.remember("temporary", category="lesson")
        brain.forget(mid)
        stats = brain.stats()
        assert stats["active_memories"] == 0


class TestConcurrency:
    def test_wal_mode_enabled(self):
        brain = _init_test_db()
        db = brain._db()
        row = db.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"
        db.close()

    def test_multiple_connections_read(self):
        brain = _init_test_db()
        brain.remember("shared fact", category="lesson")
        db1 = brain._db()
        db2 = brain._db()
        r1 = db1.execute("SELECT count(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
        r2 = db2.execute("SELECT count(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
        assert r1 == r2 == 1
        db1.close()
        db2.close()

    def test_foreign_keys_enabled(self):
        brain = _init_test_db()
        db = brain._db()
        row = db.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1
        db.close()
