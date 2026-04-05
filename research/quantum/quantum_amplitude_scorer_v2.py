#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
quantum_amplitude_scorer_v2.py — Quantum-inspired memory retrieval with phase

Extends quantum_amplitude_scorer.py to use inferred confidence_phase values.
The full quantum amplitude is now:

    α_i = √(confidence) × exp(i × confidence_phase)

Instead of assuming phase = 0 (real amplitude) for all memories.

Author: Phase (COS-392, integrated into COS-383)
Date: 2026-03-28
"""

from __future__ import annotations

import math
import cmath
import sqlite3
import numpy as np
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

# Quantum amplitude parameters
AMPLITUDE_SCALE = 1.0
INTERFERENCE_STRENGTH = 0.3
DECOHERENCE_RATE = 0.05
QUANTUM_WALK_STEPS = 5


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class QuantumMemoryState:
    """Quantum state representation with complex amplitude."""
    memory_id: int
    content: str
    category: str
    confidence: float
    confidence_phase: float  # NEW: phase component of amplitude
    temporal_class: Optional[str]
    recalled_count: int
    last_recalled_at: Optional[str]
    created_at: Optional[str]
    scope: Optional[str]

    # Quantum properties
    amplitude: complex = field(default_factory=lambda: complex(0, 0))
    similarity_phase: float = field(default=0.0)
    confidence_magnitude: float = field(default=0.0)
    graph_interference: complex = field(default_factory=lambda: complex(0, 0))


# ---------------------------------------------------------------------------
# PCA-space Mahalanobis Amplitude (COS-412)
# ---------------------------------------------------------------------------

# Lazy-loaded PCA matrices — loaded once on first call
_pca_matrix: "np.ndarray | None" = None
_pca_mean: "np.ndarray | None" = None
_pca_eigenvalues: "np.ndarray | None" = None

_PCA_DIR = Path.home() / "agentmemory/research/quantum"


def _load_pca_matrices() -> tuple:
    """Load PCA projection matrices from disk (lazy, cached)."""
    global _pca_matrix, _pca_mean, _pca_eigenvalues
    if _pca_matrix is None:
        proj_path = _PCA_DIR / "pca_projection_top159.npy"
        mean_path = _PCA_DIR / "pca_mean_159.npy"
        eigen_path = _PCA_DIR / "pca_eigenvalues_159.npy"
        _pca_matrix = np.load(proj_path)        # (768, 159)
        _pca_mean = np.load(mean_path)          # (768,)
        _pca_eigenvalues = np.load(eigen_path)  # (159,)
    return _pca_matrix, _pca_mean, _pca_eigenvalues


def project_to_pca_space(
    embedding: np.ndarray,
    pca_matrix: np.ndarray,
    pca_mean: np.ndarray,
) -> np.ndarray:
    """Project 768d embedding to 159d PCA space.

    Args:
        embedding: shape (768,)
        pca_matrix: shape (768, 159)
        pca_mean: shape (768,) centering vector
    Returns:
        shape (159,)
    """
    return (embedding - pca_mean) @ pca_matrix


def mahalanobis_amplitude(
    query_embedding: np.ndarray,
    memory_embedding: np.ndarray,
    pca_matrix: "np.ndarray | None" = None,
    pca_mean: "np.ndarray | None" = None,
    eigenvalues: "np.ndarray | None" = None,
) -> float:
    """Compute amplitude using Mahalanobis distance in 159d PCA space.

    Naturally weights each PC by its variance (eigenvalue). Replaces the
    Gaussian cosine kernel previously applied in raw 768d space.

    If pca_matrix/pca_mean/eigenvalues are not provided, loads from disk.

    Args:
        query_embedding:  shape (768,)
        memory_embedding: shape (768,)
        pca_matrix:       shape (768, 159), optional
        pca_mean:         shape (768,), optional
        eigenvalues:      shape (159,), optional

    Returns:
        Amplitude in [0, 1]; identical embeddings → 1.0, distant → ~0.
    """
    if pca_matrix is None or pca_mean is None or eigenvalues is None:
        pca_matrix, pca_mean, eigenvalues = _load_pca_matrices()

    q_pca = project_to_pca_space(query_embedding.astype(np.float64), pca_matrix, pca_mean)
    m_pca = project_to_pca_space(memory_embedding.astype(np.float64), pca_matrix, pca_mean)
    diff = q_pca - m_pca
    # Mahalanobis: sqrt(diff^T Sigma^{-1} diff) where Sigma = diag(eigenvalues)
    weighted_diff = diff / np.sqrt(eigenvalues + 1e-8)
    distance = np.sqrt(np.dot(weighted_diff, weighted_diff))
    return float(np.exp(-distance ** 2 / 2.0))


# ---------------------------------------------------------------------------
# Core Quantum Amplitude Functions (with Phase)
# ---------------------------------------------------------------------------

def amplitude_from_confidence_with_phase(
    confidence: float,
    confidence_phase: float
) -> complex:
    """
    Construct full quantum amplitude with phase.

    The full amplitude is:
        α = √(confidence) × exp(i × confidence_phase)

    Args:
        confidence: Confidence score [0,1] — gives magnitude via √(confidence)
        confidence_phase: Phase angle in radians [0, 2π)

    Returns:
        Complex amplitude
    """
    magnitude = math.sqrt(confidence)
    return magnitude * cmath.exp(1j * confidence_phase)


def amplitude_from_similarity_and_confidence_with_phase(
    similarity: float,
    confidence: float,
    confidence_phase: float
) -> complex:
    """
    Compute memory amplitude with similarity boost and phase.

    The similarity affects both magnitude and phase of the memory's
    amplitude relative to the query.

    Args:
        similarity: Embedding similarity to query [0,1]
        confidence: Memory confidence [0,1]
        confidence_phase: Memory's intrinsic phase [0, 2π)

    Returns:
        Complex amplitude suitable for Born rule calculation
    """
    # Base amplitude from confidence and phase
    base_amplitude = amplitude_from_confidence_with_phase(confidence, confidence_phase)

    # Similarity adds a phase rotation (coherence with query)
    similarity_phase = math.pi * similarity
    similarity_boost = cmath.exp(1j * similarity_phase)

    # Combined: base amplitude modulated by similarity
    magnitude = abs(base_amplitude) * (1.0 + 0.3 * similarity)
    magnitude = min(1.0, magnitude)

    phase = cmath.phase(base_amplitude) + similarity_phase * 0.3

    return magnitude * cmath.exp(1j * phase)


def interference_amplitude_with_phase(
    amplitude_a: complex,
    amplitude_b: complex,
    phase_offset: float = 0.0,
    correlation: float = 0.0
) -> complex:
    """
    Compute interference between two amplitudes with phase consideration.

    When amplitudes have the same phase (aligned), they interfere constructively.
    When phases differ by π (opposite), they interfere destructively.

    Args:
        amplitude_a: First amplitude
        amplitude_b: Second amplitude
        phase_offset: Phase difference between paths [0, 2π)
        correlation: Correlation between paths [0,1]

    Returns:
        Interfered amplitude
    """
    # Weight by correlation
    weighted_sum = (1.0 - correlation) * amplitude_a + correlation * amplitude_b

    # Apply phase offset modulation
    phase_mod = cmath.exp(1j * phase_offset)
    return weighted_sum * phase_mod


def compute_quantum_amplitude_with_phase(
    memory_id: int,
    similarity: float,
    confidence: float,
    confidence_phase: float,
    neighbor_amplitudes: Dict[int, complex],
    neighbor_phases: Dict[int, float],
    edge_weights: Dict[int, float],
    edge_relations: Dict[int, str],
    decoherence: float = DECOHERENCE_RATE
) -> complex:
    """
    Compute quantum amplitude for a memory including phase and graph interference.

    This is the core quantum amplitude calculation that includes:
    1. Base amplitude from confidence and phase
    2. Similarity-induced phase rotation
    3. Graph interference from connected memories

    Args:
        memory_id: Memory identifier
        similarity: Similarity to query [0,1]
        confidence: Memory confidence [0,1]
        confidence_phase: Memory's intrinsic phase [0, 2π)
        neighbor_amplitudes: Dict mapping neighbor_id → complex amplitude
        neighbor_phases: Dict mapping neighbor_id → phase angle
        edge_weights: Dict mapping neighbor_id → edge weight
        edge_relations: Dict mapping neighbor_id → relation type
        decoherence: Decoherence rate for interference decay

    Returns:
        Total complex amplitude for this memory
    """

    # Step 1: Base amplitude from confidence and phase
    base_amplitude = amplitude_from_similarity_and_confidence_with_phase(
        similarity, confidence, confidence_phase
    )

    # Step 2: Graph interference from neighbors
    interference_sum = 0.0

    for neighbor_id, neighbor_amp in neighbor_amplitudes.items():
        weight = edge_weights.get(neighbor_id, 0.0)
        relation_type = edge_relations.get(neighbor_id, 'co_referenced')

        # Phase relationship determines constructive vs destructive
        neighbor_phase = neighbor_phases.get(neighbor_id, 0.0)
        phase_diff = (confidence_phase - neighbor_phase) % (2 * math.pi)

        # Relation type affects interference sign
        if relation_type == 'contradicts':
            # Destructive: amplitudes should oppose
            phase_diff_target = math.pi
        elif relation_type == 'supports':
            # Constructive: amplitudes should align
            phase_diff_target = 0.0
        else:
            # Default: weakly constructive
            phase_diff_target = 0.0

        # Error in phase relationship
        phase_error = (phase_diff - phase_diff_target) % (2 * math.pi)
        if phase_error > math.pi:
            phase_error = 2 * math.pi - phase_error

        # Interference boost/suppression based on phase alignment
        phase_alignment = math.cos(phase_error)  # [-1, 1]

        # Contribution from this neighbor
        contribution = neighbor_amp * weight * phase_alignment
        contribution *= math.exp(-decoherence)  # Decay over distance

        interference_sum += contribution

    # Normalize interference
    interference_amplitude = interference_sum / max(1.0, len(neighbor_amplitudes))

    # Step 3: Combine base amplitude with interference
    total_amplitude = base_amplitude + 0.2 * interference_amplitude

    return total_amplitude


# ---------------------------------------------------------------------------
# Quantum Salience with Phase
# ---------------------------------------------------------------------------

def compute_quantum_salience_with_phase(
    memory_id: int,
    similarity: float,
    confidence: float,
    confidence_phase: float,
    temporal_class: Optional[str],
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    recalled_count: int,
    max_recalls: int,
    conn: sqlite3.Connection,
    graph: Dict[int, List[Tuple[int, float]]],
    phase_map: Dict[int, float]
) -> float:
    """
    Compute quantum-inspired salience score using phase information.

    This is the main scoring function that uses inferred phases from brain.db.

    Args:
        All parameters as before, plus:
        phase_map: Dict mapping memory_id → confidence_phase from database

    Returns:
        Salience score [0, 1]
    """

    # Get neighbor information
    neighbor_ids = [nid for nid, _ in graph.get(memory_id, [])]

    # Build amplitude and phase information for neighbors
    neighbor_amplitudes = {}
    neighbor_phases = {}
    edge_weights = {}
    edge_relations = {}

    if neighbor_ids:
        placeholders = ",".join("?" * len(neighbor_ids))
        rows = conn.execute(f"""
            SELECT id, confidence, confidence_phase
            FROM memories
            WHERE id IN ({placeholders}) AND retired_at IS NULL
        """, neighbor_ids).fetchall()

        for neighbor_id, neighbor_conf, neighbor_phase in rows:
            # Use inferred phase from database
            phase = phase_map.get(neighbor_id, neighbor_phase or 0.0)
            neighbor_phases[neighbor_id] = phase

            # Amplitude: confidence (real) × phase (complex rotation)
            amp = amplitude_from_confidence_with_phase(neighbor_conf, phase)
            neighbor_amplitudes[neighbor_id] = amp

        # Get edge information
        for nid, weight in graph.get(memory_id, []):
            edge_weights[nid] = weight

            relation = conn.execute("""
                SELECT relation_type
                FROM knowledge_edges
                WHERE (source_id = ? AND target_id = ?)
                   OR (source_id = ? AND target_id = ?)
                LIMIT 1
            """, (memory_id, nid, nid, memory_id)).fetchone()

            if relation:
                edge_relations[nid] = relation[0]

    # Get this memory's phase
    memory_phase = phase_map.get(memory_id, confidence_phase)

    # Compute total quantum amplitude
    total_amplitude = compute_quantum_amplitude_with_phase(
        memory_id=memory_id,
        similarity=similarity,
        confidence=confidence,
        confidence_phase=memory_phase,
        neighbor_amplitudes=neighbor_amplitudes,
        neighbor_phases=neighbor_phases,
        edge_weights=edge_weights,
        edge_relations=edge_relations
    )

    # Born rule: probability from squared amplitude magnitude
    probability_from_phase = abs(total_amplitude) ** 2

    # Combined with classical signals
    base_similarity_score = similarity
    confidence_modulation = 0.7 + 0.3 * confidence

    combined_score = base_similarity_score + 0.2 * probability_from_phase
    combined_score = combined_score * confidence_modulation

    # Recency decay
    recency_score = _recency_score(last_recalled_at, created_at, temporal_class)
    final_score = combined_score * recency_score

    return max(0.0, min(1.0, final_score))


def _recency_score(
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    temporal_class: Optional[str] = None
) -> float:
    """Compute recency decay (copied from quantum_amplitude_scorer.py)."""
    RECENCY_DECAY_K = 0.1
    RECENCY_DECAY_K_LONG = 0.01

    decay_k = RECENCY_DECAY_K_LONG if temporal_class in ("long", "permanent") else RECENCY_DECAY_K
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
        days_since = max(0.0, (now - dt).total_seconds() / 86400.0)
        return math.exp(-decay_k * days_since)
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Integration with Brain.db
# ---------------------------------------------------------------------------

def load_knowledge_graph(conn: sqlite3.Connection) -> Dict[int, List[Tuple[int, float]]]:
    """Load the memory knowledge graph from brain.db."""
    graph = defaultdict(list)

    try:
        rows = conn.execute("""
            SELECT source_memory_id, target_memory_id, weight
            FROM knowledge_edges
            WHERE deleted_at IS NULL
        """).fetchall()

        for src_id, tgt_id, weight in rows:
            weight = float(weight) if weight else 1.0
            graph[src_id].append((tgt_id, weight))
            graph[tgt_id].append((src_id, weight))

    except sqlite3.OperationalError:
        # Table might not exist or have different schema
        pass

    return dict(graph)


def load_phase_map(conn: sqlite3.Connection) -> Dict[int, float]:
    """Load confidence_phase values for all memories from database."""
    phase_map = {}

    try:
        rows = conn.execute("""
            SELECT id, confidence_phase
            FROM memories
            WHERE retired_at IS NULL AND confidence_phase IS NOT NULL
        """).fetchall()

        for memory_id, phase in rows:
            phase_map[memory_id] = float(phase) if phase else 0.0

    except sqlite3.OperationalError:
        # Column might not exist yet
        pass

    return phase_map


def route_memories_quantum_with_phase(
    conn: sqlite3.Connection,
    candidate_ids: List[int],
    candidate_data: Dict[int, Dict],
    similarities: Dict[int, float],
    top_k: int = 10,
    min_salience: float = 0.15
) -> List[Dict]:
    """
    Route memories using quantum-inspired amplitude scoring with phase.

    This is the main entry point that wraps existing BM25/vector results
    with quantum amplitude re-ranking using inferred phases.

    Args:
        conn: Database connection
        candidate_ids: Memory IDs from classical retrieval
        candidate_data: Dict mapping memory_id → {content, confidence, ...}
        similarities: Dict mapping memory_id → similarity_score
        top_k: Number of results to return
        min_salience: Minimum salience threshold

    Returns:
        List of dicts with quantum salience scores
    """

    graph = load_knowledge_graph(conn)
    phase_map = load_phase_map(conn)

    results = []
    max_recalls = 1

    try:
        row = conn.execute(
            "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
        ).fetchone()
        max_recalls = (row[0] or 1) if row else 1
    except Exception:
        pass

    for mem_id in candidate_ids:
        if mem_id not in candidate_data:
            continue

        data = candidate_data[mem_id]
        sim = similarities.get(mem_id, 0.0)
        confidence = float(data.get("confidence", 0.5))
        confidence_phase = phase_map.get(mem_id, 0.0)

        # Compute quantum salience WITH phase
        q_salience = compute_quantum_salience_with_phase(
            memory_id=mem_id,
            similarity=sim,
            confidence=confidence,
            confidence_phase=confidence_phase,
            temporal_class=data.get("temporal_class"),
            last_recalled_at=data.get("last_recalled_at"),
            created_at=data.get("created_at"),
            recalled_count=data.get("recalled_count", 0),
            max_recalls=max_recalls,
            conn=conn,
            graph=graph,
            phase_map=phase_map
        )

        if q_salience >= min_salience:
            result = dict(data)
            result["id"] = mem_id
            result["salience"] = round(q_salience, 4)
            result["similarity"] = round(sim, 4)
            result["confidence_phase"] = round(confidence_phase, 4)  # NEW
            result["method"] = "quantum_with_phase"
            results.append(result)

    results.sort(key=lambda x: x["salience"], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Main / Testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # Test: load graph and phases
        graph = load_knowledge_graph(conn)
        phase_map = load_phase_map(conn)

        print(f"Loaded knowledge graph: {len(graph)} nodes")
        print(f"Loaded phase map: {len(phase_map)} memories with phases")

        # Test: compute amplitudes with phase
        test_memory_id = 93  # High-recall memory
        if test_memory_id in phase_map:
            confidence = 1.0
            phase = phase_map[test_memory_id]
            amp = amplitude_from_confidence_with_phase(confidence, phase)
            print(f"\nMemory {test_memory_id} amplitude: {amp:.3f}")
            print(f"  Magnitude (probability amplitude): {abs(amp):.4f}")
            print(f"  Phase: {phase:.4f} rad ({phase*180/math.pi:.1f}°)")

        # Test: quantum walk
        neighbors = graph.get(test_memory_id, [])
        print(f"\nNeighbors of memory {test_memory_id}: {len(neighbors)}")
        if neighbors:
            for neighbor_id, weight in neighbors[:5]:
                neighbor_phase = phase_map.get(neighbor_id, 0.0)
                phase_diff = (phase - neighbor_phase) % (2 * math.pi)
                print(f"  Neighbor {neighbor_id}: weight={weight:.2f}, "
                      f"phase_diff={phase_diff*180/math.pi:.1f}°")

        conn.close()
        print("\n✓ Quantum amplitude scorer v2 (with phase) initialized")

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
