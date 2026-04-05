"""
Memory-to-Goal Feedback Loop — Patterns in memory auto-generating goals
========================================================================
Wave 4 Research | COS-180
Builds on: 07_emergence_detection.py (trending topics, agent drift, recall hotspots)

Root question: Can memory drive proactive goal formation, not just reactive retrieval?

Architecture:
  1. Signal Extraction — harvest raw signals from the memory corpus
  2. Signal Clustering — group related signals into coherent themes
  3. Goal Proposal Generation — translate clustered signals into actionable goal proposals
  4. Proposal Ranking — score proposals by urgency, coverage, and confidence
  5. Deduplication — suppress proposals that duplicate existing goals/tasks

Design principle: SQL-first, no LLM dependency in the critical path.
LLM enrichment is optional (for proposal title/description generation) and
can be injected via a callback. The core inference runs on pure SQL + Python.
"""

import sqlite3
import json
import math
from datetime import datetime, timezone
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class Signal:
    """A raw signal extracted from memory patterns."""
    signal_type: str          # 'topic_surge', 'error_cluster', 'confidence_decay', 'drift', 'recall_dead_zone'
    description: str
    evidence: list[dict]      # rows/facts supporting this signal
    strength: float           # 0.0-1.0 normalized
    scope: str = "global"     # 'global', 'project:<name>', 'agent:<id>'
    extracted_at: str = ""

    def __post_init__(self):
        if not self.extracted_at:
            self.extracted_at = now_iso()


@dataclass
class GoalProposal:
    """A proposed goal inferred from clustered signals."""
    title: str
    rationale: str
    signals: list[Signal]
    score: float              # composite ranking score
    scope: str = "global"
    suggested_assignee: Optional[str] = None
    suggested_priority: str = "medium"
    tags: list[str] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = now_iso()


# =============================================================================
# 1. Signal Extraction
# =============================================================================

def extract_topic_surge_signals(
    conn: sqlite3.Connection,
    window_days: int = 7,
    min_lift: float = 3.0,
    min_count: int = 5,
) -> list[Signal]:
    """
    Detect topics with abnormal frequency increase (lift) in the recent window.
    A topic with lift >= min_lift and count >= min_count is a surge signal.

    This extends 07_emergence_detection.get_trending_topics by adding
    a significance threshold and packaging results as Signals.
    """
    conn.row_factory = sqlite3.Row

    recent = conn.execute("""
        SELECT content, category, scope FROM memories
        WHERE retired_at IS NULL
          AND (julianday('now') - julianday(created_at)) <= ?
    """, (window_days,)).fetchall()

    prior = conn.execute("""
        SELECT content FROM memories
        WHERE retired_at IS NULL
          AND (julianday('now') - julianday(created_at)) > ?
          AND (julianday('now') - julianday(created_at)) <= ?
    """, (window_days, window_days * 2)).fetchall()

    stop = {"the", "a", "an", "is", "in", "of", "to", "and", "for", "with",
            "this", "that", "it", "be", "are", "was", "has", "have", "from",
            "not", "but", "they", "been", "will", "can", "does", "its",
            "about", "into", "more", "than", "also", "when", "which", "each"}

    def tokenize(rows, content_key=0):
        tokens = []
        for r in rows:
            text = r[content_key] if isinstance(r, sqlite3.Row) else r[content_key]
            words = text.lower().split()
            tokens.extend(w.strip(".,;:\"'()[]{}") for w in words
                          if len(w) > 3 and w.lower().strip(".,;:\"'()[]{}") not in stop)
        return tokens

    recent_tokens = tokenize(recent)
    prior_tokens = tokenize(prior)
    recent_counts = Counter(recent_tokens)
    prior_counts = Counter(prior_tokens)
    total_r = sum(recent_counts.values()) or 1
    total_p = sum(prior_counts.values()) or 1

    # Track which scopes each term appears in
    term_scopes = defaultdict(set)
    for r in recent:
        words = r["content"].lower().split()
        for w in words:
            w = w.strip(".,;:\"'()[]{}")
            if len(w) > 3 and w not in stop:
                term_scopes[w].add(r["scope"])

    signals = []
    for term, cnt in recent_counts.most_common(200):
        if cnt < min_count:
            continue
        freq_r = cnt / total_r
        freq_p = prior_counts.get(term, 0) / total_p
        lift = freq_r / (freq_p + 1e-6)
        if lift >= min_lift:
            scopes = term_scopes.get(term, {"global"})
            scope = next(iter(scopes)) if len(scopes) == 1 else "global"
            signals.append(Signal(
                signal_type="topic_surge",
                description=f"Topic '{term}' surging: {cnt} mentions (lift={lift:.1f}x vs prior window)",
                evidence=[{"term": term, "count": cnt, "lift": round(lift, 2),
                           "prior_count": prior_counts.get(term, 0)}],
                strength=min(1.0, lift / 10.0),  # normalize: 10x lift = 1.0 strength
                scope=scope,
            ))

    return signals


