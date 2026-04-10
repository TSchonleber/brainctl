"""brainctl MCP tools — memory operations & trust."""
from __future__ import annotations
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Helpers inlined from _impl.py (no cross-module import to keep module clean)
# ---------------------------------------------------------------------------

def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _days_since(created_at_str: str) -> float:
    """Return float days elapsed since the given SQLite/ISO timestamp."""
    if not created_at_str:
        return 0.0
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
                    break
                except ValueError:
                    continue
            else:
                return 0.0
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# PII (Proactive Interference Index) constants and helpers
# ---------------------------------------------------------------------------

_PII_TEMPORAL_WEIGHTS: dict[str, float] = {
    "permanent": 1.00,
    "long":      0.80,
    "medium":    0.50,
    "short":     0.30,
    "ephemeral": 0.15,
}

_PII_TIERS: list[tuple[float, str]] = [
    (0.70, "CRYSTALLIZED"),
    (0.40, "ENTRENCHED"),
    (0.20, "ESTABLISHED"),
    (0.00, "OPEN"),
]


def _compute_pii(db: sqlite3.Connection, memory_id: int) -> float:
    """Compute Proactive Interference Index for a memory. Returns float in [0.0, 1.0]."""
    row = db.execute(
        "SELECT alpha, beta, recalled_count, temporal_class FROM memories "
        "WHERE id = ? AND retired_at IS NULL", (memory_id,)
    ).fetchone()
    if not row:
        return 0.0
    alpha = float(row["alpha"] or 1.0)
    beta  = float(row["beta"]  or 1.0)
    recalled = int(row["recalled_count"] or 0)
    temporal_class = row["temporal_class"] or "medium"
    max_row = db.execute(
        "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
    ).fetchone()
    max_recalled = int(max_row[0] or 1)
    if max_recalled < 1:
        max_recalled = 1
    bayesian_strength = alpha / (alpha + beta)
    recall_weight = math.log(1 + recalled) / math.log(1 + max_recalled) if max_recalled > 0 else 0.0
    temporal_weight = _PII_TEMPORAL_WEIGHTS.get(temporal_class, 0.50)
    return min(1.0, max(0.0, bayesian_strength * recall_weight * temporal_weight))


def _pii_tier(score: float) -> str:
    for threshold, label in _PII_TIERS:
        if score >= threshold:
            return label
    return "OPEN"


# ---------------------------------------------------------------------------
# Trust engine constants and helpers
# ---------------------------------------------------------------------------

_TRUST_CATEGORY_PRIORS: dict[str, float] = {
    "identity":    0.85,
    "decision":    0.80,
    "environment": 0.72,
    "project":     0.70,
    "lesson":      0.68,
    "preference":  0.65,
    "convention":  0.65,
    "user":        0.62,
    "integration": 0.70,
}

_TRUST_AGENT_MULTIPLIERS: list[tuple[str, float]] = [
    ("supervisor",  1.15),
    ("hippocampus", 0.90),
    ("sentinel",    1.10),
    ("prune",       1.10),
]

_TRUST_DECAY_RATES: dict[str, float] = {
    "ephemeral": 0.03,
    "short":     0.02,
    "medium":    0.01,
    "long":      0.005,
    "permanent": 0.0,
}


def _trust_agent_multiplier(agent_id: str) -> float:
    if not agent_id:
        return 1.0
    aid = agent_id.lower()
    for keyword, mult in _TRUST_AGENT_MULTIPLIERS:
        if keyword in aid:
            return mult
    return 1.0


def _walk_trust_chain(db: sqlite3.Connection, memory_id: int, max_depth: int, visited: set) -> float:
    """Walk derived_from chain returning minimum trust. Max 10 hops, cycle-safe."""
    if max_depth <= 0 or memory_id in visited:
        return 1.0
    visited.add(memory_id)
    row = db.execute(
        "SELECT trust_score, derived_from_ids, retracted_at FROM memories WHERE id = ?",
        (memory_id,)
    ).fetchone()
    if not row:
        return 1.0
    if row["retracted_at"]:
        return 0.0
    current_trust = row["trust_score"] or 1.0
    if row["derived_from_ids"]:
        try:
            for pid in json.loads(row["derived_from_ids"]):
                current_trust = min(current_trust,
                                    _walk_trust_chain(db, pid, max_depth - 1, visited))
        except (json.JSONDecodeError, TypeError):
            pass
    return current_trust


