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

    def test_includes_procedural_memory_layer_migration(self):
        migrations = migrate._get_migrations()
        versions = [v for v, _, _ in migrations]
        assert 52 in versions

    def test_excludes_non_numbered_files(self):
        # quantum_schema_migration_sqlite.sql should NOT be included
        migrations = migrate._get_migrations()
        filenames = [str(p.name) for _, _, p in migrations]
        for f in filenames:
            assert f[0].isdigit(), f"Non-numbered file included: {f}"


class TestMarkAppliedUpTo:
    """Tests for migrate.mark_applied_up_to (backfill command)."""

    def test_virgin_tracker_dry_run(self, bare_db):
        """Dry-run against empty tracker lists would_mark, writes nothing."""
        result = migrate.mark_applied_up_to(bare_db, 31, dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert len(result["would_mark"]) > 0
        assert all(m["version"] <= 31 for m in result["would_mark"])

        # Dry-run must not persist
        conn = sqlite3.connect(bare_db)
        migrate._ensure_schema_versions(conn)
        rows = conn.execute("SELECT * FROM schema_versions").fetchall()
        conn.close()
        assert rows == []

    def test_virgin_tracker_real_backfill(self, bare_db):
        """Real backfill writes rows with '(backfilled)' name suffix."""
        result = migrate.mark_applied_up_to(bare_db, 31)
        assert result["ok"] is True
        assert result["dry_run"] is False
        assert len(result["marked"]) > 0

        conn = sqlite3.connect(bare_db)
        rows = conn.execute(
            "SELECT version, name FROM schema_versions ORDER BY version"
        ).fetchall()
        conn.close()
        assert len(rows) == len(result["marked"])
        assert all(version <= 31 for version, _ in rows)
        assert all("(backfilled)" in name for _, name in rows)

    def test_idempotent_rerun(self, bare_db):
        """Re-running up to the same N is a no-op."""
        migrate.mark_applied_up_to(bare_db, 31)
        result = migrate.mark_applied_up_to(bare_db, 31)
        assert result["ok"] is True
        assert result["marked"] == []
        assert "already at or above" in result.get("message", "")

    def test_extend_above_high_water_mark(self, bare_db):
        """Backfilling ABOVE current max is allowed and marks only new rows."""
        migrate.mark_applied_up_to(bare_db, 20)
        conn = sqlite3.connect(bare_db)
        first_max = conn.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0]
        conn.close()

        result = migrate.mark_applied_up_to(bare_db, 32)
        assert result["ok"] is True
        assert result["previous_max_tracked"] == first_max
        new_versions = {m["version"] for m in result["marked"]}
        assert 32 in new_versions
        assert 20 not in new_versions  # already tracked, must not re-mark

    def test_guard_refuses_below_high_water_mark(self, bare_db):
        """Cannot backfill below what's already tracked."""
        migrate.mark_applied_up_to(bare_db, 32)
        result = migrate.mark_applied_up_to(bare_db, 10)
        assert result["ok"] is False
        assert "guard_error" in result
        assert "lower than the highest already-tracked" in result["guard_error"]
        assert result["max_tracked"] == 32

    def test_guard_refuses_above_max_available(self, bare_db):
        """Cannot backfill past the highest migration file on disk."""
        result = migrate.mark_applied_up_to(bare_db, 9999)
        assert result["ok"] is False
        assert "exceeds highest" in result["guard_error"]

    def test_guard_refuses_zero_or_negative(self, bare_db):
        """N must be >= 1."""
        for n in (0, -1, -100):
            result = migrate.mark_applied_up_to(bare_db, n)
            assert result["ok"] is False
            assert "must be >= 1" in result["guard_error"]

    def test_partial_tracker_extends_cleanly(self, bare_db):
        """User who ran migrate partially (rows 2-5 tracked) can extend up."""
        conn = sqlite3.connect(bare_db)
        migrate._ensure_schema_versions(conn)
        for v in (2, 3, 4, 5):
            conn.execute(
                "INSERT INTO schema_versions (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (v, f"partial-{v}", "2026-01-01T00:00:00Z"),
            )
        conn.commit()
        conn.close()

        result = migrate.mark_applied_up_to(bare_db, 30)
        assert result["ok"] is True
        marked_versions = {m["version"] for m in result["marked"]}
        # Already-tracked versions must be skipped
        assert 2 not in marked_versions
        assert 5 not in marked_versions
        # New versions must be added
        assert 10 in marked_versions
        assert 30 in marked_versions
        assert result["previous_max_tracked"] == 5

    def test_partial_tracker_still_refuses_lower_n(self, bare_db):
        """Even with a partial tracker, going below max_tracked is refused."""
        conn = sqlite3.connect(bare_db)
        migrate._ensure_schema_versions(conn)
        conn.execute(
            "INSERT INTO schema_versions VALUES (20, 'test-20', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        result = migrate.mark_applied_up_to(bare_db, 5)
        assert result["ok"] is False
        assert "guard_error" in result

    def test_duplicate_versions_collapsed_to_one_row(self, bare_db):
        """Migrations with duplicate version numbers (012/013/017/021/023)
        collapse into one schema_versions row per version, not two."""
        migrate.mark_applied_up_to(bare_db, 31)
        conn = sqlite3.connect(bare_db)
        rows = conn.execute(
            "SELECT version, COUNT(*) FROM schema_versions "
            "GROUP BY version HAVING COUNT(*) > 1"
        ).fetchall()
        conn.close()
        assert rows == [], f"duplicate version rows found: {rows}"

    def test_subsequent_migrate_run_skips_backfilled_versions(self, bare_db):
        """After backfill, `brainctl migrate` must not re-apply tracked versions."""
        # Virgin tracker + bare db → all migrations are pending
        before = migrate.status(bare_db)
        pending_before = before["pending"]
        assert pending_before > 0

        migrate.mark_applied_up_to(bare_db, 31)

        after = migrate.status(bare_db)
        # Every migration with version <= 31 should now be tracked
        pending_versions_after = {m["version"] for m in after["pending_migrations"]}
        assert all(v > 31 for v in pending_versions_after)


class TestStatusVerbose:
    """Tests for migrate.status_verbose (DDL heuristic)."""

    def test_status_verbose_extends_status(self, fresh_db):
        """status_verbose contains everything status() does plus migrations_verbose."""
        base = migrate.status(fresh_db)
        verbose = migrate.status_verbose(fresh_db)
        for key in ("total", "applied", "pending", "applied_migrations", "pending_migrations"):
            assert key in verbose
        assert "migrations_verbose" in verbose
        assert verbose["total"] == base["total"]

    def test_status_verbose_classifies_every_migration(self, fresh_db):
        """Every migration file gets exactly one heuristic bucket."""
        result = migrate.status_verbose(fresh_db)
        allowed = {"likely-applied", "partial", "pending", "unknown"}
        for m in result["migrations_verbose"]:
            assert m["heuristic"] in allowed
            assert "ddl_hits" in m
            assert "version" in m
            assert "file" in m

    def test_fresh_db_after_init_shows_migrations_as_likely_applied(self, fresh_db):
        """A fresh db created via Brain() runs init_schema.sql which contains
        the cumulative effects of all migrations. The heuristic should see
        most DDL as already present."""
        result = migrate.status_verbose(fresh_db)
        buckets: dict[str, int] = {}
        for m in result["migrations_verbose"]:
            buckets[m["heuristic"]] = buckets.get(m["heuristic"], 0) + 1
        # At least SOME migrations should classify as likely-applied on a
        # Brain()-initialized db. If none do, the heuristic is broken or
        # init_schema.sql has regressed.
        assert buckets.get("likely-applied", 0) > 0, f"buckets={buckets}"

    def test_bare_db_shows_migrations_as_pending(self, bare_db):
        """An empty sqlite file has no tables → DDL-based migrations should
        classify as 'pending'."""
        result = migrate.status_verbose(bare_db)
        buckets: dict[str, int] = {}
        for m in result["migrations_verbose"]:
            buckets[m["heuristic"]] = buckets.get(m["heuristic"], 0) + 1
        # Migrations with introspectable DDL against an empty db should be pending
        assert buckets.get("pending", 0) > 0, f"buckets={buckets}"
        # likely-applied should be zero or near-zero
        assert buckets.get("likely-applied", 0) == 0, f"buckets={buckets}"

    def test_update_only_migrations_classified_as_unknown(self, fresh_db):
        """Migration 006 (timestamp_normalization) has only UPDATE statements —
        no DDL to introspect. Should land in 'unknown'."""
        result = migrate.status_verbose(fresh_db)
        m006 = next(
            (m for m in result["migrations_verbose"] if m["version"] == 6),
            None,
        )
        assert m006 is not None, "migration 006 not found"
        assert m006["heuristic"] == "unknown"

    def test_generated_virtual_columns_detected(self, tmp_path):
        """Migration 024 adds confidence_alpha/beta as generated virtual
        columns via `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS AS (...)
        VIRTUAL`. PRAGMA table_info HIDES these columns; PRAGMA table_xinfo
        reveals them. If status_verbose uses the wrong pragma, v24 will
        false-negative as 'pending' even on a db that has the columns.

        Regression test for a bug discovered in production: a brain.db that
        had been through a fresh Brain() init (which runs init_schema.sql
        containing the cumulative generated columns) was misdiagnosed as
        needing migration 024. Walking the recovery workflow crashed on
        'duplicate column name: confidence_alpha' because the columns did
        in fact exist — invisible to the heuristic's table_info query.
        """
        db = str(tmp_path / "gen.db")
        conn = sqlite3.connect(db)
        # Minimal memories-like table with the generated virtual columns
        # that migration 024 adds. status_verbose must see them.
        conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY,
                alpha REAL NOT NULL DEFAULT 1.0,
                beta  REAL NOT NULL DEFAULT 1.0
            );
            ALTER TABLE memories ADD COLUMN
                confidence_alpha REAL GENERATED ALWAYS AS (alpha) VIRTUAL;
            ALTER TABLE memories ADD COLUMN
                confidence_beta  REAL GENERATED ALWAYS AS (beta)  VIRTUAL;
            """
        )
        conn.commit()
        conn.close()

        result = migrate.status_verbose(db)
        m024 = next(
            (m for m in result["migrations_verbose"] if m["version"] == 24),
            None,
        )
        assert m024 is not None, "migration 024 not found"
        # Both columns exist → should classify as likely-applied (2/2)
        assert m024["heuristic"] == "likely-applied", (
            f"expected likely-applied, got {m024['heuristic']} ({m024['ddl_hits']}) — "
            f"status_verbose is probably using PRAGMA table_info which hides "
            f"generated virtual columns; must use table_xinfo"
        )
        assert m024["ddl_hits"] == "2/2"

    def test_add_column_if_not_exists_regex(self, tmp_path):
        """The heuristic's regex must tolerate `ADD COLUMN IF NOT EXISTS <col>`
        and capture the actual column name, not `IF`.

        Originally lived in migration 023; in v2.2.0 the dupe-version rename
        moved the uncertainty_log columns to migration 046. The
        `ADD COLUMN IF NOT EXISTS` syntax in the source file was a SQLite
        syntax error and was removed during the rename, but
        ``status_verbose`` still uses that regex to parse OTHER historical
        migrations (and any future ones that lazily wrote the modifier),
        so the regression coverage is still valuable.
        """
        db = str(tmp_path / "ifnotexists.db")
        conn = sqlite3.connect(db)
        # Stand up a table that mimics what the renamed migration 046
        # expects to find. status_verbose's regex should classify it as
        # likely-applied.
        conn.executescript(
            """
            CREATE TABLE agent_uncertainty_log (
                id INTEGER PRIMARY KEY,
                domain TEXT,
                query TEXT,
                result_count INTEGER,
                avg_confidence REAL,
                retrieved_at DATETIME,
                temporal_class TEXT,
                ttl_days INTEGER
            );
            """
        )
        conn.commit()
        conn.close()

        result = migrate.status_verbose(db)
        uncertainty = next(
            (m for m in result["migrations_verbose"] if m["version"] == 46),
            None,
        )
        assert uncertainty is not None, (
            "migration 046 uncertainty_log_search_columns not found"
        )
        assert "uncertainty" in uncertainty["name"]
        # All 7 ADD COLUMN columns present → should be 7/7
        assert uncertainty["ddl_hits"] == "7/7", (
            f"expected 7/7, got {uncertainty['ddl_hits']} — regex is "
            f"probably capturing 'IF' as the column name from any "
            f"`ADD COLUMN IF NOT EXISTS <col>` in migration files"
        )
        assert uncertainty["heuristic"] == "likely-applied"


class TestBrainMigrationWarning:
    """Tests for Brain.__init__ pending-migrations warning (T3).

    Branching matters here — virgin tracker vs partial tracker get different
    advice. Virgin must NOT say "run brainctl migrate" (that's the exact
    footgun v1.5.0 is closing).
    """

    def _construct_brain_capturing_warnings(self, db_path, caplog, env=None):
        """Helper: construct Brain with a fresh dedupe set and capture warnings."""
        import importlib
        from agentmemory import brain as brain_module
        # Reset per-process dedupe so each test starts clean
        brain_module._MIGRATION_WARNINGS_EMITTED.clear()
        if env:
            for k, v in env.items():
                os.environ[k] = v
        else:
            os.environ.pop("BRAINCTL_SILENT_MIGRATIONS", None)
        caplog.clear()
        with caplog.at_level("WARNING", logger="agentmemory.brain"):
            brain_module.Brain(db_path=db_path, agent_id="test")
        # Clean up env mutation
        if env:
            for k in env:
                os.environ.pop(k, None)
        return [r for r in caplog.records if r.name == "agentmemory.brain"]

    def test_virgin_tracker_warns_about_doctor_not_migrate(self, bare_db, caplog):
        """Empty schema_versions + pending migrations → advise `brainctl doctor`,
        NEVER `brainctl migrate` (would crash on column collisions)."""
        records = self._construct_brain_capturing_warnings(bare_db, caplog)
        assert len(records) == 1, f"expected 1 warning, got {len(records)}"
        msg = records[0].getMessage()
        assert "not initialized" in msg
        assert "brainctl doctor" in msg or "status-verbose" in msg
        # Critical: must NOT tell virgin-tracker users to blindly migrate
        assert "run `brainctl migrate`" not in msg

    def test_tracked_pending_warns_about_migrate(self, bare_db, caplog):
        """Partial tracker (applied > 0) + pending > 0 → advise `brainctl migrate`."""
        # Seed a partial tracker
        conn = sqlite3.connect(bare_db)
        migrate._ensure_schema_versions(conn)
        conn.execute(
            "INSERT INTO schema_versions VALUES (2, 'seed', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        records = self._construct_brain_capturing_warnings(bare_db, caplog)
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "pending" in msg
        assert "brainctl migrate" in msg

    def test_up_to_date_is_silent(self, bare_db, caplog):
        """No pending migrations → no warning."""
        # Mark every migration as applied
        conn = sqlite3.connect(bare_db)
        migrate._ensure_schema_versions(conn)
        for v, n, _ in migrate._get_migrations():
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions VALUES (?, ?, '2026-01-01T00:00:00Z')",
                (v, n),
            )
        conn.commit()
        conn.close()

        records = self._construct_brain_capturing_warnings(bare_db, caplog)
        assert records == [], f"expected silence, got {[r.getMessage() for r in records]}"

    def test_silent_env_var_suppresses_warning(self, bare_db, caplog):
        """BRAINCTL_SILENT_MIGRATIONS=1 gags the warning for CI/tests."""
        records = self._construct_brain_capturing_warnings(
            bare_db, caplog, env={"BRAINCTL_SILENT_MIGRATIONS": "1"}
        )
        assert records == []

    def test_dedupe_across_multiple_constructions(self, bare_db, caplog):
        """Multiple Brain() constructions for the same db only warn once."""
        from agentmemory import brain as brain_module
        brain_module._MIGRATION_WARNINGS_EMITTED.clear()
        os.environ.pop("BRAINCTL_SILENT_MIGRATIONS", None)

        with caplog.at_level("WARNING", logger="agentmemory.brain"):
            brain_module.Brain(db_path=bare_db, agent_id="t1")
            brain_module.Brain(db_path=bare_db, agent_id="t2")
            brain_module.Brain(db_path=bare_db, agent_id="t3")

        records = [r for r in caplog.records if r.name == "agentmemory.brain"]
        assert len(records) == 1, f"expected 1 (deduped), got {len(records)}"


class TestDuplicateVersionDetector:
    """Coverage for the v2.2.0 duplicate-version detector that prevents
    the "alphabetical sort silently skips the second file" bug from
    re-shipping. Lives in migrate._check_no_duplicate_versions and runs
    on every status/run/status_verbose call via _get_migrations.
    """

    def test_clean_tree_passes(self):
        """The post-rename tree must have no duplicate version numbers."""
        # Should not raise. The fact that any other test passes proves
        # this implicitly, but being explicit guards against regressions.
        migrate._check_no_duplicate_versions()

    def test_synthetic_dupe_raises(self, tmp_path, monkeypatch):
        """Inject two files at the same version and confirm the detector
        raises with both filenames in the message."""
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        (mdir / "099_first.sql").write_text("-- noop")
        (mdir / "099_second.sql").write_text("-- noop")
        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", mdir)

        with pytest.raises(ValueError) as excinfo:
            migrate._get_migrations()
        msg = str(excinfo.value)
        assert "version 099" in msg
        assert "099_first.sql" in msg
        assert "099_second.sql" in msg

    def test_env_bypass_allows_dupes(self, tmp_path, monkeypatch):
        """BRAINCTL_ALLOW_DUPLICATE_MIGRATIONS=1 lets you audit pre-fix
        branches without crashing the detector."""
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        (mdir / "099_first.sql").write_text("-- noop")
        (mdir / "099_second.sql").write_text("-- noop")
        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", mdir)
        monkeypatch.setenv("BRAINCTL_ALLOW_DUPLICATE_MIGRATIONS", "1")

        # Bypassed → no raise, both files present
        migrations = migrate._get_migrations()
        assert len(migrations) == 2
        assert all(v == 99 for v, _, _ in migrations)


class TestApplySqlIdempotence:
    """Coverage for migrate._apply_sql idempotent error tolerance.

    These guarantee that re-running a migration whose effects are partly
    or wholly already in the schema does not crash the migration. Without
    this, every brain.db that hits a "duplicate column" mid-run leaves
    schema_versions out-of-sync with the actual schema state.
    """

    def test_duplicate_column_swallowed(self, tmp_path):
        """ADD COLUMN of an already-existing column logs a skip note,
        does not raise, and reports stmts_run=0."""
        db = str(tmp_path / "dup.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (a INT, b INT)")
        sql = "ALTER TABLE t ADD COLUMN b INT;"
        run_count, skipped = migrate._apply_sql(conn, sql, "test.sql")
        conn.close()
        assert run_count == 0
        assert len(skipped) == 1
        assert "duplicate column name" in skipped[0]

    def test_create_index_on_missing_column_swallowed(self, tmp_path):
        """CREATE INDEX referencing a column that doesn't exist on an
        existing table is a tolerated skip (legacy column rename case)."""
        db = str(tmp_path / "missing.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (a INT)")
        sql = "CREATE INDEX IF NOT EXISTS idx_missing ON t(missing_col);"
        run_count, skipped = migrate._apply_sql(conn, sql, "test.sql")
        conn.close()
        assert run_count == 0
        assert len(skipped) == 1
        assert "tolerated CREATE INDEX skip" in skipped[0]

    def test_unrelated_select_no_such_column_still_raises(self, tmp_path):
        """no such column in a SELECT/UPDATE must NOT be tolerated —
        that's a real bug, not an idempotency signal."""
        db = str(tmp_path / "raise.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (a INT)")
        sql = "UPDATE t SET nonexistent = 1;"
        with pytest.raises(sqlite3.OperationalError):
            migrate._apply_sql(conn, sql, "test.sql")
        conn.close()


