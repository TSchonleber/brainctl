"""
belief_revision.py — AGM Credibility-Weighted Conflict Resolution (COS-406)

Implements `resolve-conflict` logic for brainctl:
  - Credibility scoring via Bayesian mean × recency × trust × expertise
  - Winner/loser selection with threshold and escalation guards
  - Retraction of loser + supersedes edge insertion
  - Permanent / too-close escalation to Hermes
"""

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"


def _get_db(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Credibility score
# ---------------------------------------------------------------------------

def compute_credibility(memory: dict, agent_expertise: dict) -> float:
    """
    C(m) = bayesian_mean(alpha, beta)
           * (1 + log1p(recalled_count) / 10)
           * max(0, 1 - days_since_write / 365)
           * trust_score
           * agent_expertise_score

    memory keys: alpha, beta, recalled_count, created_at, trust_score
    agent_expertise: mapping of domain -> strength (0-1), used as mean expertise weight
    """
    alpha = float(memory.get("alpha") or 1.0)
    beta  = float(memory.get("beta")  or 1.0)
    bayesian_mean = alpha / (alpha + beta)

    recalled = int(memory.get("recalled_count") or 0)
    recall_boost = 1.0 + math.log1p(recalled) / 10.0

    created_at = memory.get("created_at") or memory.get("updated_at") or ""
    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_since = max(0.0, (now - created_dt).total_seconds() / 86400.0)
    except (ValueError, AttributeError):
        days_since = 0.0
    recency = max(0.0, 1.0 - days_since / 365.0)

    trust = float(memory.get("trust_score") or 1.0)

    # Mean expertise across all domains if available, else 1.0
    if agent_expertise:
        expertise_score = sum(agent_expertise.values()) / len(agent_expertise)
    else:
        expertise_score = 1.0

    return bayesian_mean * recall_boost * recency * trust * expertise_score


def _fetch_expertise(conn: sqlite3.Connection, agent_id: str) -> dict:
    rows = conn.execute(
        "SELECT domain, strength FROM agent_expertise WHERE agent_id = ?",
        (agent_id,)
    ).fetchall()
    return {r["domain"]: float(r["strength"]) for r in rows}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_conflicts(db_path: str | None = None) -> list[dict]:
    """Return all open belief_conflicts with credibility scores for both sides."""
    conn = _get_db(db_path)
    rows = conn.execute(
        """
        SELECT bc.id, bc.topic, bc.conflict_type, bc.severity,
               bc.agent_a_id, bc.agent_b_id, bc.belief_a, bc.belief_b,
               bc.detected_at, bc.requires_hermes_intervention
        FROM belief_conflicts bc
        WHERE bc.resolved_at IS NULL
        ORDER BY bc.severity DESC, bc.detected_at ASC
        """
    ).fetchall()

    result = []
    for r in rows:
        entry = dict(r)
        # Attach expertise-weighted scores where we have linked memory IDs
        # (belief_a/belief_b are free text here, not memory IDs in this schema)
        entry["score_a"] = None
        entry["score_b"] = None
        result.append(entry)
    conn.close()
    return result


def resolve_conflict(
    conflict_id: int,
    db_path: str | None = None,
    dry_run: bool = False,
    force_winner_id: str | None = None,
    threshold: float = 0.05,
) -> dict:
    """
    Apply AGM credibility-weighted resolution to a single open conflict.

    Returns a dict with keys:
      winner_id, loser_id, score_a, score_b, score_delta, action,
      escalated, escalation_reason, dry_run
    """
    conn = _get_db(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # 1. Load conflict
    row = conn.execute(
        """
        SELECT id, topic, conflict_type, severity,
               agent_a_id, agent_b_id, belief_a, belief_b,
               requires_hermes_intervention
        FROM belief_conflicts
        WHERE id = ? AND resolved_at IS NULL
        """,
        (conflict_id,)
    ).fetchone()

    if not row:
        conn.close()
        return {"error": f"Conflict #{conflict_id} not found or already resolved."}

    conflict = dict(row)

    # 2. Find the most recent non-retracted memories linked to each agent's position
    #    The conflict stores free-text beliefs; find memories matching agent_a_id/agent_b_id
    #    with content overlapping the topic/belief.
    def _best_memory(agent_id: str, belief_text: str) -> dict | None:
        # Try exact content match first, then fall back to most-confident active memory
        candidates = conn.execute(
            """
            SELECT id, alpha, beta, recalled_count, created_at, trust_score,
                   temporal_class, confidence, content
            FROM memories
            WHERE agent_id = ?
              AND retracted_at IS NULL
              AND retired_at IS NULL
            ORDER BY confidence DESC, recalled_count DESC
            LIMIT 5
            """,
            (agent_id,)
        ).fetchall()
        if not candidates:
            return None
        # Prefer one whose content overlaps with belief_text
        belief_words = set(belief_text.lower().split())
        best = None
        best_overlap = -1
        for c in candidates:
            overlap = len(set(c["content"].lower().split()) & belief_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best = dict(c)
        return best or dict(candidates[0])

    mem_a = _best_memory(conflict["agent_a_id"], conflict["belief_a"])
    mem_b = _best_memory(conflict["agent_b_id"] or "unknown", conflict["belief_b"]) if conflict["agent_b_id"] else None

    exp_a = _fetch_expertise(conn, conflict["agent_a_id"])
    exp_b = _fetch_expertise(conn, conflict["agent_b_id"]) if conflict["agent_b_id"] else {}

    # 3. Compute credibility scores
    if mem_a:
        score_a = compute_credibility(mem_a, exp_a)
    else:
        # No memory found — use raw confidence placeholder
        score_a = 0.5

    if mem_b:
        score_b = compute_credibility(mem_b, exp_b)
    else:
        score_b = 0.5 if conflict["agent_b_id"] else 0.3  # ground-truth conflicts default lower

    # 4. Permanent memory guard
    perm_a = mem_a and mem_a.get("temporal_class") == "permanent"
    perm_b = mem_b and mem_b.get("temporal_class") == "permanent"
    if perm_a or perm_b:
        resolution_result = {
            "conflict_id": conflict_id,
            "topic": conflict["topic"],
            "score_a": score_a,
            "score_b": score_b,
            "score_delta": abs(score_a - score_b),
            "winner_id": None,
            "loser_id": None,
            "action": "escalate",
            "escalated": True,
            "escalation_reason": "permanent memory involved — requires Hermes review",
            "dry_run": dry_run,
        }
        if not dry_run:
            conn.execute(
                "UPDATE belief_conflicts SET requires_hermes_intervention=1 WHERE id=?",
                (conflict_id,)
            )
            conn.commit()
        conn.close()
        return resolution_result

    # 5. Threshold guard (too close to call)
    delta = abs(score_a - score_b)
    if not force_winner_id and delta < threshold:
        resolution_result = {
            "conflict_id": conflict_id,
            "topic": conflict["topic"],
            "score_a": score_a,
            "score_b": score_b,
            "score_delta": delta,
            "winner_id": None,
            "loser_id": None,
            "action": "escalate",
            "escalated": True,
            "escalation_reason": f"scores too close (delta={delta:.4f} < threshold={threshold})",
            "dry_run": dry_run,
        }
        if not dry_run:
            conn.execute(
                "UPDATE belief_conflicts SET requires_hermes_intervention=1 WHERE id=?",
                (conflict_id,)
            )
            conn.commit()
        conn.close()
        return resolution_result

    # 6. Determine winner
    if force_winner_id:
        if force_winner_id == conflict["agent_a_id"] and mem_a:
            winner_mem = mem_a
            loser_mem = mem_b
            winner_agent = conflict["agent_a_id"]
            loser_agent = conflict["agent_b_id"]
            w_score, l_score = score_a, score_b
        elif force_winner_id == conflict["agent_b_id"] and mem_b:
            winner_mem = mem_b
            loser_mem = mem_a
            winner_agent = conflict["agent_b_id"]
            loser_agent = conflict["agent_a_id"]
            w_score, l_score = score_b, score_a
        else:
            conn.close()
            return {"error": f"force_winner_id '{force_winner_id}' not matched to conflict agents."}
    else:
        if score_a >= score_b:
            winner_mem = mem_a
            loser_mem = mem_b
            winner_agent = conflict["agent_a_id"]
            loser_agent = conflict["agent_b_id"]
            w_score, l_score = score_a, score_b
        else:
            winner_mem = mem_b
            loser_mem = mem_a
            winner_agent = conflict["agent_b_id"]
            loser_agent = conflict["agent_a_id"]
            w_score, l_score = score_b, score_a

    winner_mem_id = winner_mem["id"] if winner_mem else None
    loser_mem_id  = loser_mem["id"]  if loser_mem  else None

    resolution_text = (
        f"AGM resolved: agent {winner_agent} wins (score={w_score:.4f}) "
        f"over agent {loser_agent} (score={l_score:.4f}); delta={delta:.4f}"
    )

    if dry_run:
        conn.close()
        return {
            "conflict_id": conflict_id,
            "topic": conflict["topic"],
            "score_a": score_a,
            "score_b": score_b,
            "score_delta": delta,
            "winner_agent": winner_agent,
            "loser_agent": loser_agent,
            "winner_mem_id": winner_mem_id,
            "loser_mem_id": loser_mem_id,
            "action": "retract_loser",
            "escalated": False,
            "escalation_reason": None,
            "resolution": resolution_text,
            "dry_run": True,
        }

    # 7. Retract loser memory
    if loser_mem_id:
        conn.execute(
            """
            UPDATE memories SET retracted_at=?, retraction_reason=?
            WHERE id=? AND retracted_at IS NULL
            """,
            (now, f"AGM conflict #{conflict_id}: superseded by memory {winner_mem_id}", loser_mem_id)
        )

    # 8. Insert supersedes edge
    if winner_mem_id and loser_mem_id:
        # Check for existing knowledge_edges schema
        edge_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_edges'"
        ).fetchone()
        if edge_schema:
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_edges
                  (source_table, source_id, target_table, target_id, relation_type, created_at)
                VALUES ('memories', ?, 'memories', ?, 'supersedes', ?)
                """,
                (winner_mem_id, loser_mem_id, now)
            )

    # 9. Mark conflict resolved
    conn.execute(
        "UPDATE belief_conflicts SET resolved_at=?, resolution=? WHERE id=?",
        (now, resolution_text, conflict_id)
    )

    conn.commit()
    conn.close()

    return {
        "conflict_id": conflict_id,
        "topic": conflict["topic"],
        "score_a": score_a,
        "score_b": score_b,
        "score_delta": delta,
        "winner_agent": winner_agent,
        "loser_agent": loser_agent,
        "winner_mem_id": winner_mem_id,
        "loser_mem_id": loser_mem_id,
        "action": "retract_loser",
        "escalated": False,
        "escalation_reason": None,
        "resolution": resolution_text,
        "dry_run": False,
    }


def auto_resolve(
    db_path: str | None = None,
    threshold: float = 0.05,
    dry_run: bool = False,
) -> list[dict]:
    """Batch resolve all auto-resolvable open conflicts. Returns list of resolution results."""
    conn = _get_db(db_path)
    rows = conn.execute(
        "SELECT id FROM belief_conflicts WHERE resolved_at IS NULL ORDER BY severity DESC"
    ).fetchall()
    conn.close()

    results = []
    for row in rows:
        r = resolve_conflict(
            conflict_id=row["id"],
            db_path=db_path,
            dry_run=dry_run,
            threshold=threshold,
        )
        results.append(r)
    return results
