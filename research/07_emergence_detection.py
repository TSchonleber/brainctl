"""
Emergence Detection — Patterns Visible Only in Aggregate
=========================================================
Concept: Individual memories are facts. But patterns emerge when you look
at the corpus: trending topics, shifting agent priorities, behavioral drift,
repeated failure modes. Emergence detection surfaces these.

Algorithms:
  1. Topic frequency trending — what's being written about most recently?
  2. Agent behavioral drift — is an agent's category distribution changing?
  3. Confidence distribution shift — is the memory store getting healthier or degrading?
  4. Recall cluster analysis — which memory clusters are getting heavy usage?
  5. Event causality chains — repeated causal sequences that suggest systemic issues
"""

import sqlite3
import json
from datetime import datetime, timezone
from collections import Counter

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 1. Topic Frequency Trending ───────────────────────────────────────────────

def get_trending_topics(
    conn: sqlite3.Connection,
    window_days: int = 7,
    top_k: int = 20,
) -> list[dict]:
    """
    Use FTS5 term frequencies to find trending topics in recent memories.
    Returns top tokens by frequency in the window vs. prior window.
    """
    conn.row_factory = sqlite3.Row

    # Recent window
    recent = conn.execute("""
        SELECT content FROM memories
        WHERE retired_at IS NULL
          AND (julianday('now') - julianday(created_at)) <= ?
    """, (window_days,)).fetchall()

    # Prior window (same duration before that)
    prior = conn.execute("""
        SELECT content FROM memories
        WHERE retired_at IS NULL
          AND (julianday('now') - julianday(created_at)) > ?
          AND (julianday('now') - julianday(created_at)) <= ?
    """, (window_days, window_days * 2)).fetchall()

    def tokenize(rows):
        tokens = []
        stop = {"the", "a", "an", "is", "in", "of", "to", "and", "for", "with",
                "this", "that", "it", "be", "are", "was", "has", "have", "from"}
        for r in rows:
            words = r[0].lower().split()
            tokens.extend(w.strip(".,;:\"'()[]") for w in words
                          if len(w) > 3 and w not in stop)
        return tokens

    recent_counts = Counter(tokenize(recent))
    prior_counts = Counter(tokenize(prior))
    total_recent = sum(recent_counts.values()) or 1
    total_prior = sum(prior_counts.values()) or 1

    trends = []
    for term, cnt in recent_counts.most_common(top_k * 3):
        freq_recent = cnt / total_recent
        freq_prior = prior_counts.get(term, 0) / total_prior
        lift = freq_recent / (freq_prior + 0.001)  # avoid div/0
        trends.append({
            "term": term,
            "count_recent": cnt,
            "count_prior": prior_counts.get(term, 0),
            "lift": round(lift, 2),
        })

    trends.sort(key=lambda x: x["lift"], reverse=True)
    return trends[:top_k]


# ── 2. Agent Behavioral Drift ─────────────────────────────────────────────────

def detect_agent_drift(
    conn: sqlite3.Connection,
    window_days: int = 14,
) -> list[dict]:
    """
    Compare each agent's category distribution now vs. baseline.
    Flag agents whose distribution has shifted significantly.
    """
    conn.row_factory = sqlite3.Row

    agents = conn.execute(
        "SELECT DISTINCT agent_id FROM memories WHERE retired_at IS NULL"
    ).fetchall()

    drift_report = []
    for a_row in agents:
        agent_id = a_row["agent_id"]

        recent = conn.execute("""
            SELECT category, COUNT(*) as cnt FROM memories
            WHERE agent_id = ? AND retired_at IS NULL
              AND (julianday('now') - julianday(created_at)) <= ?
            GROUP BY category
        """, (agent_id, window_days)).fetchall()

        baseline = conn.execute("""
            SELECT category, COUNT(*) as cnt FROM memories
            WHERE agent_id = ? AND retired_at IS NULL
              AND (julianday('now') - julianday(created_at)) > ?
            GROUP BY category
        """, (agent_id, window_days)).fetchall()

        if not recent or not baseline:
            continue

        recent_dist = {r["category"]: r["cnt"] for r in recent}
        baseline_dist = {r["category"]: r["cnt"] for r in baseline}
        total_r = sum(recent_dist.values()) or 1
        total_b = sum(baseline_dist.values()) or 1

        # KL-like divergence (symmetric)
        all_cats = set(recent_dist) | set(baseline_dist)
        divergence = 0.0
        for cat in all_cats:
            p = recent_dist.get(cat, 0) / total_r
            q = baseline_dist.get(cat, 0) / total_b
            if p > 0 and q > 0:
                divergence += abs(p - q)

        if divergence > 0.3:
            drift_report.append({
                "agent_id": agent_id,
                "divergence": round(divergence, 3),
                "recent_distribution": {k: round(v / total_r, 3) for k, v in recent_dist.items()},
                "baseline_distribution": {k: round(v / total_b, 3) for k, v in baseline_dist.items()},
            })

    drift_report.sort(key=lambda x: x["divergence"], reverse=True)
    return drift_report


