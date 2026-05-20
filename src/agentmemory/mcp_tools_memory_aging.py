"""brainctl MCP tools — memory aging (synaptic tagging-and-capture).

Phase 1 per research-avenues memo Avenue 2. Frey & Morris's late-LTP
biology: a tag at encoding + PRPs within a critical window → memory
persists. Without PRPs → memory decays.

brainctl analog: W(m) gate = tag; recall within window = PRP capture.
Phase 1 = inspection + manual tag/capture/sweep. Phase 2 auto-tags
on memory_add. Phase 3 demotes uncaptured tags. Phase 4 enforces.

Default enforcement_mode = 'shadow' — bookkeeping only, no demotion.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_STATUSES = {"tagged", "captured", "expired", "demoted"}
VALID_CAPTURE_KINDS = {"recall", "reconsolidation", "association", "manual_capture", "other"}
VALID_DEMOTION_TIERS = {"unconsolidated", "cold_storage", "retired"}
VALID_ENFORCEMENT_MODES = {"shadow", "enforce", "disabled"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("memory_aging_state", "memory_tags", "memory_capture_events"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"memory_aging schema missing: {t}. Run `brainctl migrate` (078)."
    return None


def tool_memory_aging_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM memory_aging_state WHERE id = 1").fetchone()
        by_status = _rows(conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_tags GROUP BY status"
        ).fetchall())
        expiring_soon = conn.execute(
            """
            SELECT COUNT(*) FROM memory_tags
             WHERE status = 'tagged' AND capture_deadline <= datetime('now', '+1 hour')
            """
        ).fetchone()[0]
        already_overdue = conn.execute(
            """
            SELECT COUNT(*) FROM memory_tags
             WHERE status = 'tagged' AND capture_deadline < datetime('now')
            """
        ).fetchone()[0]
        recent_captures = _rows(conn.execute(
            "SELECT * FROM memory_capture_events ORDER BY id DESC LIMIT 5"
        ).fetchall())
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "tags_by_status": by_status,
        "tagged_expiring_within_1h": expiring_soon,
        "tagged_already_overdue": already_overdue,
        "recent_captures": recent_captures,
    }


def tool_memory_tag(
    memory_id: int, window_hours: int | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Tag a memory with a capture deadline.

    Idempotent: if memory_id already has a tag, returns the existing
    one without changing the deadline. To extend a tag, call
    memory_recapture_tag.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        existing = conn.execute(
            "SELECT * FROM memory_tags WHERE memory_id = ?", (int(memory_id),)
        ).fetchone()
        if existing:
            return {"ok": True, "tag": dict(existing), "preexisting": True}
        # Use state's default window if window_hours not specified
        if window_hours is None:
            state = conn.execute("SELECT capture_window_hours FROM memory_aging_state WHERE id = 1").fetchone()
            window_hours = int(state["capture_window_hours"]) if state else 24
        if window_hours <= 0:
            return {"error": "window_hours must be > 0"}
        cur = conn.execute(
            """
            INSERT INTO memory_tags (memory_id, capture_deadline, status, notes)
            VALUES (?, datetime('now', ? || ' hours'), 'tagged', ?)
            """,
            (int(memory_id), f"+{int(window_hours)}", notes),
        )
        tag_id = cur.lastrowid
        conn.execute(
            "UPDATE memory_aging_state SET total_tags = total_tags + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = 1"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memory_tags WHERE id = ?", (tag_id,)).fetchone()
    return {"ok": True, "tag": dict(row) if row else None, "preexisting": False}


def tool_memory_capture(
    memory_id: int, capture_kind: str = "recall",
    agent_id: str | None = None, notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Record a capture event for a memory. If the memory is in
    'tagged' status, flips it to 'captured'. Otherwise logs the event
    against the existing status."""
    if capture_kind not in VALID_CAPTURE_KINDS:
        return {"error": f"invalid capture_kind {capture_kind!r}; expected {sorted(VALID_CAPTURE_KINDS)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        tag = conn.execute(
            "SELECT * FROM memory_tags WHERE memory_id = ?", (int(memory_id),)
        ).fetchone()
        cur = conn.execute(
            """
            INSERT INTO memory_capture_events
              (memory_id, tag_id, capture_kind, agent_id, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(memory_id), tag["id"] if tag else None, capture_kind, agent_id, notes),
        )
        event_id = cur.lastrowid
        flipped = False
        if tag and tag["status"] == "tagged":
            conn.execute(
                """
                UPDATE memory_tags SET
                  status = 'captured',
                  captured_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                  capture_count = capture_count + 1
                 WHERE id = ?
                """,
                (tag["id"],),
            )
            conn.execute(
                "UPDATE memory_aging_state SET total_captured = total_captured + 1 WHERE id = 1"
            )
            flipped = True
        elif tag:
            conn.execute(
                "UPDATE memory_tags SET capture_count = capture_count + 1 WHERE id = ?",
                (tag["id"],),
            )
        conn.commit()
    return {
        "ok": True, "event_id": event_id, "memory_id": int(memory_id),
        "tag_id": tag["id"] if tag else None,
        "flipped_to_captured": flipped,
    }


def tool_memory_aging_sweep(**_kw: Any) -> dict[str, Any]:
    """Sweep for tags past their capture deadline. In shadow mode,
    just counts. In enforce mode, transitions them to 'expired' (and
    increments total_demoted)."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM memory_aging_state WHERE id = 1").fetchone()
        overdue = conn.execute(
            """
            SELECT id, memory_id, capture_deadline FROM memory_tags
             WHERE status = 'tagged' AND capture_deadline < datetime('now')
            """
        ).fetchall()
        n = len(overdue)
        if state["enforcement_mode"] == "enforce" and n > 0:
            ids = [r["id"] for r in overdue]
            conn.executemany(
                """
                UPDATE memory_tags SET
                  status = 'expired',
                  demoted_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                 WHERE id = ?
                """,
                [(i,) for i in ids],
            )
            conn.execute(
                "UPDATE memory_aging_state SET total_demoted = total_demoted + ? WHERE id = 1",
                (n,),
            )
            conn.commit()
            return {"ok": True, "swept_count": n, "demoted_count": n, "enforcement_mode": state["enforcement_mode"]}
        return {"ok": True, "swept_count": n, "demoted_count": 0, "enforcement_mode": state["enforcement_mode"]}


