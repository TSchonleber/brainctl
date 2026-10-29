"""Regression tests for migration 048: FK integrity DELETE triggers + FTS5 retire-aware re-index.

Background
----------
The 2026-04-16 correctness audit (memory 1675) flagged that only 2 of 47
migrations declare ON DELETE clauses on FK columns. SQLite does not support
``ALTER TABLE ADD CONSTRAINT``, so we cannot retroactively add ``ON DELETE
CASCADE``/``SET NULL`` to existing tables without rebuilding them. Migration
048 emulates the intended cascade behavior with idempotent DELETE/UPDATE
triggers that fire when ``PRAGMA foreign_keys = OFF`` (raw SQL maintenance,
or ``merge.py:586`` which deliberately disables FK enforcement).

What this file pins
-------------------
1. Hard-deleting an ``agents`` row nullifies dangling
   ``memories.validation_agent_id`` references (preserves history row).
2. Hard-deleting a ``memories`` row cascade-deletes ``knowledge_edges`` rows
   that name it as source or target. The task spec said "null out", but the
   columns are ``NOT NULL`` and cannot be set to NULL via trigger, so we
   delete the edges instead — they're meaningless without their referent.
3. Same cascade for ``entities`` and ``events``.
4. Retiring a memory (``UPDATE memories SET retired_at = ...``) immediately
   removes its row from ``memories_fts`` — no waiting for
   ``cmd_vec_purge_retired``. This is achieved by migration 048 converging
   the legacy single ``memories_fts_update`` trigger to the packaged
   split-pair pattern (``_update_delete`` + ``_update_insert``) with a
   ``new.retired_at IS NULL`` guard on the insert leg. We learned the hard
   way that a separate ``AFTER UPDATE OF retired_at`` purge trigger does
   NOT work: plain ``DELETE FROM memories_fts`` corrupts content-linked
   FTS5 segments, and the proper ``'delete'`` command idiom is no-op'd by
   FTS5 statement-level batching against the just-issued INSERT from
   ``memories_fts_update``. Preventing the re-INSERT in the first place is
   the only correct fix.

These tests use a raw ``sqlite3`` connection on a temp DB seeded from
``db/init_schema.sql`` (the same path ``brainctl init`` takes for fresh
installs) so they exercise the trigger DDL exactly as a brand-new user
would see it. They explicitly set ``PRAGMA foreign_keys = OFF`` for the
cascade tests because with FK enforcement ON, the parent DELETE is rejected
before the AFTER DELETE trigger ever runs.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Same PYTHONPATH bootstrap pattern as the rest of the suite.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

INIT_SQL_DEV = Path(__file__).resolve().parent.parent / "db" / "init_schema.sql"
INIT_SQL_PKG = SRC / "agentmemory" / "db" / "init_schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_db(db_file: Path) -> sqlite3.Connection:
    """Build a fresh DB from init_schema.sql. Returns an open connection
    with FK enforcement OFF so the cascade triggers can fire."""
    schema_path = INIT_SQL_PKG if INIT_SQL_PKG.exists() else INIT_SQL_DEV
    assert schema_path.exists(), f"init_schema.sql not found at {schema_path}"

    conn = sqlite3.connect(str(db_file))
    conn.executescript(schema_path.read_text())
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_file = tmp_path / "brain.db"
    c = _build_db(db_file)
    yield c
    c.close()


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Conn with one agent + one memory pre-inserted, suitable for most tests."""
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status) "
        "VALUES ('alice', 'Alice', 'autonomous', 'active')"
    )
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status) "
        "VALUES ('validator', 'Validator', 'autonomous', 'active')"
    )
    conn.execute(
        "INSERT INTO memories (agent_id, category, content, validation_agent_id, "
        "created_at, updated_at) "
        "VALUES ('alice', 'project', 'sample memory', 'validator', "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Item 1a — agent hard-delete nullifies validation_agent_id
# ---------------------------------------------------------------------------

def test_agent_delete_nullifies_memory_validation_agent_id(seeded):
    """Hard-deleting an agent row should null out
    ``memories.validation_agent_id`` references that pointed at it,
    while leaving the memory row itself intact (history preserved)."""
    mid = seeded.execute(
        "SELECT id FROM memories WHERE validation_agent_id = 'validator'"
    ).fetchone()[0]

    seeded.execute("DELETE FROM agents WHERE id = 'validator'")
    seeded.commit()

    # Memory still exists.
    row = seeded.execute(
        "SELECT id, validation_agent_id FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    assert row is not None, "memory row was incorrectly deleted"
    assert row[1] is None, (
        f"validation_agent_id was {row[1]!r}, expected NULL after agent hard-delete"
    )


def test_agent_delete_does_not_touch_unrelated_memories(seeded):
    """Sibling memories validated by a different agent must be unaffected."""
    seeded.execute(
        "INSERT INTO agents (id, display_name, agent_type, status) "
        "VALUES ('other', 'Other', 'autonomous', 'active')"
    )
    seeded.execute(
        "INSERT INTO memories (agent_id, category, content, validation_agent_id, "
        "created_at, updated_at) "
        "VALUES ('alice', 'project', 'untouched', 'other', "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))"
    )
    seeded.commit()

    seeded.execute("DELETE FROM agents WHERE id = 'validator'")
    seeded.commit()

    untouched = seeded.execute(
        "SELECT validation_agent_id FROM memories WHERE content = 'untouched'"
    ).fetchone()[0]
    assert untouched == 'other'


