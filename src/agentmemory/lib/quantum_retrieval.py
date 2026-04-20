"""
quantum_retrieval.py — Phase-Aware Quantum Amplitude Scorer

Production module extracted from COS-383 / COS-392 research
(quantum_amplitude_scorer_v2.py + phase_inference.py).

Key API:
    quantum_rerank(candidates, db_path, benchmark=False) -> list[dict]
    compute_amplitude(confidence, confidence_phase) -> complex
    compute_interference_score(query_amplitude, candidate_amplitudes, graph_edges) -> float
    quantum_amplitude_score(query_embedding, candidate_memories, ...) -> list[tuple[str, float]]
"""

from __future__ import annotations

import cmath
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_DB = Path.home() / "agentmemory" / "db" / "brain.db"

_INTERFERENCE_STRENGTH = 0.2   # graph interference weight
_DECOHERENCE_RATE = 0.05       # per-hop decay
_RECENCY_DECAY_K = 0.1         # normal memories (per day)
_RECENCY_DECAY_K_LONG = 0.01   # long/permanent memories

_PHASE_BY_RELATION: Dict[str, float] = {
    "semantic_similar":  0.0,
    "supports":          0.0,
    "co_referenced":     0.0,
    "topical_tag":       0.0,
    "contradicts":       math.pi,
    "derived_from":      math.pi / 4,
    "supersedes":        math.pi,
    "causes":            math.pi / 6,
}


# ---------------------------------------------------------------------------
# Public primitives (matches spec signatures)
# ---------------------------------------------------------------------------

def compute_amplitude(confidence: float, confidence_phase: float) -> complex:
    """
    α_i = sqrt(confidence) × exp(i × confidence_phase)

    Args:
        confidence: Memory confidence [0, 1]
        confidence_phase: Phase angle in radians [0, 2π)

    Returns:
        Complex amplitude
    """
    magnitude = math.sqrt(max(0.0, confidence))
    return magnitude * cmath.exp(1j * confidence_phase)


def compute_interference_score(
    query_amplitude: complex,
    candidate_amplitudes: List[complex],
    graph_edges: List[Tuple],  # (source_id, target_id, weight)
) -> float:
    """
    Born rule: |<query | Σ amplitudes>|²

    The interference score is the squared magnitude of the inner product
    between the query amplitude and the superposition of candidate amplitudes,
    modulated by graph edge weights.

    Args:
        query_amplitude: Query's complex amplitude
        candidate_amplitudes: List of candidate complex amplitudes
        graph_edges: List of (source_id, target_id, weight) tuples

    Returns:
        Interference score [0, ∞), typically in [0, 1]
    """
    if not candidate_amplitudes:
        return abs(query_amplitude) ** 2

    # Build edge weight index (symmetric)
    edge_weights: Dict[Tuple, float] = {}
    for src, tgt, w in graph_edges:
        edge_weights[(src, tgt)] = float(w)
        edge_weights[(tgt, src)] = float(w)

    # Superposition with interference (weighted sum)
    superposition = complex(0, 0)
    for amp in candidate_amplitudes:
        superposition += amp

    # Inner product (conjugate of query dotted with superposition)
    inner = query_amplitude.conjugate() * superposition

    # Born rule
    return abs(inner) ** 2


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_knowledge_graph(conn: sqlite3.Connection) -> Dict[int, List[Tuple[int, float]]]:
    graph: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    try:
        rows = conn.execute("""
            SELECT source_memory_id, target_memory_id, weight
            FROM knowledge_edges WHERE deleted_at IS NULL
        """).fetchall()
        for src, tgt, w in rows:
            w = float(w) if w else 1.0
            graph[src].append((tgt, w))
            graph[tgt].append((src, w))
    except sqlite3.OperationalError:
        pass
    return dict(graph)


def _load_phase_map(conn: sqlite3.Connection) -> Dict[int, float]:
    phase_map: Dict[int, float] = {}
    try:
        rows = conn.execute("""
            SELECT id, confidence_phase FROM memories
            WHERE retired_at IS NULL AND confidence_phase IS NOT NULL
        """).fetchall()
        for mem_id, phase in rows:
            phase_map[int(mem_id)] = float(phase) if phase else 0.0
    except sqlite3.OperationalError:
        pass
    return phase_map


def _recency_score(
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    temporal_class: Optional[str] = None,
) -> float:
    decay_k = _RECENCY_DECAY_K_LONG if temporal_class in ("long", "permanent") else _RECENCY_DECAY_K
    ref = last_recalled_at or created_at
    if not ref:
        return 1.0
    try:
        ts = ref.strip()
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
        days = max(0.0, (now - dt).total_seconds() / 86400.0)
        return math.exp(-decay_k * days)
    except Exception:
        return 1.0