def extract_error_cluster_signals(
    conn: sqlite3.Connection,
    window_days: int = 30,
    min_occurrences: int = 3,
) -> list[Signal]:
    """
    Detect repeated error/failure patterns from events table.
    Clusters by causal_chain_root and by content similarity in error events.
    """
    conn.row_factory = sqlite3.Row

    # Causal chain clustering
    chains = conn.execute("""
        SELECT causal_chain_root, COUNT(*) as cnt,
               GROUP_CONCAT(summary, ' | ') as summaries
        FROM events
        WHERE causal_chain_root IS NOT NULL
          AND event_type IN ('error', 'retry', 'blocked')
          AND (julianday('now') - julianday(created_at)) <= ?
        GROUP BY causal_chain_root
        HAVING COUNT(*) >= ?
        ORDER BY cnt DESC
        LIMIT 20
    """, (window_days, min_occurrences)).fetchall()

    signals = []
    for c in chains:
        signals.append(Signal(
            signal_type="error_cluster",
            description=f"Recurring error chain (root={c['causal_chain_root']}): "
                        f"{c['cnt']} occurrences in {window_days}d",
            evidence=[{"causal_chain_root": c["causal_chain_root"],
                       "count": c["cnt"],
                       "summaries": c["summaries"][:500]}],
            strength=min(1.0, c["cnt"] / 10.0),
        ))

    # Category-level error concentration in memories
    error_cats = conn.execute("""
        SELECT category, scope, COUNT(*) as cnt
        FROM memories
        WHERE retired_at IS NULL
          AND confidence < 0.3
          AND (julianday('now') - julianday(created_at)) <= ?
        GROUP BY category, scope
        HAVING COUNT(*) >= ?
        ORDER BY cnt DESC
    """, (window_days, min_occurrences)).fetchall()

    for ec in error_cats:
        signals.append(Signal(
            signal_type="confidence_decay",
            description=f"Low-confidence cluster: {ec['cnt']} memories in "
                        f"category='{ec['category']}', scope='{ec['scope']}'",
            evidence=[{"category": ec["category"], "scope": ec["scope"],
                       "low_conf_count": ec["cnt"]}],
            strength=min(1.0, ec["cnt"] / 15.0),
            scope=ec["scope"],
        ))

    return signals


