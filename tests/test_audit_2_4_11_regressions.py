"""Regression locks for the 2.4.11 audit follow-up fixes.

Covers I22 (root schema symlink) and I35 (borrow_from scope guard).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_root_init_schema_is_a_symlink_to_packaged():
    """Audit I22 — the root `db/init_schema.sql` must point at the
    packaged canonical so drift is structurally impossible. A plain
    file at the root path was 566+ lines behind the packaged schema
    before 2.5.0 cleanup."""
    root_path = ROOT / "db" / "init_schema.sql"
    packaged = ROOT / "src" / "agentmemory" / "db" / "init_schema.sql"
    assert root_path.is_symlink(), (
        f"{root_path.relative_to(ROOT)} must be a symlink to the "
        f"packaged schema so the two copies can't drift"
    )
    assert root_path.resolve() == packaged.resolve(), (
        f"symlink must point to {packaged.relative_to(ROOT)}, "
        f"currently resolves to {root_path.resolve()}"
    )


def test_borrow_from_with_non_global_scope_rejected():
    """Audit I35 — `borrow_from` restricts to scope='global' internally;
    combining it with a non-global scope silently returned 0 results
    before 2.5.0. Now it returns a clear error instead."""
    from agentmemory import mcp_server

    r = mcp_server.tool_memory_search(
        agent_id="tester",
        query="anything",
        borrow_from="some-other-agent",
        scope="project:foo",
    )
    assert r.get("ok") is False
    assert "borrow_from" in r.get("error", ""), (
        "error message should name borrow_from so callers see the "
        "actual incompatibility"
    )