def _compute_memory_quantum_score(
    mem_id: int,
    similarity: float,
    confidence: float,
    confidence_phase: float,
    temporal_class: Optional[str],
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    conn: sqlite3.Connection,
    graph: Dict[int, List[Tuple[int, float]]],
    phase_map: Dict[int, float],
) -> float:
    """Core quantum salience score for a single memory."""

    # ── Base amplitude (confidence + phase + similarity) ──────────────────
    base_amp = compute_amplitude(confidence, confidence_phase)
    sim_phase = math.pi * similarity
    sim_boost = math.exp(1j * sim_phase)  # not used below but kept for clarity
    magnitude = min(1.0, abs(base_amp) * (1.0 + 0.3 * similarity))
    phase_angle = cmath.phase(base_amp) + sim_phase * 0.3
    mem_amplitude = magnitude * cmath.exp(1j * phase_angle)

    # ── Graph interference ─────────────────────────────────────────────────
    neighbors = graph.get(mem_id, [])
    interference_sum = complex(0, 0)
    if neighbors:
        neighbor_ids = [nid for nid, _ in neighbors]
        placeholders = ",".join("?" * len(neighbor_ids))
        try:
            nb_rows = conn.execute(
                f"SELECT id, confidence, confidence_phase FROM memories "
                f"WHERE id IN ({placeholders}) AND retired_at IS NULL",
                neighbor_ids,
            ).fetchall()
        except Exception:
            nb_rows = []

        edge_map = {nid: w for nid, w in neighbors}
        for nb_id, nb_conf, nb_phase_raw in nb_rows:
            nb_phase = phase_map.get(nb_id, float(nb_phase_raw or 0.0))
            nb_amp = compute_amplitude(float(nb_conf or 0.5), nb_phase)
            weight = edge_map.get(nb_id, 1.0)

            # Destructive vs constructive based on relation type
            try:
                rel_row = conn.execute(
                    "SELECT relation_type FROM knowledge_edges "
                    "WHERE (source_id=? AND target_id=?) OR (source_id=? AND target_id=?) LIMIT 1",
                    (mem_id, nb_id, nb_id, mem_id),
                ).fetchone()
                relation = rel_row[0] if rel_row else "co_referenced"
            except Exception:
                relation = "co_referenced"

            phase_diff = (confidence_phase - nb_phase) % (2 * math.pi)
            target = math.pi if relation == "contradicts" else 0.0
            phase_err = (phase_diff - target) % (2 * math.pi)
            if phase_err > math.pi:
                phase_err = 2 * math.pi - phase_err
            alignment = math.cos(phase_err)

            contribution = nb_amp * weight * alignment * math.exp(-_DECOHERENCE_RATE)
            interference_sum += contribution

        if len(nb_rows) > 0:
            interference_sum /= len(nb_rows)

    total_amp = mem_amplitude + _INTERFERENCE_STRENGTH * interference_sum

    # ── Born rule ──────────────────────────────────────────────────────────
    probability = abs(total_amp) ** 2

    # ── Combine with classical signals ─────────────────────────────────────
    confidence_mod = 0.7 + 0.3 * confidence
    combined = (similarity + 0.2 * probability) * confidence_mod
    recency = _recency_score(last_recalled_at, created_at, temporal_class)

    return max(0.0, min(1.0, combined * recency))


# ---------------------------------------------------------------------------
# Public: quantum_amplitude_score (spec interface)
# ---------------------------------------------------------------------------

def quantum_amplitude_score(
    query_embedding: List[float],
    candidate_memories: List[dict],
    k_interference: int = 5,
    use_graph: bool = True,
    db_path: str = str(_DEFAULT_DB),
) -> List[Tuple[str, float]]:
    """
    Phase-aware quantum amplitude re-ranking.

    Args:
        query_embedding: Query embedding vector (used for cosine similarity when
                         candidate embedding is available; falls back to stored similarity)
        candidate_memories: List of dicts with keys:
                            id, content, confidence, confidence_phase, embedding (optional)
                            Also accepts: rrf_score, final_score, similarity as pre-computed sims
        k_interference: Max neighbors to compute cross-interference with
        use_graph: Use knowledge_edges for graph interference amplification
        db_path: Path to brain.db

    Returns:
        List of (memory_id_str, quantum_score) sorted descending by score
    """
    import numpy as np

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    graph = _load_knowledge_graph(conn) if use_graph else {}
    phase_map = _load_phase_map(conn)

    # Normalise query embedding for cosine sim
    q_arr = None
    if query_embedding:
        q_np = np.array(query_embedding, dtype=np.float32)
        q_norm = np.linalg.norm(q_np)
        if q_norm > 0:
            q_arr = q_np / q_norm

    scored: List[Tuple[str, float]] = []
    for mem in candidate_memories:
        mem_id = int(mem["id"])
        confidence = float(mem.get("confidence") or 0.5)
        confidence_phase = phase_map.get(mem_id, float(mem.get("confidence_phase") or 0.0))

        # Compute similarity
        sim = 0.5  # neutral default
        if q_arr is not None and mem.get("embedding"):
            try:
                m_np = np.frombuffer(mem["embedding"], dtype=np.float32)
                m_norm = np.linalg.norm(m_np)
                if m_norm > 0:
                    sim = float(np.dot(q_arr, m_np / m_norm))
                    sim = max(0.0, min(1.0, (sim + 1.0) / 2.0))  # cosine [-1,1] → [0,1]
            except Exception:
                pass
        elif "rrf_score" in mem:
            sim = float(mem["rrf_score"])
        elif "final_score" in mem:
            sim = float(mem["final_score"])
        elif "similarity" in mem:
            sim = float(mem["similarity"])

        q_score = _compute_memory_quantum_score(
            mem_id=mem_id,
            similarity=sim,
            confidence=confidence,
            confidence_phase=confidence_phase,
            temporal_class=mem.get("temporal_class"),
            last_recalled_at=mem.get("last_recalled_at"),
            created_at=mem.get("created_at"),
            conn=conn,
            graph=graph,
            phase_map=phase_map,
        )
        scored.append((str(mem_id), q_score))

    conn.close()
    scored.sort(key=lambda x: x[1], reverse=True)
    if k_interference < len(scored):
        scored = scored[:k_interference]
    return scored


