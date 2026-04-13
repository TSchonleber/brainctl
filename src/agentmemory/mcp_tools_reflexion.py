"""brainctl MCP tools — reflexion & outcome tracking."""
from __future__ import annotations
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

from agentmemory.lib.mcp_helpers import now_iso, open_db, rows_to_list

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


_now = now_iso


def _ensure_agent(conn, agent_id: str) -> None:
    """Auto-register an agent if it doesn't exist (prevents FK violations)."""
    if not agent_id:
        return
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at)"
            " VALUES (?, ?, 'mcp', 'active', ?, ?)",
            (agent_id, agent_id, _now(), _now()),
        )
    except Exception:
        pass  # agents table may not exist in minimal schemas


def _log_access(conn, agent_id, action, target_table=None, target_id=None, query=None, result_count=None):
    try:
        conn.execute(
            "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count)"
            " VALUES (?,?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query, result_count),
        )
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass  # access_log may not exist, or agent FK not satisfied


_rows_to_list = rows_to_list


# ---------------------------------------------------------------------------
# FTS helpers (replicated from _impl.py)
# ---------------------------------------------------------------------------

_FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^]')


def _sanitize_fts_query(query: str) -> str:
    cleaned = _FTS5_SPECIAL.sub(" ", query or "")
    return re.sub(r"\s+", " ", cleaned).strip()


# ---------------------------------------------------------------------------
# Reflexion defaults (replicated from _impl.py)
# ---------------------------------------------------------------------------

_REFLEXION_VALID_CLASSES = {
    "COORDINATION_FAILURE",
    "TOOL_MISUSE",
    "CONTEXT_LOSS",
    "REASONING_ERROR",
    "HALLUCINATION",
}

_REFLEXION_DEFAULT_CONFIDENCE = {
    "COORDINATION_FAILURE": 0.95,
    "TOOL_MISUSE": 0.80,
    "CONTEXT_LOSS": 0.75,
    "REASONING_ERROR": 0.60,
    "HALLUCINATION": 0.55,
}

_REFLEXION_DEFAULT_N = {
    "COORDINATION_FAILURE": 3,
    "CONTEXT_LOSS": 5,
    "TOOL_MISUSE": 5,
    "REASONING_ERROR": 10,
    "HALLUCINATION": 10,
}

_REFLEXION_DEFAULT_TTL = {
    "COORDINATION_FAILURE": 30,
    "CONTEXT_LOSS": 90,
    "TOOL_MISUSE": 60,
    "REASONING_ERROR": 180,
    "HALLUCINATION": 365,
}

_REFLEXION_DEFAULT_GENERALIZABLE = {
    "COORDINATION_FAILURE": ["agent_type:external"],
    "TOOL_MISUSE": ["capability:brainctl"],
    "CONTEXT_LOSS": ["scope:global"],
    "REASONING_ERROR": [],
    "HALLUCINATION": [],
}

_REFLEXION_DEFAULT_OVERRIDE = {
    "COORDINATION_FAILURE": "HARD_OVERRIDE",
    "TOOL_MISUSE": "HARD_OVERRIDE",
    "CONTEXT_LOSS": "SOFT_HINT",
    "REASONING_ERROR": "SOFT_HINT",
    "HALLUCINATION": "SOFT_HINT",
}


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

