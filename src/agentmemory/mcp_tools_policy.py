"""brainctl MCP tools — policy system."""
from __future__ import annotations
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

from agentmemory.lib.mcp_helpers import now_iso, open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


_now = now_iso


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_policy_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS policy_memories (
            policy_id               TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            category                TEXT NOT NULL DEFAULT 'general',
            status                  TEXT NOT NULL DEFAULT 'active',
            scope                   TEXT NOT NULL DEFAULT 'global',
            priority                INTEGER NOT NULL DEFAULT 50,
            trigger_condition       TEXT NOT NULL,
            action_directive        TEXT NOT NULL,
            authored_by             TEXT NOT NULL DEFAULT 'unknown',
            derived_from            TEXT,
            confidence_threshold    REAL NOT NULL DEFAULT 0.5,
            wisdom_half_life_days   INTEGER NOT NULL DEFAULT 30,
            version                 INTEGER NOT NULL DEFAULT 1,
            active_since            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            last_validated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            expires_at              TEXT,
            feedback_count          INTEGER NOT NULL DEFAULT 0,
            success_count           INTEGER NOT NULL DEFAULT 0,
            failure_count           INTEGER NOT NULL DEFAULT 0,
            created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_status_category ON policy_memories(status, category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_scope ON policy_memories(scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_confidence ON policy_memories(confidence_threshold DESC)")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS policy_memories_fts USING fts5(
            trigger_condition, action_directive, name,
            content=policy_memories, content_rowid=rowid
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pm_fts_insert AFTER INSERT ON policy_memories BEGIN
            INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
            VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pm_fts_update AFTER UPDATE ON policy_memories BEGIN
            INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
            VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
            INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
            VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pm_fts_delete AFTER DELETE ON policy_memories BEGIN
            INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
            VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
        END
    """)
    conn.commit()


def _policy_effective_confidence(confidence: float, half_life_days: int, last_validated_at: str) -> float:
    """Apply temporal decay to confidence based on time since last validation."""
    try:
        validated = datetime.fromisoformat(last_validated_at)
        # Make both timezone-naive for comparison
        if validated.tzinfo is not None:
            validated = validated.replace(tzinfo=None)
        age_days = (datetime.utcnow() - validated).days
        if half_life_days <= 0:
            return confidence
        decay = 0.5 ** (age_days / half_life_days)
        return confidence * decay
    except Exception:
        return confidence


def _neuromod_org_state(conn: sqlite3.Connection) -> str:
    """Return the current org_state from neuromodulation_state if the table exists, else 'normal'."""
    try:
        row = conn.execute("SELECT org_state FROM neuromodulation_state WHERE id=1").fetchone()
        return row["org_state"] if row else "normal"
    except Exception:
        return "normal"


def _log_access(conn: sqlite3.Connection, agent_id: str, action: str,
                target_table: str | None = None, target_id: str | None = None,
                query: str | None = None, result_count: int | None = None) -> None:
    try:
        conn.execute(
            "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count)"
            " VALUES (?,?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query, result_count),
        )
    except Exception:
        pass  # access_log is best-effort


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_policy_match(
    agent_id: str = "mcp-client",
    context: str = "",
    category: str | None = None,
    scope: str | None = None,
    top_k: int = 3,
    min_confidence: float = 0.4,
    staleness_mode: str = "warn",
    all_policies: bool = False,
    **_kw: Any,
) -> dict:
    """Match active policies to a given context string."""
    if not context:
        return {"ok": False, "error": "context is required"}

    conn = _db()
    try:
        _ensure_policy_tables(conn)
        now_str = datetime.utcnow().isoformat()

        # Neuromod mode: surface ALL policies when org is in incident/sprint state
        org_state = _neuromod_org_state(conn)
        neuromod_active = all_policies or org_state in ("incident", "sprint")
        effective_top_k = 9999 if neuromod_active else (top_k or 3)
        effective_min_conf = 0.0 if neuromod_active else (min_confidence if min_confidence is not None else 0.4)

        base_where = "status = 'active' AND (expires_at IS NULL OR expires_at > ?)"
        base_params: list = [now_str]

        if category:
            base_where += " AND category = ?"
            base_params.append(category)

        if scope:
            base_where += " AND (scope = 'global' OR scope = ?)"
            base_params.append(scope)

        fts_rows = []
        try:
            fts_query = " OR ".join(w for w in context.split() if len(w) > 3)
            if fts_query:
                fts_rows = conn.execute(
                    f"""SELECT pm.*, pmf.rank as fts_rank
                        FROM policy_memories_fts pmf
                        JOIN policy_memories pm ON pm.rowid = pmf.rowid
                        WHERE pmf MATCH ? AND {base_where}
                        ORDER BY pmf.rank
                        LIMIT ?""",
                    [fts_query] + base_params + [effective_top_k * 2],
                ).fetchall()
        except Exception:
            fts_rows = []

        if not fts_rows:
            fts_rows = conn.execute(
                f"SELECT *, NULL as fts_rank FROM policy_memories WHERE {base_where}"
                " ORDER BY priority DESC, confidence_threshold DESC LIMIT ?",
                base_params + [effective_top_k * 2],
            ).fetchall()

        results = []
        stale_warnings = []
        for row in fts_rows:
            r = dict(row)
            eff_conf = _policy_effective_confidence(
                r["confidence_threshold"], r["wisdom_half_life_days"], r["last_validated_at"]
            )
            r["confidence_effective"] = round(eff_conf, 4)
            if eff_conf < effective_min_conf:
                if staleness_mode == "warn":
                    r["staleness_warning"] = True
                    stale_warnings.append(r)
                continue
            r["staleness_warning"] = eff_conf < r["confidence_threshold"] * 0.8
            results.append(r)

        results = sorted(
            results, key=lambda x: (x["priority"], x["confidence_effective"]), reverse=True
        )[:effective_top_k]

        _log_access(conn, agent_id, "policy_match", "policy_memories", None, context, len(results))
        conn.commit()

        return {
            "ok": True,
            "policies": results,
            "stale_excluded": stale_warnings,
            "query": context,
            "neuromod_active": neuromod_active,
            "org_state": org_state,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def tool_policy_add(
    agent_id: str = "mcp-client",
    name: str = "",
    trigger: str = "",
    directive: str = "",
    category: str = "general",
    scope: str = "global",
    priority: int = 50,
    confidence: float = 0.5,
    half_life: int = 30,
    derived_from: str | None = None,
    expires_at: str | None = None,
    **_kw: Any,
) -> dict:
    """Add a new policy memory."""
    if not name:
        return {"ok": False, "error": "name is required"}
    if not trigger:
        return {"ok": False, "error": "trigger is required"}
    if not directive:
        return {"ok": False, "error": "directive is required"}

    conn = _db()
    try:
        _ensure_policy_tables(conn)
        policy_id = f"pol_{uuid.uuid4().hex[:12]}"
        now = datetime.utcnow().isoformat()

        conn.execute(
            """INSERT INTO policy_memories
               (policy_id, name, category, scope, priority, trigger_condition, action_directive,
                authored_by, derived_from, confidence_threshold, wisdom_half_life_days,
                active_since, last_validated_at, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                policy_id,
                name,
                category or "general",
                scope or "global",
                priority if priority is not None else 50,
                trigger,
                directive,
                agent_id,
                derived_from or None,
                confidence if confidence is not None else 0.5,
                half_life if half_life is not None else 30,
                now,
                now,
                expires_at or None,
                now,
                now,
            ),
        )
        conn.commit()
        _log_access(conn, agent_id, "policy_add", "policy_memories", policy_id)
        conn.commit()

        return {"ok": True, "policy_id": policy_id, "name": name, "created_at": now}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def tool_policy_feedback(
    agent_id: str = "mcp-client",
    policy_id: str = "",
    outcome: str = "",
    boost: float = 0.02,
    notes: str | None = None,
    **_kw: Any,
) -> dict:
    """Record success or failure feedback for a policy, adjusting its confidence."""
    if not policy_id:
        return {"ok": False, "error": "policy_id is required"}
    if outcome not in ("success", "failure"):
        return {"ok": False, "error": "outcome must be 'success' or 'failure'"}

    conn = _db()
    try:
        _ensure_policy_tables(conn)
        now = datetime.utcnow().isoformat()

        row = conn.execute(
            "SELECT * FROM policy_memories WHERE policy_id = ? OR name = ?", (policy_id, policy_id)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Policy not found: {policy_id}"}

        row = dict(row)
        old_conf = row["confidence_threshold"]

        if outcome == "success":
            delta = boost if boost is not None else 0.02
            new_conf = min(1.0, old_conf + delta)
            sc_delta, fc_delta = 1, 0
        else:  # failure
            new_conf = max(0.1, old_conf - 0.05)
            sc_delta, fc_delta = 0, 1

        new_feedback_count = row["feedback_count"] + 1
        new_failure_count = row["failure_count"] + fc_delta
        new_success_count = row["success_count"] + sc_delta

        # Auto-flag for review if >50% failure rate with >= 5 feedback events
        stale_flagged = (
            new_feedback_count >= 5 and new_failure_count / new_feedback_count > 0.5
        )

        conn.execute(
            """UPDATE policy_memories SET
               confidence_threshold = ?,
               success_count = success_count + ?,
               failure_count = failure_count + ?,
               feedback_count = feedback_count + 1,
               last_validated_at = ?,
               updated_at = ?
               WHERE policy_id = ?""",
            (new_conf, sc_delta, fc_delta, now, now, row["policy_id"]),
        )
        conn.commit()
        _log_access(conn, agent_id, f"policy_feedback_{outcome}", "policy_memories", row["policy_id"])
        conn.commit()

        result: dict = {
            "ok": True,
            "policy_id": row["policy_id"],
            "name": row["name"],
            "outcome": outcome,
            "confidence_before": round(old_conf, 4),
            "confidence_after": round(new_conf, 4),
            "feedback_count": new_feedback_count,
            "notes": notes or None,
        }
        if stale_flagged:
            result["stale_warning"] = (
                f"Policy failure rate > 50% over {new_feedback_count} events — flagged for review"
            )
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def tool_policy_list(
    agent_id: str = "mcp-client",
    status: str = "active",
    category: str | None = None,
    scope: str | None = None,
    **_kw: Any,
) -> dict:
    """List policies with optional filtering by status, category, or scope."""
    conn = _db()
    try:
        _ensure_policy_tables(conn)

        where = "1=1"
        params: list = []

        status_filter = status or "active"
        if status_filter != "all":
            where += " AND status = ?"
            params.append(status_filter)

        if category:
            where += " AND category = ?"
            params.append(category)

        if scope:
            where += " AND (scope = 'global' OR scope = ?)"
            params.append(scope)

        rows = conn.execute(
            f"SELECT * FROM policy_memories WHERE {where} ORDER BY priority DESC, confidence_threshold DESC",
            params,
        ).fetchall()

        results = []
        flagged_ids = []
        for row in rows:
            r = dict(row)
            eff_conf = _policy_effective_confidence(
                r["confidence_threshold"], r["wisdom_half_life_days"], r["last_validated_at"]
            )
            r["confidence_effective"] = round(eff_conf, 4)
            total = r["success_count"] + r["failure_count"]
            r["failure_rate"] = round(r["failure_count"] / total, 3) if total >= 5 else None
            r["stale"] = r["failure_rate"] is not None and r["failure_rate"] > 0.5
            if r["stale"]:
                flagged_ids.append(r["policy_id"])
            results.append(r)

        _log_access(conn, agent_id, "policy_list", "policy_memories", None, status_filter, len(results))
        conn.commit()

        return {
            "ok": True,
            "policies": results,
            "stale_flagged": flagged_ids,
            "count": len(results),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="policy_match",
        description=(
            "Match active policies to a context string using full-text search and confidence "
            "decay. Returns ranked policies whose trigger conditions match the given context. "
            "Useful for agents to look up applicable rules/guidelines before taking action."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "The context or situation to match policies against",
                },
                "category": {
                    "type": "string",
                    "description": "Filter to a specific policy category (e.g. 'safety', 'general')",
                },
                "scope": {
                    "type": "string",
                    "description": "Scope filter: match 'global' plus this scope (e.g. 'project:foo')",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of policies to return",
                    "default": 3,
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum effective confidence threshold (0.0-1.0)",
                    "default": 0.4,
                },
                "staleness_mode": {
                    "type": "string",
                    "enum": ["warn", "ignore"],
                    "description": "How to handle stale/low-confidence policies: 'warn' excludes them and reports them separately, 'ignore' skips them silently",
                    "default": "warn",
                },
                "all_policies": {
                    "type": "boolean",
                    "description": "Return all matching policies regardless of confidence (neuromod override)",
                    "default": False,
                },
            },
            "required": ["context"],
        },
    ),
    Tool(
        name="policy_add",
        description=(
            "Add a new policy memory. Policies encode behavioral rules: when a trigger condition "
            "is met, an action directive is applied. Confidence and half-life control how the "
            "policy's trustworthiness decays over time without validation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short unique name for this policy",
                },
                "trigger": {
                    "type": "string",
                    "description": "Condition that activates this policy (plain text description)",
                },
                "directive": {
                    "type": "string",
                    "description": "What to do when the trigger fires",
                },
                "category": {
                    "type": "string",
                    "description": "Policy category (e.g. 'safety', 'workflow', 'general')",
                    "default": "general",
                },
                "scope": {
                    "type": "string",
                    "description": "Scope of the policy: 'global' or 'project:<name>'",
                    "default": "global",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority 0-100, higher wins when multiple policies match",
                    "default": 50,
                },
                "confidence": {
                    "type": "number",
                    "description": "Initial confidence 0.0-1.0",
                    "default": 0.5,
                },
                "half_life": {
                    "type": "integer",
                    "description": "Wisdom half-life in days: how quickly confidence decays without revalidation",
                    "default": 30,
                },
                "derived_from": {
                    "type": "string",
                    "description": "Source policy_id or name this was derived from (optional)",
                },
                "expires_at": {
                    "type": "string",
                    "description": "ISO-8601 expiry datetime (optional)",
                },
            },
            "required": ["name", "trigger", "directive"],
        },
    ),
    Tool(
        name="policy_feedback",
        description=(
            "Record success or failure feedback for a policy. Positive feedback boosts confidence; "
            "negative feedback reduces it. Policies with > 50% failure rate over 5+ events are "
            "automatically flagged for review."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "policy_id": {
                    "type": "string",
                    "description": "policy_id or name of the policy to update",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure"],
                    "description": "Whether the policy worked correctly ('success') or failed ('failure')",
                },
                "boost": {
                    "type": "number",
                    "description": "Confidence delta to add on success (default 0.02)",
                    "default": 0.02,
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes about this feedback event",
                },
            },
            "required": ["policy_id", "outcome"],
        },
    ),
    Tool(
        name="policy_list",
        description=(
            "List policies with optional filtering. Returns full policy details including "
            "effective confidence (with decay applied), failure rates, and stale flags. "
            "Policies flagged as stale (>50% failure rate, >=5 events) are highlighted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "inactive", "all"],
                    "description": "Filter by status (default: 'active')",
                    "default": "active",
                },
                "category": {
                    "type": "string",
                    "description": "Filter to a specific category",
                },
                "scope": {
                    "type": "string",
                    "description": "Filter to 'global' plus this scope",
                },
            },
        },
    ),
]

DISPATCH: dict = {
    "policy_match": tool_policy_match,
    "policy_add": tool_policy_add,
    "policy_feedback": tool_policy_feedback,
    "policy_list": tool_policy_list,
}
