"""Canonical home for small helpers shared by MCP tool modules.

Historically every ``src/agentmemory/mcp_tools_*.py`` file defined its own
near-identical copies of ``_db``, ``_now``, ``_rows_to_list``, ``_safe_fts``,
and friends.  That duplication made mechanical fixes (e.g. changing the FTS
sanitizer, standardizing error envelopes) require touching ~40 files.

This module is the single source of truth for those helpers.  New
``mcp_tools_*.py`` files should import from here, and existing ones are
being migrated to do the same via a module-level alias pattern::

    from agentmemory.lib.mcp_helpers import open_db, now_iso, rows_to_list

    _db = open_db          # keep legacy call sites working
    _now = now_iso
    _rows_to_list = rows_to_list

This file intentionally does NOT depend on :mod:`agentmemory.brain` — the
``Brain`` class owns its own per-agent connection semantics (agent upsert,
journal_mode bootstrap) that are not appropriate for cross-agent MCP tools.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from agentmemory.paths import get_db_path

__all__ = [
    "open_db",
    "now_iso",
    "rows_to_list",
    "safe_fts",
    "tool_error",
    "tool_ok",
]


def open_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection to ``brain.db`` with standard MCP-tool settings.

    The returned connection has ``row_factory = sqlite3.Row`` and foreign
    keys enabled.  ``journal_mode`` is *not* touched here — WAL is configured
    once at schema creation time and persists on disk, so re-setting it on
    every connection is redundant.

    Args:
        db_path: Optional override for the brain.db location.  Defaults to
            :func:`agentmemory.paths.get_db_path`.

    Returns:
        A ready-to-use :class:`sqlite3.Connection`.
    """
    path = db_path if db_path is not None else str(get_db_path())
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix.

    Microseconds are stripped so the value is stable for logs and diffs.
    Matches the historical ``_utc_now_iso()`` in ``brain.py``.
    """
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def rows_to_list(rows: Optional[Iterable[sqlite3.Row]]) -> list[dict]:
    """Convert an iterable of :class:`sqlite3.Row` to a list of dicts.

    Safe for ``None`` and empty inputs (both return ``[]``).
    """
    if not rows:
        return []
    return [dict(r) for r in rows]


def safe_fts(query: str) -> str:
    """Sanitize a user-supplied query string for FTS5 ``MATCH`` syntax.

    Strips all non-word / non-whitespace characters, then joins the remaining
    tokens with ``OR`` so the result is always a valid FTS5 expression.
    Returns an empty string if nothing usable remains.

    Mirrors the baseline implementation from ``brain.py`` so this module can
    serve as the single source of truth.
    """
    safe = re.sub(r"[^\w\s]", " ", query or "").strip()
    return " OR ".join(safe.split()) if safe else ""


def tool_error(msg: str, code: str = "error") -> str:
    """Return a JSON-encoded error envelope for an MCP tool response.

    Shape: ``{"ok": false, "error": msg, "code": code}``.  Provided as the
    canonical error shape for future tool-return standardization — callers
    are not yet required to use it.
    """
    return json.dumps({"ok": False, "error": msg, "code": code})


def tool_ok(data: Any) -> str:
    """Return a JSON-encoded success envelope for an MCP tool response.

    Shape: ``{"ok": true, "result": data}``.  Companion to :func:`tool_error`.
    """
    return json.dumps({"ok": True, "result": data})
