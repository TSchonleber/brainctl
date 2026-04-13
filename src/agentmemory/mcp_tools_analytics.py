"""brainctl MCP tools — access analytics & retrieval effectiveness."""
from __future__ import annotations
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from mcp.types import Tool

from agentmemory.lib.mcp_helpers import now_iso, open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


_now = now_iso


# ---------------------------------------------------------------------------
# Stopwords for search_patterns tokenization
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for",
    "from", "has", "he", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "will", "with", "i",
    "my", "me", "we", "our", "you", "your", "not", "no", "so", "but",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase-tokenize text; drop stopwords and tokens shorter than 3 chars."""
    tokens = _TOKEN_RE.findall((text or "").lower())
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Tool: hot_memories
# ---------------------------------------------------------------------------

def tool_hot_memories(
    agent_id: str = "mcp-client",
    days: int = 30,
    limit: int = 20,
    **_kw,
) -> dict:
    """Return memories recalled most frequently in the last N days.

    Joins access_log (action='read', target_table='memories') with the
    memories table to count reads per memory.
    """
    try:
        conn = _db()
        rows = conn.execute(
            """
            SELECT
                m.id           AS memory_id,
                m.content      AS content,
                m.category     AS category,
                m.confidence   AS confidence,
                COUNT(al.id)   AS recall_count,
                MAX(al.created_at) AS last_recalled
            FROM access_log al
            JOIN memories m
                ON al.target_id = m.id
                AND al.target_table = 'memories'
            WHERE al.agent_id = ?
              AND al.action = 'read'
              AND al.created_at >= datetime('now', ?)
              AND m.retired_at IS NULL
            GROUP BY m.id
            ORDER BY recall_count DESC, last_recalled DESC
            LIMIT ?
            """,
            (agent_id, f"-{days} days", limit),
        ).fetchall()

        conn.close()
        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "memories": [
                {
                    "memory_id": r["memory_id"],
                    "content_snippet": r["content"][:200],
                    "recall_count": r["recall_count"],
                    "last_recalled": r["last_recalled"],
                    "category": r["category"],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: cold_memories
# ---------------------------------------------------------------------------

def tool_cold_memories(
    agent_id: str = "mcp-client",
    older_than_days: int = 90,
    limit: int = 20,
    **_kw,
) -> dict:
    """Return active memories that have never been recalled (pruning candidates).

    Uses memories.recalled_count = 0 for efficiency; optionally restricts to
    memories older than older_than_days.
    """
    try:
        conn = _db()
        rows = conn.execute(
            """
            SELECT id, content, confidence, category, created_at
            FROM memories
            WHERE agent_id = ?
              AND retired_at IS NULL
              AND recalled_count = 0
              AND created_at <= datetime('now', ?)
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (agent_id, f"-{older_than_days} days", limit),
        ).fetchall()

        conn.close()
        return {
            "ok": True,
            "agent_id": agent_id,
            "older_than_days": older_than_days,
            "memories": [
                {
                    "memory_id": r["id"],
                    "content_snippet": r["content"][:200],
                    "confidence": r["confidence"],
                    "category": r["category"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: search_patterns
# ---------------------------------------------------------------------------

def tool_search_patterns(
    agent_id: str = "mcp-client",
    days: int = 7,
    limit: int = 20,
    **_kw,
) -> dict:
    """Return most common query terms from access_log searches in the last N days."""
    try:
        conn = _db()
        rows = conn.execute(
            """
            SELECT query
            FROM access_log
            WHERE agent_id = ?
              AND action = 'search'
              AND query IS NOT NULL
              AND query != ''
              AND created_at >= datetime('now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchall()
        conn.close()

        freq: dict[str, int] = {}
        for r in rows:
            for token in _tokenize(r["query"]):
                freq[token] = freq.get(token, 0) + 1

        sorted_terms = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:limit]
        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "terms": [{"term": t, "frequency": f} for t, f in sorted_terms],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: retrieval_effectiveness
# ---------------------------------------------------------------------------

def tool_retrieval_effectiveness(
    agent_id: str = "mcp-client",
    days: int = 30,
    **_kw,
) -> dict:
    """Return the ratio of searches that returned results in the last N days."""
    try:
        conn = _db()
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                                AS total_searches,
                SUM(CASE WHEN result_count > 0 THEN 1 ELSE 0 END)     AS searches_with_results,
                AVG(CASE WHEN result_count IS NOT NULL THEN result_count ELSE 0 END) AS avg_result_count
            FROM access_log
            WHERE agent_id = ?
              AND action = 'search'
              AND created_at >= datetime('now', ?)
            """,
            (agent_id, f"-{days} days"),
        ).fetchone()
        conn.close()

        total = row["total_searches"] or 0
        with_results = row["searches_with_results"] or 0
        effectiveness_rate = round(with_results / total, 4) if total > 0 else 0.0
        avg_result_count = round(row["avg_result_count"] or 0.0, 2)

        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "total_searches": total,
            "searches_with_results": with_results,
            "effectiveness_rate": effectiveness_rate,
            "avg_result_count": avg_result_count,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: agent_activity
# ---------------------------------------------------------------------------

def tool_agent_activity(
    agent_id: str = "mcp-client",
    days: int = 7,
    **_kw,
) -> dict:
    """Return per-agent operation breakdown for the last N days."""
    _TRACKED_ACTIONS = ("read", "write", "search", "push", "promote", "retire")
    try:
        conn = _db()
        rows = conn.execute(
            """
            SELECT agent_id, action, COUNT(*) AS cnt
            FROM access_log
            WHERE created_at >= datetime('now', ?)
            GROUP BY agent_id, action
            ORDER BY agent_id, action
            """,
            (f"-{days} days",),
        ).fetchall()
        conn.close()

        # Build agent -> operations map
        agents_map: dict[str, dict] = {}
        for r in rows:
            aid = r["agent_id"]
            if aid not in agents_map:
                agents_map[aid] = {"agent_id": aid, "operations": {}, "total": 0}
            agents_map[aid]["operations"][r["action"]] = r["cnt"]
            agents_map[aid]["total"] += r["cnt"]

        # Ensure all tracked actions appear (with 0) for each agent
        for entry in agents_map.values():
            for act in _TRACKED_ACTIONS:
                entry["operations"].setdefault(act, 0)

        agents_list = sorted(agents_map.values(), key=lambda x: x["total"], reverse=True)

        return {
            "ok": True,
            "days": days,
            "agents": agents_list,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: memory_utility_rate
# ---------------------------------------------------------------------------

def tool_memory_utility_rate(
    agent_id: str = "mcp-client",
    days: int = 30,
    **_kw,
) -> dict:
    """Return the fraction of pushed memories that were subsequently recalled.

    Inspects push_delivered events in the events table.  Each event's
    detail column is a JSON blob with keys:
      memory_ids        — list of memory IDs pushed
      recalled_at_push  — {memory_id: recalled_count} snapshot at push time

    We compare each memory's current recalled_count to its snapshot value;
    if it increased, the memory was recalled after the push.
    """
    try:
        conn = _db()
        push_rows = conn.execute(
            """
            SELECT id, detail, created_at
            FROM events
            WHERE agent_id = ?
              AND event_type = 'push_delivered'
              AND created_at >= datetime('now', ?)
            ORDER BY created_at ASC
            """,
            (agent_id, f"-{days} days"),
        ).fetchall()

        pushes_tracked = len(push_rows)
        all_pushed_ids: set[int] = set()
        snapshot_map: dict[int, int] = {}  # memory_id -> recalled_count at push time

        for r in push_rows:
            try:
                meta = json.loads(r["detail"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            memory_ids: list[int] = [int(x) for x in (meta.get("memory_ids") or [])]
            recalled_at_push: dict = meta.get("recalled_at_push") or {}
            for mid in memory_ids:
                all_pushed_ids.add(mid)
                # Take the minimum (earliest) snapshot value so later pushes don't hide recall
                snap_val = int(recalled_at_push.get(str(mid), recalled_at_push.get(mid, 0)))
                if mid not in snapshot_map or snap_val < snapshot_map[mid]:
                    snapshot_map[mid] = snap_val

        memories_pushed = len(all_pushed_ids)
        memories_recalled = 0

        if all_pushed_ids:
            ph = ",".join("?" * len(all_pushed_ids))
            current_rows = conn.execute(
                f"SELECT id, recalled_count FROM memories WHERE id IN ({ph})",
                list(all_pushed_ids),
            ).fetchall()
            for m in current_rows:
                current_count = m["recalled_count"] or 0
                baseline = snapshot_map.get(m["id"], 0)
                if current_count > baseline:
                    memories_recalled += 1

        conn.close()

        utility_rate = round(memories_recalled / memories_pushed, 4) if memories_pushed > 0 else 0.0

        return {
            "ok": True,
            "agent_id": agent_id,
            "days": days,
            "pushes_tracked": pushes_tracked,
            "memories_pushed": memories_pushed,
            "memories_recalled": memories_recalled,
            "utility_rate": utility_rate,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="hot_memories",
        description=(
            "Return memories recalled most frequently by an agent in the last N days. "
            "Joins access_log reads to the memories table. Useful for identifying "
            "which memories the agent relies on most."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent whose access log to query"},
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30)",
                    "default": 30,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 20)",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="cold_memories",
        description=(
            "Return active memories that have never been recalled — potential pruning "
            "candidates. Restricts to memories older than older_than_days to avoid "
            "flagging newly written content."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to inspect"},
                "older_than_days": {
                    "type": "integer",
                    "description": "Only include memories older than this many days (default: 90)",
                    "default": 90,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 20)",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="search_patterns",
        description=(
            "Return the most frequent query terms from an agent's searches in the last "
            "N days. Tokenizes query_text from access_log and counts term frequency. "
            "Useful for understanding what an agent looks for most."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to inspect"},
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 7)",
                    "default": 7,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of top terms to return (default: 20)",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="retrieval_effectiveness",
        description=(
            "Return the fraction of searches that returned at least one result in the "
            "last N days, plus total search count and average result count. "
            "Low effectiveness_rate suggests missing memories or poor query quality."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to inspect"},
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30)",
                    "default": 30,
                },
            },
        },
    ),
    Tool(
        name="agent_activity",
        description=(
            "Return a per-agent operation breakdown (read, write, search, push, …) "
            "for the last N days. Returns an activity matrix across all agents "
            "visible in access_log, sorted by total activity descending."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID (ignored; fleet-wide)"},
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 7)",
                    "default": 7,
                },
            },
        },
    ),
    Tool(
        name="memory_utility_rate",
        description=(
            "Return what fraction of memories delivered via push were subsequently recalled. "
            "Inspects push_delivered events and compares each memory's recalled_count "
            "to its snapshot value at push time. utility_rate near 1.0 indicates the "
            "push pipeline is delivering genuinely useful memories."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to inspect"},
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default: 30)",
                    "default": 30,
                },
            },
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch table (tool name -> callable)
# ---------------------------------------------------------------------------

DISPATCH: dict = {
    "hot_memories": tool_hot_memories,
    "cold_memories": tool_cold_memories,
    "search_patterns": tool_search_patterns,
    "retrieval_effectiveness": tool_retrieval_effectiveness,
    "agent_activity": tool_agent_activity,
    "memory_utility_rate": tool_memory_utility_rate,
}
