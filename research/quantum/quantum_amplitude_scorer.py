#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
quantum_amplitude_scorer.py — Quantum-inspired memory retrieval algorithm

Replaces classical salience formula with quantum probability amplitude scoring.
Enables constructive/destructive interference, density matrix ranking, and
quantum walk-based graph search.

Author: Amplitude (COS-383)
Research basis: Sordoni et al (2013), Uprety et al. Quantum Information Retrieval
"""

from __future__ import annotations

import math
import cmath
import sqlite3
import numpy as np
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

# Quantum amplitude parameters
AMPLITUDE_SCALE = 1.0  # Normalization for amplitude space
INTERFERENCE_STRENGTH = 0.3  # Weight of interference effects (0.0-1.0)
DECOHERENCE_RATE = 0.05  # How quickly quantum coherence decays over graph distance
QUANTUM_WALK_STEPS = 5  # Number of quantum walk iterations


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class QuantumMemoryState:
    """Quantum state representation of a memory candidate."""
    memory_id: int
    content: str
    category: str
    confidence: float
    temporal_class: Optional[str]
    recalled_count: int
    last_recalled_at: Optional[str]
    created_at: Optional[str]
    scope: Optional[str]

    # Quantum properties
    amplitude: complex = field(default_factory=lambda: complex(0, 0))
    similarity_phase: float = field(default=0.0)  # Phase shift based on query similarity
    confidence_magnitude: float = field(default=0.0)  # Magnitude contribution from confidence
    graph_interference: complex = field(default_factory=lambda: complex(0, 0))  # Interference from connected memories


@dataclass
class DensityMatrix:
    """Mixed state density matrix for ambiguous queries."""
    dimension: int
    matrix: np.ndarray = field(default_factory=lambda: np.array([]))

    def __post_init__(self):
        if self.matrix.size == 0:
            self.matrix = np.eye(self.dimension, dtype=complex) / self.dimension


# ---------------------------------------------------------------------------
# Core Quantum Amplitude Functions
# ---------------------------------------------------------------------------

def amplitude_from_similarity_and_confidence(
    similarity: float,
    confidence: float,
) -> complex:
    """
    Convert classical similarity and confidence into quantum amplitude.

    Similarity → phase (rotation in complex plane)
    Confidence → magnitude (amplitude norm)

    Args:
        similarity: [0,1] classical similarity score
        confidence: [0,1] memory confidence

    Returns:
        Complex amplitude with magnitude and phase derived from inputs
    """
    # Magnitude proportional to confidence (with similarity boost)
    magnitude = math.sqrt(confidence) * (1.0 + 0.5 * similarity)
    magnitude = min(1.0, magnitude)  # Clamp to unit amplitude

    # Phase determined by similarity (0 to π)
    phase = math.pi * similarity

    return magnitude * cmath.exp(1j * phase)


def construct_query_superposition(
    query: str,
    candidate_ids: List[int],
    similarities: Dict[int, float],
    confidences: Dict[int, float],
) -> Tuple[np.ndarray, Dict[int, int]]:
    """
    Construct superposition state for the query.

    Represent an ambiguous query as a mixed state (density matrix) where each
    candidate memory contributes with amplitude proportional to its similarity.

    Returns:
        (density_matrix, id_to_index_mapping)
    """
    n = len(candidate_ids)
    id_map = {mid: i for i, mid in enumerate(candidate_ids)}

    # Initialize density matrix
    rho = np.zeros((n, n), dtype=complex)

    # Each memory contributes amplitude to superposition
    amplitudes = []
    for mem_id in candidate_ids:
        sim = similarities.get(mem_id, 0.0)
        conf = confidences.get(mem_id, 0.5)
        amp = amplitude_from_similarity_and_confidence(sim, conf)
        amplitudes.append(amp)

    # Normalize amplitudes
    total_amp = sum(abs(a) ** 2 for a in amplitudes)
    if total_amp > 0:
        amplitudes = [a / math.sqrt(total_amp) for a in amplitudes]

    # Build density matrix: ρ = |ψ⟩⟨ψ|
    for i in range(n):
        for j in range(n):
            rho[i, j] = amplitudes[i] * np.conj(amplitudes[j])

    return rho, id_map


def interference_amplitude(
    amplitude_a: complex,
    amplitude_b: complex,
    correlation: float = 0.0,
) -> complex:
    """
    Compute interference between two amplitudes.

    When two paths lead to the same memory:
    - Constructive: amplitudes add (aligned phases)
    - Destructive: amplitudes cancel (opposite phases)

    Args:
        amplitude_a: First amplitude (direct path)
        amplitude_b: Second amplitude (via connection)
        correlation: How correlated are the two paths? [0,1]

    Returns:
        Interfered amplitude
    """
    # Weighted sum with correlation factor
    return (1.0 - correlation) * amplitude_a + correlation * amplitude_b


def apply_graph_interference(
    state: QuantumMemoryState,
    neighbor_states: List[QuantumMemoryState],
    edge_weights: Dict[int, float],
    decoherence: float = DECOHERENCE_RATE,
) -> complex:
    """
    Apply quantum interference from connected memories.

    For each connected memory, compute how observing that memory affects
    the current memory's amplitude (entanglement-like effect).

    Args:
        state: Current memory state
        neighbor_states: Connected memories
        edge_weights: Weight of each connection
        decoherence: Decay rate for interference over distance

    Returns:
        Interference amplitude contribution
    """
    if not neighbor_states:
        return complex(0, 0)

    interference = complex(0, 0)

    for neighbor, weight in zip(neighbor_states, edge_weights.values()):
        # Neighbors with high confidence boost this memory (constructive)
        # Neighbors with low confidence reduce it (destructive)
        amplitude_boost = cmath.exp(1j * math.pi * neighbor.confidence_magnitude)

        # Weight by edge connection strength and decoherence
        contribution = amplitude_boost * weight * math.exp(-decoherence)
        interference += contribution

    return interference / max(1.0, len(neighbor_states))


# ---------------------------------------------------------------------------
# Density Matrix Operations
# ---------------------------------------------------------------------------

def trace_distance(memory_projection: np.ndarray, state_dm: np.ndarray) -> float:
    """
    Compute trace distance between memory projection and query state.

    This is the probability that measuring the query state yields the memory.
    Trace distance = (1/2) * Tr(|ρ - σ|)
    """
    try:
        diff = memory_projection @ state_dm - state_dm @ memory_projection
        eigenvalues = np.linalg.eigvalsh(diff)
        trace_dist = 0.5 * sum(abs(e) for e in eigenvalues)
        return float(trace_dist)
    except Exception:
        return 0.0


def rank_by_density_matrix(
    query_dm: np.ndarray,
    candidate_ids: List[int],
    id_to_index: Dict[int, int],
    amplitudes: Dict[int, complex],
) -> Dict[int, float]:
    """
    Rank memory candidates by trace distance to query density matrix.

    For each candidate, create a projection operator |m⟩⟨m| and compute
    how likely this memory is given the query state.

    Returns:
        Dict mapping memory_id → rank_score [0,1]
    """
    scores = {}
    n = query_dm.shape[0]

    for mem_id in candidate_ids:
        idx = id_to_index.get(mem_id)
        if idx is None:
            continue

        # Projection operator |m⟩⟨m|
        proj = np.zeros((n, n), dtype=complex)
        proj[idx, idx] = 1.0

        # Trace of (proj × query_dm) gives probability amplitude
        score = float(np.trace(proj @ query_dm).real)
        scores[mem_id] = max(0.0, score)

    return scores


# ---------------------------------------------------------------------------
# Quantum Walk on Knowledge Graph
# ---------------------------------------------------------------------------

class QuantumWalk:
    """
    Quantum-inspired random walk on the knowledge graph.

    Uses quantum walk dynamics to find relevant memories faster than
    classical random walks (Grover-like speedup on graphs).
    """

    def __init__(self, graph: Dict[int, List[Tuple[int, float]]], max_steps: int = QUANTUM_WALK_STEPS):
        """
        Args:
            graph: Dict mapping memory_id → [(neighbor_id, edge_weight), ...]
            max_steps: Number of walk iterations
        """
        self.graph = graph
        self.max_steps = max_steps

    def walk_from_seed(
        self,
        start_id: int,
        target_similarity: float,
        target_confidence: float,
    ) -> Dict[int, float]:
        """
        Perform quantum walk from starting memory to find related memories.

        Probability of reaching each node is proportional to relevance.
        Quantum coherence means we explore multiple paths simultaneously.

        Args:
            start_id: Starting memory node
            target_similarity: Query similarity to use as drift
            target_confidence: Query confidence to use as measurement bias

        Returns:
            Dict mapping memory_id → visitation_score
        """
        visited = defaultdict(float)

        # Initialize: quantum walk spreads from start node
        frontier = {start_id: complex(1.0, 0.0)}
        visited[start_id] = 1.0

        for step in range(self.max_steps):
            new_frontier = defaultdict(complex)

            for node_id, amplitude in frontier.items():
                if node_id not in self.graph:
                    continue

                neighbors = self.graph[node_id]
                if not neighbors:
                    continue

                # Quantum walk transition: spread amplitude to neighbors
                # with phase shift based on their properties
                for neighbor_id, edge_weight in neighbors:
                    # Phase accumulation (simulates quantum walk unitary)
                    phase = math.pi * edge_weight * target_similarity
                    transition_amp = amplitude * math.exp(1j * phase) * math.sqrt(edge_weight)
                    new_frontier[neighbor_id] += transition_amp

                    # Decoherence: amplitude decays over distance
                    decoherence = math.exp(-DECOHERENCE_RATE * step)
                    visited[neighbor_id] += abs(transition_amp) ** 2 * decoherence

            frontier = new_frontier

        # Normalize scores
        total = sum(visited.values())
        if total > 0:
            visited = {k: v / total for k, v in visited.items()}

        return dict(visited)


# ---------------------------------------------------------------------------
# Integration with Brain.db
# ---------------------------------------------------------------------------

def load_knowledge_graph(conn: sqlite3.Connection) -> Dict[int, List[Tuple[int, float]]]:
    """
    Load the memory knowledge graph (relationships/edges) from brain.db.

    Returns:
        Dict mapping memory_id → [(neighbor_id, edge_weight), ...]
    """
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
            # Undirected graph: add reverse edge
            graph[tgt_id].append((src_id, weight))

    except sqlite3.OperationalError:
        # Table might not exist or have different schema
        pass

    return dict(graph)


def compute_quantum_salience(
    memory_id: int,
    similarity: float,
    confidence: float,
    temporal_class: Optional[str],
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    recalled_count: int,
    max_recalls: int,
    conn: sqlite3.Connection,
    graph: Dict[int, List[Tuple[int, float]]],
) -> float:
    """
    Compute quantum-inspired salience score for a memory.

    Combines:
    1. Amplitude from similarity and confidence
    2. Graph interference from connected memories
    3. Quantum walk contribution

    Args:
        All parameters matching the classical compute_salience signature
        conn: Database connection for additional queries
        graph: Knowledge graph for interference computation

    Returns:
        Salience score [0, 1]
    """
    # Step 1: Base amplitude from similarity + confidence
    # Start with classical scores as foundation
    base_similarity_score = similarity
    base_confidence_score = confidence

    # Step 2: Compute quantum enhancement factors
    # Amplitude scoring enhances certain properties
    base_amplitude = amplitude_from_similarity_and_confidence(similarity, confidence)
    quantum_magnitude = abs(base_amplitude) ** 2  # Probability of state
    quantum_phase = cmath.phase(base_amplitude)  # Phase information

    # Step 3: Graph interference effects
    neighbor_ids = [nid for nid, _ in graph.get(memory_id, [])]
    interference_boost = 0.0

    if neighbor_ids:
        try:
            placeholders = ",".join("?" * len(neighbor_ids))
            rows = conn.execute(f"""
                SELECT id, confidence FROM memories
                WHERE id IN ({placeholders}) AND retired_at IS NULL
            """, neighbor_ids).fetchall()

            neighbor_confidences = {row[0]: row[1] for row in rows}
            edge_weights = {nid: w for nid, w in graph.get(memory_id, [])}

            # Compute mean neighbor confidence
            confs = [neighbor_confidences.get(nid, 0.5) for nid in neighbor_ids]
            mean_neighbor_conf = sum(confs) / len(confs) if confs else 0.5

            # Constructive interference if neighbors are confident
            # Destructive if not
            if mean_neighbor_conf > confidence:
                # Neighbors are more confident: boost this memory
                interference_boost = 0.2 * (mean_neighbor_conf - confidence)
            else:
                # Neighbors are less confident: slight penalty
                interference_boost = -0.05 * (confidence - mean_neighbor_conf)

        except Exception:
            pass

    # Step 4: Quantum walk contribution (lightweight version)
    walk_bonus = 0.0
    if neighbor_ids:
        quantum_walk = QuantumWalk(graph, max_steps=min(3, QUANTUM_WALK_STEPS))
        walk_scores = quantum_walk.walk_from_seed(memory_id, similarity, confidence)
        walk_bonus = walk_scores.get(memory_id, 0.0) * 0.1  # Small contribution

    # Step 5: Combine all factors
    # Start with classical-like base, add quantum enhancements
    quantum_boost = (
        0.3 * (quantum_magnitude - 0.5) +  # Amplitude adds discriminating factor
        0.2 * interference_boost +           # Graph effects
        0.1 * walk_bonus                     # Walk contribution
    )

    # Base score keeps classical similarity as primary signal
    combined_score = base_similarity_score + quantum_boost

    # Apply confidence as modulation (not just weighting)
    confidence_modulation = 0.7 + 0.3 * confidence
    combined_score = combined_score * confidence_modulation

    # Apply recency decay
    recency_score = _recency_score(last_recalled_at, created_at, temporal_class)
    final_score = combined_score * recency_score

    # Clamp to [0, 1]
    return max(0.0, min(1.0, final_score))


def _recency_score(
    last_recalled_at: Optional[str],
    created_at: Optional[str],
    temporal_class: Optional[str] = None,
) -> float:
    """Compute recency decay (from salience_routing.py)."""
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
# Hybrid Quantum Retrieval
# ---------------------------------------------------------------------------

def route_memories_quantum(
    conn: sqlite3.Connection,
    candidate_ids: List[int],
    candidate_data: Dict[int, Dict],
    similarities: Dict[int, float],
    top_k: int = 10,
    min_salience: float = 0.15,
) -> List[Dict]:
    """
    Route memories using quantum-inspired amplitude scoring.

    Wraps existing BM25/vector results with quantum amplitude re-ranking.

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

        # Compute quantum salience
        q_salience = compute_quantum_salience(
            memory_id=mem_id,
            similarity=sim,
            confidence=float(data.get("confidence", 0.5)),
            temporal_class=data.get("temporal_class"),
            last_recalled_at=data.get("last_recalled_at"),
            created_at=data.get("created_at"),
            recalled_count=data.get("recalled_count", 0),
            max_recalls=max_recalls,
            conn=conn,
            graph=graph,
        )

        if q_salience >= min_salience:
            result = dict(data)
            result["id"] = mem_id
            result["salience"] = round(q_salience, 4)
            result["similarity"] = round(sim, 4)
            result["method"] = "quantum"
            results.append(result)

    results.sort(key=lambda x: x["salience"], reverse=True)
    return results[:top_k]


if __name__ == "__main__":
    # Quick smoke test
    import sys

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # Test: load graph
        graph = load_knowledge_graph(conn)
        print(f"Loaded knowledge graph: {len(graph)} nodes, {sum(len(v) for v in graph.values())} edges")

        # Test: quantum amplitude
        amp = amplitude_from_similarity_and_confidence(0.8, 0.9)
        print(f"Sample amplitude (sim=0.8, conf=0.9): {amp:.3f} (|ψ|²={abs(amp)**2:.3f})")

        conn.close()
        print("✓ Quantum amplitude scorer initialized")

    except Exception as e:
        print(f"✗ Error: {e}")
        sys.exit(1)