def handle_reflexion_write(arguments: dict) -> dict:
    agent_id = arguments.get("agent") or "unknown"
    failure_class_raw = arguments.get("failure_class")
    if not failure_class_raw:
        return {"ok": False, "error": "failure_class is required"}
    failure_class = failure_class_raw.upper()
    if failure_class not in _REFLEXION_VALID_CLASSES:
        return {"ok": False, "error": f"Invalid failure_class '{failure_class}'. Must be one of: {sorted(_REFLEXION_VALID_CLASSES)}"}

    trigger = arguments.get("trigger")
    lesson = arguments.get("lesson")
    if not trigger:
        return {"ok": False, "error": "trigger is required"}
    if not lesson:
        return {"ok": False, "error": "lesson is required"}

    confidence_raw = arguments.get("confidence")
    confidence = float(confidence_raw) if confidence_raw is not None else _REFLEXION_DEFAULT_CONFIDENCE[failure_class]
    override_level = arguments.get("override_level") or _REFLEXION_DEFAULT_OVERRIDE[failure_class]
    expiration_policy = arguments.get("expiration_policy") or "success_count"

    expiration_n_raw = arguments.get("expiration_n")
    expiration_n = int(expiration_n_raw) if expiration_n_raw is not None else _REFLEXION_DEFAULT_N[failure_class]

    expiration_ttl_raw = arguments.get("expiration_ttl_days")
    expiration_ttl_days = int(expiration_ttl_raw) if expiration_ttl_raw is not None else _REFLEXION_DEFAULT_TTL[failure_class]

    generalizable_to_raw = arguments.get("generalizable_to")
    if generalizable_to_raw:
        generalizable = json.dumps(generalizable_to_raw.split(","))
    else:
        base = _REFLEXION_DEFAULT_GENERALIZABLE[failure_class][:]
        if failure_class in ("REASONING_ERROR", "HALLUCINATION"):
            base = [f"agent:{agent_id}"]
        generalizable = json.dumps(base)

    try:
        db = _db()
        _ensure_agent(db, agent_id)
        cur = db.execute(
            """INSERT INTO reflexion_lessons (
                source_agent_id, source_event_id, source_run_id,
                failure_class, failure_subclass,
                trigger_conditions, lesson_content, generalizable_to,
                confidence, override_level, status,
                expiration_policy, expiration_n, expiration_ttl_days,
                root_cause_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                agent_id,
                arguments.get("source_event") or None,
                arguments.get("source_run") or None,
                failure_class,
                arguments.get("failure_subclass") or None,
                trigger,
                lesson,
                generalizable,
                confidence,
                override_level,
                "active",
                expiration_policy,
                expiration_n,
                expiration_ttl_days,
                arguments.get("root_cause_ref") or None,
            ),
        )
        db.commit()
        lesson_id = cur.lastrowid
        _log_access(db, agent_id, "reflexion_write", "reflexion_lessons", lesson_id)
        db.commit()
        db.close()
        return {
            "ok": True,
            "lesson_id": lesson_id,
            "failure_class": failure_class,
            "confidence": confidence,
            "override_level": override_level,
            "expiration_policy": expiration_policy,
            "generalizable_to": json.loads(generalizable),
        }
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"ok": False, "error": f"reflexion_lessons table not found: {e}"}
        return {"ok": False, "error": str(e)}


def handle_reflexion_list(arguments: dict) -> dict:
    agent_id = arguments.get("agent") or "unknown"
    where_clauses: list[str] = []
    params: list[Any] = []

    failure_class = arguments.get("failure_class")
    if failure_class:
        where_clauses.append("failure_class = ?")
        params.append(failure_class.upper())

    status = arguments.get("status")
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    else:
        where_clauses.append("status = 'active'")

    source_agent = arguments.get("source_agent")
    if source_agent:
        where_clauses.append("source_agent_id = ?")
        params.append(source_agent)

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    limit_raw = arguments.get("limit")
    limit = int(limit_raw) if limit_raw is not None else 50

    try:
        db = _db()
        rows = db.execute(
            f"SELECT * FROM reflexion_lessons {where} ORDER BY confidence DESC, created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        _log_access(db, agent_id, "reflexion_list", "reflexion_lessons", None, None, len(rows))
        db.commit()
        result = _rows_to_list(rows)
        db.close()
        return result
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"ok": False, "error": f"reflexion_lessons table not found: {e}"}
        return {"ok": False, "error": str(e)}


def handle_reflexion_query(arguments: dict) -> dict:
    agent_id = arguments.get("agent") or "unknown"
    task_desc = arguments.get("task_description") or ""

    _raw = _sanitize_fts_query(task_desc)
    sanitized = " OR ".join(_raw.split()) if _raw else ""

    scope_filters: list[str] = []
    scope_params: list[Any] = []
    scope_raw = arguments.get("scope")
    if scope_raw:
        for s in scope_raw.split(","):
            scope_filters.append("generalizable_to LIKE ?")
            scope_params.append(f'%"{s.strip()}"%')
        scope_filters.append("generalizable_to LIKE '%\"scope:global\"%'")
    scope_where = ("AND (" + " OR ".join(scope_filters) + ")") if scope_filters else ""

    top_k_raw = arguments.get("top_k")
    top_k = int(top_k_raw) if top_k_raw is not None else 5

    min_conf_raw = arguments.get("min_confidence")
    min_confidence = float(min_conf_raw) if min_conf_raw is not None else 0.0

    try:
        db = _db()
        if sanitized:
            rows = db.execute(
                f"""SELECT rl.*, reflexion_lessons_fts.rank as fts_rank
                FROM reflexion_lessons_fts
                JOIN reflexion_lessons rl ON rl.id = reflexion_lessons_fts.rowid
                WHERE reflexion_lessons_fts MATCH ?
                  AND rl.status = 'active' AND rl.confidence >= ?
                  {scope_where}
                ORDER BY rl.confidence DESC, fts_rank LIMIT ?""",
                [sanitized, min_confidence] + scope_params + [top_k],
            ).fetchall()
        else:
            rows = db.execute(
                f"""SELECT * FROM reflexion_lessons
                WHERE status = 'active' AND confidence >= ?
                  {scope_where}
                ORDER BY confidence DESC LIMIT ?""",
                [min_confidence] + scope_params + [top_k],
            ).fetchall()

        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            db.execute(
                f"UPDATE reflexion_lessons SET times_retrieved = times_retrieved + 1 WHERE id IN ({placeholders})",
                ids,
            )
            db.commit()

        _log_access(db, agent_id, "reflexion_query", "reflexion_lessons", None, task_desc, len(rows))
        db.commit()
        result = _rows_to_list(rows)
        db.close()
        return result
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"ok": False, "error": f"reflexion_lessons table not found: {e}"}
        return {"ok": False, "error": str(e)}


def handle_reflexion_success(arguments: dict) -> dict:
    agent_id = arguments.get("agent") or "unknown"
    lesson_ids_raw = arguments.get("lesson_ids")
    if not lesson_ids_raw:
        return {"ok": False, "error": "lesson_ids is required"}

    try:
        lesson_ids = [int(x.strip()) for x in str(lesson_ids_raw).split(",")]
    except ValueError as e:
        return {"ok": False, "error": f"Invalid lesson_ids: {e}"}

    now = _now()
    archived: list[int] = []
    updated: list[int] = []

    try:
        db = _db()
        for lid in lesson_ids:
            row = db.execute("SELECT * FROM reflexion_lessons WHERE id = ?", (lid,)).fetchone()
            if not row:
                continue
            new_successes = row["consecutive_successes"] + 1
            new_confidence = min(1.0, row["confidence"] + 0.02)
            exp_n = row["expiration_n"] or 5
            if new_successes >= exp_n and row["expiration_policy"] == "success_count":
                db.execute(
                    """UPDATE reflexion_lessons SET consecutive_successes=?, confidence=?,
                       status='archived', archived_at=?, last_validated_at=?,
                       times_prevented_failure=times_prevented_failure+1 WHERE id=?""",
                    (new_successes, new_confidence, now, now, lid),
                )
                archived.append(lid)
            else:
                db.execute(
                    """UPDATE reflexion_lessons SET consecutive_successes=?, confidence=?,
                       last_validated_at=?, times_prevented_failure=times_prevented_failure+1 WHERE id=?""",
                    (new_successes, new_confidence, now, lid),
                )
                updated.append(lid)
        db.commit()
        _log_access(db, agent_id, "reflexion_success", "reflexion_lessons")
        db.commit()
        db.close()
        return {"ok": True, "updated": updated, "archived": archived}
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"ok": False, "error": f"reflexion_lessons table not found: {e}"}
        return {"ok": False, "error": str(e)}


def handle_reflexion_failure_recurrence(arguments: dict) -> dict:
    agent_id = arguments.get("agent") or "unknown"
    lid_raw = arguments.get("lesson_id")
    if lid_raw is None:
        return {"ok": False, "error": "lesson_id is required"}
    try:
        lid = int(lid_raw)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"Invalid lesson_id: {e}"}

    try:
        db = _db()
        row = db.execute("SELECT * FROM reflexion_lessons WHERE id = ?", (lid,)).fetchone()
        if not row:
            db.close()
            return {"ok": False, "error": f"lesson {lid} not found"}

        new_confidence = max(0.0, row["confidence"] - 0.15)
        db.execute(
            """UPDATE reflexion_lessons SET confidence=?, consecutive_successes=0,
               times_failed_to_prevent=times_failed_to_prevent+1 WHERE id=?""",
            (new_confidence, lid),
        )
        db.commit()

        note = arguments.get("note")
        if note:
            try:
                _ensure_agent(db, agent_id)
                db.execute(
                    "INSERT INTO events (agent_id, event_type, summary, tags) VALUES (?,?,?,?)",
                    (
                        agent_id,
                        "warning",
                        f"Reflexion lesson {lid} failed to prevent recurrence: {note}",
                        json.dumps(["reflexion", "failure_recurrence", f"lesson:{lid}"]),
                    ),
                )
                db.commit()
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                pass  # events table may not exist in test schemas

        _log_access(db, agent_id, "reflexion_failure_recurrence", "reflexion_lessons", lid)
        db.commit()
        db.close()
        return {"ok": True, "lesson_id": lid, "new_confidence": new_confidence}
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"ok": False, "error": f"reflexion_lessons table not found: {e}"}
        return {"ok": False, "error": str(e)}


def handle_reflexion_retire(arguments: dict) -> dict:
    agent_id = arguments.get("agent") or "unknown"
    lid_raw = arguments.get("lesson_id")
    if lid_raw is None:
        return {"ok": False, "error": "lesson_id is required"}
    try:
        lid = int(lid_raw)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"Invalid lesson_id: {e}"}

    try:
        db = _db()
        row = db.execute("SELECT * FROM reflexion_lessons WHERE id = ?", (lid,)).fetchone()
        if not row:
            db.close()
            return {"ok": False, "error": f"lesson {lid} not found"}

        now = _now()
        reason = arguments.get("reason") or "manual retirement"
        db.execute(
            "UPDATE reflexion_lessons SET status='retired', retired_at=?, retirement_reason=? WHERE id=?",
            (now, reason, lid),
        )
        db.commit()
        _log_access(db, agent_id, "reflexion_retire", "reflexion_lessons", lid)
        db.commit()
        db.close()
        return {"ok": True, "lesson_id": lid, "retired_at": now, "reason": reason}
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"ok": False, "error": f"reflexion_lessons table not found: {e}"}
        return {"ok": False, "error": str(e)}


def handle_outcome_annotate(arguments: dict) -> dict:
    try:
        import sys
        sys.path.insert(0, str(Path.home() / "bin" / "lib"))
        from outcome_eval import annotate_task_retrieval
    except ImportError:
        return {"ok": False, "error": "outcome_eval module not available"}

    task_id = arguments.get("task_id")
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    outcome = arguments.get("outcome")
    if not outcome:
        return {"ok": False, "error": "outcome is required"}
    agent_id = arguments.get("agent_id") or os.environ.get("AGENT_ID", "unknown")

    try:
        n = annotate_task_retrieval(task_id, agent_id, outcome)
        return {"ok": True, "task_id": task_id, "outcome": outcome, "agent_id": agent_id, "rows_annotated": n}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def handle_outcome_report(arguments: dict) -> dict:
    try:
        import sys
        sys.path.insert(0, str(Path.home() / "bin" / "lib"))
        from outcome_eval import compute_memory_lift, compute_brier_score, compute_precision_at_k, run_calibration_pass
    except ImportError:
        return {"ok": False, "error": "outcome_eval module not available"}

    agent_id = arguments.get("agent_id") or os.environ.get("AGENT_ID", "unknown")
    period_raw = arguments.get("period")
    period = int(period_raw) if period_raw is not None else 30
    save = bool(arguments.get("save", False))

    try:
        if save:
            result = run_calibration_pass(agent_id=agent_id, period_days=period)
        else:
            lift = compute_memory_lift(period_days=period)
            brier = compute_brier_score(agent_id=agent_id, period_days=period)
            p5 = compute_precision_at_k(agent_id=agent_id, k=5, period_days=period)
            result = {
                "agent_id": agent_id,
                "period_days": period,
                "success_with_memory": lift["with_memory_success_rate"],
                "success_without_memory": lift["without_memory_success_rate"],
                "lift_pp": lift["lift_pp"],
                "brier_score": brier,
                "p_at_5": p5,
                "tasks_with_memory": lift["tasks_with_memory"],
                "tasks_without_memory": lift["tasks_without_memory"],
            }
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="reflexion_write",
        description=(
            "Record a reflexion lesson — a structured lesson learned from a failure. "
            "Captures failure class, trigger conditions, the lesson itself, "
            "confidence, and expiration policy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID recording the lesson"},
                "failure_class": {
                    "type": "string",
                    "description": "Failure category",
                    "enum": sorted(_REFLEXION_VALID_CLASSES),
                },
                "trigger": {"type": "string", "description": "Conditions that trigger this lesson"},
                "lesson": {"type": "string", "description": "The lesson content / corrective action"},
                "failure_subclass": {"type": "string", "description": "Optional sub-category"},
                "confidence": {"type": "number", "description": "Confidence score (0-1). Defaults per failure_class."},
                "override_level": {
                    "type": "string",
                    "description": "Override strength",
                    "enum": ["HARD_OVERRIDE", "SOFT_HINT", "SILENT_LOG"],
                },
                "expiration_policy": {
                    "type": "string",
                    "description": "When the lesson expires",
                    "enum": ["success_count", "code_fix", "ttl", "manual"],
                },
                "expiration_n": {"type": "integer", "description": "Number of successes before expiry"},
                "expiration_ttl_days": {"type": "integer", "description": "TTL in days"},
                "generalizable_to": {"type": "string", "description": "Comma-separated scopes"},
                "source_event": {"type": "integer", "description": "Source event ID"},
                "source_run": {"type": "string", "description": "Source run ID"},
                "root_cause_ref": {"type": "string", "description": "Reference to root cause"},
            },
            "required": ["failure_class", "trigger", "lesson"],
        },
    ),
    Tool(
        name="reflexion_list",
        description="List reflexion lessons, optionally filtered by failure class, status, or source agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID for access logging"},
                "failure_class": {"type": "string", "description": "Filter by failure class"},
                "status": {
                    "type": "string",
                    "description": "Filter by status (default: active)",
                    "enum": ["active", "archived", "retired"],
                },
                "source_agent": {"type": "string", "description": "Filter by source agent ID"},
                "limit": {"type": "integer", "description": "Max results (default: 50)"},
            },
        },
    ),
    Tool(
        name="reflexion_query",
        description=(
            "Query active reflexion lessons relevant to a task description using FTS5 search. "
            "Returns lessons sorted by confidence, with retrieval counters incremented."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID for access logging"},
                "task_description": {"type": "string", "description": "Task description to match lessons against"},
                "scope": {"type": "string", "description": "Comma-separated scope filters"},
                "top_k": {"type": "integer", "description": "Max results (default: 5)"},
                "min_confidence": {"type": "number", "description": "Minimum confidence threshold (default: 0.0)"},
            },
        },
    ),
    Tool(
        name="reflexion_success",
        description=(
            "Signal that one or more lessons successfully prevented a failure. "
            "Increments consecutive_successes and confidence; archives if expiration threshold is met."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID"},
                "lesson_ids": {
                    "type": "string",
                    "description": "Comma-separated lesson IDs that prevented failures",
                },
            },
            "required": ["lesson_ids"],
        },
    ),
    Tool(
        name="reflexion_failure_recurrence",
        description=(
            "Signal that a lesson failed to prevent recurrence of the same failure. "
            "Decreases confidence by 0.15 and resets consecutive_successes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID"},
                "lesson_id": {"type": "integer", "description": "Lesson ID that failed to prevent recurrence"},
                "note": {"type": "string", "description": "Optional note about the recurrence"},
            },
            "required": ["lesson_id"],
        },
    ),
    Tool(
        name="reflexion_retire",
        description="Manually retire a reflexion lesson, marking it as no longer applicable.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent ID"},
                "lesson_id": {"type": "integer", "description": "Lesson ID to retire"},
                "reason": {"type": "string", "description": "Reason for retirement (default: 'manual retirement')"},
            },
            "required": ["lesson_id"],
        },
    ),
    Tool(
        name="outcome_annotate",
        description=(
            "Annotate memory retrievals for a completed task with a success/failure outcome. "
            "Requires the outcome_eval library."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to annotate"},
                "outcome": {
                    "type": "string",
                    "description": "Outcome label (e.g. 'success', 'failure')",
                },
                "agent_id": {"type": "string", "description": "Agent ID (falls back to AGENT_ID env var)"},
            },
            "required": ["task_id", "outcome"],
        },
    ),
    Tool(
        name="outcome_report",
        description=(
            "Compute memory lift, Brier score, and Precision@5 for outcome-linked memory evaluation. "
            "Requires the outcome_eval library."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID"},
                "period": {"type": "integer", "description": "Evaluation period in days (default: 30)"},
                "save": {
                    "type": "boolean",
                    "description": "If true, run calibration pass and persist results",
                },
            },
        },
    ),
]

DISPATCH: dict = {
    "reflexion_write": handle_reflexion_write,
    "reflexion_list": handle_reflexion_list,
    "reflexion_query": handle_reflexion_query,
    "reflexion_success": handle_reflexion_success,
    "reflexion_failure_recurrence": handle_reflexion_failure_recurrence,
    "reflexion_retire": handle_reflexion_retire,
    "outcome_annotate": handle_outcome_annotate,
    "outcome_report": handle_outcome_report,
}