# ---------------------------------------------------------------------------
# Item 1b — memory hard-delete cascades knowledge_edges
# ---------------------------------------------------------------------------

def _insert_edge(conn, src_t, src_id, tgt_t, tgt_id, rel='related'):
    conn.execute(
        "INSERT INTO knowledge_edges "
        "(source_table, source_id, target_table, target_id, relation_type) "
        "VALUES (?, ?, ?, ?, ?)",
        (src_t, src_id, tgt_t, tgt_id, rel),
    )


def test_memory_delete_cascades_edges_as_source(seeded):
    """Edge where the deleted memory is the SOURCE must vanish."""
    mid = seeded.execute("SELECT id FROM memories LIMIT 1").fetchone()[0]
    _insert_edge(seeded, 'memories', mid, 'memories', 999)
    seeded.commit()

    pre = seeded.execute(
        "SELECT COUNT(*) FROM knowledge_edges WHERE source_table='memories' AND source_id=?",
        (mid,),
    ).fetchone()[0]
    assert pre == 1

    seeded.execute("DELETE FROM memories WHERE id = ?", (mid,))
    seeded.commit()

    post = seeded.execute(
        "SELECT COUNT(*) FROM knowledge_edges WHERE source_table='memories' AND source_id=?",
        (mid,),
    ).fetchone()[0]
    assert post == 0, "edge with deleted-memory source was not cascaded"


def test_memory_delete_cascades_edges_as_target(seeded):
    """Edge where the deleted memory is the TARGET must also vanish.

    The task spec phrased this as "null out target_id WHERE target_table='memories'",
    but ``target_id`` is NOT NULL in the live schema; cascade-delete is the only
    semantically correct option (an edge to a deleted referent is meaningless)."""
    mid = seeded.execute("SELECT id FROM memories LIMIT 1").fetchone()[0]
    _insert_edge(seeded, 'memories', 999, 'memories', mid)
    seeded.commit()

    seeded.execute("DELETE FROM memories WHERE id = ?", (mid,))
    seeded.commit()

    post = seeded.execute(
        "SELECT COUNT(*) FROM knowledge_edges WHERE target_table='memories' AND target_id=?",
        (mid,),
    ).fetchone()[0]
    assert post == 0


def test_memory_delete_does_not_touch_unrelated_edges(seeded):
    """Edges that don't involve the deleted memory must remain."""
    mid = seeded.execute("SELECT id FROM memories LIMIT 1").fetchone()[0]
    _insert_edge(seeded, 'memories', mid, 'memories', 999)
    _insert_edge(seeded, 'memories', 7777, 'memories', 8888, rel='unrelated')
    seeded.commit()

    seeded.execute("DELETE FROM memories WHERE id = ?", (mid,))
    seeded.commit()

    survivors = seeded.execute(
        "SELECT COUNT(*) FROM knowledge_edges WHERE source_id = 7777"
    ).fetchone()[0]
    assert survivors == 1


# ---------------------------------------------------------------------------
# Item 1c — entity & event hard-delete cascade knowledge_edges
# ---------------------------------------------------------------------------

def test_entity_delete_cascades_edges(seeded):
    seeded.execute(
        "INSERT INTO entities (name, entity_type, agent_id, properties, observations) "
        "VALUES ('Acme', 'organization', 'alice', '{}', '[]')"
    )
    eid = seeded.execute("SELECT id FROM entities WHERE name='Acme'").fetchone()[0]
    _insert_edge(seeded, 'entities', eid, 'entities', 12345)
    _insert_edge(seeded, 'memories', 99, 'entities', eid)
    seeded.commit()

    seeded.execute("DELETE FROM entities WHERE id = ?", (eid,))
    seeded.commit()

    cnt = seeded.execute(
        "SELECT COUNT(*) FROM knowledge_edges WHERE "
        "(source_table='entities' AND source_id=?) OR "
        "(target_table='entities' AND target_id=?)",
        (eid, eid),
    ).fetchone()[0]
    assert cnt == 0