class TestSqlSplitter:
    """Coverage for migrate._split_sql_statements. Specific regression
    tests for the bugs caught while implementing v2.2.0:
      * inline `-- comment` after a terminating `;` must end the statement
      * CREATE TRIGGER ... BEGIN ... END; must be one statement
      * single-quoted string literals containing -- must not start a comment
    """

    def test_terminator_with_inline_comment(self):
        """`('enabled', '1');     -- kill switch` must terminate, not
        absorb the next CREATE TABLE block. (Reproduces the
        044_global_workspace splitter bug.)"""
        sql = (
            "INSERT INTO t (k, v) VALUES ('a', '1');     -- kill switch\n"
            "\n"
            "CREATE TABLE next_thing (id INT);\n"
        )
        stmts = migrate._split_sql_statements(sql)
        assert len(stmts) == 2
        assert "INSERT INTO" in stmts[0]
        assert "CREATE TABLE" in stmts[1]

    def test_trigger_body_kept_as_one_statement(self):
        sql = (
            "CREATE TRIGGER trg AFTER INSERT ON t\n"
            "BEGIN\n"
            "    UPDATE t SET x = x + 1;\n"
            "    INSERT INTO log (msg) VALUES ('fired');\n"
            "END;\n"
            "\n"
            "CREATE INDEX idx_t ON t(x);\n"
        )
        stmts = migrate._split_sql_statements(sql)
        assert len(stmts) == 2
        assert "CREATE TRIGGER" in stmts[0]
        assert "END;" in stmts[0]
        assert "CREATE INDEX" in stmts[1]

    def test_dash_inside_string_literal_not_a_comment(self):
        """A `--` inside a quoted string must not start a line comment."""
        sql = "INSERT INTO t (label) VALUES ('two--dashes');\nSELECT 1;\n"
        stmts = migrate._split_sql_statements(sql)
        assert len(stmts) == 2
        assert "two--dashes" in stmts[0]


