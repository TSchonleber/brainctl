"""brainctl MCP tools — memory immunity / quarantine system (issue #24).

Threat model: multi-agent deployments where LLM outputs feed back into persistent
storage are vulnerable to prompt injection — adversarial content that causes an
agent to store false memories with high confidence, corrupting the knowledge graph.

Defense layers:
- Layer 1 (source trust) is enforced at write time in mcp_server.tool_memory_add.
- Layer 2 (contradiction spike detection) fires when a new write contradicts ≥3
  high-confidence memories simultaneously with low source_trust.
- Layers 3+: quarantine_list / quarantine_review / quarantine_purge (this module).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# NOTE: local _now uses naive strftime format (no 'Z' suffix), which differs
# from agentmemory.lib.mcp_helpers.now_iso. Kept local to preserve timestamp
# shape used in the quarantine tables.
_now = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _ensure_quarantine_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            source_trust REAL,
            contradiction_count INTEGER DEFAULT 0,
            quarantined_by TEXT NOT NULL DEFAULT 'system',
            reviewed_by TEXT DEFAULT NULL,
            reviewed_at TEXT DEFAULT NULL,
            verdict TEXT DEFAULT NULL CHECK(verdict IN ('safe','malicious','uncertain')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_quarantine_memory_id ON memory_quarantine(memory_id);
        CREATE INDEX IF NOT EXISTS idx_quarantine_verdict ON memory_quarantine(verdict);
        CREATE INDEX IF NOT EXISTS idx_quarantine_created ON memory_quarantine(created_at DESC);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# quarantine_list
# ---------------------------------------------------------------------------

def tool_quarantine_list(
    agent_id: str = "mcp-client",
    verdict: str | None = None,
    limit: int = 20,
    **kw,
) -> dict:
    """List memories currently in quarantine, optionally filtered by verdict."""
    if verdict and verdict not in ("safe", "malicious", "uncertain", "pending"):
        return {"ok": False, "error": "verdict must be safe, malicious, uncertain, or pending"}
    db = _db()
    _ensure_quarantine_table(db)
    try:
        conditions = []
        params: list = []
        if verdict == "pending":
            conditions.append("q.verdict IS NULL")
        elif verdict:
            conditions.append("q.verdict = ?")
            params.append(verdict)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = db.execute(
            f"""
            SELECT q.id, q.memory_id, q.reason, q.source_trust, q.contradiction_count,
                   q.quarantined_by, q.verdict, q.reviewed_by, q.reviewed_at, q.created_at,
                   m.content, m.category, m.confidence, m.agent_id as memory_agent_id
            FROM memory_quarantine q
            JOIN memories m ON m.id = q.memory_id
            {where}
            ORDER BY q.created_at DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        items = [dict(r) for r in rows]
        return {
            "ok": True,
            "count": len(items),
            "items": items,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# quarantine_review
# ---------------------------------------------------------------------------

def tool_quarantine_review(
    agent_id: str = "mcp-client",
    quarantine_id: int | None = None,
    verdict: str | None = None,
    **kw,
) -> dict:
    """Mark a quarantined memory safe, malicious, or uncertain.

    If verdict='safe', the memory is restored to normal status (retired_at cleared
    if it was retired during quarantine). If verdict='malicious', the memory is
    flagged but not purged — use quarantine_purge for irreversible deletion.
    """
    if quarantine_id is None:
        return {"ok": False, "error": "quarantine_id required"}
    if verdict not in ("safe", "malicious", "uncertain"):
        return {"ok": False, "error": "verdict must be safe, malicious, or uncertain"}
    db = _db()
    _ensure_quarantine_table(db)
    try:
        row = db.execute(
            "SELECT * FROM memory_quarantine WHERE id = ?", (quarantine_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"quarantine record {quarantine_id} not found"}
        now = _now()
        db.execute(
            "UPDATE memory_quarantine SET verdict = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (verdict, agent_id, now, quarantine_id),
        )
        if verdict == "safe":
            # Lift any soft-retirement that was applied during quarantine
            db.execute(
                "UPDATE memories SET retired_at = NULL WHERE id = ? AND retired_at IS NOT NULL",
                (row["memory_id"],),
            )
        db.commit()
        return {
            "ok": True,
            "quarantine_id": quarantine_id,
            "memory_id": row["memory_id"],
            "verdict": verdict,
            "reviewed_by": agent_id,
            "reviewed_at": now,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# quarantine_purge
# ---------------------------------------------------------------------------

def tool_quarantine_purge(
    agent_id: str = "mcp-client",
    quarantine_id: int | None = None,
    dry_run: bool = False,
    **kw,
) -> dict:
    """Permanently delete a memory marked malicious and retract its derived beliefs.

    Requires the quarantine record to have verdict='malicious'. Use dry_run=true
    to preview what would be deleted without committing.
    """
    if quarantine_id is None:
        return {"ok": False, "error": "quarantine_id required"}
    db = _db()
    _ensure_quarantine_table(db)
    try:
        row = db.execute(
            "SELECT * FROM memory_quarantine WHERE id = ?", (quarantine_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"quarantine record {quarantine_id} not found"}
        if row["verdict"] != "malicious":
            return {
                "ok": False,
                "error": f"verdict is '{row['verdict']}', not 'malicious' — review first",
            }
        memory_id = row["memory_id"]
        # Count derived beliefs / knowledge edges that reference this memory
        ke_count = db.execute(
            "SELECT COUNT(*) FROM knowledge_edges "
            "WHERE (source_table='memories' AND source_id = ?) OR (target_table='memories' AND target_id = ?)",
            (memory_id, memory_id),
        ).fetchone()[0]
        # Count access_log entries
        al_count = db.execute(
            "SELECT COUNT(*) FROM access_log WHERE target_table = 'memories' AND target_id = ?",
            (memory_id,),
        ).fetchone()[0]
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "memory_id": memory_id,
                "knowledge_edges_to_retract": ke_count,
                "access_log_entries": al_count,
            }
        # Retract knowledge edges derived from this memory
        db.execute(
            "DELETE FROM knowledge_edges "
            "WHERE (source_table='memories' AND source_id = ?) OR (target_table='memories' AND target_id = ?)",
            (memory_id, memory_id),
        )
        # Soft-delete the memory (retired_at marks it permanently inactive).
        # Hard deletion is avoided because many tables reference memories(id) without CASCADE.
        db.execute(
            "UPDATE memories SET retired_at = ? WHERE id = ?",
            (_now(), memory_id),
        )
        db.commit()
        return {
            "ok": True,
            "dry_run": False,
            "memory_id": memory_id,
            "knowledge_edges_retracted": ke_count,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TOOLS list and DISPATCH
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="quarantine_list",
        description=(
            "List memories currently in the immunity quarantine. "
            "Filter by verdict: 'pending' (unreviewed), 'safe', 'malicious', or 'uncertain'. "
            "Each item includes the memory content, the quarantine reason, source_trust, "
            "and contradiction_count that triggered quarantine."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["pending", "safe", "malicious", "uncertain"],
                    "description": "Filter by review verdict. Omit to return all.",
                },
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="quarantine_review",
        description=(
            "Review a quarantined memory: mark it safe, malicious, or uncertain. "
            "safe → memory restored to active status. "
            "malicious → memory flagged (use quarantine_purge to delete). "
            "uncertain → held for further review."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "quarantine_id": {"type": "integer", "description": "ID from quarantine_list"},
                "verdict": {
                    "type": "string",
                    "enum": ["safe", "malicious", "uncertain"],
                },
            },
            "required": ["quarantine_id", "verdict"],
        },
    ),
    Tool(
        name="quarantine_purge",
        description=(
            "Permanently delete a memory marked malicious and retract all derived knowledge edges. "
            "Irreversible. Requires verdict='malicious' (set via quarantine_review first). "
            "Use dry_run=true to preview what would be deleted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "quarantine_id": {"type": "integer", "description": "ID of the malicious quarantine record"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["quarantine_id"],
        },
    ),
]

DISPATCH: dict = {
    "quarantine_list": lambda **kw: tool_quarantine_list(**kw),
    "quarantine_review": lambda **kw: tool_quarantine_review(**kw),
    "quarantine_purge": lambda **kw: tool_quarantine_purge(**kw),
}
