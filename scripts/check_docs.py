#!/usr/bin/env python3
"""
Verify that docs match implementation counts.

Checks:
1. MCP_SERVER.md tool count matches actual TOOLS list in mcp_server.py
2. README.md version of tool count (if present) matches

Exits 1 if any count is wrong.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "agentmemory"


def count_mcp_tools() -> int:
    """Count Tool() entries in mcp_server.py TOOLS list."""
    text = (SRC / "mcp_server.py").read_text()
    # Count Tool( entries within the TOOLS list
    # Find TOOLS = [ ... ] block
    m = re.search(r'^TOOLS\s*=\s*\[(.+?)^\]', text, re.MULTILINE | re.DOTALL)
    if not m:
        print("ERROR: Could not find TOOLS list in mcp_server.py", file=sys.stderr)
        sys.exit(2)
    tools_block = m.group(1)
    return len(re.findall(r'\bTool\s*\(', tools_block))


def count_in_doc(path: Path, pattern: str) -> int | None:
    """Extract a tool/command count from a doc file. Returns None if not found."""
    text = path.read_text()
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def main():
    errors = []
    warnings = []

    actual_tools = count_mcp_tools()
    print(f"MCP tools in mcp_server.py: {actual_tools}")

    # Check MCP_SERVER.md
    mcp_doc = ROOT / "MCP_SERVER.md"
    if mcp_doc.exists():
        doc_count = count_in_doc(mcp_doc, r'Available Tools\s*\((\d+)\)')
        if doc_count is None:
            warnings.append(f"MCP_SERVER.md: could not find 'Available Tools (N)' header")
        elif doc_count != actual_tools:
            errors.append(
                f"MCP_SERVER.md says {doc_count} tools, but mcp_server.py has {actual_tools}. "
                f"Update the header: ## Available Tools ({actual_tools})"
            )
        else:
            print(f"MCP_SERVER.md: OK ({doc_count} tools)")
    else:
        warnings.append("MCP_SERVER.md not found")

    # Check README.md for MCP tool count if mentioned
    readme = ROOT / "README.md"
    if readme.exists():
        doc_count = count_in_doc(readme, r'(\d+)[\s-]+tool\s+MCP')
        if doc_count is not None and doc_count != actual_tools:
            errors.append(
                f"README.md mentions {doc_count}-tool MCP server, but actual count is {actual_tools}."
            )
        elif doc_count is not None:
            print(f"README.md MCP count: OK ({doc_count})")

    # Print results
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if errors:
        print("\nDOCS DRIFT DETECTED:", file=sys.stderr)
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    print("\nAll doc counts match implementation. ✓")
    sys.exit(0)


if __name__ == "__main__":
    main()