class TestDoctorMigrationsCheck:
    """Tests for the migrations section of `brainctl doctor` (T4)."""

    def test_doctor_json_includes_migrations_section(self, fresh_db, monkeypatch, capsys):
        """JSON output must include a 'migrations' key with state info."""
        import argparse
        from agentmemory import _impl
        # Point _impl at our test db
        monkeypatch.setattr(_impl, "DB_PATH", Path(fresh_db))
        # Doctor uses get_db() which reads module-level DB_PATH
        monkeypatch.setattr(_impl, "get_db",
                            lambda *a, **k: sqlite3.connect(fresh_db))

        args = argparse.Namespace(json=True)
        # 2.4.9 (audit I9): doctor now sys.exit(1)s on any issues. A
        # virgin-tracker fixture surfaces one. Handle both paths so the
        # JSON payload assertion is the real subject of the test.
        try:
            _impl.cmd_doctor(args)
        except SystemExit:
            pass
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "migrations" in data
        assert "state" in data["migrations"]
        assert data["migrations"]["state"] in (
            "up-to-date", "pending", "virgin-tracker-clean", "virgin-tracker-with-drift"
        )

    def test_doctor_detects_virgin_tracker_with_drift(self, tmp_path, monkeypatch, capsys):
        """A fresh-init brain.db has late-migration columns (via init_schema.sql)
        but virgin schema_versions → should flag as virgin-tracker-with-drift."""
        import argparse
        from agentmemory import _impl
        from agentmemory.brain import Brain

        db = tmp_path / "drift.db"
        os.environ["BRAINCTL_SILENT_MIGRATIONS"] = "1"
        Brain(str(db))  # init_schema.sql has write_tier, ewc_importance, etc.
        os.environ.pop("BRAINCTL_SILENT_MIGRATIONS", None)

        monkeypatch.setattr(_impl, "DB_PATH", db)
        monkeypatch.setattr(_impl, "get_db",
                            lambda *a, **k: sqlite3.connect(str(db)))

        args = argparse.Namespace(json=True)
        # See note in test_doctor_json_includes_migrations_section —
        # SystemExit is expected now that doctor reflects health in $?.
        try:
            _impl.cmd_doctor(args)
        except SystemExit:
            pass
        out = capsys.readouterr().out
        data = json.loads(out)
        # init_schema.sql has the cumulative effects, so virgin tracker +
        # late columns → drift state
        assert data["migrations"]["state"] == "virgin-tracker-with-drift"
        assert data["migrations"].get("ad_hoc_hits", 0) >= 2
