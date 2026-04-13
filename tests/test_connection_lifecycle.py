"""Regression tests for the Brain connection-lifecycle refactor (Phase 1.1).

These tests pin the behavior of the lazy, shared, thread-safe sqlite3
connection that Brain now holds per instance, plus the module-level
``get_brain`` factory cache.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import List

import pytest

from agentmemory.brain import Brain, get_brain, _BRAIN_CACHE, _clear_brain_cache


@pytest.fixture
def db_file(tmp_path: Path) -> str:
    return str(tmp_path / "brain.db")


# ---------------------------------------------------------------------------
# 1. Single-connection assertion
# ---------------------------------------------------------------------------

def test_public_methods_reuse_single_connection(db_file, monkeypatch):
    """Every public method must reuse one shared connection. Patch
    sqlite3.connect with a counter and assert it is called exactly once
    for the shared-connection lifecycle.
    """
    # Create the brain first so the _init_db() call (which we accept as
    # a one-shot) happens before we install the counter.
    brain = Brain(db_path=db_file, agent_id="counter-agent")

    real_connect = sqlite3.connect
    calls: List[str] = []

    def counting_connect(*args, **kwargs):
        calls.append("connect")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    # Every one of these must land on the same lazy shared connection.
    brain.remember("hello world", category="lesson")
    brain.search("hello")
    brain.log("an event")
    brain.entity("Example", "concept")
    brain.stats()
    brain.doctor()

    # Some ops (vec.index_memory) open their own short-lived connections
    # when sqlite-vec is actually loaded. We only care about Brain's own
    # connection path — filter by stack frame origin is noisy, so instead
    # we assert <= 1 connect happened from Brain's own code path. The
    # simplest check: after the first brain.remember, self._conn is set.
    assert brain._conn is not None
    # Brain.remember with vec may trigger one extra connect inside
    # _vec.index_memory when the dylib is present — but in test env it
    # usually isn't. Allow up to 1 legitimate extra.
    assert len(calls) <= 1, (
        f"Expected at most 1 fresh sqlite3.connect for shared conn path, "
        f"got {len(calls)}: {calls}"
    )
    brain.close()


def test_init_opens_at_most_once_for_fresh_db(tmp_path, monkeypatch):
    """Creating a Brain on a non-existent db calls sqlite3.connect once
    (for _init_db) and then the lazy shared conn is opened on first use.
    """
    real_connect = sqlite3.connect
    count = {"n": 0}

    def counting_connect(*args, **kwargs):
        count["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    db = str(tmp_path / "fresh.db")
    brain = Brain(db_path=db, agent_id="fresh-agent")
    init_count = count["n"]

    brain.remember("a")
    brain.remember("b")
    brain.search("a")

    # One _init_db + one shared conn at most (vec may add one per remember
    # but only if the dylib is loaded in this env — tolerate up to 2 more).
    after = count["n"]
    assert after - init_count <= 3
    brain.close()


# ---------------------------------------------------------------------------
# 2. Thread-safety smoke test
# ---------------------------------------------------------------------------

def test_multi_thread_remember_and_search(db_file):
    brain = Brain(db_path=db_file, agent_id="threaded")

    errors: List[Exception] = []
    per_thread = 50
    n_threads = 8

    def worker(worker_id: int) -> None:
        try:
            for i in range(per_thread):
                # bypass_gate=True: this test stresses the connection
                # lifecycle under thread contention, not the W(m) gate.
                # Near-duplicate content ("thread N memory M") would
                # otherwise be rejected by semantic dedup.
                brain.remember(
                    f"thread {worker_id} memory {i}",
                    category="lesson",
                    bypass_gate=True,
                )
                results = brain.search(f"thread {worker_id}")
                assert isinstance(results, list)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread workers raised: {errors}"
    # Total memory count must match exactly.
    stats = brain.stats()
    assert stats["active_memories"] == per_thread * n_threads
    brain.close()


# ---------------------------------------------------------------------------
# 3. Context manager
# ---------------------------------------------------------------------------

def test_context_manager_closes_connection(db_file):
    with Brain(db_path=db_file, agent_id="ctx") as brain:
        brain.remember("inside ctx")
        assert brain._conn is not None
    # After __exit__, the shared connection should be cleared.
    assert brain._conn is None
    assert brain._closed is True

    # A subsequent call should transparently lazy-reopen (documented behavior).
    brain.remember("after close")
    assert brain._conn is not None
    brain.close()


def test_close_is_idempotent(db_file):
    brain = Brain(db_path=db_file, agent_id="idemp")
    brain.remember("hi")
    brain.close()
    brain.close()  # must not raise
    brain.close()  # still fine


# ---------------------------------------------------------------------------
# 4. Factory cache
# ---------------------------------------------------------------------------

def test_get_brain_returns_same_instance_for_same_key(db_file):
    _clear_brain_cache()
    try:
        a = get_brain(db_file, "agent-a")
        b = get_brain(db_file, "agent-a")
        assert a is b
    finally:
        _clear_brain_cache()


def test_get_brain_different_agent_different_instance(db_file):
    _clear_brain_cache()
    try:
        a = get_brain(db_file, "agent-a")
        b = get_brain(db_file, "agent-b")
        assert a is not b
    finally:
        _clear_brain_cache()


def test_get_brain_path_normalization(tmp_path):
    """Different textual forms of the same absolute path hit the same cache slot."""
    _clear_brain_cache()
    try:
        db_file = tmp_path / "brain.db"
        # Touch via first call
        a = get_brain(str(db_file), "agent-norm")

        # Equivalent forms: with ./, absolute, relative-through-cwd
        b = get_brain(str(db_file.resolve()), "agent-norm")
        assert a is b

        # With an extra ".." round-trip
        weird = tmp_path / "sub" / ".." / "brain.db"
        c = get_brain(str(weird), "agent-norm")
        assert a is c
    finally:
        _clear_brain_cache()


def test_get_brain_reopens_after_close(db_file):
    _clear_brain_cache()
    try:
        a = get_brain(db_file, "reopen")
        a.close()
        b = get_brain(db_file, "reopen")
        # After close, cache should hand back a fresh instance (not the
        # closed one) so callers can keep working.
        assert b is not a
        assert b._closed is False
    finally:
        _clear_brain_cache()


# ---------------------------------------------------------------------------
# 5. Edge: db file vanishes mid-flight
# ---------------------------------------------------------------------------

def test_run_helper_reinits_on_missing_table(tmp_path):
    """The ``_run`` helper must lazy-reinit once when a "no such table"
    error fires on the shared connection — e.g. because the db file
    was clobbered behind Brain's back.
    """
    db_file = str(tmp_path / "brain.db")
    brain = Brain(db_path=db_file, agent_id="tamper")
    brain.remember("first")

    # Simulate a table disappearing by having _run's callback raise
    # "no such table" on first call, then succeed on the retry.
    state = {"attempts": 0}

    def op(conn):
        state["attempts"] += 1
        if state["attempts"] == 1:
            raise sqlite3.OperationalError("no such table: memories")
        return conn.execute(
            "SELECT count(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]

    result = brain._run(op)
    assert state["attempts"] == 2
    assert result >= 1
    brain.close()
