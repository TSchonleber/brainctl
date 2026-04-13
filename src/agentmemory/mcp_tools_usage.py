"""brainctl MCP tools -- LLM usage tracking & per-agent rate limiting."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


# NOTE: local _now_ts uses naive strftime format (no 'Z' suffix); differs
# from agentmemory.lib.mcp_helpers.now_iso. Kept local for schema-stable
# timestamps in usage tracking rows.
def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_usage_log(
    agent_id: str = "mcp-client",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    tool_name: str | None = None,
    project: str | None = None,
    **_kw: Any,
) -> dict:
    """Log a single LLM call for the given agent."""
    if not model:
        return {"ok": False, "error": "model is required"}
    total_tokens = prompt_tokens + completion_tokens
    try:
        conn = _db()
        cur = conn.execute(
            "INSERT INTO llm_usage_log "
            "(agent_id, model, prompt_tokens, completion_tokens, total_tokens, "
            " cost_usd, tool_name, project, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, model, prompt_tokens, completion_tokens, total_tokens,
             cost_usd, tool_name, project, _now_ts()),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return {"ok": True, "id": row_id, "total_tokens": total_tokens, "cost_usd": cost_usd}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_usage_summary(
    agent_id: str = "mcp-client",
    month: str | None = None,
    **_kw: Any,
) -> dict:
    """Return usage totals for an agent in a given calendar month."""
    month = month or _current_month()
    try:
        conn = _db()
        # Totals
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS total_tokens, "
            "       COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd, "
            "       COUNT(*) AS call_count "
            "FROM llm_usage_log "
            "WHERE agent_id = ? AND strftime('%Y-%m', created_at) = ?",
            (agent_id, month),
        ).fetchone()
        total_tokens = row["total_tokens"]
        total_cost_usd = row["total_cost_usd"]
        call_count = row["call_count"]

        # Per-model breakdown
        model_rows = conn.execute(
            "SELECT model, "
            "       SUM(total_tokens) AS tokens, "
            "       SUM(cost_usd) AS cost, "
            "       COUNT(*) AS calls "
            "FROM llm_usage_log "
            "WHERE agent_id = ? AND strftime('%Y-%m', created_at) = ? "
            "GROUP BY model",
            (agent_id, month),
        ).fetchall()
        by_model = {
            r["model"]: {"tokens": r["tokens"], "cost_usd": r["cost"], "calls": r["calls"]}
            for r in model_rows
        }
        conn.close()
        return {
            "ok": True,
            "agent_id": agent_id,
            "month": month,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost_usd,
            "call_count": call_count,
            "by_model": by_model,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_usage_check(
    agent_id: str = "mcp-client",
    **_kw: Any,
) -> dict:
    """Check whether an agent is within its LLM budget for the current month."""
    try:
        conn = _db()
        # Fetch budget (may not exist)
        budget_row = conn.execute(
            "SELECT monthly_limit_usd, alert_threshold, hard_limit "
            "FROM agent_budget WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if budget_row:
            limit_usd = budget_row["monthly_limit_usd"]
            alert_threshold = budget_row["alert_threshold"]
            hard_limit = budget_row["hard_limit"]
        else:
            limit_usd = 10.0
            alert_threshold = 0.8
            hard_limit = 1.0

        # Current month spend
        month = _current_month()
        spend_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS spend "
            "FROM llm_usage_log "
            "WHERE agent_id = ? AND strftime('%Y-%m', created_at) = ?",
            (agent_id, month),
        ).fetchone()
        current_spend = spend_row["spend"]
        conn.close()

        pct_used = current_spend / limit_usd if limit_usd > 0 else 0.0

        if pct_used >= hard_limit:
            status = "blocked"
            allowed = False
        elif pct_used >= alert_threshold:
            status = "warning"
            allowed = True
        else:
            status = "green"
            allowed = True

        return {
            "ok": True,
            "allowed": allowed,
            "current_spend_usd": current_spend,
            "limit_usd": limit_usd,
            "pct_used": round(pct_used, 4),
            "status": status,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_budget_set(
    agent_id: str = "mcp-client",
    monthly_limit_usd: float = 10.0,
    alert_threshold: float = 0.8,
    hard_limit: float = 1.0,
    reset_day: int = 1,
    **_kw: Any,
) -> dict:
    """Set or update the LLM budget for an agent."""
    try:
        conn = _db()
        conn.execute(
            "INSERT OR REPLACE INTO agent_budget "
            "(agent_id, monthly_limit_usd, alert_threshold, hard_limit, reset_day, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, monthly_limit_usd, alert_threshold, hard_limit, reset_day, _now_ts()),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "agent_id": agent_id, "monthly_limit_usd": monthly_limit_usd}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_usage_fleet(
    month: str | None = None,
    **_kw: Any,
) -> dict:
    """Fleet-wide usage summary: top 10 agents by spend for the given month."""
    month = month or _current_month()
    try:
        conn = _db()
        # Fleet totals
        totals_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS fleet_total_usd, "
            "       COALESCE(SUM(total_tokens), 0) AS fleet_total_tokens "
            "FROM llm_usage_log "
            "WHERE strftime('%Y-%m', created_at) = ?",
            (month,),
        ).fetchone()

        # Top 10 agents by spend
        agent_rows = conn.execute(
            "SELECT agent_id, "
            "       SUM(cost_usd) AS total_cost_usd, "
            "       SUM(total_tokens) AS total_tokens, "
            "       COUNT(*) AS call_count "
            "FROM llm_usage_log "
            "WHERE strftime('%Y-%m', created_at) = ? "
            "GROUP BY agent_id "
            "ORDER BY total_cost_usd DESC "
            "LIMIT 10",
            (month,),
        ).fetchall()
        agents = [
            {
                "agent_id": r["agent_id"],
                "total_cost_usd": r["total_cost_usd"],
                "total_tokens": r["total_tokens"],
                "call_count": r["call_count"],
            }
            for r in agent_rows
        ]
        conn.close()
        return {
            "ok": True,
            "month": month,
            "fleet_total_usd": totals_row["fleet_total_usd"],
            "fleet_total_tokens": totals_row["fleet_total_tokens"],
            "agents": agents,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="usage_log",
        description=(
            "Log a single LLM API call with token counts and cost. "
            "Use after every LLM call to track spend per agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Model identifier (e.g. 'claude-sonnet-4-6', 'gpt-4o').",
                },
                "prompt_tokens": {
                    "type": "integer",
                    "description": "Number of input/prompt tokens.",
                },
                "completion_tokens": {
                    "type": "integer",
                    "description": "Number of output/completion tokens.",
                },
                "cost_usd": {
                    "type": "number",
                    "description": "Cost of this call in USD. Defaults to 0.0 if unknown.",
                },
                "tool_name": {
                    "type": "string",
                    "description": "MCP tool that triggered this LLM call (optional).",
                },
                "project": {
                    "type": "string",
                    "description": "Project context for the call (optional).",
                },
            },
            "required": ["model"],
        },
    ),
    Tool(
        name="usage_summary",
        description=(
            "Get LLM usage totals for the current agent in a given calendar month. "
            "Returns token counts, cost, call count, and per-model breakdown."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "Month in YYYY-MM format. Defaults to current month.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="usage_check",
        description=(
            "Check if the current agent is within its LLM budget. "
            "Returns allowed/blocked status, current spend, limit, and percentage used. "
            "Call before making expensive LLM calls to respect rate limits."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="budget_set",
        description=(
            "Set or update the monthly LLM budget for the current agent. "
            "Configures spend limit, alert threshold, and hard-block threshold."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "monthly_limit_usd": {
                    "type": "number",
                    "description": "Monthly budget in USD.",
                },
                "alert_threshold": {
                    "type": "number",
                    "description": "Fraction (0-1) of budget that triggers a warning. Default 0.8.",
                },
                "hard_limit": {
                    "type": "number",
                    "description": "Fraction (0-1) of budget at which calls are blocked. Default 1.0.",
                },
                "reset_day": {
                    "type": "integer",
                    "description": "Day of month when budgets reset. Default 1.",
                },
            },
            "required": ["monthly_limit_usd"],
        },
    ),
    Tool(
        name="usage_fleet",
        description=(
            "Fleet-wide LLM usage summary across all agents. "
            "Returns top 10 agents by spend for the given month plus fleet totals."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "Month in YYYY-MM format. Defaults to current month.",
                },
            },
            "required": [],
        },
    ),
]

DISPATCH: dict = {
    "usage_log": lambda agent_id=None, **kw: tool_usage_log(agent_id=agent_id, **kw),
    "usage_summary": lambda agent_id=None, **kw: tool_usage_summary(agent_id=agent_id, **kw),
    "usage_check": lambda agent_id=None, **kw: tool_usage_check(agent_id=agent_id, **kw),
    "budget_set": lambda agent_id=None, **kw: tool_budget_set(agent_id=agent_id, **kw),
    "usage_fleet": lambda agent_id=None, **kw: tool_usage_fleet(**kw),
}