def test_event_delete_cascades_edges(seeded):
    seeded.execute(
        "INSERT INTO events (agent_id, event_type, summary, created_at) "
        "VALUES ('alice', 'observation', 'something happened', "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))"
    )
    eid = seeded.execute(
        "SELECT id FROM events WHERE summary = 'something happened'"
    ).fetchone()[0]
    _insert_edge(seeded, 'events', eid, 'memories', 1234)
    seeded.commit()

    seeded.execute("DELETE FROM events WHERE id = ?", (eid,))
    seeded.commit()

    cnt = seeded.execute(
        "SELECT COUNT(*) FROM knowledge_edges WHERE "
        "source_table='events' AND source_id=?",
        (eid,),
    ).fetchone()[0]
    assert cnt == 0


# ---------------------------------------------------------------------------
# Item 2 — memories_fts purge on retire
# ---------------------------------------------------------------------------

def test_retire_memory_purges_fts_row(seeded):
    """When ``retired_at`` transitions NULL → non-NULL, the matching FTS5
    row must disappear immediately, not wait for a vacuum or
    cmd_vec_purge_retired pass.

    Critical: this also verifies FTS5 segment integrity after the purge.
    A naive ``DELETE FROM memories_fts WHERE rowid = ?`` on a content-linked
    FTS5 looks like it works (MATCH returns 0) but corrupts the segment
    structure, surfacing later as "database disk image is malformed". The
    PRAGMA integrity_check at the end is the canary for that."""
    # Use a unique token so we don't collide with seed content.
    token = "uniqueretirementtokenzeta"
    seeded.execute(
        "INSERT INTO memories (agent_id, category, content, "
        "created_at, updated_at) "
        "VALUES ('alice', 'project', ?, "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (token,),
    )
    mid = seeded.execute("SELECT id FROM memories WHERE content = ?", (token,)).fetchone()[0]
    seeded.commit()

    # Pre-condition: FTS row exists.
    pre_hits = seeded.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
        (token,),
    ).fetchone()[0]
    assert pre_hits == 1, (
        f"setup expected 1 fts hit for unique token, got {pre_hits} — "
        f"check that memories_fts_insert trigger fires on this schema variant"
    )

    # Retire: NULL → non-NULL transition on retired_at.
    seeded.execute(
        "UPDATE memories SET retired_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
        "WHERE id = ?", (mid,),
    )
    seeded.commit()

    # FTS row should be gone — MATCH must miss it.
    post_hits = seeded.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
        (token,),
    ).fetchone()[0]
    assert post_hits == 0, (
        f"expected 0 fts hits after retire, got {post_hits} — "
        f"trg_memories_fts_purge_on_retire did not fire or fired before "
        f"memories_fts_update re-inserted"
    )

    # Integrity canary: a follow-up MATCH-driven query must not surface
    # corruption. The earlier "DELETE FROM memories_fts WHERE rowid"
    # implementation passed the MATCH==0 assertion above but corrupted
    # the segment metadata; this catches that class of regression.
    integrity = seeded.execute("PRAGMA integrity_check").fetchall()
    assert integrity == [("ok",)], (
        f"PRAGMA integrity_check failed after retire: {integrity}. "
        f"The retire trigger may be using a non-FTS5-safe DELETE form."
    )

    # Insert another memory after the retire to confirm FTS5 is still writable.
    seeded.execute(
        "INSERT INTO memories (agent_id, category, content, "
        "created_at, updated_at) "
        "VALUES ('alice', 'project', 'postretireposttokenetazeta', "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))"
    )
    seeded.commit()
    post_hits2 = seeded.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'postretireposttokenetazeta'"
    ).fetchone()[0]
    assert post_hits2 == 1, "FTS5 became unwritable after retire trigger fired"


def test_retire_memory_does_not_purge_other_memories_fts(seeded):
    """Retiring memory A must not affect memory B's FTS row."""
    seeded.execute(
        "INSERT INTO memories (agent_id, category, content, created_at, updated_at) "
        "VALUES ('alice', 'project', 'tokenAAA', "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))"
    )
    seeded.execute(
        "INSERT INTO memories (agent_id, category, content, created_at, updated_at) "
        "VALUES ('alice', 'project', 'tokenBBB', "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))"
    )
    seeded.commit()
    a_id = seeded.execute("SELECT id FROM memories WHERE content='tokenAAA'").fetchone()[0]

    seeded.execute(
        "UPDATE memories SET retired_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
        "WHERE id = ?", (a_id,)
    )
    seeded.commit()

    bbb_hits = seeded.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'tokenBBB'"
    ).fetchone()[0]
    assert bbb_hits == 1, "unrelated memory's FTS row was incorrectly purged"


