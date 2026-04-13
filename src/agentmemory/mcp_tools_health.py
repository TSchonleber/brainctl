"""brainctl MCP tools — health & maintenance."""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
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
# VALIDATE — schema/integrity checks
# ---------------------------------------------------------------------------

def _validate() -> dict:
    """Check that all required tables exist, FTS tables exist, and DB integrity is ok."""
    try:
        db = _db()
        issues = []

        # Check all required tables exist
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {r["name"] for r in tables}
        required = {
            "agents", "memories", "events", "context", "tasks", "decisions",
            "agent_state", "blobs", "access_log", "memory_trust_scores",
        }
        missing = required - table_names
        if missing:
            issues.append(f"Missing tables: {sorted(missing)}")

        # Check FTS tables
        for fts in ["memories_fts", "events_fts", "context_fts", "reflexion_lessons_fts"]:
            if fts not in table_names:
                issues.append(f"Missing FTS table: {fts}")

        # Check reflexion_lessons table exists
        if "reflexion_lessons" not in table_names:
            issues.append("Missing table: reflexion_lessons (migration 008 not applied)")

        # Check for orphaned reflexion lessons (source agent doesn't exist)
        if "reflexion_lessons" in table_names:
            rlex_orphans = db.execute(
                "SELECT count(*) as cnt FROM reflexion_lessons WHERE source_agent_id NOT IN (SELECT id FROM agents)"
            ).fetchone()
            if rlex_orphans["cnt"] > 0:
                issues.append(f"Orphaned reflexion_lessons (no matching agent): {rlex_orphans['cnt']}")

        # Check integrity
        result = db.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            issues.append(f"Integrity check failed: {result[0]}")

        # Check for orphaned memories (agent doesn't exist)
        orphans = db.execute(
            "SELECT count(*) as cnt FROM memories WHERE agent_id NOT IN (SELECT id FROM agents)"
        ).fetchone()
        if orphans["cnt"] > 0:
            issues.append(f"Orphaned memories (no matching agent): {orphans['cnt']}")

        db.close()

        if issues:
            return {"ok": True, "valid": False, "issues": issues}
        else:
            return {"ok": True, "valid": True, "issues": []}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# HEALTH — Memory SLO dashboard
# ---------------------------------------------------------------------------

def _slo_signal(value, green_thresh, yellow_thresh, higher_is_better=True) -> str:
    """Return 'green', 'yellow', or 'red' for a metric value."""
    if higher_is_better:
        if value >= green_thresh:
            return "green"
        elif value >= yellow_thresh:
            return "yellow"
        else:
            return "red"
    else:
        if value <= green_thresh:
            return "green"
        elif value <= yellow_thresh:
            return "yellow"
        else:
            return "red"


def _hhi(values) -> float:
    """Herfindahl-Hirschman Index — 0 = max diversity, 1 = monopoly."""
    if not values:
        return 0.0
    counts = Counter(values)
    total = sum(counts.values())
    return sum((c / total) ** 2 for c in counts.values())


def _gini_list(values) -> float:
    """Gini coefficient over a list of non-negative values (0 = equality, 1 = monopoly)."""
    n = len(values)
    if n == 0:
        return 0.0
    s = sorted(values)
    total = sum(s)
    if total == 0:
        return 0.0
    cumsum = 0
    lorenz = 0
    for v in s:
        cumsum += v
        lorenz += cumsum
    return 1 - 2 * lorenz / (n * total)