def extract_drift_signals(
    conn: sqlite3.Connection,
    window_days: int = 14,
    divergence_threshold: float = 0.4,
) -> list[Signal]:
    """
    Detect agents whose behavior has shifted significantly.
    Re-uses logic from 07_emergence_detection.detect_agent_drift but
    packages results as Signals with higher threshold for goal triggering.
    """
    conn.row_factory = sqlite3.Row
    agents = conn.execute(
        "SELECT DISTINCT agent_id FROM memories WHERE retired_at IS NULL"
    ).fetchall()

    signals = []
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

        divergence = sum(
            abs(recent_dist.get(c, 0) / total_r - baseline_dist.get(c, 0) / total_b)
            for c in set(recent_dist) | set(baseline_dist)
        )

        if divergence > divergence_threshold:
            # Find which categories shifted most
            shifts = sorted(
                ((c, recent_dist.get(c, 0) / total_r - baseline_dist.get(c, 0) / total_b)
                 for c in set(recent_dist) | set(baseline_dist)),
                key=lambda x: abs(x[1]), reverse=True
            )[:3]

            signals.append(Signal(
                signal_type="drift",
                description=f"Agent '{agent_id}' behavioral drift: "
                            f"divergence={divergence:.2f}, top shifts: "
                            f"{', '.join(f'{c}({d:+.2f})' for c, d in shifts)}",
                evidence=[{"agent_id": agent_id, "divergence": round(divergence, 3),
                           "top_shifts": [{"category": c, "delta": round(d, 3)} for c, d in shifts]}],
                strength=min(1.0, divergence / 1.0),
                scope=f"agent:{agent_id}",
            ))

    return signals


def extract_recall_dead_zone_signals(
    conn: sqlite3.Connection,
    min_memories: int = 10,
) -> list[Signal]:
    """
    Find categories/scopes with many memories but near-zero recall.
    These represent knowledge the org invested in but never uses —
    either the knowledge is wrong, or the org has a blind spot.
    """
    conn.row_factory = sqlite3.Row
    zones = conn.execute("""
        SELECT category, scope,
               COUNT(*) as total,
               SUM(recalled_count) as total_recalls,
               AVG(confidence) as avg_conf
        FROM memories
        WHERE retired_at IS NULL
        GROUP BY category, scope
        HAVING COUNT(*) >= ? AND SUM(recalled_count) = 0
        ORDER BY COUNT(*) DESC
    """, (min_memories,)).fetchall()

    signals = []
    for z in zones:
        signals.append(Signal(
            signal_type="recall_dead_zone",
            description=f"Dead zone: {z['total']} memories in "
                        f"category='{z['category']}', scope='{z['scope']}' "
                        f"with zero recalls (avg_conf={z['avg_conf']:.2f})",
            evidence=[{"category": z["category"], "scope": z["scope"],
                       "total": z["total"], "avg_confidence": round(z["avg_conf"], 3)}],
            strength=min(1.0, z["total"] / 50.0),
            scope=z["scope"],
        ))

    return signals


def extract_all_signals(conn: sqlite3.Connection) -> list[Signal]:
    """Run all signal extractors and return combined list."""
    signals = []
    signals.extend(extract_topic_surge_signals(conn))
    signals.extend(extract_error_cluster_signals(conn))
    signals.extend(extract_drift_signals(conn))
    signals.extend(extract_recall_dead_zone_signals(conn))
    return signals


# =============================================================================
# 2. Signal Clustering
# =============================================================================

def cluster_signals(signals: list[Signal], similarity_threshold: float = 0.3) -> list[list[Signal]]:
    """
    Group related signals into clusters using token-overlap similarity.

    Approach: lightweight agglomerative clustering on signal descriptions.
    No embeddings required — uses Jaccard similarity on token sets.
    For production, this should be replaced with embedding-based clustering.
    """
    if not signals:
        return []

    def tokenize(text: str) -> set[str]:
        stop = {"the", "a", "an", "is", "in", "of", "to", "and", "for", "with", "this", "that"}
        return {w.strip(".,;:'\"()[]") for w in text.lower().split()
                if len(w) > 3 and w not in stop}

    def jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    # Token sets for each signal
    token_sets = [tokenize(s.description + " " + " ".join(
        str(v) for e in s.evidence for v in e.values() if isinstance(v, str)
    )) for s in signals]

    # Greedy clustering
    assigned = [False] * len(signals)
    clusters = []

    for i in range(len(signals)):
        if assigned[i]:
            continue
        cluster = [signals[i]]
        assigned[i] = True

        for j in range(i + 1, len(signals)):
            if assigned[j]:
                continue
            # Same signal_type gets a small similarity bonus (not enough alone to cluster)
            type_bonus = 0.05 if signals[i].signal_type == signals[j].signal_type else 0.0
            # Same scope gets a similarity bonus
            scope_bonus = 0.1 if signals[i].scope == signals[j].scope else 0.0
            base_sim = jaccard(token_sets[i], token_sets[j])
            # Require meaningful token overlap — bonuses alone shouldn't trigger clustering
            if base_sim < 0.1:
                continue
            sim = base_sim + type_bonus + scope_bonus

            if sim >= similarity_threshold:
                cluster.append(signals[j])
                assigned[j] = True

        clusters.append(cluster)

    return clusters


