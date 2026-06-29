"""Regression test for the `[mcp]` extra not being installed.

`brainctl-mcp` (entry point `agentmemory.mcp_server:run`) used to die with a
bare `ModuleNotFoundError: No module named 'mcp'` traceback when the optional
`[mcp]` extra was absent — the MCP client only saw "failed to connect". The
import is now guarded to exit with an actionable install hint instead.

See issue #159.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# A subprocess that hides the `mcp` package (it IS installed in the test env,
# so we block it at import time) and then imports the MCP server module.
_HIDE_MCP_AND_IMPORT = """
import sys

class _BlockMcp:
    def find_spec(self, name, path, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return None

# Drop anything already cached, then refuse future imports of `mcp`.
for _m in [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]:
    del sys.modules[_m]
sys.meta_path.insert(0, _BlockMcp())

import agentmemory.mcp_server  # noqa: F401  (import is the thing under test)
"""


def _run_with_mcp_hidden() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _HIDE_MCP_AND_IMPORT],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )


def test_missing_mcp_extra_exits_with_actionable_message():
    proc = _run_with_mcp_hidden()

    # Clean exit code, not an unhandled traceback (which would be 1 *with* a
    # Traceback dump on stderr).
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr

    # The message must tell the user exactly how to fix it.
    assert "brainctl[mcp]" in proc.stderr
    assert "pip install" in proc.stderr
    assert "MCP server extra" in proc.stderr
