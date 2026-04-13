"""brainctl MCP tools — D-MEM RPE routing (issue #31).

D-MEM: three-tier write gate based on Reward Prediction Error routing.
(Song & Xin, arXiv 2603.14597, 2025)

RPE tiers:
  SKIP           (score < 0.3)  — discarded, never written
  CONSTRUCT_ONLY (0.3 ≤ score < 0.7) — written to DB, not embedded or FTS-indexed
  FULL_EVOLUTION (score ≥ 0.7) — full pipeline: embed + FTS index + KG links

Tools:
  memory_promote  — promote a CONSTRUCT_ONLY memory to FULL_EVOLUTION (embed + index)
  tier_stats      — show write-tier distribution for an agent
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))
OLLAMA_EMBED_URL = os.environ.get("BRAINCTL_OLLAMA_URL", "http://localhost:11434/api/embed")
EMBED_MODEL = os.environ.get("BRAINCTL_EMBED_MODEL", "nomic-embed-text")
EMBED_DIMENSIONS = int(os.environ.get("BRAINCTL_EMBED_DIMENSIONS", "768"))

_now = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _embed(text: str) -> bytes | None:
    """Get embedding blob from Ollama. Returns None if unavailable."""
    try:
        import urllib.request, json as _json, struct as _struct
        body = _json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(OLLAMA_EMBED_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        vec = data.get("embeddings", [None])[0]
        if vec:
            return _struct.pack(f"{len(vec)}f", *vec)
    except Exception:
        pass
    return None


def _get_vec_db() -> sqlite3.Connection | None:
    """Open a connection with sqlite-vec extension loaded. Returns None if unavailable."""
    try:
        import sqlite_vec
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn
    except Exception:
        return None


# ---------------------------------------------------------------------------
# memory_promote
# ---------------------------------------------------------------------------

def tool_memory_promote(
    memory_id: int | None = None,
    agent_id: str = "mcp-client",
    dry_run: bool = False,
    **kw,
) -> dict:
    """Promote a CONSTRUCT_ONLY memory to FULL_EVOLUTION.

    Embeds the memory content and adds it to FTS5 (memories_fts) and the
    vector store (vec_memories). Sets write_tier='full', indexed=1, and
    records promoted_at timestamp.

    Safe to call on already-full memories (idempotent: reports already_full=True).
    """
    if not memory_id:
        return {"ok": False, "error": "memory_id is required"}

    db = _db()
    try:
        row = db.execute(
            "SELECT id, content, write_tier, indexed FROM memories "
            "WHERE id = ? AND agent_id = ?",
            (memory_id, agent_id),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found for agent {agent_id}"}

        if row["write_tier"] == "full" and row["indexed"] == 1:
            return {"ok": True, "memory_id": memory_id, "already_full": True}

        if dry_run:
            return {"ok": True, "dry_run": True, "memory_id": memory_id,
                    "write_tier": row["write_tier"], "indexed": row["indexed"]}

        content = row["content"]
        blob = _embed(content)
        embedded = False

        if blob:
            vdb = _get_vec_db()
            if vdb:
                try:
                    vdb.execute(
                        "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                        (memory_id, blob),
                    )
                    vdb.execute(
                        "INSERT OR IGNORE INTO embeddings "
                        "(source_table, source_id, model, dimensions, vector) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("memories", memory_id, EMBED_MODEL, EMBED_DIMENSIONS, blob),
                    )
                    vdb.commit()
                    embedded = True
                finally:
                    vdb.close()

        # Promote: set write_tier='full', indexed=1, promoted_at=now
        # The memories_fts_update_insert trigger fires when indexed becomes 1
        db.execute(
            "UPDATE memories SET write_tier = 'full', indexed = 1, promoted_at = ? WHERE id = ?",
            (_now(), memory_id),
        )
        db.commit()

        # Log a memory_promoted event
        try:
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, created_at) "
                "VALUES (?, 'memory_promoted', ?, ?)",
                (agent_id, f"Promoted memory {memory_id} to FULL_EVOLUTION", _now()),
            )
            db.commit()
        except Exception:
            pass

        return {
            "ok": True,
            "memory_id": memory_id,
            "promoted": True,
            "embedded": embedded,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# tier_stats
# ---------------------------------------------------------------------------

def tool_tier_stats(
    agent_id: str = "mcp-client",
    **kw,
) -> dict:
    """Show write-tier distribution for an agent.

    Returns count and percentage for each tier (full, construct, skip-equivalent),
    plus average worthiness_score per tier if available.
    """
    db = _db()
    try:
        rows = db.execute(
            "SELECT write_tier, COUNT(*) as cnt FROM memories "
            "WHERE agent_id = ? AND retired_at IS NULL "
            "GROUP BY write_tier",
            (agent_id,),
        ).fetchall()
        if not rows:
            return {"ok": True, "agent_id": agent_id, "total": 0, "tiers": {}}

        total = sum(r["cnt"] for r in rows)
        tiers = {}
        for r in rows:
            tiers[r["write_tier"]] = {
                "count": r["cnt"],
                "pct": round(100.0 * r["cnt"] / total, 1),
            }

        # Construct-only: unindexed memories that can be promoted
        unindexed = db.execute(
            "SELECT COUNT(*) as cnt FROM memories "
            "WHERE agent_id = ? AND indexed = 0 AND retired_at IS NULL",
            (agent_id,),
        ).fetchone()["cnt"]

        return {
            "ok": True,
            "agent_id": agent_id,
            "total": total,
            "tiers": tiers,
            "unindexed_count": unindexed,
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
        name="memory_promote",
        description=(
            "Promote a CONSTRUCT_ONLY memory to FULL_EVOLUTION: embed content and add to FTS5 "
            "and vector store. Sets write_tier='full', indexed=1. Idempotent — safe to call on "
            "memories that are already full. Use dry_run=true to preview without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "ID of memory to promote"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="tier_stats",
        description=(
            "Show write-tier distribution (full/construct) for an agent. "
            "Reports count, percentage, and number of unindexed CONSTRUCT_ONLY memories "
            "eligible for promotion."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
]

DISPATCH: dict = {
    "memory_promote": lambda **kw: tool_memory_promote(**kw),
    "tier_stats": lambda **kw: tool_tier_stats(**kw),
}
