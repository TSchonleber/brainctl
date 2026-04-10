"""Tests for brainctl-mcp --doctor."""
import json
import subprocess
import sys
import os
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def run_doctor(*extra_args, db_path=None, expect_ok=True):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    if db_path:
        env["BRAIN_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(SRC)!r}); "
         f"import agentmemory.mcp_server as m; "
         f"sys.argv = ['brainctl-mcp', '--doctor'] + {list(extra_args)!r}; "
         f"import asyncio; asyncio.run(m.main())"],
        capture_output=True, text=True, timeout=10, env=env
    )
    return result


class TestDoctor:
    def test_doctor_runs_with_valid_db(self, tmp_path):
        from agentmemory.brain import Brain
        db = tmp_path / "brain.db"
        Brain(str(db), agent_id="default")
        result = run_doctor(db_path=db)
        # Doctor may exit 0 or 1 depending on Ollama/vec availability — just check it ran
        assert "brainctl-mcp doctor" in result.stderr

    def test_doctor_fails_with_missing_db(self, tmp_path):
        bad_db = tmp_path / "nonexistent.db"
        result = run_doctor(db_path=bad_db)
        assert result.returncode == 1
        assert "brain.db" in result.stderr.lower() or "missing" in result.stderr.lower()

    def test_doctor_json_output(self, tmp_path):
        from agentmemory.brain import Brain
        db = tmp_path / "brain.db"
        Brain(str(db), agent_id="default")
        result = run_doctor("--json", db_path=db)
        # JSON should be on stdout
        if result.stdout.strip():
            data = json.loads(result.stdout)
            assert "ok" in data
            assert "checks" in data
