"""Tests for brainctl migrate command."""
import json
import os
import sqlite3
import tempfile
import pytest
from pathlib import Path

from agentmemory.brain import Brain
from agentmemory import migrate


@pytest.fixture
def fresh_db(tmp_path):
    db_path = str(tmp_path / "brain.db")
    Brain(db_path, agent_id="default")
    return db_path


@pytest.fixture
def bare_db(tmp_path):
    """A minimal SQLite DB with no schema — migrations can apply cleanly."""
    db_path = str(tmp_path / "bare.db")
    conn = sqlite3.connect(db_path)
    conn.close()
    return db_path


class TestMigrateStatus:
    def test_status_returns_dict(self, fresh_db):
        result = migrate.status(fresh_db)
        assert "total" in result
        assert "applied" in result
        assert "pending" in result
        assert isinstance(result["pending_migrations"], list)

    def test_fresh_db_has_pending_or_applied(self, fresh_db):
        result = migrate.status(fresh_db)
        # total should equal applied + pending
        assert result["total"] == result["applied"] + result["pending"]

    def test_status_creates_schema_versions_table(self, fresh_db):
        migrate.status(fresh_db)
        conn = sqlite3.connect(fresh_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "schema_versions" in tables

    def test_status_shows_migrations_dir_contents(self, fresh_db):
        result = migrate.status(fresh_db)
        # We have 31 migration files (non-quantum)
        assert result["total"] > 0

    def test_pending_migrations_have_expected_keys(self, fresh_db):
        result = migrate.status(fresh_db)
        for entry in result["pending_migrations"]:
            assert "version" in entry
            assert "name" in entry
            assert "file" in entry


class TestMigrateRun:
    def test_dry_run_returns_dry_run_flag(self, fresh_db):
        result = migrate.run(fresh_db, dry_run=True)
        assert result["dry_run"] is True

    def test_dry_run_does_not_write(self, fresh_db):
        status_before = migrate.status(fresh_db)
        migrate.run(fresh_db, dry_run=True)
        status_after = migrate.status(fresh_db)
        assert status_before["pending"] == status_after["pending"]

    def test_dry_run_lists_migrations(self, fresh_db):
        result = migrate.run(fresh_db, dry_run=True)
        # dry_run should list all pending migrations without applying them
        assert isinstance(result["migrations"], list)
        if result["applied"] > 0:
            for m in result["migrations"]:
                assert m.get("dry_run") is True

    def test_already_up_to_date(self, fresh_db):
        # Mark all migrations as applied manually
        conn = sqlite3.connect(fresh_db)
        migrate._ensure_schema_versions(conn)
        for version, name, path in migrate._get_migrations():
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, migrate._utc_now_iso())
            )
        conn.commit()
        conn.close()

        # Run again — should be no-op
        result = migrate.run(fresh_db)
        assert result["ok"] is True
        assert result["applied"] == 0
        assert "Already up to date" in result.get("message", "")

    def test_idempotent_when_up_to_date(self, fresh_db):
        # Mark all as applied
        conn = sqlite3.connect(fresh_db)
        migrate._ensure_schema_versions(conn)
        for version, name, path in migrate._get_migrations():
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, migrate._utc_now_iso())
            )
        conn.commit()
        conn.close()

        r1 = migrate.run(fresh_db)
        r2 = migrate.run(fresh_db)
        assert r2["applied"] == 0  # nothing new to apply

    def test_run_result_has_ok_field(self, fresh_db):
        result = migrate.run(fresh_db)
        assert "ok" in result

    def test_run_result_has_applied_count(self, fresh_db):
        result = migrate.run(fresh_db)
        assert "applied" in result
        assert isinstance(result["applied"], int)


class TestMigrateGetMigrations:
    def test_returns_list_of_tuples(self):
        migrations = migrate._get_migrations()
        assert isinstance(migrations, list)
        assert len(migrations) > 0

    def test_tuples_have_correct_shape(self):
        migrations = migrate._get_migrations()
        for version, name, path in migrations:
            assert isinstance(version, int)
            assert isinstance(name, str)
            assert isinstance(path, Path)

    def test_sorted_by_version(self):
        migrations = migrate._get_migrations()
        versions = [v for v, _, _ in migrations]
        assert versions == sorted(versions)

    def test_excludes_non_numbered_files(self):
        # quantum_schema_migration_sqlite.sql should NOT be included
        migrations = migrate._get_migrations()
        filenames = [str(p.name) for _, _, p in migrations]
        for f in filenames:
            assert f[0].isdigit(), f"Non-numbered file included: {f}"
