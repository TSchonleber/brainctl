"""Regression tests for FTS index maintenance on the ``memories`` table.

Issue #152 (FTS index corruption on memory_search): the ``memories_fts``
external-content FTS5 index is maintained by two ``AFTER UPDATE ON memories``
triggers that are *unscoped* (they fire on every column update). A
metadata-only UPDATE -- e.g. ``_retrieval_practice_boost`` bumping
``confidence`` / ``recalled_count`` on each search hit -- therefore fires the
delete+reinsert pair even though no FTS-indexed column (content/category/tags)
changed. The net effect erodes the inverted index, so repeated searches
progressively lose rows from ``memory_search``.

Invariant under test: a metadata-only UPDATE must NOT change the FTS index,
while genuine content/category/tags edits, retire, and delete MUST still
maintain it. Exercised against the full production schema (``cli_db`` fixture)
so the real, effective triggers (init_schema + migrations) are what runs.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_impl = pytest.importorskip("agentmemory._impl")


def _add(conn, content, category="note", tags="", agent="tester"):
    cur = conn.execute(
        "INSERT INTO memories (agent_id, category, content, tags, indexed, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 1, "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent, category, content, tags),
    )
    return cur.lastrowid


def _match(conn, token):
    return conn.execute(
        "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH ?", (token,)
    ).fetchone()[0]


def _docsize(conn):
    return conn.execute("SELECT count(*) FROM memories_fts_docsize").fetchone()[0]


def _rebuild(conn):
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()


def _integrity_ok(conn):
    """True if the FTS5 internal integrity-check passes (no exception)."""
    try:
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")
        return True
    except sqlite3.DatabaseError:
        return False


@pytest.fixture
def conn(cli_db):
    c = sqlite3.connect(str(cli_db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_metadata_update_preserves_fts_index(conn):
    """A single metadata-only UPDATE must not evict the row from FTS (#152)."""
    mid = _add(conn, "kelly village infrastructure")
    conn.commit()
    _rebuild(conn)
    assert _match(conn, "kelly") == 1
    base = _docsize(conn)

    # The exact column set _retrieval_practice_boost touches -- no
    # content/category/tags among them.
    conn.execute(
        "UPDATE memories SET confidence = MIN(1.0, confidence + 0.01), "
        "recalled_count = recalled_count + 1, "
        "last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "labile_until = strftime('%Y-%m-%dT%H:%M:%S','now','+2 hours') "
        "WHERE id = ?",
        (mid,),
    )
    conn.commit()

    assert _match(conn, "kelly") == 1, "metadata update evicted the row from FTS"
    assert _docsize(conn) == base, "metadata update changed FTS docsize"
    assert _integrity_ok(conn), "metadata update left FTS index inconsistent"


def test_repeated_metadata_updates_do_not_erode_index(conn):
    """Repeated metadata UPDATEs reproduce the docsize-bleed symptom (#152)."""
    ids = [
        _add(conn, "alpha kelly village"),
        _add(conn, "beta howler routine"),
        _add(conn, "gamma api ratelimit"),
    ]
    conn.commit()
    _rebuild(conn)
    base = _docsize(conn)

    for _ in range(5):
        for mid in ids:
            conn.execute(
                "UPDATE memories SET confidence = MIN(1.0, confidence + 0.01), "
                "recalled_count = recalled_count + 1 WHERE id = ?",
                (mid,),
            )
        conn.commit()

    assert _docsize(conn) == base, "repeated metadata updates eroded the FTS index"
    assert _match(conn, "kelly") == 1
    assert _match(conn, "howler") == 1
    assert _match(conn, "ratelimit") == 1


def test_real_retrieval_practice_preserves_index(conn):
    """The real driver: _retrieval_practice_boost must not erode FTS (#152)."""
    ids = [
        _add(conn, "alpha kelly village"),
        _add(conn, "beta howler routine"),
        _add(conn, "gamma api ratelimit"),
    ]
    conn.commit()
    _rebuild(conn)
    base = _docsize(conn)

    for _ in range(5):
        for mid in ids:
            _impl._retrieval_practice_boost(conn, mid)
        conn.commit()

    assert _docsize(conn) == base, "_retrieval_practice_boost eroded the FTS index"
    assert _match(conn, "kelly") == 1
    assert _match(conn, "howler") == 1
    assert _match(conn, "ratelimit") == 1


# --- regression guards: must stay GREEN through the trigger-scoping fix ---

def test_content_edit_reindexes(conn):
    mid = _add(conn, "alpha zebra")
    conn.commit()
    _rebuild(conn)
    assert _match(conn, "zebra") == 1

    conn.execute(
        "UPDATE memories SET content = ?, version = version + 1 WHERE id = ?",
        ("beta giraffe", mid),
    )
    conn.commit()
    assert _match(conn, "zebra") == 0, "stale content still indexed after edit"
    assert _match(conn, "giraffe") == 1, "new content not indexed after edit"


def test_retire_removes_from_fts(conn):
    mid = _add(conn, "kelly village retire")
    conn.commit()
    _rebuild(conn)
    assert _match(conn, "kelly") == 1

    conn.execute(
        "UPDATE memories SET retired_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
        (mid,),
    )
    conn.commit()
    assert _match(conn, "kelly") == 0, "retired memory still in FTS index"


def test_delete_removes_from_fts(conn):
    mid = _add(conn, "kelly village delete")
    conn.commit()
    _rebuild(conn)
    assert _match(conn, "kelly") == 1

    conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
    conn.commit()
    assert _match(conn, "kelly") == 0, "deleted memory still in FTS index"