def _compute_trust_breakdown(db: sqlite3.Connection, mem) -> dict:
    mem = dict(mem)
    mem_id = mem["id"]
    if mem.get("retracted_at"):
        return {"memory_id": mem_id, "retracted": True, "trust_score": 0.05,
                "components": {"retracted": True}}

    base_trust = _TRUST_CATEGORY_PRIORS.get(mem.get("category", ""), 0.70)
    agent_id = mem.get("agent_id", "")
    category = mem.get("category", "")
    sr_row = db.execute(
        "SELECT trust_score FROM memory_trust_scores WHERE agent_id = ? AND category = ?",
        (agent_id, category)
    ).fetchone()
    source_reliability = sr_row["trust_score"] if sr_row else 0.75
    source_reliability = min(1.0, source_reliability * _trust_agent_multiplier(agent_id))

    validation_count = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'memory_validated' "
        "AND JSON_EXTRACT(metadata, '$.memory_id') = ?", (mem_id,)
    ).fetchone()[0]
    if mem.get("validated_at") or validation_count > 0:
        validation_bonus = min(1.50, 1.0 + validation_count * 0.35)
    else:
        validation_bonus = 1.0

    if mem.get("validated_at"):
        age_penalty = 1.0
        days_unvalidated = 0.0
    else:
        days_unvalidated = _days_since(mem.get("created_at", ""))
        age_penalty = max(0.50, 1.0 - 0.01 * days_unvalidated)

    n_unresolved = db.execute(
        "SELECT COUNT(*) FROM knowledge_edges "
        "WHERE relation_type = 'contradicts' AND (source_id = ? OR target_id = ?)",
        (mem_id, mem_id)
    ).fetchone()[0]
    n_resolved = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'contradiction_resolved' "
        "AND (JSON_EXTRACT(metadata, '$.kept') = ? OR JSON_EXTRACT(metadata, '$.retired') = ?)",
        (str(mem_id), str(mem_id))
    ).fetchone()[0]
    contradiction_penalty = max(0.30, 1.0 - (n_unresolved * 0.20) - (n_resolved * 0.05))

    score = round(min(1.0, max(0.05,
        base_trust * source_reliability * validation_bonus * age_penalty * contradiction_penalty
    )), 4)

    return {
        "memory_id": mem_id,
        "retracted": False,
        "trust_score": score,
        "components": {
            "base_trust": round(base_trust, 4),
            "source_reliability": round(source_reliability, 4),
            "validation_bonus": round(validation_bonus, 4),
            "age_penalty": round(age_penalty, 4),
            "days_unvalidated": round(days_unvalidated, 1),
            "contradiction_penalty": round(contradiction_penalty, 4),
            "n_unresolved_contradictions": n_unresolved,
            "n_resolved_contradictions": n_resolved,
            "validation_count": validation_count,
            "validated": mem.get("validated_at") is not None,
        },
    }


# ---------------------------------------------------------------------------
# Category inference (inlined from _impl.py)
# ---------------------------------------------------------------------------

VALID_MEMORY_CATEGORIES: set[str] = {
    "identity", "user", "environment", "convention",
    "project", "decision", "lesson", "preference", "integration",
}

_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("decision",    ["decided", "chose", "option", "tradeoff", "approved", "rejected",
                     "selected", "architecture", "design choice", "will use", "going with"]),
    ("lesson",      ["lesson:", "lesson —", "learned:", "never run", "always ", "mistake",
                     "bug:", "failure:", "incident:", "root cause", "postmortem",
                     "regression", "gotcha", "footgun", "caution:"]),
    ("identity",    ["i am ", "my role", "my name", "agent id", "i report to",
                     "my capabilities", "identity:", "persona:", "i own "]),
    ("environment", ["schema", "database", "db path", "cron", "infrastructure",
                     "endpoint", "api key", "config", "env var", "port ", "url:",
                     "installed", "deployed", "server", "tooling", "pipeline"]),
    ("project",     ["milestone", "shipped", "released", "completed", "done:",
                     "sprint", "wave ", "cos-", "issue", "heartbeat", "task",
                     "implemented", "delivered", "closed", "fixed"]),
]


def _infer_category_from_content(content: str) -> str:
    """Infer the most appropriate memory category from content text."""
    if not content:
        return "project"
    lower = content.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category
    return "project"


# ---------------------------------------------------------------------------
# Tool handler functions
# ---------------------------------------------------------------------------