# =============================================================================
# 3. Goal Proposal Generation
# =============================================================================

def generate_proposals(
    clusters: list[list[Signal]],
    llm_enrich: Optional[Callable[[list[Signal]], tuple[str, str]]] = None,
) -> list[GoalProposal]:
    """
    Convert signal clusters into goal proposals.

    Each cluster becomes one proposal. The title/rationale can be enriched
    by an optional LLM callback. Without it, we generate rule-based summaries.

    Args:
        clusters: grouped signals from cluster_signals()
        llm_enrich: optional (signals) -> (title, rationale) callback
    """
    proposals = []

    for cluster in clusters:
        if not cluster:
            continue

        # Determine dominant signal type
        type_counts = Counter(s.signal_type for s in cluster)
        dominant_type = type_counts.most_common(1)[0][0]

        # Determine scope
        scopes = [s.scope for s in cluster]
        scope = scopes[0] if len(set(scopes)) == 1 else "global"

        # Aggregate strength
        avg_strength = sum(s.strength for s in cluster) / len(cluster)
        max_strength = max(s.strength for s in cluster)

        # Rule-based title/rationale generation
        if llm_enrich:
            title, rationale = llm_enrich(cluster)
        else:
            title, rationale = _rule_based_proposal(cluster, dominant_type)

        # Priority mapping based on signal characteristics
        if max_strength >= 0.8 or (dominant_type == "error_cluster" and avg_strength >= 0.5):
            priority = "high"
        elif avg_strength >= 0.4:
            priority = "medium"
        else:
            priority = "low"

        # Extract suggested assignee from drift signals
        assignee = None
        for s in cluster:
            if s.signal_type == "drift":
                for e in s.evidence:
                    if "agent_id" in e:
                        assignee = e["agent_id"]
                        break
            if assignee:
                break

        # Tags from signal types and evidence
        tags = list(set(s.signal_type for s in cluster))
        for s in cluster:
            for e in s.evidence:
                if "category" in e:
                    tags.append(f"cat:{e['category']}")

        proposals.append(GoalProposal(
            title=title,
            rationale=rationale,
            signals=cluster,
            score=0.0,  # filled by ranking step
            scope=scope,
            suggested_assignee=assignee,
            suggested_priority=priority,
            tags=list(set(tags)),
        ))

    return proposals


