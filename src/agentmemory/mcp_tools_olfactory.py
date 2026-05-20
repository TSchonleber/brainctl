"""brainctl MCP tools — olfactory cortex (direct sensory-emotional binding).

Phase 1: direct imprint of (content_hash, valence, memory pointer)
that bypasses the standard W(m) write gate + thalamus routing —
just like olfaction in biology, which is the only sense that doesn't
relay through thalamus before reaching amygdala.

Use for input patterns the operator wants to flag as primally
significant (Proust-effect smell-equivalents): a specific phrase
that should always resurface a memory, an entity name that always
fires a particular valence, etc.

Default enforcement_mode = 'shadow' — imprints recorded but not
automatically routed into amygdala / retrieval. Phase 2 wires the
bypass.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_ENFORCEMENT_MODES = {"shadow", "enforce", "disabled"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("olfactory_state", "olfactory_imprints"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"olfactory schema missing: {t}. Run `brainctl migrate` (082)."
    return None


def _hash_content(content: str) -> str:
    return hashlib.blake2b(content.encode("utf-8"), digest_size=16).hexdigest()


def tool_olfactory_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM olfactory_state WHERE id = 1").fetchone()
        last_5 = _rows(conn.execute(
            "SELECT * FROM olfactory_imprints ORDER BY id DESC LIMIT 5"
        ).fetchall())
        valence_dist = _rows(conn.execute(
            """
            SELECT
              CASE WHEN valence >= 0.3 THEN 'positive'
                   WHEN valence <= -0.3 THEN 'negative'
                   ELSE 'neutral' END AS bucket,
              COUNT(*) AS n
              FROM olfactory_imprints GROUP BY bucket
            """
        ).fetchall())
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_imprints": last_5,
        "valence_distribution": valence_dist,
    }


def tool_olfactory_imprint(
    content: str, valence: float,
    arousal: float = 0.5,
    content_kind: str | None = None,
    bound_memory_id: int | None = None,
    bound_entity_id: int | None = None,
    agent_id: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Create or update an olfactory imprint. UPSERT keyed by
    (content_hash, agent_id) — repeated imprints with the same content
    + agent update the valence/arousal/pointers."""
    if not -1.0 <= valence <= 1.0:
        return {"error": "valence must be in [-1, 1]"}
    if not 0.0 <= arousal <= 1.0:
        return {"error": "arousal must be in [0, 1]"}
    if not content:
        return {"error": "content must be non-empty"}
    content_hash = _hash_content(content)
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        existing = conn.execute(
            "SELECT id FROM olfactory_imprints WHERE content_hash = ? AND agent_id IS ?",
            (content_hash, agent_id),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE olfactory_imprints SET
                  valence = ?, arousal = ?, content_kind = ?,
                  bound_memory_id = COALESCE(?, bound_memory_id),
                  bound_entity_id = COALESCE(?, bound_entity_id),
                  notes = COALESCE(?, notes)
                 WHERE id = ?
                """,
                (float(valence), float(arousal), content_kind,
                 bound_memory_id, bound_entity_id, notes, existing["id"]),
            )
            imprint_id = int(existing["id"])
            preexisting = True
        else:
            cur = conn.execute(
                """
                INSERT INTO olfactory_imprints
                  (content_hash, content_kind, valence, arousal,
                   bound_memory_id, bound_entity_id, agent_id, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (content_hash, content_kind, float(valence), float(arousal),
                 bound_memory_id, bound_entity_id, agent_id, notes),
            )
            imprint_id = cur.lastrowid
            preexisting = False
            conn.execute(
                "UPDATE olfactory_state SET total_imprints = total_imprints + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = 1"
            )
        conn.commit()
    return {
        "ok": True, "imprint_id": imprint_id,
        "content_hash": content_hash,
        "preexisting": preexisting,
        "valence": float(valence), "arousal": float(arousal),
    }


def tool_olfactory_recall(content: str, agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    """Look up an olfactory imprint by content. If found, increments
    times_recalled + returns the bound memory_id/entity_id + valence.
    Equivalent to the Proust-effect lookup."""
    if not content:
        return {"error": "content must be non-empty"}
    content_hash = _hash_content(content)
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        row = conn.execute(
            "SELECT * FROM olfactory_imprints WHERE content_hash = ? AND agent_id IS ?",
            (content_hash, agent_id),
        ).fetchone()
        if not row:
            return {"ok": True, "matched": False, "imprint": None}
        conn.execute(
            """
            UPDATE olfactory_imprints SET
              times_recalled = times_recalled + 1,
              last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = ?
            """,
            (row["id"],),
        )
        conn.commit()
        # Re-read to get updated times_recalled
        row = conn.execute("SELECT * FROM olfactory_imprints WHERE id = ?", (row["id"],)).fetchone()
    return {"ok": True, "matched": True, "imprint": dict(row)}


def tool_olfactory_set(enforcement_mode: str, **_kw: Any) -> dict[str, Any]:
    if enforcement_mode not in VALID_ENFORCEMENT_MODES:
        return {"error": f"invalid enforcement_mode; expected {sorted(VALID_ENFORCEMENT_MODES)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            "UPDATE olfactory_state SET enforcement_mode = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = 1",
            (enforcement_mode,),
        )
        conn.commit()
        state = conn.execute("SELECT * FROM olfactory_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None}


TOOLS: list[Tool] = [
    Tool(
        name="olfactory_status",
        description="Olfactory state + last 5 imprints + valence distribution (positive/neutral/negative).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="olfactory_imprint",
        description=(
            "Create or update an olfactory imprint — a direct (content_hash, valence, "
            "memory/entity pointer) binding that bypasses the standard W(m) gate. "
            "Idempotent: UPSERT keyed by (content_hash, agent_id). valence in [-1, 1], "
            "arousal in [0, 1]."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "valence": {"type": "number"},
                "arousal": {"type": "number", "default": 0.5},
                "content_kind": {"type": "string"},
                "bound_memory_id": {"type": "integer"},
                "bound_entity_id": {"type": "integer"},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["content", "valence"],
        },
    ),
    Tool(
        name="olfactory_recall",
        description=(
            "Look up an olfactory imprint by content (Proust-style fast emotional recall). "
            "Returns matched=True + the imprint if found and increments times_recalled. "
            "Returns matched=False if no imprint exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="olfactory_set",
        description="Set enforcement_mode ∈ {shadow, enforce, disabled}. Default shadow.",
        inputSchema={
            "type": "object",
            "properties": {
                "enforcement_mode": {"type": "string", "enum": sorted(VALID_ENFORCEMENT_MODES)},
            },
            "required": ["enforcement_mode"],
        },
    ),
]


_OLF_TOOLS = {
    "olfactory_status": tool_olfactory_status,
    "olfactory_imprint": tool_olfactory_imprint,
    "olfactory_recall": tool_olfactory_recall,
    "olfactory_set": tool_olfactory_set,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _OLF_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