def tool_memory_pii(memory_id: int) -> dict:
    """Compute Proactive Interference Index for a single memory."""
    db = _db()
    try:
        row = db.execute(
            "SELECT id, content, alpha, beta, recalled_count, temporal_class FROM memories "
            "WHERE id = ? AND retired_at IS NULL", (memory_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found or retired"}
        score = _compute_pii(db, memory_id)
        tier = _pii_tier(score)
        return {
            "ok": True,
            "memory_id": memory_id,
            "pii": round(score, 4),
            "tier": tier,
            "alpha": float(row["alpha"] or 1.0),
            "beta": float(row["beta"] or 1.0),
            "recalled_count": int(row["recalled_count"] or 0),
            "temporal_class": row["temporal_class"] or "medium",
            "content_snippet": (row["content"] or "")[:120],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_memory_pii_scan(top: int = 20) -> dict:
    """Scan all active memories sorted by PII score descending."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT id, content, alpha, beta, recalled_count, temporal_class "
            "FROM memories WHERE retired_at IS NULL"
        ).fetchall()
        max_row = db.execute(
            "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
        ).fetchone()
        max_recalled = int(max_row[0] or 1)
        if max_recalled < 1:
            max_recalled = 1

        scored = []
        for r in rows:
            alpha = float(r["alpha"] or 1.0)
            beta  = float(r["beta"]  or 1.0)
            recalled = int(r["recalled_count"] or 0)
            temporal_class = r["temporal_class"] or "medium"
            bayesian_strength = alpha / (alpha + beta)
            recall_weight = math.log(1 + recalled) / math.log(1 + max_recalled)
            temporal_weight = _PII_TEMPORAL_WEIGHTS.get(temporal_class, 0.50)
            pii = min(1.0, max(0.0, bayesian_strength * recall_weight * temporal_weight))
            scored.append({
                "memory_id": r["id"],
                "pii": round(pii, 4),
                "tier": _pii_tier(pii),
                "alpha": round(alpha, 2),
                "beta": round(beta, 2),
                "recalled_count": recalled,
                "temporal_class": temporal_class,
                "content_snippet": (r["content"] or "")[:100],
            })

        scored.sort(key=lambda x: x["pii"], reverse=True)
        scored = scored[:top]
        return {"ok": True, "count": len(scored), "memories": scored}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_memory_trust_propagate() -> dict:
    """Recalculate trust scores, propagating through derived_from chains (max 10 hops)."""
    db = _db()
    try:
        updated = []
        rows = db.execute(
            "SELECT agent_id, category, COUNT(*) as total, "
            "SUM(CASE WHEN retracted_at IS NOT NULL THEN 1 ELSE 0 END) as retracted, "
            "SUM(CASE WHEN validated_at IS NOT NULL THEN 1 ELSE 0 END) as validated "
            "FROM memories WHERE retired_at IS NULL GROUP BY agent_id, category"
        ).fetchall()

        for row in rows:
            a = row["agent_id"]
            c = row["category"]
            t = row["total"]
            ret = row["retracted"]
            val = row["validated"]
            score = max(0.0, min(1.0, (t - ret * 2) / max(1, t)))
            if val > 0:
                score = min(1.0, score + 0.1 * (val / t))
            score = round(score, 4)
            db.execute(
                "INSERT INTO memory_trust_scores (agent_id, category, trust_score, sample_count, "
                "validated_count, retracted_count, last_evaluated_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now')) "
                "ON CONFLICT(agent_id, category) DO UPDATE SET "
                "trust_score=excluded.trust_score, sample_count=excluded.sample_count, "
                "validated_count=excluded.validated_count, retracted_count=excluded.retracted_count, "
                "last_evaluated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now'), updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')",
                (a, c, score, t, val, ret)
            )
            updated.append({"agent_id": a, "category": c, "score": score, "total": t})

        derived = db.execute(
            "SELECT id, trust_score, derived_from_ids FROM memories "
            "WHERE retired_at IS NULL AND retracted_at IS NULL AND derived_from_ids IS NOT NULL"
        ).fetchall()
        propagated = 0
        for mem in derived:
            try:
                source_ids = json.loads(mem["derived_from_ids"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not source_ids:
                continue
            min_trust = 1.0
            for sid in source_ids:
                min_trust = min(min_trust, _walk_trust_chain(db, sid, max_depth=10, visited=set()))
            inherited = round(min(mem["trust_score"] or 1.0, min_trust), 4)
            if abs(inherited - (mem["trust_score"] or 1.0)) > 0.001:
                db.execute("UPDATE memories SET trust_score = ? WHERE id = ?", (inherited, mem["id"]))
                propagated += 1

        db.commit()
        return {"ok": True, "agent_category_scores": updated, "derived_propagated": propagated}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_memory_suggest_category(content: str) -> dict:
    """Return an inferred category for the given content string."""
    inferred = _infer_category_from_content(content)
    return {
        "ok": True,
        "inferred_category": inferred,
        "valid_categories": sorted(VALID_MEMORY_CATEGORIES),
        "note": "Heuristic inference — verify before use",
    }


def tool_trust_show(memory_id: int) -> dict:
    """Show full trust breakdown for a memory."""
    db = _db()
    try:
        mem = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not mem:
            return {"ok": False, "error": f"Memory {memory_id} not found"}
        breakdown = _compute_trust_breakdown(db, mem)
        mem_d = dict(mem)
        breakdown["ok"] = True
        breakdown.update({
            "content_preview": (mem_d.get("content", "") or "")[:120],
            "category": mem_d.get("category"),
            "agent_id": mem_d.get("agent_id"),
            "temporal_class": mem_d.get("temporal_class"),
            "stored_trust_score": mem_d.get("trust_score"),
        })
        return breakdown
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_trust_audit(threshold: float = 0.5, limit: int = 50) -> dict:
    """Audit memories with trust scores below threshold."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT id, agent_id, category, scope, temporal_class, trust_score, "
            "validated_at, retracted_at, created_at, content "
            "FROM memories WHERE retired_at IS NULL AND trust_score < ? "
            "ORDER BY trust_score ASC LIMIT ?",
            (threshold, limit)
        ).fetchall()
        result = []
        for r in rows:
            rd = dict(r)
            rd["content_preview"] = (rd.pop("content", "") or "")[:100]
            result.append(rd)
        return {"ok": True, "threshold": threshold, "count": len(result), "memories": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_trust_calibrate(dry_run: bool = False) -> dict:
    """Calibrate trust scores using category priors and agent multipliers."""
    db = _db()
    try:
        updated = 0
        rows = db.execute(
            "SELECT id, agent_id, category, trust_score, retracted_at, validated_at "
            "FROM memories WHERE retired_at IS NULL"
        ).fetchall()
        for r in rows:
            r = dict(r)
            if r.get("retracted_at"):
                new_trust = 0.05
            elif r.get("validated_at"):
                cur = r.get("trust_score") or 1.0
                new_trust = cur if cur < 0.999 else _TRUST_CATEGORY_PRIORS.get(r.get("category", ""), 0.70)
            else:
                prior = _TRUST_CATEGORY_PRIORS.get(r.get("category", ""), 0.70)
                new_trust = round(min(1.0, prior * _trust_agent_multiplier(r.get("agent_id", ""))), 4)
            if abs((r.get("trust_score") or 1.0) - new_trust) > 0.001:
                if not dry_run:
                    db.execute(
                        "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                        (new_trust, r["id"])
                    )
                updated += 1

        if not dry_run:
            db.execute("""
                INSERT OR IGNORE INTO memory_trust_scores (agent_id, category, trust_score, sample_count)
                SELECT agent_id, category, 0.75, COUNT(*)
                FROM memories WHERE retired_at IS NULL GROUP BY agent_id, category
            """)
            db.execute("""
                UPDATE memory_trust_scores SET
                  sample_count = (SELECT COUNT(*) FROM memories m
                    WHERE m.agent_id = memory_trust_scores.agent_id
                    AND m.category = memory_trust_scores.category AND m.retired_at IS NULL),
                  retracted_count = (SELECT COUNT(*) FROM memories m
                    WHERE m.agent_id = memory_trust_scores.agent_id
                    AND m.category = memory_trust_scores.category AND m.retracted_at IS NOT NULL),
                  last_evaluated_at = strftime('%Y-%m-%dT%H:%M:%S','now'),
                  updated_at = strftime('%Y-%m-%dT%H:%M:%S','now')
            """)
            # Ensure the system agent exists before logging (FK constraint on events.agent_id)
            db.execute(
                "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
                "VALUES ('trust-engine', 'trust-engine', 'system', 'active', ?, ?)",
                (_now(), _now()),
            )
            db.execute(
                "INSERT INTO events (agent_id, event_type, summary, metadata, created_at) VALUES (?,?,?,?,?)",
                ("trust-engine", "result",
                 f"Trust calibration: {updated} memories updated with category priors",
                 json.dumps({"updated_count": updated, "dry_run": dry_run}), _now())
            )
            db.commit()
        return {"ok": True, "updated": updated, "dry_run": dry_run}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_trust_decay(dry_run: bool = False) -> dict:
    """Apply temporal decay to unvalidated memories."""
    db = _db()
    try:
        updated = 0
        rows = db.execute(
            "SELECT id, trust_score, temporal_class, updated_at, created_at "
            "FROM memories WHERE retired_at IS NULL AND retracted_at IS NULL "
            "  AND validated_at IS NULL AND temporal_class != 'permanent'"
        ).fetchall()
        for r in rows:
            r = dict(r)
            rate = _TRUST_DECAY_RATES.get(r.get("temporal_class", "medium"), 0.01)
            if rate == 0.0:
                continue
            days = _days_since(r.get("updated_at") or r.get("created_at", ""))
            if days < 1.0:
                continue
            current = r.get("trust_score") or 1.0
            new_trust = round(max(0.50, current * (1.0 - rate * days)), 4)
            if abs(new_trust - current) > 0.001:
                if not dry_run:
                    db.execute(
                        "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                        (new_trust, r["id"])
                    )
                updated += 1
        if not dry_run and updated > 0:
            db.commit()
        return {"ok": True, "decayed": updated, "dry_run": dry_run}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_trust_update_contradiction(memory_id_a: int, memory_id_b: int, resolved: bool = False) -> dict:
    """Penalize trust scores on contradicting memories."""
    db = _db()
    try:
        if resolved:
            db.execute(
                "UPDATE memories SET trust_score = ROUND(MAX(0.30, trust_score - 0.05), 4), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?", (memory_id_a,)
            )
        else:
            db.execute(
                "UPDATE memories SET trust_score = ROUND(MAX(0.30, trust_score - 0.20), 4), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id IN (?, ?)",
                (memory_id_a, memory_id_b)
            )
        rows = db.execute(
            "SELECT id, trust_score FROM memories WHERE id IN (?, ?)", (memory_id_a, memory_id_b)
        ).fetchall()
        db.commit()
        return {"ok": True, "resolved": resolved, "updated_memories": _rows_to_list(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_trust_process_meb(since: int = 0, dry_run: bool = False) -> dict:
    """Process memory event bus (MEB) entries and update trust scores accordingly."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT me.id, me.memory_id, me.operation, me.category, me.agent_id "
            "FROM memory_events me WHERE me.id > ? ORDER BY me.id ASC LIMIT 200",
            (since,)
        ).fetchall()
        processed = 0
        new_watermark = since
        for ev in rows:
            ev = dict(ev)
            new_watermark = max(new_watermark, ev["id"])
            mem_id = ev["memory_id"]
            mem = db.execute(
                "SELECT id, trust_score, category, agent_id, validated_at, retracted_at "
                "FROM memories WHERE id = ?", (mem_id,)
            ).fetchone()
            if not mem:
                continue
            mem = dict(mem)
            op = ev.get("operation", "")
            if op in ("insert", "backfill") and (mem.get("trust_score") or 1.0) >= 0.999:
                prior = _TRUST_CATEGORY_PRIORS.get(mem.get("category", ""), 0.70)
                new_trust = round(min(1.0, prior * _trust_agent_multiplier(mem.get("agent_id", ""))), 4)
                if not dry_run:
                    db.execute(
                        "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                        (new_trust, mem_id)
                    )
                processed += 1
            elif op == "update":
                if mem.get("retracted_at") and (mem.get("trust_score") or 1.0) > 0.05:
                    if not dry_run:
                        db.execute(
                            "UPDATE memories SET trust_score = 0.05, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                            (mem_id,)
                        )
                    processed += 1
                elif mem.get("validated_at") and (mem.get("trust_score") or 0.0) < 0.80:
                    new_trust = round(min(0.95, (mem.get("trust_score") or 0.70) * 1.25), 4)
                    if not dry_run:
                        db.execute(
                            "UPDATE memories SET trust_score = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
                            (new_trust, mem_id)
                        )
                    processed += 1

        if not dry_run and processed > 0:
            db.commit()
        return {"ok": True, "processed": processed, "new_watermark": new_watermark, "dry_run": dry_run}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="memory_pii",
        description=(
            "Compute the Proactive Interference Index (PII) for a single memory. "
            "PII quantifies how strongly a stored memory may interfere with recall of newer memories. "
            "Returns a score in [0,1] and a tier: OPEN, ESTABLISHED, ENTRENCHED, or CRYSTALLIZED."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "ID of the memory to compute PII for",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="memory_pii_scan",
        description=(
            "Scan all active memories and return the top-N ranked by PII score descending. "
            "Useful for identifying high-interference memories that might need pruning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "top": {
                    "type": "integer",
                    "description": "Number of top memories to return (default 20)",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="memory_trust_propagate",
        description=(
            "Recalculate trust scores for all agent/category combinations, then propagate "
            "those scores through derived_from chains (up to 10 hops). Updates the "
            "memory_trust_scores table and adjusts individual memory trust_score values."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="memory_suggest_category",
        description=(
            "Infer the most appropriate memory category for a given content string using keyword heuristics. "
            "Returns one of the valid category names. Always verify the result before storing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Memory content to categorize",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="trust_show",
        description=(
            "Show a full trust breakdown for a single memory: base prior, source reliability, "
            "validation bonus, age penalty, contradiction penalty, and final computed score."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "Memory ID to inspect",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="trust_audit",
        description=(
            "List active memories whose stored trust_score falls below the given threshold. "
            "Useful for identifying low-confidence or degraded memories that need attention."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "Trust score ceiling — memories below this are returned (default 0.5)",
                    "default": 0.5,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 50)",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="trust_calibrate",
        description=(
            "Apply category-based trust priors and agent multipliers to all active memories. "
            "Also updates the memory_trust_scores aggregate table. "
            "Use dry_run=true to preview changes without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, compute changes but do not write them (default false)",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="trust_decay",
        description=(
            "Apply temporal decay to unvalidated, non-permanent memories. "
            "Trust scores decay according to per-temporal-class rates; floor is 0.50. "
            "Use dry_run=true to preview without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, compute changes but do not write them (default false)",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="trust_update_contradiction",
        description=(
            "Penalize the trust scores of two contradicting memories. "
            "If resolved=false (default), both memories are penalized by 0.20. "
            "If resolved=true, only memory_id_a is penalized by 0.05 (loser-of-conflict penalty). "
            "Minimum score floor is 0.30."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id_a": {
                    "type": "integer",
                    "description": "First memory ID in the contradiction pair",
                },
                "memory_id_b": {
                    "type": "integer",
                    "description": "Second memory ID in the contradiction pair",
                },
                "resolved": {
                    "type": "boolean",
                    "description": "Whether the contradiction has been resolved (default false)",
                    "default": False,
                },
            },
            "required": ["memory_id_a", "memory_id_b"],
        },
    ),
    Tool(
        name="trust_process_meb",
        description=(
            "Process memory event bus (MEB) entries since a given watermark ID, "
            "updating trust scores for insert, backfill, update, retract, and validate operations. "
            "Returns the new high-water mark for incremental processing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "integer",
                    "description": "Process MEB events with id > this value (default 0 = all)",
                    "default": 0,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, compute changes but do not write them (default false)",
                    "default": False,
                },
            },
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch table: tool name -> handler
# ---------------------------------------------------------------------------

DISPATCH: dict[str, Any] = {
    "memory_pii":                 lambda args: tool_memory_pii(
                                      memory_id=int(args["memory_id"])),
    "memory_pii_scan":            lambda args: tool_memory_pii_scan(
                                      top=int(args.get("top", 20))),
    "memory_trust_propagate":     lambda args: tool_memory_trust_propagate(),
    "memory_suggest_category":    lambda args: tool_memory_suggest_category(
                                      content=args["content"]),
    "trust_show":                 lambda args: tool_trust_show(
                                      memory_id=int(args["memory_id"])),
    "trust_audit":                lambda args: tool_trust_audit(
                                      threshold=float(args.get("threshold", 0.5)),
                                      limit=int(args.get("limit", 50))),
    "trust_calibrate":            lambda args: tool_trust_calibrate(
                                      dry_run=bool(args.get("dry_run", False))),
    "trust_decay":                lambda args: tool_trust_decay(
                                      dry_run=bool(args.get("dry_run", False))),
    "trust_update_contradiction":  lambda args: tool_trust_update_contradiction(
                                      memory_id_a=int(args["memory_id_a"]),
                                      memory_id_b=int(args["memory_id_b"]),
                                      resolved=bool(args.get("resolved", False))),
    "trust_process_meb":          lambda args: tool_trust_process_meb(
                                      since=int(args.get("since", 0)),
                                      dry_run=bool(args.get("dry_run", False))),
}
