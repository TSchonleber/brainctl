#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
salience_routing.py — Production salience-weighted memory routing

Provides hybrid BM25+vector retrieval for the agent memory spine.
Graduates the research prototype (04_attention_salience_routing.py) into
production use by hippocampus.py and brainctl.

Model: salience(m, q) = w_sim*sim(m,q) + w_rec*recency(m) + w_con*confidence + w_imp*importance
"""

from __future__ import annotations

import math
import re
import sqlite3
import struct
import time
import urllib.request
import urllib.error
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# sqlite-vec extension path (Python 3.13 Homebrew install)
VEC_DYLIB = "/opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib"

# Ollama embedding config
OLLAMA_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768

# Salience weight vector
W_SIMILARITY = 0.45
W_RECENCY    = 0.25
W_CONFIDENCE = 0.20
W_IMPORTANCE = 0.10

RECENCY_DECAY_K = 0.1        # ~7-day half-life
RECENCY_DECAY_K_LONG = 0.01  # ~70-day half-life for temporal_class='long'/'permanent'
ESCALATION_THRESHOLD = 0.85  # route up chain-of-command above this

# ---------------------------------------------------------------------------
# Query-type weight profiles (COS-201)
# ---------------------------------------------------------------------------

QUERY_WEIGHTS = {
    "temporal":   dict(similarity=0.25, recency=0.50, confidence=0.15, importance=0.10),
    "factual":    dict(similarity=0.45, recency=0.20, confidence=0.30, importance=0.05),
    "procedural": dict(similarity=0.40, recency=0.15, confidence=0.20, importance=0.25),
    "default":    dict(similarity=0.45, recency=0.25, confidence=0.20, importance=0.10),
}

_TEMPORAL_CUES = re.compile(
    r"\b(yesterday|last\s+\w+|recent|since|when|ago|latest|this\s+week|today)\b",
    re.IGNORECASE,
)
_FACTUAL_CUES = re.compile(
    r"\b(what\s+is|where\s+is|how\s+many|which|what's|the\s+\w+\s+url|what\s+version)\b",
    re.IGNORECASE,
)
_PROCEDURAL_CUES = re.compile(
    r"\b(how\s+to|how\s+do|steps?\s+to|workflow|deploy|run|install|configure|what\s+steps)\b",
    re.IGNORECASE,
)

# Cache for adaptive weights: (base_weights_dict, timestamp)
_ADAPTIVE_WEIGHTS_CACHE: tuple = ({}, 0.0)
_ADAPTIVE_CACHE_TTL = 60.0  # seconds


# ---------------------------------------------------------------------------
# Adaptive weight helpers (COS-201 / COS-336)
# ---------------------------------------------------------------------------

def classify_query(query: str) -> str:
    """Return 'temporal', 'factual', 'procedural', or 'default'."""
    if _TEMPORAL_CUES.search(query):
        return "temporal"
    if _FACTUAL_CUES.search(query):
        return "factual"
    if _PROCEDURAL_CUES.search(query):
        return "procedural"
    return "default"


def _gini(values: list) -> float:
    """Gini coefficient over a list of non-negative values."""
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
    # Trapezoidal Lorenz rule: correct formula includes +1/n offset
    return 1 - 2 * lorenz / (n * total) + 1 / n


def _confidence_entropy(values: list, bins: int = 5) -> float:
    """Discretized entropy of confidence values (0.0 = all same bucket)."""
    if not values:
        return 0.0
    buckets = [int(v * bins) for v in values]
    counts = Counter(buckets)
    total = len(values)
    return -sum((c / total) * math.log(c / total + 1e-9) for c in counts.values())


def compute_adaptive_weights(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    neuro: Optional[dict] = None,
) -> dict:
    """
    Compute adaptive salience weights from store statistics, query type, and
    neuromodulation state. (COS-201 / COS-336)

    Base weights are derived analytically from:
      - R_spread: recency spread in days → W_RECENCY
      - G_recall: Gini coefficient of recalled_count → W_IMPORTANCE
      - H_conf: entropy of confidence distribution → W_CONFIDENCE
      - W_SIMILARITY: gets the remainder

    Then blended 50/50 with query-type profiles and adjusted for neuromod
    org_state (URGENT → +recency, FOCUSED → +similarity) and low avg
    confidence (reduces W_CONFIDENCE, compensates in W_SIMILARITY).

    Returns dict with keys: similarity, recency, confidence, importance,
    plus diagnostic keys prefixed with '_'.

    Base weights are cached for 60s; query/neuromod adjustments are fresh.
    """
    global _ADAPTIVE_WEIGHTS_CACHE
    cache_weights, cache_ts = _ADAPTIVE_WEIGHTS_CACHE
    now_ts = time.monotonic()

    if cache_weights and (now_ts - cache_ts) < _ADAPTIVE_CACHE_TTL:
        base = dict(cache_weights)
        diagnostics = {}
    else:
        rows = conn.execute(
            "SELECT confidence, recalled_count, created_at FROM memories WHERE retired_at IS NULL"
        ).fetchall()

        if not rows:
            base = dict(similarity=0.45, recency=0.25, confidence=0.20, importance=0.10)
            diagnostics = dict(_r_spread_days=0.0, _h_conf=0.0, _g_recall=0.0)
        else:
            confidences = [r[0] for r in rows if r[0] is not None]
            recalls = [float(r[1] or 0) for r in rows]

            now_dt = datetime.now(timezone.utc)
            ages = []
            for r in rows:
                ts = r[2]
                if ts:
                    try:
                        ts = ts.strip()
                        if ts.endswith("Z"):
                            ts = ts[:-1] + "+00:00"
                        if " " in ts and "T" not in ts:
                            ts = ts.replace(" ", "T", 1)
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        ages.append(
                            (now_dt - dt.astimezone(timezone.utc)).total_seconds() / 86400
                        )
                    except Exception:
                        pass

            r_spread = (max(ages) - min(ages)) if len(ages) > 1 else 0.0
            h_conf = _confidence_entropy(confidences)
            g_recall = _gini(recalls)

            w_recency = 0.15 + 0.15 * min(r_spread / 14.0, 1.0)
            # Invert Gini: high inequality → reduce importance weight so
            # dominant memories don't compound their advantage (COS-350).
            w_importance = 0.05 + 0.15 * (1.0 - g_recall)
            w_confidence = 0.15 + 0.10 * min(h_conf / 1.5, 1.0)
            w_similarity = max(1.0 - w_recency - w_importance - w_confidence, 0.20)

            total = w_similarity + w_recency + w_confidence + w_importance
            base = {
                "similarity": round(w_similarity / total, 3),
                "recency": round(w_recency / total, 3),
                "confidence": round(w_confidence / total, 3),
                "importance": round(w_importance / total, 3),
            }
            diagnostics = {
                "_r_spread_days": round(r_spread, 1),
                "_h_conf": round(h_conf, 3),
                "_g_recall": round(g_recall, 3),
            }

        _ADAPTIVE_WEIGHTS_CACHE = (dict(base), now_ts)

    weights = dict(base)

    # --- Query-type profile: blend 50/50 with analytical base ---
    if query:
        qtype = classify_query(query)
        weights["_query_type"] = qtype
        if qtype != "default":
            profile = QUERY_WEIGHTS[qtype]
            for k in ("similarity", "recency", "confidence", "importance"):
                weights[k] = round(0.5 * weights.get(k, 0.0) + 0.5 * profile[k], 3)

    # --- Neuromodulation org_state adjustments ---
    if neuro is None:
        neuro = {}
    org_state = neuro.get("org_state", "normal")
    weights["_org_state"] = org_state

    if org_state == "urgent":
        weights["recency"] = min(1.0, weights.get("recency", 0.25) + 0.10)
        weights["similarity"] = max(0.10, weights.get("similarity", 0.45) - 0.05)
        weights["confidence"] = max(0.05, weights.get("confidence", 0.20) - 0.05)
    elif org_state == "focused":
        weights["similarity"] = min(0.70, weights.get("similarity", 0.45) + 0.10)
        weights["recency"] = max(0.05, weights.get("recency", 0.25) - 0.05)
        weights["importance"] = max(0.05, weights.get("importance", 0.10) - 0.05)

    # --- Low avg confidence: reduce confidence weight, compensate with similarity ---
    try:
        row = conn.execute(
            "SELECT AVG(confidence) FROM memories WHERE retired_at IS NULL"
        ).fetchone()
        avg_conf = row[0] if row and row[0] is not None else 1.0
    except Exception:
        avg_conf = 1.0
    weights["_avg_confidence"] = round(avg_conf, 3)

    if avg_conf < 0.6:
        delta = min(0.05, max(0.0, weights.get("confidence", 0.20) - 0.05))
        weights["confidence"] = max(0.05, weights.get("confidence", 0.20) - delta)
        weights["similarity"] = min(0.70, weights.get("similarity", 0.45) + delta)

    # --- Normalize core weights to sum to 1.0 ---
    core_keys = ("similarity", "recency", "confidence", "importance")
    total = sum(weights.get(k, 0.0) for k in core_keys)
    if total > 0:
        for k in core_keys:
            weights[k] = round(weights.get(k, 0.0) / total, 3)

    weights.update(diagnostics)
    return weights


# ---------------------------------------------------------------------------
# Neuromodulation integration
# ---------------------------------------------------------------------------

def load_neuro_state(conn: sqlite3.Connection) -> dict:
    """
    Load current neuromodulation_state from brain.db.
    Returns a dict with org_state and parameter overrides, or an empty dict
    if the table doesn't exist (pre-migration DB).
    """
    try:
        row = conn.execute("SELECT * FROM neuromodulation_state WHERE id=1").fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def apply_neuro_params(
    neuro: dict,
    top_k: int,
    min_salience: float,
) -> tuple[int, float, float]:
    """
    Apply neuromodulation parameters to routing call.

    Returns (adjusted_top_k, adjusted_min_salience, recency_decay_k).
    """
    if not neuro:
        return top_k, min_salience, RECENCY_DECAY_K

    breadth = neuro.get("retrieval_breadth_multiplier", 1.0)
    adjusted_top_k = max(1, round(top_k * breadth))

    # similarity_threshold_delta shifts the min_salience floor
    threshold_delta = neuro.get("similarity_threshold_delta", 0.0)
    adjusted_min_salience = max(0.0, min(1.0, min_salience + threshold_delta))

    # temporal_lambda from neuromod overrides module default
    recency_k = neuro.get("temporal_lambda", RECENCY_DECAY_K)

    return adjusted_top_k, adjusted_min_salience, recency_k


# ---------------------------------------------------------------------------
# sqlite-vec loader
# ---------------------------------------------------------------------------

def load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into conn. Returns True on success, False if unavailable."""
    try:
        conn.enable_load_extension(True)
        conn.load_extension(VEC_DYLIB)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def get_db_with_vec(db_path: Optional[Path] = None) -> tuple[sqlite3.Connection, bool]:
    """Open brain.db with sqlite-vec if available. Returns (conn, vec_loaded)."""
    if db_path is None:
        db_path = Path.home() / "agentmemory" / "db" / "brain.db"
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    vec_loaded = load_vec_extension(conn)
    return conn, vec_loaded


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_query(text: str) -> Optional[bytes]:
    """Embed query text via Ollama. Returns packed float32 bytes or None."""
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
            if len(vec) != EMBED_DIM:
                return None
            return struct.pack(f"{EMBED_DIM}f", *vec)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _days_since(ts_str: Optional[str]) -> float:
    if not ts_str:
        return 999.0
    try:
        ts = ts_str.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        if " " in ts and "T" not in ts:
            ts = ts.replace(" ", "T", 1)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def _recency_score(
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    temporal_class: Optional[str] = None,
    decay_k: Optional[float] = None,
) -> float:
    # Long/permanent memories use a much softer decay (~70-day half-life)
    if decay_k is None:
        decay_k = RECENCY_DECAY_K_LONG if temporal_class in ("long", "permanent") else RECENCY_DECAY_K
    ref = last_recalled_at or created_at
    return math.exp(-decay_k * _days_since(ref))


