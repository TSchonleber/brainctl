"""brainctl MCP tools — workspace coordination."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _age_str(created_at_str: str | None) -> str:
    """Return human-readable relative age like '3 days ago'."""
    if not created_at_str:
        return "unknown"
    try:
        ts = created_at_str.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts, fmt)
                    dt = dt.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return "unknown"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return "unknown"

    if days < 1 / 1440:
        return "just now"
    if days < 1 / 24:
        minutes = int(days * 1440)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if days < 1:
        hours = int(days * 24)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days < 7:
        d = int(days)
        return f"{d} day{'s' if d != 1 else ''} ago"
    if days < 30:
        w = int(days / 7)
        return f"{w} week{'s' if w != 1 else ''} ago"
    if days < 365:
        m = int(days / 30)
        return f"{m} month{'s' if m != 1 else ''} ago"
    y = int(days / 365)
    return f"{y} year{'s' if y != 1 else ''} ago"


def _ws_config(db: sqlite3.Connection) -> dict:
    """Return workspace_config as a dict. Falls back to defaults if table missing."""
    try:
        rows = db.execute("SELECT key, value FROM workspace_config").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {
            "ignition_threshold": "0.85",
            "urgent_threshold": "0.65",
            "governor_max_per_hour": "20",
            "broadcast_ttl_hours": "48",
            "phi_window_hours": "24",
            "phi_warn_below": "0.05",
            "enabled": "1",
        }


def _ws_ignition_threshold(db: sqlite3.Connection) -> float:
    """Return current effective ignition threshold (adjusted for neuromod state)."""
    cfg = _ws_config(db)
    try:
        row = db.execute(
            "SELECT org_state FROM neuromodulation_state WHERE id = 1"
        ).fetchone()
        if row and row["org_state"] == "incident":
            return float(cfg.get("urgent_threshold", "0.65"))
    except Exception:
        pass
    return float(cfg.get("ignition_threshold", "0.85"))


def _ws_compute_salience(category: str, confidence: float, scope: str | None, tags_json=None) -> float:
    """Compute salience score for a memory (0.0-1.0)."""
    base = 0.0
    cat_weights = {
        "decision": 0.30, "identity": 0.30, "convention": 0.25,
        "lesson": 0.20, "preference": 0.15, "project": 0.15,
        "user": 0.10, "environment": 0.10, "integration": 0.05,
    }
    base += cat_weights.get(category, 0.10)
    base += confidence * 0.50
    if scope == "global" or not scope:
        base += 0.10
    elif scope.startswith("project:"):
        base += 0.08
    if tags_json:
        try:
            tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
            if any(t in ("critical", "incident", "urgent", "blocker") for t in (tags or [])):
                base += 0.15
        except Exception:
            pass
    return round(min(base, 1.0), 4)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_workspace_status(n: int = 20, scope: str | None = None, **kw) -> dict[str, Any]:
    """Show current global workspace — broadcasts active right now."""
    db = _db()
    try:
        cfg = _ws_config(db)
        threshold = _ws_ignition_threshold(db)
        ttl_hours = int(cfg.get("broadcast_ttl_hours", 48))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        sql = """
            SELECT wb.id, wb.memory_id, wb.agent_id, wb.salience, wb.summary,
                   wb.target_scope, wb.broadcast_at, wb.ack_count, wb.triggered_by,
                   m.category, m.confidence, m.scope as mem_scope
            FROM workspace_broadcasts wb
            JOIN memories m ON wb.memory_id = m.id
            WHERE wb.broadcast_at >= ?
        """
        params: list[Any] = [cutoff]
        if scope:
            sql += " AND wb.target_scope LIKE ?"
            params.append(f"{scope}%")
        sql += " ORDER BY wb.salience DESC, wb.broadcast_at DESC LIMIT ?"
        params.append(n)
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {"ok": True, "data": None, "message": "table not found"}
            raise
        results = _rows_to_list(rows)
        for r in results:
            r["age"] = _age_str(r.get("broadcast_at"))
        try:
            nm_row = db.execute("SELECT org_state FROM neuromodulation_state WHERE id=1").fetchone()
            org_state = nm_row["org_state"] if nm_row else "normal"
        except Exception:
            org_state = "unknown"
        return {
            "ok": True,
            "active_broadcasts": len(results),
            "ignition_threshold": threshold,
            "org_state": org_state,
            "broadcasts": results,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def tool_workspace_history(
    n: int = 30,
    since: int | None = None,
    agent: str | None = None,
    **kw,
) -> dict[str, Any]:
    """Show recent broadcast history (all time, paginated)."""
    db = _db()
    try:
        sql = """
            SELECT wb.id, wb.memory_id, wb.agent_id, wb.salience, wb.summary,
                   wb.target_scope, wb.broadcast_at, wb.ack_count, wb.triggered_by,
                   m.category
            FROM workspace_broadcasts wb
            JOIN memories m ON wb.memory_id = m.id
            WHERE 1=1
        """
        params: list[Any] = []
        if since is not None:
            sql += " AND wb.id > ?"
            params.append(since)
        if agent:
            sql += " AND wb.agent_id = ?"
            params.append(agent)
        sql += " ORDER BY wb.id DESC LIMIT ?"
        params.append(n)
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {"ok": True, "data": None, "message": "table not found"}
            raise
        results = list(reversed(_rows_to_list(rows)))
        for r in results:
            r["age"] = _age_str(r.get("broadcast_at"))
        return {"ok": True, "history": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def tool_workspace_broadcast(
    memory_id: int,
    agent: str = "manual",
    summary: str | None = None,
    scope: str = "global",
    **kw,
) -> dict[str, Any]:
    """Manually broadcast a memory into the global workspace."""
    db = _db()
    try:
        row = db.execute(
            "SELECT id, content, confidence, category, scope, tags FROM memories WHERE id = ? AND retired_at IS NULL",
            (memory_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found or retired"}
        salience = _ws_compute_salience(row["category"], row["confidence"], row["scope"], row["tags"])
        effective_summary = summary if summary else str(row["content"])[:200]
        try:
            db.execute(
                "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) VALUES (?,?,?,?,?,?)",
                (memory_id, agent, salience, effective_summary, scope, "manual")
            )
            db.commit()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {"ok": True, "data": None, "message": "table not found"}
            raise
        broadcast_id = db.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
        return {"ok": True, "broadcast_id": broadcast_id, "salience": salience, "scope": scope}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def tool_workspace_ack(
    broadcast_id: int,
    agent: str = "unknown",
    **kw,
) -> dict[str, Any]:
    """Acknowledge receipt of a broadcast."""
    db = _db()
    try:
        try:
            db.execute(
                "INSERT INTO workspace_acks (broadcast_id, agent_id) VALUES (?,?)",
                (broadcast_id, agent)
            )
            db.commit()
            return {"ok": True, "broadcast_id": broadcast_id, "agent_id": agent}
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint" in str(e):
                return {"ok": True, "already_acked": True}
            raise
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {"ok": True, "data": None, "message": "table not found"}
            raise
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def tool_workspace_phi(breakdown: bool = False, **kw) -> dict[str, Any]:
    """Compute and display the organizational integration (Phi) metric."""
    db = _db()
    try:
        cfg = _ws_config(db)
        window_hours = int(cfg.get("phi_window_hours", 24))
        phi_warn = float(cfg.get("phi_warn_below", 0.05))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            agent_rows = db.execute(
                "SELECT agent_id, COUNT(*) as cnt FROM workspace_broadcasts WHERE broadcast_at >= ? GROUP BY agent_id",
                (cutoff,)
            ).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {"ok": True, "data": None, "message": "table not found"}
            raise
        total_broadcasts = sum(r["cnt"] for r in agent_rows)
        total_acks = db.execute(
            "SELECT COUNT(*) FROM workspace_acks wa JOIN workspace_broadcasts wb ON wa.broadcast_id = wb.id WHERE wb.broadcast_at >= ?",
            (cutoff,)
        ).fetchone()[0]
        ack_rate = round(total_acks / total_broadcasts, 4) if total_broadcasts > 0 else 0.0
        active_agents = len(agent_rows)
        phi_org = ack_rate
        result: dict[str, Any] = {
            "ok": True,
            "phi_org": phi_org,
            "ack_rate": ack_rate,
            "total_broadcasts": total_broadcasts,
            "total_acks": total_acks,
            "active_agents": active_agents,
            "window_hours": window_hours,
            "warn": phi_org < phi_warn and total_broadcasts > 0,
            "warn_threshold": phi_warn,
        }
        if breakdown:
            result["agent_breakdown"] = _rows_to_list(agent_rows)
        window_start = cutoff
        window_end = _now()
        db.execute(
            "INSERT INTO workspace_phi (window_start, window_end, phi_org, broadcast_count, ack_rate, agent_pair_count) VALUES (?,?,?,?,?,?)",
            (window_start, window_end, phi_org, total_broadcasts, ack_rate, active_agents)
        )
        db.commit()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def tool_workspace_ingest(
    agent: str = "workspace-ingest",
    hours: int = 1,
    dry_run: bool = False,
    **kw,
) -> dict[str, Any]:
    """Score recent memories for ignition and broadcast any above threshold."""
    db = _db()
    try:
        threshold = _ws_ignition_threshold(db)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            rows = db.execute("""
                SELECT m.id, m.category, m.confidence, m.scope, m.content, m.tags
                FROM memories m
                WHERE m.created_at >= ?
                  AND m.retired_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM workspace_broadcasts wb WHERE wb.memory_id = m.id)
                ORDER BY m.confidence DESC
                LIMIT 50
            """, (cutoff,)).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {"ok": True, "data": None, "message": "table not found"}
            raise
        fired = []
        for row in rows:
            salience = _ws_compute_salience(row["category"], row["confidence"], row["scope"], row["tags"])
            if salience >= threshold:
                fired.append({"memory_id": row["id"], "salience": salience, "scope": row["scope"]})
                if not dry_run:
                    db.execute(
                        "INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by) VALUES (?,?,?,?,?,?)",
                        (row["id"], agent, salience, str(row["content"])[:200], row["scope"] or "global", "ingest")
                    )
        if not dry_run and fired:
            db.commit()
        return {
            "ok": True,
            "scanned": len(rows),
            "ignited": len(fired),
            "threshold": threshold,
            "dry_run": dry_run,
            "broadcasts": fired,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="workspace_status",
        description=(
            "Show the current global workspace — broadcasts that are active right now. "
            "Returns active broadcasts sorted by salience, the current ignition threshold, "
            "and the neuromodulation org_state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Maximum number of broadcasts to return",
                    "default": 20,
                },
                "scope": {
                    "type": "string",
                    "description": "Filter by target scope prefix (e.g. 'project:brain')",
                },
            },
        },
    ),
    Tool(
        name="workspace_history",
        description=(
            "Show recent broadcast history (all time, paginated). "
            "Optionally filter by agent or paginate from a specific broadcast ID."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Maximum number of records to return",
                    "default": 30,
                },
                "since": {
                    "type": "integer",
                    "description": "Return only broadcasts with id > this value (for pagination)",
                },
                "agent": {
                    "type": "string",
                    "description": "Filter by agent_id",
                },
            },
        },
    ),
    Tool(
        name="workspace_broadcast",
        description=(
            "Manually broadcast a memory into the global workspace. "
            "Computes salience from the memory's category, confidence, and tags."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "ID of the memory to broadcast",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent ID responsible for the broadcast",
                    "default": "manual",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional summary override (defaults to first 200 chars of content)",
                },
                "scope": {
                    "type": "string",
                    "description": "Target scope for the broadcast",
                    "default": "global",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="workspace_ack",
        description=(
            "Acknowledge receipt of a workspace broadcast. "
            "Idempotent — re-acknowledging the same broadcast returns ok=true with already_acked=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "broadcast_id": {
                    "type": "integer",
                    "description": "ID of the broadcast to acknowledge",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent ID acknowledging the broadcast",
                    "default": "unknown",
                },
            },
            "required": ["broadcast_id"],
        },
    ),
    Tool(
        name="workspace_phi",
        description=(
            "Compute and record the organizational integration (Phi) metric. "
            "Phi is the ack_rate over the configured window and is stored to workspace_phi. "
            "Returns a warn flag when Phi drops below the configured threshold."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "breakdown": {
                    "type": "boolean",
                    "description": "Include per-agent broadcast breakdown",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="workspace_ingest",
        description=(
            "Score recent memories for ignition and broadcast any that exceed the salience threshold. "
            "Use dry_run=true to preview what would be broadcast without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent ID to attribute the broadcasts to",
                    "default": "workspace-ingest",
                },
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to scan for new memories",
                    "default": 1,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview without writing broadcasts",
                    "default": False,
                },
            },
        },
    ),
]

DISPATCH: dict[str, Any] = {
    "workspace_status": tool_workspace_status,
    "workspace_history": tool_workspace_history,
    "workspace_broadcast": tool_workspace_broadcast,
    "workspace_ack": tool_workspace_ack,
    "workspace_phi": tool_workspace_phi,
    "workspace_ingest": tool_workspace_ingest,
}
