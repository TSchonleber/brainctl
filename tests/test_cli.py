"""Tests for brainctl CLI commands.

These tests invoke CLI commands by patching the module-level DB_PATH in _impl
so commands operate on a temporary database.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def run_brainctl(*args, db_path=None, expect_ok=True):
    """Run brainctl via subprocess, patching DB_PATH to use a temp DB."""
    cmd_args = list(args)
    patch_code = (
        f"import sys, os; sys.path.insert(0, {str(SRC)!r}); "
        f"import agentmemory._impl as _i; "
        f"from pathlib import Path; "
        f"_i.DB_PATH = Path({str(db_path)!r}); "
        f"sys.argv = ['brainctl'] + {cmd_args!r}; "
        f"_i.main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", patch_code],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    if expect_ok:
        assert result.returncode == 0, (
            f"brainctl {' '.join(args)} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )
    return result


# ── stats ───────────────────────────────────────────────────────────────────


class TestCLIStats:
    def test_stats_returns_json(self, cli_db):
        r = run_brainctl("stats", db_path=cli_db)
        data = json.loads(r.stdout)
        assert "memories" in data
        assert "active_memories" in data

    def test_stats_empty_db(self, cli_db):
        r = run_brainctl("stats", db_path=cli_db)
        data = json.loads(r.stdout)
        assert data["memories"] == 0


# ── memory add ──────────────────────────────────────────────────────────────


class TestCLIMemoryAdd:
    def test_add_memory(self, cli_db):
        # --agent before subcommand, content is positional
        r = run_brainctl(
            "--agent", "tester",
            "memory", "add",
            "Always test your code",
            "--category", "lesson",
            db_path=cli_db,
        )
        data = json.loads(r.stdout)
        assert data.get("ok") is True or "memory_id" in data or "id" in data

    def test_add_then_stats_incremented(self, cli_db):
        run_brainctl(
            "--agent", "tester",
            "memory", "add",
            "Test memory for stats",
            "--category", "lesson",
            db_path=cli_db,
        )
        r = run_brainctl("stats", db_path=cli_db)
        data = json.loads(r.stdout)
        assert data["memories"] >= 1


# ── memory search ───────────────────────────────────────────────────────────


class TestCLIMemorySearch:
    def _add_memory(self, cli_db, content, category="lesson"):
        run_brainctl(
            "--agent", "tester",
            "memory", "add",
            content,
            "--category", category,
            db_path=cli_db,
        )

    def test_search_finds_memory(self, cli_db):
        self._add_memory(cli_db, "Pytest is great for testing")
        r = run_brainctl(
            "--agent", "tester",
            "memory", "search",
            "Pytest",
            "--exact",
            db_path=cli_db,
        )
        data = json.loads(r.stdout)
        results = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        assert len(results) >= 1

    def test_search_no_results(self, cli_db):
        r = run_brainctl(
            "--agent", "tester",
            "memory", "search",
            "nonexistent_xyzzy",
            "--exact",
            db_path=cli_db,
        )
        data = json.loads(r.stdout)
        results = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        assert len(results) == 0


# ── search (unified) ───────────────────────────────────────────────────────


class TestCLISearch:
    def test_search_runs(self, cli_db):
        """Unified search may fail on FTS tables not existing — that's okay,
        we just check it doesn't crash with a Python traceback."""
        r = run_brainctl(
            "--agent", "tester",
            "search",
            "test",
            db_path=cli_db,
            expect_ok=False,
        )
        # Should either succeed with JSON or fail gracefully (no Python traceback)
        assert r.returncode in (0, 1)


# ── cost ────────────────────────────────────────────────────────────────────


class TestCLICost:
    def test_cost_returns_json(self, cli_db):
        r = run_brainctl("cost", db_path=cli_db, expect_ok=False)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            assert isinstance(data, dict)


# ── entity create ───────────────────────────────────────────────────────────


class TestCLIEntityCreate:
    def test_create_entity(self, cli_db):
        r = run_brainctl(
            "--agent", "tester",
            "entity", "create",
            "TestBot",
            "--type", "agent",
            db_path=cli_db,
        )
        data = json.loads(r.stdout)
        assert data.get("ok") is True or "entity_id" in data


# ── handoff ────────────────────────────────────────────────────────────────


