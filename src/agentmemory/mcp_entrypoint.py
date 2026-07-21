"""Console-script boundary for the optional MCP server dependency."""

from __future__ import annotations

from importlib import import_module
import sys


_MISSING_MCP_EXTRA_MESSAGE = """\
brainctl-mcp requires the MCP extra, which is not installed.
Install it with:

  pip install 'brainctl[mcp]'
  # or, if brainctl is managed by pipx:
  pipx install --force 'brainctl[mcp]'
"""


def run() -> int | None:
    """Load and run the MCP server, or explain how to install its extra."""
    try:
        server_module = import_module("agentmemory.mcp_server")
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".", 1)[0] != "mcp":
            raise
        sys.stderr.write(_MISSING_MCP_EXTRA_MESSAGE)
        return 1

    return server_module.run()