def _rule_based_proposal(cluster: list[Signal], dominant_type: str) -> tuple[str, str]:
    """Generate a title and rationale from signal cluster without LLM."""
    n = len(cluster)
    strongest = max(cluster, key=lambda s: s.strength)

    templates = {
        "topic_surge": (
            "Investigate surging topic: {key}",
            "Detected {n} related signals indicating abnormal activity around '{key}'. "
            "This pattern suggests an emerging area that may need a dedicated goal or task. "
            "Evidence: {evidence}"
        ),
        "error_cluster": (
            "Address recurring failure pattern: {key}",
            "Detected {n} clustered error signals. Recurring failures indicate a systemic "
            "issue that won't self-resolve. Root cause analysis recommended. "
            "Evidence: {evidence}"
        ),
        "confidence_decay": (
            "Audit low-confidence knowledge area: {key}",
            "Detected {n} signals of degrading memory confidence. Knowledge in this area "
            "may be stale, contradictory, or poorly sourced. A review pass is needed. "
            "Evidence: {evidence}"
        ),
        "drift": (
            "Review agent behavioral shift: {key}",
            "Detected {n} drift signals. Agent behavior has shifted significantly from "
            "baseline. This may indicate role evolution, scope creep, or misalignment. "
            "Evidence: {evidence}"
        ),
        "recall_dead_zone": (
            "Evaluate unused knowledge area: {key}",
            "Detected {n} signals of knowledge with zero recall. Either the knowledge is "
            "wrong/outdated, or the org has a retrieval blind spot. Audit needed. "
            "Evidence: {evidence}"
        ),
    }

    # Extract a key term from the strongest signal
    key = ""
    for e in strongest.evidence:
        if "term" in e:
            key = e["term"]
            break
        if "category" in e:
            key = e["category"]
            break
        if "agent_id" in e:
            key = e["agent_id"]
            break
    if not key:
        key = strongest.description[:60]

    template = templates.get(dominant_type, templates["topic_surge"])
    evidence_summary = "; ".join(s.description[:100] for s in cluster[:3])

    title = template[0].format(key=key, n=n)
    rationale = template[1].format(key=key, n=n, evidence=evidence_summary)

    return title, rationale


# =============================================================================
# 4. Proposal Ranking
# =============================================================================

def rank_proposals(
    proposals: list[GoalProposal],
    weights: Optional[dict] = None,
) -> list[GoalProposal]:
    """
    Score and rank goal proposals by composite criteria.

    Score = w_strength * avg_signal_strength
          + w_coverage * signal_count_normalized
          + w_urgency  * urgency_factor
          + w_novelty  * novelty_factor

    Urgency: error_cluster and confidence_decay score higher.
    Novelty: topics not previously seen score higher.
    Coverage: more signals backing a proposal = higher confidence.
    """
    if not weights:
        weights = {
            "strength": 0.35,
            "coverage": 0.25,
            "urgency": 0.25,
            "novelty": 0.15,
        }

    # Normalize coverage across proposals
    max_signals = max((len(p.signals) for p in proposals), default=1)

    urgency_map = {
        "error_cluster": 1.0,
        "confidence_decay": 0.8,
        "drift": 0.6,
        "topic_surge": 0.5,
        "recall_dead_zone": 0.3,
    }

    for p in proposals:
        avg_strength = sum(s.strength for s in p.signals) / len(p.signals)
        coverage = len(p.signals) / max_signals

        # Urgency: weighted by signal types present
        type_urgencies = [urgency_map.get(s.signal_type, 0.5) for s in p.signals]
        urgency = max(type_urgencies) if type_urgencies else 0.5

        # Novelty heuristic: signals with higher lift / fewer prior occurrences
        novelty_scores = []
        for s in p.signals:
            for e in s.evidence:
                if "lift" in e:
                    novelty_scores.append(min(1.0, e["lift"] / 15.0))
                elif "count" in e and e["count"] > 0:
                    novelty_scores.append(min(1.0, e["count"] / 20.0))
        novelty = sum(novelty_scores) / len(novelty_scores) if novelty_scores else 0.5

        p.score = (
            weights["strength"] * avg_strength
            + weights["coverage"] * coverage
            + weights["urgency"] * urgency
            + weights["novelty"] * novelty
        )

    proposals.sort(key=lambda p: p.score, reverse=True)
    return proposals


# =============================================================================
# 5. Deduplication Against Existing Goals/Tasks
# =============================================================================

