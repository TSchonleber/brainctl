"""
Built-in W(m) write worthiness gate for brainctl.

Evaluates candidate memories against existing embeddings to determine
if the write is novel enough to proceed. Returns (score, reason, components).

score: float 0.0-1.0 (higher = more worthy)
reason: str (empty string = approved, non-empty = rejection reason)
components: dict of scoring breakdown

D-MEM RPE routing (issue #31):
  score < 0.3  → SKIP       (discard, no insert)
  0.3 ≤ score < 0.7 → CONSTRUCT_ONLY  (write, no embedding/FTS)
  score ≥ 0.7  → FULL_EVOLUTION  (write + embed + FTS)

Long-term utility added as a scoring component:
  - category_weight: identity/decision > lesson > convention > general
  - scope_weight: agent-scoped > project-scoped > global
  - recall_rate: historical avg recall_rate from memory_stats (if available)
"""

import struct
import math


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(x * x for x in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def gate_write(
    candidate_blob: bytes,
    confidence: float,
    temporal_class: str | None,
    category: str,
    scope: str,
    db_vec,
    force: bool = False,
    arousal_gain: float = 1.0,
    db_stats=None,
    agent_id: str | None = None,
) -> tuple[float, str, dict]:
    """
    Evaluate write worthiness of a candidate memory.

    Args:
        db_stats: optional sqlite3 connection to the main brain DB (for memory_stats lookup)
        agent_id: agent writing the memory (for memory_stats lookup)

    Returns:
        (score, reason, components)
        - score: 0.0-1.0 worthiness score
        - reason: empty string if approved, rejection reason if rejected (score < 0.3)
        - components: breakdown dict for diagnostics
    """
    if force:
        return (1.0, "", {"forced": True})

    n_dims = len(candidate_blob) // 4
    cand_vec = list(struct.unpack(f"{n_dims}f", candidate_blob[:n_dims * 4]))

    # Find nearest neighbors in existing embeddings
    max_similarity = 0.0
    neighbor_count = 0
    try:
        rows = db_vec.execute(
            "SELECT rowid FROM vec_memories WHERE embedding MATCH ? AND k=?",
            (candidate_blob, 10)
        ).fetchall()
        for row in rows:
            rid = row[0] if isinstance(row, tuple) else row["rowid"]
            e = db_vec.execute(
                "SELECT vector FROM embeddings WHERE source_table='memories' AND source_id=?",
                (rid,)
            ).fetchone()
            if e:
                v_bytes = bytes(e[0] if isinstance(e, tuple) else e["vector"])
                n2 = len(v_bytes) // 4
                v2 = list(struct.unpack(f"{n2}f", v_bytes[:n2 * 4]))
                sim = _cosine_similarity(cand_vec, v2)
                max_similarity = max(max_similarity, sim)
                neighbor_count += 1
    except Exception:
        # vec table may not exist — treat as fully novel
        pass

    # Scoring components
    novelty = 1.0 - max_similarity
    importance = confidence
    category_weights = {
        "identity": 1.0, "decision": 0.95, "lesson": 0.85,
        "convention": 0.80, "preference": 0.70, "project": 0.65,
        "environment": 0.50, "user": 0.50, "integration": 0.50,
    }
    cat_weight = category_weights.get(category, 0.50)

    # Scope specificity weight (D-MEM long-term utility component)
    # Agent-scoped memories are more specific → higher utility
    scope_weight = 0.50
    if scope and scope.startswith("agent:"):
        scope_weight = 1.0
    elif scope and scope.startswith("project:"):
        scope_weight = 0.75

    # Historical recall rate from memory_stats (if DB available)
    recall_rate = 0.50
    if db_stats is not None and agent_id:
        try:
            row = db_stats.execute(
                "SELECT avg_recall_rate FROM memory_stats "
                "WHERE agent_id = ? AND category = ? AND scope = ?",
                (agent_id, category, scope or "global"),
            ).fetchone()
            if row:
                recall_rate = row[0] if isinstance(row, tuple) else row["avg_recall_rate"]
        except Exception:
            pass

    # Long-term utility: geometric mean of category weight, scope weight, recall rate
    long_term_utility = math.pow(cat_weight * scope_weight * recall_rate, 1.0 / 3.0)

    # D-MEM RPE = semantic_surprise × long_term_utility
    # Blend: novelty (surprise) 45% + long_term_utility 25% + importance 20% + scope_weight 10%
    base_score = (novelty * 0.45) + (long_term_utility * 0.25) + (importance * 0.20) + (scope_weight * 0.10)
    gain = max(0.5, min(2.0, arousal_gain))  # clamp to [0.5, 2.0]
    score = min(1.0, base_score * gain)

    components = {
        "novelty": round(novelty, 4),
        "max_similarity": round(max_similarity, 4),
        "neighbor_count": neighbor_count,
        "importance": round(importance, 4),
        "category_weight": round(cat_weight, 4),
        "scope_weight": round(scope_weight, 4),
        "recall_rate": round(recall_rate, 4),
        "long_term_utility": round(long_term_utility, 4),
        "arousal_gain": round(gain, 4),
        "base_score": round(base_score, 4),
        "score": round(score, 4),
    }

    # SKIP threshold (D-MEM: RPE < 0.3 → discard)
    if score < 0.3:
        return (round(score, 4), f"Low worthiness ({score:.3f}): near-duplicate or low-utility content", components)

    return (round(score, 4), "", components)