def _health(window_days: int = 7) -> dict:
    """Run Memory SLO health check and return structured metrics."""
    try:
        db = _db()

        # ── 1. Coverage (distillation ratio) ────────────────────────────────
        row = db.execute(
            """
            SELECT
              CAST(COUNT(DISTINCT m.id) AS REAL) / NULLIF(COUNT(DISTINCT e.id), 0) AS ratio,
              COUNT(DISTINCT e.id) AS total_events
            FROM events e
              LEFT JOIN memories m
                ON m.source_event_id = e.id AND m.retired_at IS NULL
            WHERE e.created_at >= datetime('now', ?)
            """,
            (f"-{window_days} days",),
        ).fetchone()
        coverage = row["ratio"] if row["ratio"] is not None else 0.0
        total_events = row["total_events"] or 0

        row_hi = db.execute(
            """
            SELECT
              CAST(COUNT(DISTINCT m.id) AS REAL) / NULLIF(COUNT(DISTINCT e.id), 0) AS ratio
            FROM events e
              LEFT JOIN memories m
                ON m.source_event_id = e.id AND m.retired_at IS NULL
            WHERE e.importance >= 0.8
              AND e.created_at >= datetime('now', ?)
            """,
            (f"-{window_days} days",),
        ).fetchone()
        coverage_hi = row_hi["ratio"] if row_hi["ratio"] is not None else 0.0

        # ── 2. Freshness (median event-to-memory lag in minutes) ─────────────
        lag_rows = db.execute(
            """
            SELECT (julianday(m.created_at) - julianday(e.created_at)) * 1440 AS lag_min
            FROM memories m
              JOIN events e ON m.source_event_id = e.id
            WHERE m.created_at >= datetime('now', ?)
              AND m.retired_at IS NULL
            """,
            (f"-{window_days} days",),
        ).fetchall()
        if lag_rows:
            lags = sorted(r["lag_min"] for r in lag_rows if r["lag_min"] is not None)
            freshness_median = lags[len(lags) // 2] if lags else None
        else:
            freshness_median = None

        # ── 3. Precision / Engagement ────────────────────────────────────────
        prec_row = db.execute(
            """
            SELECT
              ROUND(
                CAST(SUM(CASE WHEN last_recalled_at >= datetime('now', '-30 days') THEN 1 ELSE 0 END) AS REAL)
                / NULLIF(COUNT(*), 0), 3
              ) AS engagement_rate,
              ROUND(AVG(confidence), 3) AS avg_confidence,
              COUNT(*) AS active_count,
              SUM(CASE WHEN recalled_count > 0 THEN 1 ELSE 0 END) AS ever_recalled
            FROM memories
            WHERE retired_at IS NULL
            """
        ).fetchone()
        engagement_rate = prec_row["engagement_rate"] or 0.0
        avg_confidence = prec_row["avg_confidence"] or 0.0
        active_count = prec_row["active_count"] or 0
        ever_recalled = prec_row["ever_recalled"] or 0

        # ── 3b. Recall Gini ───────────────────────────────────────────────────
        recall_rows = db.execute(
            "SELECT recalled_count FROM memories WHERE retired_at IS NULL"
        ).fetchall()
        recall_gini = _gini_list([float(r["recalled_count"] or 0) for r in recall_rows])

        # ── 4. Diversity (HHI) ────────────────────────────────────────────────
        mem_rows = db.execute(
            "SELECT category, scope FROM memories WHERE retired_at IS NULL"
        ).fetchall()
        categories = [r["category"] for r in mem_rows]
        scopes = [r["scope"] for r in mem_rows]
        cat_hhi = _hhi(categories)
        scope_hhi = _hhi(scopes)

        # ── 5. Temporal balance ───────────────────────────────────────────────
        temporal_rows = db.execute(
            """
            SELECT temporal_class, COUNT(*) AS cnt
            FROM memories WHERE retired_at IS NULL
            GROUP BY temporal_class
            """
        ).fetchall()
        temporal_total = sum(r["cnt"] for r in temporal_rows)
        temporal_dist = {r["temporal_class"]: r["cnt"] for r in temporal_rows}

        def _tpct(cls):
            return (temporal_dist.get(cls, 0) / temporal_total * 100) if temporal_total else 0.0

        ephemeral_pct = _tpct("ephemeral")
        short_pct = _tpct("short")
        medium_pct = _tpct("medium")
        long_pct = _tpct("long")
        permanent_pct = _tpct("permanent")
        temporal_frozen = (ephemeral_pct + short_pct) < 1.0 and temporal_total > 0

        # ── 6. Vec coverage ───────────────────────────────────────────────────
        try:
            vec_row = db.execute(
                """SELECT COUNT(DISTINCT v.rowid) AS cnt
                   FROM vec_memories_rowids v
                   JOIN memories m ON m.id = v.rowid AND m.retired_at IS NULL"""
            ).fetchone()
            vec_count = vec_row["cnt"] if vec_row else 0
        except Exception:
            vec_count = 0
        vec_coverage = (vec_count / active_count) if active_count else 0.0

        # ── 7. Contradiction count ────────────────────────────────────────────
        contradiction_row = db.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE retracted_at IS NULL AND retired_at IS NULL AND retraction_reason IS NOT NULL"
        ).fetchone()
        contradictions = contradiction_row["cnt"] if contradiction_row else 0

        # ── 8. Bayesian α/β coverage ──────────────────────────────────────────
        try:
            ab_row = db.execute(
                """SELECT
                    SUM(CASE WHEN alpha IS NOT NULL AND beta IS NOT NULL THEN 1 ELSE 0 END) AS ab_populated
                   FROM memories WHERE retired_at IS NULL"""
            ).fetchone()
            ab_count = ab_row["ab_populated"] if ab_row else 0
        except Exception:
            ab_count = 0
        ab_coverage = (ab_count / active_count) if active_count else 0.0

        db.close()

        # ── SLO signals ───────────────────────────────────────────────────────
        sig_coverage = _slo_signal(coverage, 0.10, 0.05)
        sig_coverage_hi = _slo_signal(coverage_hi, 0.50, 0.25)
        sig_freshness = _slo_signal(
            freshness_median if freshness_median is not None else 9999, 60, 240, higher_is_better=False
        )
        sig_engagement = _slo_signal(engagement_rate, 0.30, 0.10)
        sig_confidence = _slo_signal(avg_confidence, 0.80, 0.60)
        sig_recall_gini = _slo_signal(recall_gini, 0.60, 0.80, higher_is_better=False)
        sig_cat_hhi = _slo_signal(cat_hhi, 0.35, 0.55, higher_is_better=False)
        sig_scope_hhi = _slo_signal(scope_hhi, 0.40, 0.60, higher_is_better=False)
        sig_temporal = (
            "red" if temporal_frozen else ("green" if (ephemeral_pct + short_pct) >= 10 else "yellow")
        )
        sig_vec = _slo_signal(vec_coverage, 0.90, 0.50)
        sig_contradictions = "green" if contradictions == 0 else "red"
        sig_ab = _slo_signal(ab_coverage, 1.0, 0.50)

        # ── Composite score ───────────────────────────────────────────────────
        WEIGHTS = {
            "coverage": 0.25,
            "freshness": 0.20,
            "precision": 0.25,
            "diversity": 0.15,
            "temporal": 0.15,
        }
        SCORE_MAP = {"green": 2, "yellow": 1, "red": 0}
        dimension_scores = {
            "coverage": max(SCORE_MAP[sig_coverage], SCORE_MAP[sig_coverage_hi]),
            "freshness": SCORE_MAP[sig_freshness],
            "precision": min(SCORE_MAP[sig_engagement], SCORE_MAP[sig_confidence]),
            "diversity": min(SCORE_MAP[sig_cat_hhi], SCORE_MAP[sig_scope_hhi]),
            "temporal": SCORE_MAP[sig_temporal],
        }
        composite = sum(dimension_scores[d] * WEIGHTS[d] for d in WEIGHTS) / 2.0

        if composite >= 0.7:
            overall_label = "healthy"
        elif composite >= 0.4:
            overall_label = "degraded"
        else:
            overall_label = "critical"

        # ── Alerts ────────────────────────────────────────────────────────────
        alerts = []
        if coverage < 0.05:
            alerts.append("Coverage below 0.05 — distillation pipeline may not be linking source events")
        if coverage_hi < 0.25:
            alerts.append("High-importance event coverage below 0.25")
        if freshness_median is not None and freshness_median > 240:
            alerts.append(f"Median distillation lag {freshness_median:.0f}m exceeds 240m threshold")
        if engagement_rate < 0.10:
            alerts.append(f"30-day recall engagement {engagement_rate:.1%} — most memories never retrieved")
        if recall_gini > 0.80:
            alerts.append(
                f"Recall Gini {recall_gini:.3f} > 0.80 — retrieval monopoly, retrieval-induced forgetting risk"
            )
        elif recall_gini > 0.60:
            alerts.append(
                f"Recall Gini {recall_gini:.3f} > 0.60 — recall inequality elevated, consider MMR/diversity boost"
            )
        if cat_hhi > 0.55:
            alerts.append(f"Category HHI {cat_hhi:.3f} > 0.55 — topic collapse detected")
        if scope_hhi > 0.70:
            alerts.append(f"Scope HHI {scope_hhi:.3f} > 0.70 — pipeline work narrowly concentrated")
        if temporal_frozen:
            alerts.append("Temporal class freeze — ephemeral+short at 0%, classification pipeline may be halted")
        if permanent_pct > 20:
            alerts.append(f"Permanent memory share {permanent_pct:.1f}% > 20% — over-promotion risk")
        if contradictions > 0:
            alerts.append(f"{contradictions} unresolved contradiction(s) flagged")

        return {
            "ok": True,
            "composite_score": round(composite, 3),
            "overall": overall_label,
            "window_days": window_days,
            "metrics": {
                "coverage": {"value": round(coverage, 4), "signal": sig_coverage},
                "coverage_hi": {"value": round(coverage_hi, 4), "signal": sig_coverage_hi},
                "freshness_median_min": {
                    "value": round(freshness_median, 1) if freshness_median is not None else None,
                    "signal": sig_freshness,
                },
                "engagement_rate": {"value": round(engagement_rate, 4), "signal": sig_engagement},
                "avg_confidence": {"value": round(avg_confidence, 4), "signal": sig_confidence},
                "recall_gini": {"value": round(recall_gini, 4), "signal": sig_recall_gini},
                "category_hhi": {"value": round(cat_hhi, 4), "signal": sig_cat_hhi},
                "scope_hhi": {"value": round(scope_hhi, 4), "signal": sig_scope_hhi},
                "temporal_frozen": temporal_frozen,
                "temporal_dist_pct": {
                    "ephemeral": round(ephemeral_pct, 1),
                    "short": round(short_pct, 1),
                    "medium": round(medium_pct, 1),
                    "long": round(long_pct, 1),
                    "permanent": round(permanent_pct, 1),
                },
                "vec_coverage": {"value": round(vec_coverage, 4), "signal": sig_vec},
                "contradictions": {"value": contradictions, "signal": sig_contradictions},
                "bayesian_ab_coverage": {
                    "value": round(ab_coverage, 4),
                    "signal": sig_ab,
                    "populated": ab_count,
                },
            },
            "alerts": alerts,
            "active_memories": active_count,
            "total_events": total_events,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# LINT — integrity / quality checks
# ---------------------------------------------------------------------------

def _lint(fix: bool = False) -> dict:
    """Run health checks on brain.db — find issues, optionally fix some."""
    try:
        db = _db()
        issues = []
        fixed = 0

        # 1. Low-confidence memories
        low_conf = db.execute(
            "SELECT id, content, confidence FROM memories WHERE retired_at IS NULL AND confidence < 0.3"
        ).fetchall()
        if low_conf:
            issues.append({
                "check": "low_confidence",
                "severity": "warning",
                "count": len(low_conf),
                "description": f"{len(low_conf)} memories with confidence < 0.3 — may be unreliable",
                "items": [
                    {"id": r["id"], "confidence": r["confidence"], "preview": r["content"][:100]}
                    for r in low_conf[:5]
                ],
            })

        # 2. Never-recalled memories (potentially useless)
        never_recalled = db.execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL AND recalled_count = 0"
        ).fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
        if never_recalled > 0 and active > 0:
            pct = round(never_recalled / active * 100, 1)
            issues.append({
                "check": "never_recalled",
                "severity": "info" if pct < 50 else "warning",
                "count": never_recalled,
                "description": (
                    f"{never_recalled}/{active} memories ({pct}%) have never been recalled — potential dead weight"
                ),
            })

        # 3. Orphan entities (no edges)
        orphans = db.execute("""
            SELECT e.id, e.name, e.entity_type FROM entities e
            WHERE e.retired_at IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM knowledge_edges ke
                WHERE (ke.source_table='entities' AND ke.source_id=e.id)
                   OR (ke.target_table='entities' AND ke.target_id=e.id)
            )
        """).fetchall()
        if orphans:
            issues.append({
                "check": "orphan_entities",
                "severity": "info",
                "count": len(orphans),
                "description": f"{len(orphans)} entities have no edges — isolated in the knowledge graph",
                "items": [
                    {"id": r["id"], "name": r["name"], "type": r["entity_type"]}
                    for r in orphans[:10]
                ],
            })

        # 4. Knowledge gaps (unresolved)
        try:
            gaps = db.execute(
                "SELECT COUNT(*) FROM knowledge_gaps WHERE resolved_at IS NULL"
            ).fetchone()[0]
            if gaps > 0:
                gap_rows = db.execute(
                    "SELECT domain, gap_description FROM knowledge_gaps WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
                issues.append({
                    "check": "knowledge_gaps",
                    "severity": "warning",
                    "count": gaps,
                    "description": f"{gaps} unresolved knowledge gaps detected",
                    "items": [
                        {"domain": r["domain"], "gap": r["gap_description"][:100]}
                        for r in gap_rows
                    ],
                })
        except Exception:
            pass

        # 5. Duplicate entity names (case-insensitive)
        dupes = db.execute("""
            SELECT LOWER(name) as lname, COUNT(*) as c, GROUP_CONCAT(id) as ids
            FROM entities WHERE retired_at IS NULL
            GROUP BY LOWER(name) HAVING c > 1
        """).fetchall()
        if dupes:
            issues.append({
                "check": "duplicate_entities",
                "severity": "warning",
                "count": len(dupes),
                "description": f"{len(dupes)} entity names appear more than once (case-insensitive)",
                "items": [
                    {"name": r["lname"], "count": r["c"], "ids": r["ids"]}
                    for r in dupes[:5]
                ],
            })
            if fix:
                for d in dupes:
                    ids = [int(x) for x in d["ids"].split(",")]
                    rows = db.execute(
                        f"SELECT id, confidence FROM entities WHERE id IN ({','.join('?' * len(ids))}) ORDER BY confidence DESC",
                        ids,
                    ).fetchall()
                    keep = rows[0]["id"]
                    retire = [r["id"] for r in rows[1:]]
                    for rid in retire:
                        db.execute(
                            "UPDATE entities SET retired_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                            (rid,),
                        )
                        fixed += 1
                db.commit()

        # 6. Stale affect data (agents not reporting)
        try:
            stale_affect = db.execute("""
                SELECT agent_id, MAX(created_at) as last_report
                FROM affect_log
                GROUP BY agent_id
                HAVING last_report < datetime('now', '-7 days')
            """).fetchall()
            if stale_affect:
                issues.append({
                    "check": "stale_affect",
                    "severity": "info",
                    "count": len(stale_affect),
                    "description": f"{len(stale_affect)} agents haven't reported affect state in 7+ days",
                    "items": [
                        {"agent": r["agent_id"], "last_report": r["last_report"]}
                        for r in stale_affect[:5]
                    ],
                })
        except Exception:
            pass

        # 7. Access log bloat
        try:
            log_count = db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
            if log_count > 10000:
                issues.append({
                    "check": "access_log_bloat",
                    "severity": "info",
                    "count": log_count,
                    "description": f"Access log has {log_count:,} entries — consider running brainctl prune-log",
                })
                if fix:
                    from datetime import timedelta
                    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
                    cursor = db.execute("DELETE FROM access_log WHERE created_at < ?", (cutoff,))
                    db.commit()
                    fixed += cursor.rowcount
        except Exception:
            pass

        # 8. DB size check
        try:
            db_size = DB_PATH.stat().st_size / 1048576
            if db_size > 100:
                issues.append({
                    "check": "large_database",
                    "severity": "warning",
                    "count": 1,
                    "description": (
                        f"Database is {db_size:.1f} MB — consider running consolidation (brainctl-consolidate sweep)"
                    ),
                })
        except Exception:
            pass

        db.close()

        critical = sum(1 for i in issues if i["severity"] == "critical")
        warnings = sum(1 for i in issues if i["severity"] == "warning")
        infos = sum(1 for i in issues if i["severity"] == "info")

        return {
            "ok": True,
            "health": "critical" if critical else "warning" if warnings else "healthy",
            "issues": len(issues),
            "critical": critical,
            "warnings": warnings,
            "info": infos,
            "fixed": fixed,
            "checks": issues,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# BACKUP — copy DB file to a timestamped backup
# ---------------------------------------------------------------------------

def _backup(dest_path: str | None = None) -> dict:
    """Copy the brain.db to a timestamped backup (and optionally a SQL dump)."""
    try:
        # Close any open connection first to flush WAL
        conn = _db()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        if dest_path:
            backup_path = Path(dest_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Default: timestamped backup next to the DB
            backups_dir = Path(os.environ.get(
                "BRAINCTL_BACKUPS_DIR",
                str(Path.home() / "agentmemory" / "backups"),
            ))
            backups_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backups_dir / f"brain_{ts}.db"

        shutil.copy2(str(DB_PATH), str(backup_path))

        # Also export to SQL for safer backup
        sql_path = backup_path.with_suffix(".sql")
        try:
            subprocess.run(
                ["sqlite3", str(DB_PATH), ".dump"],
                stdout=open(str(sql_path), "w"),
                check=True,
                timeout=60,
            )
            sql_str = str(sql_path)
        except Exception:
            sql_str = None

        # Prune old backups (keep last 30) — only if using the default backups dir
        if not dest_path:
            all_backups = sorted(backup_path.parent.glob("brain_*.db"), reverse=True)
            for old in all_backups[30:]:
                try:
                    old.unlink()
                    sql_sibling = old.with_suffix(".sql")
                    if sql_sibling.exists():
                        sql_sibling.unlink()
                except Exception:
                    pass

        size = backup_path.stat().st_size
        return {
            "ok": True,
            "backup": str(backup_path),
            "sql": sql_str,
            "size_bytes": size,
            "timestamp": ts,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# BUDGET STATUS — per-agent token consumption dashboard
# ---------------------------------------------------------------------------

_BUDGET_TIER_CEILINGS = {
    0: None,   # Tier 0 — exec/CEO: unlimited
    1: 5000,   # Tier 1 — senior IC
    2: 2000,   # Tier 2 — specialist
    3: 500,    # Tier 3 — worker
}
_BUDGET_TIER_LABELS = {
    0: "exec (unlimited)",
    1: "senior-ic (5K)",
    2: "specialist (2K)",
    3: "worker (500)",
}


def _budget_status() -> dict:
    """Show per-agent and fleet-wide token consumption for the current day."""
    try:
        db = _db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = db.execute(
            """
            SELECT
                al.agent_id,
                a.display_name,
                COALESCE(a.attention_budget_tier, 1) AS tier,
                COUNT(*) AS query_count,
                COALESCE(SUM(al.tokens_consumed), 0) AS tokens_today
            FROM access_log al
            LEFT JOIN agents a ON al.agent_id = a.id
            WHERE al.created_at >= ? AND al.tokens_consumed IS NOT NULL
            GROUP BY al.agent_id
            ORDER BY tokens_today DESC
            """,
            (today + " 00:00:00",),
        ).fetchall()

        db.close()

        fleet_total = sum(r["tokens_today"] for r in rows)
        at_cap = []
        agents_out = []

        for r in rows:
            tier = r["tier"] if r["tier"] is not None else 1
            ceiling = _BUDGET_TIER_CEILINGS.get(tier)
            tier_label = _BUDGET_TIER_LABELS.get(tier, f"tier-{tier}")
            pct = None
            flagged = False
            if ceiling:
                avg_per_query = r["tokens_today"] / max(r["query_count"], 1)
                flagged = avg_per_query >= ceiling * 0.8
                if flagged:
                    at_cap.append(r["agent_id"])
                pct = round(r["tokens_today"] / ceiling * 100, 1)
            entry = {
                "agent_id": r["agent_id"],
                "display_name": r["display_name"] or r["agent_id"],
                "tier": tier,
                "tier_label": tier_label,
                "queries_today": r["query_count"],
                "tokens_today": r["tokens_today"],
                "ceiling": ceiling,
                "ceiling_pct": pct,
                "flagged": flagged,
            }
            agents_out.append(entry)

        return {
            "ok": True,
            "date": today,
            "fleet_total": fleet_total,
            "agents": agents_out,
            "at_cap": at_cap,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP dispatch helpers
# ---------------------------------------------------------------------------

def _call_validate(args: dict) -> dict:
    return _validate()


def _call_health(args: dict) -> dict:
    window_days = int(args.get("window_days", 7))
    return _health(window_days=window_days)


def _call_lint(args: dict) -> dict:
    fix = bool(args.get("fix", False))
    return _lint(fix=fix)


def _call_backup(args: dict) -> dict:
    dest_path = args.get("dest_path") or None
    return _backup(dest_path=dest_path)


def _call_budget_status(args: dict) -> dict:
    return _budget_status()


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="validate",
        description=(
            "Validate brain.db schema integrity: checks that all required tables and FTS tables exist, "
            "runs SQLite integrity_check, and detects orphaned records."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="health",
        description=(
            "Run the Memory SLO health dashboard. Returns composite score, per-dimension metrics "
            "(coverage, freshness, precision, diversity, temporal balance, vec coverage), "
            "SLO signals (green/yellow/red), and actionable alerts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "window_days": {
                    "type": "integer",
                    "description": "Lookback window in days for event/memory metrics (default: 7).",
                    "default": 7,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="lint",
        description=(
            "Run quality lint checks on brain.db: low-confidence memories, never-recalled memories, "
            "orphan entities, knowledge gaps, duplicate entity names, stale affect data, access log bloat, "
            "and DB size. Optionally auto-fix safe issues."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "fix": {
                    "type": "boolean",
                    "description": "If true, auto-fix safe issues (e.g. retire duplicate entities, prune old access log).",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="backup",
        description=(
            "Back up brain.db to a timestamped file. By default, creates a .db and .sql dump in "
            "~/agentmemory/backups/ and keeps the last 30 backups. Provide dest_path to override."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dest_path": {
                    "type": "string",
                    "description": "Optional explicit path for the backup file (e.g. /tmp/brain_manual.db). "
                                   "If omitted, a timestamped file is created in the default backups directory.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="budget_status",
        description=(
            "Show per-agent and fleet-wide token consumption for the current UTC day. "
            "Flags agents whose average per-query consumption is within 80% of their tier ceiling."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
]

DISPATCH: dict = {
    "validate": _call_validate,
    "health": _call_health,
    "lint": _call_lint,
    "backup": _call_backup,
    "budget_status": _call_budget_status,
}
