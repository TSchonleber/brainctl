"""
Built-in W(m) write worthiness gate for brainctl.

Evaluates candidate memories against existing embeddings to determine
if the write is novel enough to proceed. Returns (score, reason, components).

score: float 0.0-1.0 (higher = more worthy)
reason: str (empty string = approved, non-empty = rejection reason)
components: dict of scoring breakdown
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
) -> tuple[float, str, dict]:
    """
    Evaluate write worthiness of a candidate memory.

    Returns:
        (score, reason, components)
        - score: 0.0-1.0 worthiness score
        - reason: empty string if approved, rejection reason if rejected
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
        "identity": 1.0, "convention": 0.9, "decision": 0.9,
        "lesson": 0.8, "preference": 0.7, "project": 0.6,
        "environment": 0.5, "user": 0.5, "integration": 0.5,
    }
    cat_weight = category_weights.get(category, 0.5)

    # Final worthiness score
    score = novelty * 0.5 + importance * 0.3 + cat_weight * 0.2

    components = {
        "novelty": round(novelty, 4),
        "max_similarity": round(max_similarity, 4),
        "neighbor_count": neighbor_count,
        "importance": round(importance, 4),
        "category_weight": round(cat_weight, 4),
        "score": round(score, 4),
    }

    # Rejection threshold: score < 0.15 means near-duplicate with low importance
    if score < 0.15:
        return (round(score, 4), f"Low worthiness ({score:.3f}): near-duplicate content", components)

    return (round(score, 4), "", components)
