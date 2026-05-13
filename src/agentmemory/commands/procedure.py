"""CLI procedure commands."""

from __future__ import annotations

import sqlite3
from typing import Any

from agentmemory import procedural


def _impl():
    from agentmemory import _impl

    return _impl


def _open_db() -> sqlite3.Connection:
    return _impl().get_db()


def _payload_from_args(args) -> dict[str, Any]:
    steps = [{"action": step} for step in (getattr(args, "step", None) or [])]
    return {
        "title": getattr(args, "title", None),
        "goal": getattr(args, "goal", None),
        "description": getattr(args, "description", None),
        "task_family": getattr(args, "task_family", None),
        "procedure_kind": getattr(args, "kind", None),
        "trigger_conditions": getattr(args, "trigger", None) or [],
        "preconditions": getattr(args, "precondition", None) or [],
        "steps_json": steps,
        "tools_json": getattr(args, "tool", None) or [],
        "failure_modes_json": getattr(args, "failure", None) or [],
        "rollback_steps_json": getattr(args, "rollback", None) or [],
        "success_criteria_json": getattr(args, "success_criterion", None) or [],
        "expected_outcomes": getattr(args, "expected_outcome", None) or [],
        "applicability_scope": getattr(args, "scope", None) or "global",
        "status": getattr(args, "status", None) or "active",
    }


def cmd_procedure_add(args) -> None:
    db = _open_db()
    try:
        payload = _payload_from_args(args)
        result = procedural.create_procedure(
            db,
            agent_id=args.agent,
            payload=payload,
            category=args.category,
            scope=args.scope,
            confidence=args.confidence,
        )
        db.commit()
        _impl().json_out({"ok": True, **result})
    finally:
        db.close()


def cmd_procedure_get(args) -> None:
    db = _open_db()
    try:
        result = procedural.get_procedure(db, args.id, include_sources=True)
        _impl().json_out({"ok": True, **result})
    finally:
        db.close()


def cmd_procedure_list(args) -> None:
    db = _open_db()
    try:
        result = procedural.list_procedures(
            db,
            status=args.status,
            scope=args.scope,
            limit=args.limit,
        )
        _impl().json_out({"ok": True, "count": len(result), "procedures": result})
    finally:
        db.close()


def cmd_procedure_search(args) -> None:
    db = _open_db()
    try:
        result = procedural.search_procedures(
            db,
            args.query,
            limit=args.limit,
            scope=args.scope,
            status=args.status,
            debug=getattr(args, "debug", False),
        )
        _impl().json_out(result)
    finally:
        db.close()


def cmd_procedure_update(args) -> None:
    db = _open_db()
    try:
        changes = {k: v for k, v in _payload_from_args(args).items() if v not in (None, [], "")}
        result = procedural.update_procedure(db, args.id, changes)
        db.commit()
        _impl().json_out({"ok": True, **result})
    finally:
        db.close()


def cmd_procedure_feedback(args) -> None:
    db = _open_db()
    try:
        result = procedural.record_feedback(
            db,
            procedure_id=args.id,
            agent_id=args.agent,
            success=bool(args.success),
            usefulness_score=args.usefulness,
            outcome_summary=args.outcome,
            errors_seen=args.errors,
            validated=args.validated,
            task_signature=args.task_signature,
            input_summary=args.input_summary,
        )
        db.commit()
        _impl().json_out({"ok": True, **result})
    finally:
        db.close()


