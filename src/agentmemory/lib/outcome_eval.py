"""
outcome_eval.py — COS-405: Outcome-Linked Memory Evaluation

Functions for annotating access_log with task outcome signals and computing
Brier score calibration metrics. Used by brainctl and brainctl-mcp.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

VALID_OUTCOMES = {"success", "blocked", "escalated", "cancelled"}


def _get_db(db_path: str = None) -> sqlite3.Connection:
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def annotate_task_retrieval(
    task_id: str,
    agent_id: str,
    outcome: str,
    db_path: str = None,
) -> int:
    """
    Annotate all access_log rows for this agent/task with the task outcome.

    Matches rows where task_id IS NULL and agent_id matches, within a
    reasonable window (last 24h) — since we don't have the task start time,
    we annotate the most recent untagged batch.

    Returns the number of rows annotated.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome '{outcome}'. Must be one of: {sorted(VALID_OUTCOMES)}")

    conn = _get_db(db_path)
    try:
        # Look up the most recent pre-task uncertainty for this agent
        unc_row = conn.execute(
            """
            SELECT free_energy FROM agent_uncertainty_log
            WHERE agent_id = ? AND created_at >= datetime('now', '-24 hours')
            ORDER BY created_at DESC LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
        pre_uncertainty = unc_row["free_energy"] if unc_row else None

        # Annotate rows: only untagged rows for this agent from the last 24h
        cursor = conn.execute(
            """
            UPDATE access_log
            SET task_id = ?,
                task_outcome = ?,
                pre_task_uncertainty = COALESCE(pre_task_uncertainty, ?)
            WHERE agent_id = ?
              AND task_id IS NULL
              AND created_at >= datetime('now', '-24 hours')
            """,
            (task_id, outcome, pre_uncertainty, agent_id),
        )
        annotated = cursor.rowcount
        conn.commit()
        return annotated
    finally:
        conn.close()


def compute_brier_score(
    agent_id: str,
    period_days: int = 30,
    db_path: str = None,
) -> Optional[float]:
    """
    Brier score: sum((predicted_confidence - actual_success)²) / N

    Uses access_log rows with task_outcome set, joined to memories via
    target_id to get the confidence at retrieval time. Returns None when
    there is insufficient data (< 3 annotated retrievals with outcomes).
    """
    conn = _get_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                al.task_outcome,
                COALESCE(m.confidence, 0.5) AS confidence
            FROM access_log al
            LEFT JOIN memories m ON m.id = al.target_id
            WHERE al.agent_id = ?
              AND al.task_outcome IS NOT NULL
              AND al.target_table = 'memories'
              AND al.created_at >= datetime('now', ?)
            """,
            (agent_id, f"-{period_days} days"),
        ).fetchall()

        if len(rows) < 3:
            return None

        total = 0.0
        for r in rows:
            actual = 1.0 if r["task_outcome"] == "success" else 0.0
            diff = r["confidence"] - actual
            total += diff * diff

        return total / len(rows)
    finally:
        conn.close()


def compute_memory_lift(
    period_days: int = 30,
    db_path: str = None,
) -> dict:
    """
    Compare task success rate when memory was retrieved vs. cold start.

    Returns:
      {
        with_memory_success_rate: float | None,
        without_memory_success_rate: float | None,
        lift_pp: float | None,   # percentage points
        tasks_with_memory: int,
        tasks_without_memory: int,
      }
    """
    conn = _get_db(db_path)
    try:
        # Tasks that had at least one memory retrieval
        with_mem = conn.execute(
            """
            SELECT
                task_id,
                MAX(CASE WHEN task_outcome = 'success' THEN 1 ELSE 0 END) AS succeeded
            FROM access_log
            WHERE task_id IS NOT NULL
              AND task_outcome IS NOT NULL
              AND target_table = 'memories'
              AND created_at >= datetime('now', ?)
            GROUP BY task_id
            """,
            (f"-{period_days} days",),
        ).fetchall()

        # Tasks that completed but had no memory retrieval rows
        without_mem = conn.execute(
            """
            SELECT
                task_id,
                MAX(CASE WHEN task_outcome = 'success' THEN 1 ELSE 0 END) AS succeeded
            FROM access_log
            WHERE task_id IS NOT NULL
              AND task_outcome IS NOT NULL
              AND created_at >= datetime('now', ?)
            GROUP BY task_id
            HAVING SUM(CASE WHEN target_table = 'memories' THEN 1 ELSE 0 END) = 0
            """,
            (f"-{period_days} days",),
        ).fetchall()

        def _success_rate(rows):
            if not rows:
                return None
            return sum(r["succeeded"] for r in rows) / len(rows)

        rate_with = _success_rate(with_mem)
        rate_without = _success_rate(without_mem)
        lift = None
        if rate_with is not None and rate_without is not None:
            lift = (rate_with - rate_without) * 100.0

        return {
            "with_memory_success_rate": rate_with,
            "without_memory_success_rate": rate_without,
            "lift_pp": lift,
            "tasks_with_memory": len(with_mem),
            "tasks_without_memory": len(without_mem),
        }
    finally:
        conn.close()