def tool_memory_aging_set(
    capture_window_hours: int | None = None,
    demotion_tier: str | None = None,
    enforcement_mode: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if capture_window_hours is not None and capture_window_hours <= 0:
        return {"error": "capture_window_hours must be > 0"}
    if demotion_tier is not None and demotion_tier not in VALID_DEMOTION_TIERS:
        return {"error": f"invalid demotion_tier; expected {sorted(VALID_DEMOTION_TIERS)}"}
    if enforcement_mode is not None and enforcement_mode not in VALID_ENFORCEMENT_MODES:
        return {"error": f"invalid enforcement_mode; expected {sorted(VALID_ENFORCEMENT_MODES)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        updates, params = [], []
        if capture_window_hours is not None:
            updates.append("capture_window_hours = ?"); params.append(int(capture_window_hours))
        if demotion_tier is not None:
            updates.append("demotion_tier = ?"); params.append(demotion_tier)
        if enforcement_mode is not None:
            updates.append("enforcement_mode = ?"); params.append(enforcement_mode)
        if not updates:
            return {"error": "no fields to update"}
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        conn.execute(f"UPDATE memory_aging_state SET {', '.join(updates)} WHERE id = 1", tuple(params))
        conn.commit()
        state = conn.execute("SELECT * FROM memory_aging_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None}


def tool_memory_tag_get(memory_id: int, **_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        tag = conn.execute(
            "SELECT * FROM memory_tags WHERE memory_id = ?", (int(memory_id),)
        ).fetchone()
        if not tag:
            return {"ok": True, "tag": None, "captures": []}
        captures = _rows(conn.execute(
            "SELECT * FROM memory_capture_events WHERE memory_id = ? ORDER BY id DESC LIMIT 50",
            (int(memory_id),),
        ).fetchall())
    return {"ok": True, "tag": dict(tag), "captures": captures}


TOOLS: list[Tool] = [
    Tool(
        name="memory_aging_status",
        description="Memory aging state + tags by status + expiring-soon counts + recent captures.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="memory_tag",
        description=(
            "Tag a memory with a capture deadline (idempotent — re-tagging returns the "
            "existing tag without changing the deadline). window_hours defaults to "
            "state.capture_window_hours."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "window_hours": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="memory_capture",
        description=(
            "Record a capture event (recall / reconsolidation / association / manual). "
            "If the memory is in 'tagged' status, flips to 'captured'. capture_kind ∈ "
            "{recall, reconsolidation, association, manual_capture, other}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "capture_kind": {"type": "string", "enum": sorted(VALID_CAPTURE_KINDS), "default": "recall"},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="memory_aging_sweep",
        description=(
            "Sweep for tags past their capture_deadline. In shadow mode: counts only. "
            "In enforce mode: transitions overdue tags to 'expired' and increments "
            "total_demoted."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="memory_aging_set",
        description=(
            "Update memory_aging_state. capture_window_hours > 0; demotion_tier ∈ "
            "{unconsolidated, cold_storage, retired}; enforcement_mode ∈ "
            "{shadow, enforce, disabled}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "capture_window_hours": {"type": "integer"},
                "demotion_tier": {"type": "string", "enum": sorted(VALID_DEMOTION_TIERS)},
                "enforcement_mode": {"type": "string", "enum": sorted(VALID_ENFORCEMENT_MODES)},
            },
        },
    ),
    Tool(
        name="memory_tag_get",
        description="Get a memory's tag + the last 50 capture events.",
        inputSchema={
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}},
            "required": ["memory_id"],
        },
    ),
]


_MA_TOOLS = {
    "memory_aging_status": tool_memory_aging_status,
    "memory_tag": tool_memory_tag,
    "memory_capture": tool_memory_capture,
    "memory_aging_sweep": tool_memory_aging_sweep,
    "memory_aging_set": tool_memory_aging_set,
    "memory_tag_get": tool_memory_tag_get,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _MA_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
