"""Test that docs match implementation — run as part of test suite."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_docs_match_implementation():
    """Docs should match actual tool counts — fails if MCP_SERVER.md is stale."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_docs.py")],
        capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"Docs drift detected:\n{result.stdout}\n{result.stderr}"
    )