def compute_precision_at_k(
    agent_id: str,
    k: int = 5,
    period_days: int = 30,
    db_path: str = None,
) -> Optional[float]:
    """
    Precision@k: fraction of the top-k retrieved memories (per task) that
    came from tasks that succeeded. Uses retrieval_contributed when set,
    falls back to task_outcome == 'success'.
    """
    conn = _get_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                task_id,
                id,
                task_outcome,
                retrieval_contributed,
                ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id) AS rank
            FROM access_log
            WHERE agent_id = ?
              AND task_id IS NOT NULL
              AND task_outcome IS NOT NULL
              AND target_table = 'memories'
              AND created_at >= datetime('now', ?)
            """,
            (agent_id, f"-{period_days} days"),
        ).fetchall()

        top_k = [r for r in rows if r["rank"] <= k]
        if not top_k:
            return None

        hits = 0
        for r in top_k:
            if r["retrieval_contributed"] is not None:
                hits += r["retrieval_contributed"]
            elif r["task_outcome"] == "success":
                hits += 1

        return hits / len(top_k)
    finally:
        conn.close()


def run_calibration_pass(
    agent_id: str,
    period_days: int = 30,
    db_path: str = None,
) -> dict:
    """
    Compute and persist a calibration snapshot for the given agent/period.

    Writes a row to memory_outcome_calibration and returns the computed dict.
    """
    conn = _get_db(db_path)
    try:
        # Count tasks in period
        task_counts = conn.execute(
            """
            SELECT
                COUNT(DISTINCT task_id) AS total,
                COUNT(DISTINCT CASE WHEN target_table = 'memories' THEN task_id END) AS used_memory
            FROM access_log
            WHERE agent_id = ?
              AND task_id IS NOT NULL
              AND task_outcome IS NOT NULL
              AND created_at >= datetime('now', ?)
            """,
            (agent_id, f"-{period_days} days"),
        ).fetchone()

        total_tasks = task_counts["total"] or 0
        tasks_used_memory = task_counts["used_memory"] or 0

        lift = compute_memory_lift(period_days=period_days, db_path=db_path)
        brier = compute_brier_score(agent_id=agent_id, period_days=period_days, db_path=db_path)
        p5 = compute_precision_at_k(agent_id=agent_id, k=5, period_days=period_days, db_path=db_path)

        now = _now_iso()
        period_start = (
            datetime.now(timezone.utc) - timedelta(days=period_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        conn.execute(
            """
            INSERT INTO memory_outcome_calibration
              (agent_id, period_start, period_end, total_tasks, tasks_used_memory,
               success_with_memory, success_without_memory, brier_score, p_at_5, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                period_start,
                now,
                total_tasks,
                tasks_used_memory,
                lift["with_memory_success_rate"],
                lift["without_memory_success_rate"],
                brier,
                p5,
                now,
            ),
        )
        conn.commit()

        return {
            "agent_id": agent_id,
            "period_days": period_days,
            "period_start": period_start,
            "period_end": now,
            "total_tasks": total_tasks,
            "tasks_used_memory": tasks_used_memory,
            "success_with_memory": lift["with_memory_success_rate"],
            "success_without_memory": lift["without_memory_success_rate"],
            "lift_pp": lift["lift_pp"],
            "brier_score": brier,
            "p_at_5": p5,
        }
    finally:
        conn.close()