def _importance_proxy(recalled_count: int, max_recalls: int) -> float:
    if max_recalls <= 0:
        return 0.0
    return math.log(1 + recalled_count) / math.log(1 + max_recalls)


def compute_salience(
    similarity: float,
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    confidence: float,
    recalled_count: int,
    max_recalls: int,
    weights: Optional[dict] = None,
    temporal_class: Optional[str] = None,
    decay_k: Optional[float] = None,
) -> float:
    """
    Compute salience score for a memory candidate.

    weights: optional dict with keys similarity/recency/confidence/importance
             (from compute_adaptive_weights). Falls back to module-level constants.
    temporal_class: if 'long' or 'permanent', applies softer recency decay.
    decay_k: explicit override for recency decay rate (overrides temporal_class logic).
    """
    w = weights or {}
    ws = w.get("similarity", W_SIMILARITY)
    wr = w.get("recency", W_RECENCY)
    wc = w.get("confidence", W_CONFIDENCE)
    wi = w.get("importance", W_IMPORTANCE)

    rec = _recency_score(last_recalled_at, created_at, temporal_class=temporal_class, decay_k=decay_k)
    imp = _importance_proxy(recalled_count, max_recalls)
    return (
        ws * similarity
        + wr * rec
        + wc * confidence
        + wi * imp
    )