# ── 3. Confidence Distribution Health ─────────────────────────────────────────

def assess_store_health(conn: sqlite3.Connection) -> dict:
    """
    Summarize confidence distribution across temporal classes.
    Returns health metrics: mean confidence, % at risk, class breakdown.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT temporal_class,
               COUNT(*) as total,
               AVG(confidence) as avg_conf,
               SUM(CASE WHEN confidence < 0.3 THEN 1 ELSE 0 END) as at_risk,
               SUM(CASE WHEN confidence >= 0.8 THEN 1 ELSE 0 END) as healthy
        FROM memories
        WHERE retired_at IS NULL
        GROUP BY temporal_class
    """).fetchall()

    breakdown = {}
    total_all = 0
    at_risk_all = 0

    for r in rows:
        breakdown[r["temporal_class"]] = {
            "total": r["total"],
            "avg_confidence": round(r["avg_conf"], 3),
            "at_risk": r["at_risk"],
            "healthy": r["healthy"],
            "at_risk_pct": round(r["at_risk"] / r["total"] * 100, 1) if r["total"] else 0,
        }
        total_all += r["total"]
        at_risk_all += r["at_risk"]

    return {
        "total_memories": total_all,
        "at_risk_count": at_risk_all,
        "at_risk_pct": round(at_risk_all / total_all * 100, 1) if total_all else 0,
        "by_class": breakdown,
        "signal_to_noise": round(1.0 - at_risk_all / total_all, 3) if total_all else 1.0,
        "assessed_at": now_iso(),
    }


# ── 4. Recall Cluster Analysis ────────────────────────────────────────────────

def get_recall_hotspots(
    conn: sqlite3.Connection,
    top_k: int = 10,
) -> list[dict]:
    """
    Find the most recalled memories — these are the high-value nodes.
    Candidates for promotion to 'permanent'.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, content, category, scope, agent_id,
               recalled_count, confidence, temporal_class
        FROM memories
        WHERE retired_at IS NULL
        ORDER BY recalled_count DESC
        LIMIT ?
    """, (top_k,)).fetchall()

    return [dict(r) for r in rows]


# ── 5. Event Causality Chain Patterns ─────────────────────────────────────────

def detect_recurring_error_chains(
    conn: sqlite3.Connection,
    window_days: int = 30,
    min_occurrences: int = 3,
) -> list[dict]:
    """
    Find repeated causal chains in events (e.g., repeated 'error' → 'retry' sequences).
    Uses causal_chain_root to group events by root cause.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT causal_chain_root, event_type, COUNT(*) as cnt
        FROM events
        WHERE causal_chain_root IS NOT NULL
          AND event_type IN ('error', 'retry', 'blocked', 'handoff')
          AND (julianday('now') - julianday(created_at)) <= ?
        GROUP BY causal_chain_root, event_type
        HAVING COUNT(*) >= ?
        ORDER BY cnt DESC
        LIMIT 20
    """, (window_days, min_occurrences)).fetchall()

    return [dict(r) for r in rows]


# ── Report ─────────────────────────────────────────────────────────────────────

def run_emergence_report(db_path: str = DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    report = {
        "generated_at": now_iso(),
        "store_health": assess_store_health(conn),
        "trending_topics": get_trending_topics(conn, window_days=7, top_k=10),
        "agent_drift": detect_agent_drift(conn, window_days=14),
        "recall_hotspots": get_recall_hotspots(conn, top_k=5),
        "recurring_error_chains": detect_recurring_error_chains(conn),
    }
    conn.close()
    return report


if __name__ == "__main__":
    report = run_emergence_report()
    health = report["store_health"]
    print(f"Store health: {health['total_memories']} memories, "
          f"signal-to-noise={health['signal_to_noise']:.3f}, "
          f"at-risk={health['at_risk_pct']}%")
    print(f"Trending topics: {[t['term'] for t in report['trending_topics'][:5]]}")
    print(f"Agent drift detected: {len(report['agent_drift'])} agents")
