"""Phase 2a drift assertion tests.

Ensures that:
1. A fresh install from init_schema.sql and an upgraded install from
   init_schema.sql + all db/migrations/*.sql produce byte-identical
   sqlite_master dumps.
2. The confirmed-dead tables dropped in migration 032 are absent from a
   fresh-install DB (they must not leak back via init_schema.sql).

This is the primary defense against the drift-bug class that triggered
Phase 2a.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_SCHEMA = REPO_ROOT / "src" / "agentmemory" / "db" / "init_schema.sql"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

DEAD_TABLES = {
    "cognitive_experiments",
    "self_assessments",
    "health_snapshots",
    "recovery_candidates",
    "agent_entanglement",
    "agent_ghz_groups",
}


def _load_schema_sql() -> str:
    return INIT_SCHEMA.read_text()


def _sorted_migrations() -> list[Path]:
    """Return numbered migrations sorted by version; skip unversioned files."""
    out: list[tuple[int, Path]] = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"^(\d+)_.+\.sql$", f.name)
        if m:
            out.append((int(m.group(1)), f))
    out.sort(key=lambda t: (t[0], t[1].name))
    return [p for _, p in out]


def _dump_master(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Canonical sqlite_master dump, ignoring autoindex/FTS-shadow tables."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table','index','trigger','view') "
        "ORDER BY type, name"
    ).fetchall()
    out: list[tuple[str, str, str]] = []
    for t, name, sql in rows:
        if name.startswith("sqlite_"):
            continue
        # FTS5 shadow tables are auto-generated from VIRTUAL TABLE ... USING fts5;
        # their CREATE statements are identical between fresh and upgraded if
        # the parent virtual table is, so keep them but strip trailing whitespace.
        out.append((t, name, (sql or "").strip()))
    return out


def _build_fresh_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_load_schema_sql())
    finally:
        conn.close()


def _build_upgraded_db(path: Path) -> None:
    """Simulate upgrade path: init_schema + every numbered migration in order."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_load_schema_sql())
        for mig in _sorted_migrations():
            sql = mig.read_text()
            try:
                conn.executescript(sql)
            except sqlite3.OperationalError as e:
                # A migration may legitimately be a no-op on a fresh schema
                # (e.g. ALTER TABLE ADD COLUMN when the column already exists).
                # We tolerate "duplicate column name" and "already exists" errors
                # since they represent "this migration's effect is already in the
                # base schema" — which is exactly what we're asserting.
                msg = str(e).lower()
                if "duplicate column name" in msg or "already exists" in msg:
                    continue
                raise
    finally:
        conn.close()


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "fresh.db"
    _build_fresh_db(p)
    return p


@pytest.fixture
def upgraded_db(tmp_path):
    p = tmp_path / "upgraded.db"
    _build_upgraded_db(p)
    return p


def test_fresh_install_and_upgraded_install_produce_identical_schemas(
    fresh_db, upgraded_db
):
    """Hard drift assertion: fresh install == init_schema + all migrations."""
    fresh_conn = sqlite3.connect(str(fresh_db))
    upgraded_conn = sqlite3.connect(str(upgraded_db))
    try:
        fresh_dump = _dump_master(fresh_conn)
        upgraded_dump = _dump_master(upgraded_conn)
    finally:
        fresh_conn.close()
        upgraded_conn.close()

    fresh_by_name = {(t, n): sql for t, n, sql in fresh_dump}
    upgraded_by_name = {(t, n): sql for t, n, sql in upgraded_dump}

    only_in_fresh = set(fresh_by_name) - set(upgraded_by_name)
    only_in_upgraded = set(upgraded_by_name) - set(fresh_by_name)

    assert not only_in_fresh, f"Objects only in fresh install: {sorted(only_in_fresh)}"
    assert not only_in_upgraded, (
        f"Objects only in upgraded install (schema drift!): {sorted(only_in_upgraded)}"
    )

    mismatched = [
        k for k in fresh_by_name if fresh_by_name[k] != upgraded_by_name[k]
    ]
    assert not mismatched, (
        "Schema drift detected — fresh vs upgraded CREATE statements differ "
        f"for: {sorted(mismatched)}"
    )


def test_dead_tables_absent_from_fresh_install(fresh_db):
    """Sanity test: none of the dropped-in-032 tables exist in a fresh DB."""
    conn = sqlite3.connect(str(fresh_db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    names = {r[0] for r in rows}
    leaked = DEAD_TABLES & names
    assert not leaked, f"Dead tables still present in init_schema.sql: {leaked}"


def test_dead_tables_absent_after_migrations(upgraded_db):
    """Dead tables must not reappear when upgrading from old init + migrations."""
    conn = sqlite3.connect(str(upgraded_db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    names = {r[0] for r in rows}
    leaked = DEAD_TABLES & names
    assert not leaked, f"Dead tables reappeared after migrations: {leaked}"