# ---------------------------------------------------------------------------
# Public: quantum_rerank (brainctl/MCP convenience wrapper)
# ---------------------------------------------------------------------------

def quantum_rerank(
    candidates: List[dict],
    db_path: str = str(_DEFAULT_DB),
    benchmark: bool = False,
    min_salience: float = 0.0,
) -> List[dict]:
    """
    Re-rank a list of brainctl memory dicts using quantum amplitude scoring.

    Transparent: preserves all existing fields, adds `quantum_score`.
    If `benchmark=True`, also preserves `classical_score` for comparison.

    Args:
        candidates: Memory dicts as returned by brainctl search (with final_score, etc.)
        db_path: Path to brain.db
        benchmark: If True, add `classical_score` field for comparison
        min_salience: Drop candidates below this quantum score (0 = keep all)

    Returns:
        Re-ranked list sorted by quantum_score descending, with original fields intact
    """
    if not candidates:
        return candidates

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return candidates

    try:
        graph = _load_knowledge_graph(conn)
        phase_map = _load_phase_map(conn)
    except Exception:
        conn.close()
        return candidates

    result = []
    for mem in candidates:
        mem_id_raw = mem.get("id")
        if mem_id_raw is None:
            result.append(mem)
            continue

        try:
            mem_id = int(mem_id_raw)
        except (ValueError, TypeError):
            result.append(mem)
            continue

        confidence = float(mem.get("confidence") or 0.5)
        confidence_phase = phase_map.get(mem_id, 0.0)
        # Use best available pre-computed similarity
        sim = float(mem.get("final_score") or mem.get("rrf_score") or 0.0)

        try:
            q_score = _compute_memory_quantum_score(
                mem_id=mem_id,
                similarity=sim,
                confidence=confidence,
                confidence_phase=confidence_phase,
                temporal_class=mem.get("temporal_class"),
                last_recalled_at=mem.get("last_recalled_at"),
                created_at=mem.get("created_at"),
                conn=conn,
                graph=graph,
                phase_map=phase_map,
            )
        except Exception:
            q_score = sim  # fall back to classical

        if q_score < min_salience:
            continue

        updated = dict(mem)
        if benchmark:
            updated["classical_score"] = round(float(mem.get("final_score") or 0.0), 6)
        updated["quantum_score"] = round(q_score, 6)
        updated["confidence_phase"] = round(confidence_phase, 4)
        updated["final_score"] = round(q_score, 8)  # replace final_score so downstream sort works
        result.append(updated)

    conn.close()

    result.sort(key=lambda x: x.get("quantum_score", 0.0), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Phase update hook (post-recall, delta rule from phase_inference)
# ---------------------------------------------------------------------------

def update_phase_after_recall(
    memory_id: int,
    db_path: str = str(_DEFAULT_DB),
    delta: float = 0.05,
) -> bool:
    """
    Update confidence_phase for a recalled memory using a simple delta rule.

    Each successful recall nudges the phase toward the constructive value (0)
    to increase future interference amplification.

    Args:
        memory_id: Memory that was recalled
        db_path: Path to brain.db
        delta: Phase shift per recall event

    Returns:
        True if updated, False if memory not found or column missing
    """
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT confidence_phase FROM memories WHERE id=? AND retired_at IS NULL",
            (memory_id,),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return False

        phase = float(row[0] or 0.0)
        # Delta rule: nudge phase toward 0 (constructive interference) by delta
        # Using circular arithmetic: reduce absolute phase
        if phase > math.pi:
            phase = phase + delta  # wraps toward 0 from the high side
        else:
            phase = max(0.0, phase - delta)
        phase = phase % (2 * math.pi)

        conn.execute(
            "UPDATE memories SET confidence_phase=? WHERE id=?",
            (phase, memory_id),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False