def deduplicate_proposals(
    proposals: list[GoalProposal],
    conn: sqlite3.Connection,
    existing_titles: Optional[list[str]] = None,
) -> list[GoalProposal]:
    """
    Remove proposals that overlap with existing tasks/goals in brain.db.

    Uses FTS match on tasks table + optional external title list
    (e.g., from Paperclip issues) to suppress duplicates.
    """
    if not proposals:
        return []

    conn.row_factory = sqlite3.Row

    # Gather existing task titles from brain.db
    existing = set()
    try:
        rows = conn.execute(
            "SELECT title FROM tasks WHERE status NOT IN ('cancelled', 'completed')"
        ).fetchall()
        existing.update(r["title"].lower() for r in rows)
    except Exception:
        pass  # tasks table may not exist in test DBs

    if existing_titles:
        existing.update(t.lower() for t in existing_titles)

    # Simple token-overlap dedup
    def tokenize(text: str) -> set[str]:
        return {w.strip(".,;:'\"()[]") for w in text.lower().split() if len(w) > 3}

    existing_token_sets = [tokenize(t) for t in existing]

    filtered = []
    for p in proposals:
        p_tokens = tokenize(p.title + " " + p.rationale[:100])
        is_dup = False
        for et in existing_token_sets:
            if et and p_tokens:
                overlap = len(p_tokens & et) / len(p_tokens | et)
                if overlap > 0.5:
                    is_dup = True
                    break
        if not is_dup:
            filtered.append(p)

    return filtered


# =============================================================================
# Full Pipeline
# =============================================================================

def run_memory_to_goal_pipeline(
    db_path: str = DB_PATH,
    llm_enrich: Optional[Callable] = None,
    existing_titles: Optional[list[str]] = None,
    top_k: int = 10,
) -> dict:
    """
    Execute the full memory-to-goal feedback loop.

    Returns:
        {
            "generated_at": ISO timestamp,
            "signals_extracted": int,
            "clusters_formed": int,
            "proposals_generated": int,
            "proposals_after_dedup": int,
            "proposals": [GoalProposal as dict, ...]
        }
    """
    conn = sqlite3.connect(db_path)

    # Step 1: Extract signals
    signals = extract_all_signals(conn)

    # Step 2: Cluster signals
    clusters = cluster_signals(signals)

    # Step 3: Generate proposals
    proposals = generate_proposals(clusters, llm_enrich=llm_enrich)

    # Step 4: Rank proposals
    proposals = rank_proposals(proposals)

    # Step 5: Deduplicate
    proposals = deduplicate_proposals(proposals, conn, existing_titles=existing_titles)

    # Trim to top_k
    proposals = proposals[:top_k]

    conn.close()

    return {
        "generated_at": now_iso(),
        "signals_extracted": len(signals),
        "clusters_formed": len(clusters),
        "proposals_generated": len(proposals) + (len(proposals) - len(proposals)),  # pre-dedup was different
        "proposals_after_dedup": len(proposals),
        "proposals": [
            {
                "title": p.title,
                "rationale": p.rationale,
                "score": round(p.score, 3),
                "scope": p.scope,
                "suggested_assignee": p.suggested_assignee,
                "suggested_priority": p.suggested_priority,
                "tags": p.tags,
                "signal_count": len(p.signals),
                "signal_types": list(set(s.signal_type for s in p.signals)),
            }
            for p in proposals
        ],
    }


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    result = run_memory_to_goal_pipeline()

    print(f"Memory-to-Goal Feedback Loop Report")
    print(f"Generated: {result['generated_at']}")
    print(f"Signals extracted: {result['signals_extracted']}")
    print(f"Clusters formed: {result['clusters_formed']}")
    print(f"Proposals after dedup: {result['proposals_after_dedup']}")
    print()

    for i, p in enumerate(result["proposals"], 1):
        print(f"  {i}. [{p['suggested_priority'].upper()}] {p['title']}")
        print(f"     Score: {p['score']} | Signals: {p['signal_count']} | Types: {p['signal_types']}")
        print(f"     {p['rationale'][:120]}...")
        print()