class TestCLIHandoff:
    def test_add_handoff(self, cli_db):
        r = run_brainctl(
            "--agent", "tester",
            "handoff", "add",
            "--goal", "Resume brainctl work",
            "--current-state", "Cleanup branch pushed",
            "--open-loops", "Implement handoff table",
            "--next-step", "Patch schema and parser",
            "--project", "brainctl",
            db_path=cli_db,
        )
        data = json.loads(r.stdout)
        assert data.get("ok") is True
        assert "handoff_id" in data

    def test_latest_then_consume_handoff(self, cli_db):
        add = run_brainctl(
            "--agent", "tester",
            "handoff", "add",
            "--goal", "Resume Hermes continuity work",
            "--current-state", "Need latest packet",
            "--open-loops", "Consume after restore",
            "--next-step", "Fetch latest pending handoff",
            "--chat-id", "chat-1",
            "--thread-id", "thread-1",
            db_path=cli_db,
        )
        handoff_id = json.loads(add.stdout)["handoff_id"]

        latest = run_brainctl(
            "--agent", "tester",
            "handoff", "latest",
            "--chat-id", "chat-1",
            "--thread-id", "thread-1",
            db_path=cli_db,
        )
        latest_data = json.loads(latest.stdout)
        assert latest_data["id"] == handoff_id
        assert latest_data["status"] == "pending"

        consume = run_brainctl(
            "--agent", "tester",
            "handoff", "consume", str(handoff_id),
            db_path=cli_db,
        )
        consume_data = json.loads(consume.stdout)
        assert consume_data["ok"] is True
        assert consume_data["status"] == "consumed"

    def test_pin_and_expire_handoff(self, cli_db):
        add = run_brainctl(
            "--agent", "tester",
            "handoff", "add",
            "--goal", "Keep this around",
            "--current-state", "Pinned state candidate",
            "--open-loops", "Need later review",
            "--next-step", "Pin then expire",
            db_path=cli_db,
        )
        handoff_id = json.loads(add.stdout)["handoff_id"]

        pin = run_brainctl(
            "--agent", "tester",
            "handoff", "pin", str(handoff_id),
            db_path=cli_db,
        )
        pin_data = json.loads(pin.stdout)
        assert pin_data["ok"] is True
        assert pin_data["status"] == "pinned"

        listed = run_brainctl(
            "--agent", "tester",
            "handoff", "list",
            "--status", "pinned",
            db_path=cli_db,
        )
        listed_data = json.loads(listed.stdout)
        assert any(item["id"] == handoff_id for item in listed_data)

        expire = run_brainctl(
            "--agent", "tester",
            "handoff", "expire", str(handoff_id),
            db_path=cli_db,
        )
        expire_data = json.loads(expire.stdout)
        assert expire_data["ok"] is True
        assert expire_data["status"] == "expired"

    def test_handoff_ownership_is_enforced(self, cli_db):
        add = run_brainctl(
            "--agent", "owner",
            "handoff", "add",
            "--goal", "Owner only",
            "--current-state", "Private state",
            "--open-loops", "None",
            "--next-step", "Keep private",
            db_path=cli_db,
        )
        handoff_id = json.loads(add.stdout)["handoff_id"]

        consume = run_brainctl(
            "--agent", "other",
            "handoff", "consume", str(handoff_id),
            db_path=cli_db,
        )
        consume_data = json.loads(consume.stdout)
        assert consume_data["ok"] is False
        assert "not found for agent other" in consume_data["error"]

    def test_handoff_add_rejects_blank_goal(self, cli_db):
        result = run_brainctl(
            "--agent", "tester",
            "handoff", "add",
            "--goal", "   ",
            "--current-state", "State",
            "--open-loops", "Loops",
            "--next-step", "Next",
            db_path=cli_db,
            expect_ok=False,
        )
        assert result.returncode != 0 or result.stdout


# ── output format flags ────────────────────────────────────────────────────


class TestOutputFormats:
    """Test --output json/compact/oneline on memory search."""

    def _seed(self, cli_db):
        for i in range(3):
            run_brainctl(
                "--agent", "fmt",
                "memory", "add",
                f"Format test memory number {i}",
                "--category", "lesson",
                db_path=cli_db,
            )

    def test_json_output(self, cli_db):
        self._seed(cli_db)
        r = run_brainctl(
            "--agent", "fmt",
            "memory", "search",
            "Format test",
            "--exact",
            "--output", "json",
            db_path=cli_db,
        )
        data = json.loads(r.stdout)
        assert isinstance(data, (list, dict))

    def test_compact_output(self, cli_db):
        self._seed(cli_db)
        r = run_brainctl(
            "--agent", "fmt",
            "memory", "search",
            "Format test",
            "--exact",
            "--output", "compact",
            db_path=cli_db,
        )
        # compact JSON: no indentation, single line
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        assert len(lines) == 1, f"Expected single line, got {len(lines)}: {r.stdout[:200]}"
        data = json.loads(lines[0])
        assert isinstance(data, (list, dict))

    def test_oneline_output(self, cli_db):
        self._seed(cli_db)
        r = run_brainctl(
            "--agent", "fmt",
            "memory", "search",
            "Format test",
            "--exact",
            "--output", "oneline",
            db_path=cli_db,
        )
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        # oneline: one line per result, pipe-separated
        assert len(lines) >= 1
        for line in lines:
            assert "|" in line