def cmd_procedure_backfill(args) -> None:
    db = _open_db()
    try:
        result = procedural.backfill_procedures(
            db,
            agent_id=args.agent,
            scope=args.scope,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            db.commit()
        _impl().json_out(result)
    finally:
        db.close()


def cmd_procedure_stats(args) -> None:
    db = _open_db()
    try:
        result = procedural.procedure_stats(db)
        _impl().json_out(result)
    finally:
        db.close()


def register_parser(sub) -> None:
    proc = sub.add_parser("procedure", help="Manage canonical procedural memories")
    proc_sub = proc.add_subparsers(dest="procedure_cmd")

    add = proc_sub.add_parser("add", help="Create a structured procedure")
    add.add_argument("--title")
    add.add_argument("--goal", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--kind", default="workflow")
    add.add_argument("--task-family", dest="task_family")
    add.add_argument("--category", default="convention")
    add.add_argument("--scope", default="global")
    add.add_argument("--confidence", type=float, default=0.9)
    add.add_argument("--status", default="active")
    add.add_argument("--step", action="append", default=[], help="Repeatable ordered step")
    add.add_argument("--trigger", action="append", default=[])
    add.add_argument("--precondition", action="append", default=[])
    add.add_argument("--tool", action="append", default=[])
    add.add_argument("--failure", action="append", default=[])
    add.add_argument("--rollback", action="append", default=[])
    add.add_argument("--success-criterion", dest="success_criterion", action="append", default=[])
    add.add_argument("--expected-outcome", dest="expected_outcome", action="append", default=[])

    get = proc_sub.add_parser("get", help="Fetch a procedure by id")
    get.add_argument("id", type=int)

    lst = proc_sub.add_parser("list", help="List procedures")
    lst.add_argument("--status", default="all")
    lst.add_argument("--scope")
    lst.add_argument("--limit", type=int, default=50)

    search = proc_sub.add_parser("search", help="Search procedures")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--scope")
    search.add_argument("--status", default="all")
    search.add_argument("--debug", action="store_true")

    update = proc_sub.add_parser("update", help="Update a procedure")
    update.add_argument("id", type=int)
    update.add_argument("--title")
    update.add_argument("--goal")
    update.add_argument("--description")
    update.add_argument("--kind")
    update.add_argument("--task-family", dest="task_family")
    update.add_argument("--scope")
    update.add_argument("--status")
    update.add_argument("--step", action="append", default=None)
    update.add_argument("--trigger", action="append", default=None)
    update.add_argument("--precondition", action="append", default=None)
    update.add_argument("--tool", action="append", default=None)
    update.add_argument("--failure", action="append", default=None)
    update.add_argument("--rollback", action="append", default=None)
    update.add_argument("--success-criterion", dest="success_criterion", action="append", default=None)
    update.add_argument("--expected-outcome", dest="expected_outcome", action="append", default=None)

    feedback = proc_sub.add_parser("feedback", help="Record procedural execution feedback")
    feedback.add_argument("id", type=int)
    feedback.add_argument("--success", action="store_true", default=False)
    feedback.add_argument("--failure", dest="success", action="store_false")
    feedback.add_argument("--validated", action="store_true")
    feedback.add_argument("--usefulness", type=float, default=None)
    feedback.add_argument("--outcome", default=None)
    feedback.add_argument("--errors", default=None)
    feedback.add_argument("--task-signature", dest="task_signature", default=None)
    feedback.add_argument("--input-summary", dest="input_summary", default=None)

    backfill = proc_sub.add_parser("backfill", help="Backfill procedures from existing evidence")
    backfill.add_argument("--scope")
    backfill.add_argument("--limit", type=int, default=100)
    backfill.add_argument("--dry-run", action="store_true")

    proc_sub.add_parser("stats", help="Show procedure stats")


def dispatch(args) -> bool:
    fn = {
        "add": cmd_procedure_add,
        "get": cmd_procedure_get,
        "list": cmd_procedure_list,
        "search": cmd_procedure_search,
        "update": cmd_procedure_update,
        "feedback": cmd_procedure_feedback,
        "backfill": cmd_procedure_backfill,
        "stats": cmd_procedure_stats,
    }.get(getattr(args, "procedure_cmd", None))
    if not fn:
        return False
    fn(args)
    return True


__all__ = [
    "cmd_procedure_add",
    "cmd_procedure_backfill",
    "cmd_procedure_feedback",
    "cmd_procedure_get",
    "cmd_procedure_list",
    "cmd_procedure_search",
    "cmd_procedure_stats",
    "cmd_procedure_update",
    "dispatch",
    "register_parser",
]