def test_subsequent_update_after_retire_does_not_resurrect_fts(seeded):
    """Once retired, further UPDATEs must not re-insert the FTS row.

    The split-pair pattern handles this naturally: ``memories_fts_update_insert``
    has ``WHEN ... AND new.retired_at IS NULL``, so any UPDATE on a row whose
    ``retired_at`` is non-NULL will not re-insert it into FTS5. The
    companion ``_update_delete`` will still fire (when ``old.indexed = 1``),
    keeping FTS clean. So this test exercises the right path: insert, retire,
    edit-the-retired-row, verify FTS still empty for the unique token."""
    token = "subsequenttokensigma"
    seeded.execute(
        "INSERT INTO memories (agent_id, category, content, "
        "created_at, updated_at) "
        "VALUES ('alice', 'project', ?, "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "  strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (token,),
    )
    mid = seeded.execute(
        "SELECT id FROM memories WHERE content = ?", (token,)
    ).fetchone()[0]
    seeded.commit()

    # Retire.
    seeded.execute(
        "UPDATE memories SET retired_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
        "WHERE id = ?", (mid,),
    )
    seeded.commit()
    assert seeded.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
        (token,),
    ).fetchone()[0] == 0

    # Edit something else on the retired row — must not resurrect FTS entry.
    seeded.execute(
        "UPDATE memories SET confidence = 0.42 WHERE id = ?", (mid,)
    )
    seeded.commit()

    after_edit = seeded.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
        (token,),
    ).fetchone()[0]
    assert after_edit == 0, (
        f"editing a retired memory resurrected its FTS row "
        f"(count={after_edit}). The _update_insert guard must include "
        f"new.retired_at IS NULL."
    )


# ---------------------------------------------------------------------------
# Idempotency — re-applying the migration must be a no-op
# ---------------------------------------------------------------------------

def test_migration_048_idempotent(tmp_path):
    """Apply migration 048 twice in a row — second application must succeed
    silently. CREATE TRIGGER IF NOT EXISTS handles the cascade triggers;
    the FTS5 split-pair conversion uses DROP IF EXISTS + CREATE IF NOT
    EXISTS so re-application is also a no-op. The schema_version INSERT
    is allowed to duplicate; that table is an audit log, not a primary key
    constraint."""
    db_file = tmp_path / "brain.db"
    conn = _build_db(db_file)

    migration_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "agentmemory" / "db" / "migrations"
        / "048_fk_integrity_fts_retire_trigger.sql"
    )
    assert migration_path.exists(), f"migration not found at {migration_path}"

    sql = migration_path.read_text()

    conn.executescript(sql)
    conn.commit()

    try:
        conn.executescript(sql)
        conn.commit()
    except sqlite3.OperationalError as exc:
        pytest.fail(f"second application of migration 048 failed: {exc}")

    # Third application also no-op-safe.
    try:
        conn.executescript(sql)
        conn.commit()
    except sqlite3.OperationalError as exc:
        pytest.fail(f"third application of migration 048 failed: {exc}")

    conn.close()


def test_migration_048_converges_legacy_single_trigger(tmp_path):
    """Pin the dev-style → packaged-style convergence behavior on a
    simulated legacy DB.

    Background: some user brain.db files were marked as having migration 031
    (which split the trigger) applied via ``--mark-applied-up-to`` without
    actually running its body. Those DBs still carry the OLD single
    ``memories_fts_update`` trigger. Migration 048 must replace it with
    the split pair on those DBs."""
    # Build a DB from packaged init_schema, then manually inject the OLD
    # single trigger to simulate the legacy state.
    db_file = tmp_path / "legacy.db"
    conn = _build_db(db_file)
    # Strip the new split triggers and install the legacy one.
    conn.execute("DROP TRIGGER IF EXISTS memories_fts_update_delete")
    conn.execute("DROP TRIGGER IF EXISTS memories_fts_update_insert")
    conn.execute(
        "CREATE TRIGGER memories_fts_update AFTER UPDATE ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) "
        "VALUES('delete', old.id, old.content, old.category, old.tags); "
        "INSERT INTO memories_fts(rowid, content, category, tags) "
        "VALUES (new.id, new.content, new.category, new.tags); "
        "END"
    )
    conn.commit()

    # Confirm pre-state.
    pre_trigs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'memories_fts_update%'"
        ).fetchall()
    }
    assert pre_trigs == {"memories_fts_update"}, (
        f"legacy seed unexpectedly produced {pre_trigs}"
    )

    # Apply migration 048.
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "agentmemory" / "db" / "migrations"
        / "048_fk_integrity_fts_retire_trigger.sql"
    )
    conn.executescript(migration_path.read_text())
    conn.commit()

    # Post-state: legacy trigger is gone, split pair is present.
    post_trigs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'memories_fts_update%'"
        ).fetchall()
    }
    assert post_trigs == {
        "memories_fts_update_delete",
        "memories_fts_update_insert",
    }, f"convergence failed: post-trigger set is {post_trigs}"

    conn.close()