def should_escalate(salience: float, temporal_class: str) -> bool:
    return salience >= ESCALATION_THRESHOLD and temporal_class in ("permanent", "long")


# ---------------------------------------------------------------------------
# FTS-only routing (fallback)
# ---------------------------------------------------------------------------

def _fts_or_query(query: str) -> str:
    tokens = [t.strip('"\' ') for t in query.split() if len(t.strip()) > 1]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def route_memories_fts(
    conn: sqlite3.Connection,
    query: str,
    agent_id: Optional[str] = None,
    scope: Optional[str] = None,
    top_k: int = 10,
    min_salience: float = 0.2,
    weights: Optional[dict] = None,
    neuro: Optional[dict] = None,
) -> list[dict]:
    """Salience-weighted FTS5 routing. Used when vec extension unavailable."""
    fts_query = _fts_or_query(query)
    if not fts_query:
        return []

    if weights is None:
        weights = compute_adaptive_weights(conn, query=query, neuro=neuro)

    max_row = conn.execute(
        "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
    ).fetchone()
    max_recalls = (max_row[0] or 1) if max_row else 1

    params: list = [fts_query]
    extra = ""
    if scope:
        extra += " AND m.scope = ?"
        params.append(scope)
    if agent_id:
        extra += " AND m.agent_id = ?"
        params.append(agent_id)
    params.append(top_k * 3)

    try:
        rows = conn.execute(
            f"""
            SELECT m.id, m.content, m.category, m.confidence, m.temporal_class,
                   m.recalled_count, m.last_recalled_at, m.created_at, m.scope,
                   -bm25(memories_fts) AS bm25_score
            FROM memories m
            JOIN memories_fts ON memories_fts.rowid = m.id
            WHERE memories_fts MATCH ?
              AND m.retired_at IS NULL
              {extra}
            ORDER BY bm25(memories_fts)
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    candidates = []
    for row in rows:
        d = dict(row)
        sim = min(1.0, d["bm25_score"] / 10.0)
        d["salience"] = round(compute_salience(
            similarity=sim,
            last_recalled_at=d["last_recalled_at"],
            created_at=d["created_at"],
            confidence=float(d["confidence"]),
            recalled_count=d["recalled_count"] or 0,
            max_recalls=max_recalls,
            weights=weights,
            temporal_class=d.get("temporal_class"),
        ), 4)
        d["similarity"] = round(sim, 4)
        d["method"] = "fts"
        candidates.append(d)

    candidates.sort(key=lambda x: x["salience"], reverse=True)
    return [c for c in candidates[:top_k] if c["salience"] >= min_salience]


# ---------------------------------------------------------------------------
# Vector routing (requires sqlite-vec loaded)
# ---------------------------------------------------------------------------

def route_memories_vec(
    conn: sqlite3.Connection,
    query_blob: bytes,
    top_k: int = 10,
    scope: Optional[str] = None,
    min_confidence: float = 0.3,
    weights: Optional[dict] = None,
) -> list[dict]:
    """Pure vector KNN routing via sqlite-vec."""
    max_row = conn.execute(
        "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
    ).fetchone()
    max_recalls = (max_row[0] or 1) if max_row else 1

    scope_clause = "AND m.scope = ?" if scope else ""
    params: list = [query_blob, top_k * 2, min_confidence]
    if scope:
        params.append(scope)

    try:
        rows = conn.execute(
            f"""
            SELECT m.id, m.content, m.category, m.confidence, m.temporal_class,
                   m.recalled_count, m.last_recalled_at, m.created_at, m.scope,
                   v.distance
            FROM vec_memories v
            JOIN memories m ON m.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
              AND m.retired_at IS NULL
              AND m.confidence >= ?
              {scope_clause}
            ORDER BY v.distance
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    results = []
    for row in rows:
        d = dict(row)
        sim = max(0.0, 1.0 - d["distance"] / 2.0)
        d["salience"] = round(compute_salience(
            similarity=sim,
            last_recalled_at=d["last_recalled_at"],
            created_at=d["created_at"],
            confidence=float(d["confidence"]),
            recalled_count=d["recalled_count"] or 0,
            max_recalls=max_recalls,
            weights=weights,
            temporal_class=d.get("temporal_class"),
        ), 4)
        d["similarity"] = round(sim, 4)
        d["method"] = "vec"
        results.append(d)

    results.sort(key=lambda x: x["salience"], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Hybrid routing (primary production path)
# ---------------------------------------------------------------------------

def route_memories_hybrid(
    conn: sqlite3.Connection,
    query: str,
    top_k: int = 10,
    scope: Optional[str] = None,
    agent_id: Optional[str] = None,
    min_salience: float = 0.15,
    alpha: float = 0.5,
    vec_available: bool = True,
    neuro: Optional[dict] = None,
) -> list[dict]:
    """
    Hybrid BM25+vector salience routing with adaptive weights and neuromodulation.

    If vec_available=True (sqlite-vec loaded + Ollama reachable), combines
    vector KNN with FTS5 BM25 scores, re-ranks by full salience formula.
    Falls back to FTS-only when vec is unavailable.

    Adaptive weights are computed from store statistics, query type, and
    neuromodulation org_state. (COS-201 / COS-336)

    neuro: optional neuromodulation_state dict from load_neuro_state().
           If provided, adjusts top_k, min_salience, recency decay, and
           salience weights.

    alpha: weight for vector score vs BM25 (0.0 = pure FTS, 1.0 = pure vec).
    """
    # Apply neuromodulation parameter overrides
    if neuro is None:
        neuro = load_neuro_state(conn)
    top_k, min_salience, decay_k = apply_neuro_params(neuro, top_k, min_salience)

    # Compute adaptive weights (COS-336)
    adaptive_weights = compute_adaptive_weights(conn, query=query, neuro=neuro)

    # Try vector path first
    query_blob = None
    if vec_available:
        query_blob = embed_query(query)

    if query_blob is None:
        # Fallback: pure FTS salience with adaptive weights
        results = route_memories_fts(conn, query, agent_id=agent_id,
                                     scope=scope, top_k=top_k,
                                     min_salience=min_salience,
                                     weights=adaptive_weights)
        for r in results:
            r["method"] = "fts_fallback"
        return results

    # Step 1: vector KNN (fetch 3x for re-ranking)
    vec_results = route_memories_vec(conn, query_blob, top_k=top_k * 3,
                                     scope=scope, weights=adaptive_weights)
    vec_map = {r["id"]: r for r in vec_results}

    # Step 2: FTS results (different candidates possible)
    fts_results = route_memories_fts(conn, query, agent_id=agent_id,
                                     scope=scope, top_k=top_k * 3,
                                     min_salience=0.0, weights=adaptive_weights)
    fts_map = {r["id"]: r for r in fts_results}

    # Step 3: merge candidate pools
    all_ids = list({**vec_map, **fts_map}.keys())

    if not all_ids:
        return []

    # Normalize scores within each pool
    def _normalize(vals: list[float]) -> list[float]:
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [1.0] * len(vals)
        return [(v - mn) / (mx - mn) for v in vals]

    vec_saliences = [vec_map[i]["salience"] if i in vec_map else 0.0 for i in all_ids]
    fts_saliences = [fts_map[i]["salience"] if i in fts_map else 0.0 for i in all_ids]
    norm_vec = _normalize(vec_saliences)
    norm_fts = _normalize(fts_saliences)

    combined = []
    for idx, mem_id in enumerate(all_ids):
        src = vec_map.get(mem_id) or fts_map[mem_id]
        hybrid_score = alpha * norm_vec[idx] + (1.0 - alpha) * norm_fts[idx]
        entry = dict(src)
        entry["salience"] = round(hybrid_score, 4)
        entry["method"] = "hybrid"
        combined.append(entry)

    combined.sort(key=lambda x: x["salience"], reverse=True)
    return [c for c in combined[:top_k] if c["salience"] >= min_salience]


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "memory consolidation decay"
    conn, vec_ok = get_db_with_vec()
    print(f"sqlite-vec loaded: {vec_ok}")
    print(f"Query: {query!r}\n")
    results = route_memories_hybrid(conn, query, top_k=5, vec_available=vec_ok)
    for r in results:
        print(f"  [{r['salience']:.3f}] ({r['method']}) {r['content'][:80]}")
    conn.close()
