"""
Attention / Salience Routing — Computational Model
===================================================
Concept: When an agent needs context, we don't want to dump all memories.
We want to route only the memories with highest relevance to the current
task/query. Salience = f(semantic_similarity, recency, confidence, importance).

Algorithm:
  salience(m, q) = w1*sim(m, q) + w2*recency(m) + w3*m.confidence + w4*m.importance_proxy

Where:
  sim(m, q)         = cosine similarity via vec_memories (sqlite-vec)
  recency(m)        = exp(-k * days_since_last_recall)
  m.confidence      = stored in memories table
  importance_proxy  = log(1 + recalled_count) / log(1 + max_recalls)

Routing modes:
  - FOCUSED: top-K by salience for single agent query
  - BROADCAST: route different memory subsets to different agents based on scope
  - HIERARCHICAL: surface memories up the chain of command when importance > threshold
"""

import sqlite3
import math
from datetime import datetime, timezone

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"

# Salience weight vector
W_SIMILARITY  = 0.45
W_RECENCY     = 0.25
W_CONFIDENCE  = 0.20
W_IMPORTANCE  = 0.10

RECENCY_DECAY_K = 0.1   # controls recency half-life (~7 days at k=0.1)

ESCALATION_THRESHOLD = 0.85  # salience above this → route to manager too


def days_since(ts_str: str) -> float:
    if not ts_str:
        return 999.0
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def recency_score(last_recalled_at: str, created_at: str) -> float:
    """Exponential recency — 1.0 if just recalled, decays toward 0."""
    ref = last_recalled_at or created_at
    d = days_since(ref)
    return math.exp(-RECENCY_DECAY_K * d)


def importance_proxy(recalled_count: int, max_recalls: int) -> float:
    """Log-normalized recall frequency as importance signal."""
    if max_recalls == 0:
        return 0.0
    return math.log(1 + recalled_count) / math.log(1 + max_recalls)


def compute_salience(
    similarity: float,
    last_recalled_at: str,
    created_at: str,
    confidence: float,
    recalled_count: int,
    max_recalls: int,
) -> float:
    rec = recency_score(last_recalled_at, created_at)
    imp = importance_proxy(recalled_count, max_recalls)
    return (
        W_SIMILARITY * similarity
        + W_RECENCY   * rec
        + W_CONFIDENCE * confidence
        + W_IMPORTANCE * imp
    )


def route_memories_fts(
    conn: sqlite3.Connection,
    query: str,
    agent_id: str = None,
    scope: str = None,
    top_k: int = 10,
    min_salience: float = 0.2,
) -> list[dict]:
    """
    Route memories to an agent using FTS5 for similarity + salience scoring.
    Falls back to FTS when embeddings aren't available for the query.
    """
    conn.row_factory = sqlite3.Row

    # Get max recalled_count for normalization
    max_row = conn.execute("SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL").fetchone()
    max_recalls = max_row[0] or 1

    # FTS5 BM25 similarity search
    fts_query = " OR ".join(f'"{t}"' for t in query.split() if t)
    params = [fts_query]
    scope_clause = ""
    agent_clause = ""

    if scope:
        scope_clause = "AND m.scope = ?"
        params.append(scope)
    if agent_id:
        agent_clause = "AND m.agent_id = ?"
        params.append(agent_id)

    sql = f"""
        SELECT m.id, m.content, m.category, m.confidence, m.temporal_class,
               m.recalled_count, m.last_recalled_at, m.created_at, m.scope, m.agent_id,
               -bm25(memories_fts) AS similarity
        FROM memories m
        JOIN memories_fts ON memories_fts.rowid = m.id
        WHERE memories_fts MATCH ?
          AND m.retired_at IS NULL
          {scope_clause}
          {agent_clause}
        ORDER BY bm25(memories_fts)
        LIMIT ?
    """
    params.append(top_k * 3)  # fetch extra, re-rank by salience

    rows = conn.execute(sql, params).fetchall()

    candidates = []
    for row in rows:
        row = dict(row)
        # Normalize BM25 score (already negated above; range varies)
        raw_sim = min(1.0, row["similarity"] / 10.0)
        sal = compute_salience(
            similarity=raw_sim,
            last_recalled_at=row["last_recalled_at"],
            created_at=row["created_at"],
            confidence=row["confidence"],
            recalled_count=row["recalled_count"],
            max_recalls=max_recalls,
        )
        row["salience"] = round(sal, 4)
        candidates.append(row)

    # Re-rank by salience and apply threshold
    candidates.sort(key=lambda x: x["salience"], reverse=True)
    return [c for c in candidates[:top_k] if c["salience"] >= min_salience]


def route_memories_vec(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    top_k: int = 10,
    scope: str = None,
    min_confidence: float = 0.3,
) -> list[dict]:
    """
    Route memories using sqlite-vec cosine similarity on precomputed embeddings.
    Requires query to be pre-embedded with the same model as vec_memories (dim=768).
    """
    conn.row_factory = sqlite3.Row

    # sqlite-vec KNN query
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    sql = """
        SELECT m.id, m.content, m.category, m.confidence, m.temporal_class,
               m.recalled_count, m.last_recalled_at, m.created_at, m.scope,
               v.distance
        FROM vec_memories v
        JOIN memories m ON m.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
          AND m.retired_at IS NULL
          AND m.confidence >= ?
        ORDER BY v.distance
    """
    params = [vec_str, top_k * 2, min_confidence]
    if scope:
        sql += " AND m.scope = ?"
        params.append(scope)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []  # vec extension not loaded

    max_row = conn.execute("SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL").fetchone()
    max_recalls = max_row[0] or 1

    results = []
    for row in rows:
        row = dict(row)
        # Convert L2 distance to similarity (cosine approx for unit vectors)
        similarity = max(0.0, 1.0 - row["distance"] / 2.0)
        sal = compute_salience(
            similarity=similarity,
            last_recalled_at=row["last_recalled_at"],
            created_at=row["created_at"],
            confidence=row["confidence"],
            recalled_count=row["recalled_count"],
            max_recalls=max_recalls,
        )
        row["salience"] = round(sal, 4)
        row["similarity"] = round(similarity, 4)
        results.append(row)

    results.sort(key=lambda x: x["salience"], reverse=True)
    return results[:top_k]


def should_escalate(salience: float, temporal_class: str) -> bool:
    """
    Determine if a memory is salient enough to push up the chain of command.
    Permanent/long memories above the escalation threshold get routed to managers.
    """
    return salience >= ESCALATION_THRESHOLD and temporal_class in ("permanent", "long")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    results = route_memories_fts(conn, "consolidation cycle schedule", top_k=5)
    for r in results:
        print(f"  [{r['salience']:.3f}] {r['content'][:80]}")
    conn.close()
