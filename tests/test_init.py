"""Tests for brainctl init command and fresh database setup."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

BRAINCTL = [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); from agentmemory.cli import main; sys.argv = ['brainctl'] + sys.argv[1:]; main()"]


def run_brainctl(*args, db_path=None, expect_fail=False):
    env = os.environ.copy()
    if db_path:
        env["BRAIN_DB"] = db_path
    cmd = BRAINCTL + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."), env=env)
    if not expect_fail:
        assert result.returncode == 0, f"Command failed: {args}\nstderr: {result.stderr}\nstdout: {result.stdout}"
    return result.stdout.strip(), result.stderr.strip(), result.returncode


class TestInit:
    def test_creates_database(self, tmp_path):
        db = str(tmp_path / "test.db")
        out, _, _ = run_brainctl("init", "--path", db)
        data = json.loads(out)
        assert data["ok"] is True
        assert os.path.exists(db)
        assert data["tables"] > 50  # full schema has 80+

    def test_refuses_overwrite_without_force(self, tmp_path):
        db = str(tmp_path / "test.db")
        run_brainctl("init", "--path", db)
        out, _, _ = run_brainctl("init", "--path", db)
        data = json.loads(out)
        assert data["ok"] is False
        assert "already exists" in data["error"]

    def test_force_overwrites(self, tmp_path):
        db = str(tmp_path / "test.db")
        run_brainctl("init", "--path", db)
        out, _, _ = run_brainctl("init", "--path", db, "--force")
        data = json.loads(out)
        assert data["ok"] is True

    def test_full_schema_has_key_tables(self, tmp_path):
        db = str(tmp_path / "test.db")
        out, _, _ = run_brainctl("init", "--path", db)
        data = json.loads(out)
        tables = data["table_list"]
        for required in ["memories", "events", "entities", "decisions", "agents",
                         "knowledge_edges", "affect_log", "workspace_config",
                         "neuromodulation_state", "access_log"]:
            assert required in tables, f"Missing table: {required}"


class TestInitThenUse:
    """Full flow: init -> memory add -> search -> affect -> stats on a fresh DB."""

    @pytest.fixture
    def fresh_db(self, tmp_path):
        db = str(tmp_path / "fresh.db")
        run_brainctl("init", "--path", db)
        return db

    def test_memory_add(self, fresh_db):
        out, _, _ = run_brainctl("-a", "tester", "memory", "add",
                                  "Test memory from fresh DB", "-c", "lesson", "--force",
                                  db_path=fresh_db)
        data = json.loads(out)
        assert data.get("ok") is True or data.get("memory_id") is not None

    def test_search_after_add(self, fresh_db):
        run_brainctl("-a", "tester", "memory", "add", "searchable content here",
                     "-c", "lesson", "--force", db_path=fresh_db)
        out, _, _ = run_brainctl("search", "searchable", "--output", "oneline",
                                  "--limit", "3", db_path=fresh_db)
        assert "searchable" in out.lower()

    def test_affect_classify(self, fresh_db):
        out, _, _ = run_brainctl("affect", "classify", "terrible catastrophic panic",
                                  db_path=fresh_db)
        data = json.loads(out)
        assert data["valence"] < -0.3
        assert data["arousal"] > 0.2

    def test_affect_log(self, fresh_db):
        out, _, _ = run_brainctl("-a", "tester", "affect", "log",
                                  "everything is great and successful",
                                  db_path=fresh_db)
        data = json.loads(out)
        assert data["valence"] > 0.2

    def test_affect_check(self, fresh_db):
        run_brainctl("-a", "tester", "affect", "log", "feeling good",
                     db_path=fresh_db)
        out, _, _ = run_brainctl("-a", "tester", "affect", "check",
                                  db_path=fresh_db)
        data = json.loads(out)
        assert data["status"] in ("healthy", "warning", "critical", "no_data")

    def test_affect_monitor(self, fresh_db):
        run_brainctl("-a", "agent1", "affect", "log", "happy and productive",
                     db_path=fresh_db)
        out, _, _ = run_brainctl("affect", "monitor", db_path=fresh_db)
        data = json.loads(out)
        assert data["agents"] >= 1

    def test_stats(self, fresh_db):
        out, _, _ = run_brainctl("stats", db_path=fresh_db)
        data = json.loads(out)
        assert "active_memories" in data

    def test_cost(self, fresh_db):
        out, _, _ = run_brainctl("cost", db_path=fresh_db)
        data = json.loads(out)
        assert "recommendations" in data

    def test_entity_create_and_search(self, fresh_db):
        out, _, _ = run_brainctl("-a", "tester", "entity", "create", "TestBot",
                                  "-t", "agent", "-o", "A test entity",
                                  db_path=fresh_db)
        data = json.loads(out)
        assert data.get("ok") is True or data.get("id") is not None

    def test_event_add(self, fresh_db):
        out, _, _ = run_brainctl("-a", "tester", "event", "add",
                                  "Test event from init flow",
                                  "-t", "result", db_path=fresh_db)
        data = json.loads(out)
        assert data.get("ok") is True or data.get("id") is not None


class TestErrorHandling:
    def test_missing_db_returns_json(self, tmp_path):
        db = str(tmp_path / "nonexistent" / "nope.db")
        out, _, rc = run_brainctl("stats", db_path=db, expect_fail=True)
        data = json.loads(out)
        assert "error" in data
        assert "hint" in data

    def test_output_format_oneline_empty(self, tmp_path):
        db = str(tmp_path / "empty.db")
        run_brainctl("init", "--path", db)
        out, _, _ = run_brainctl("search", "nonexistent_query_xyz",
                                  "--output", "oneline", "--limit", "3",
                                  db_path=db)
        # Should not crash, output may be empty or contain no results
        assert isinstance(out, str)

    def test_output_format_compact(self, tmp_path):
        db = str(tmp_path / "compact.db")
        run_brainctl("init", "--path", db)
        run_brainctl("-a", "t", "memory", "add", "compact test", "-c", "lesson",
                     "--force", db_path=db)
        out, _, _ = run_brainctl("search", "compact", "--output", "compact",
                                  "--limit", "2", db_path=db)
        # Compact JSON should be parseable and single-line
        data = json.loads(out)
        assert "memories" in data or "mode" in data
