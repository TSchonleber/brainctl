"""
Quantum Decoherence Model Implementation Sketch for Brain.db

This module provides methods for:
1. Computing quantum purity of memories
2. Estimating decoherence rates from historical data
3. Identifying pointer states
4. Recovering information from decoherence using syndrome data
5. Adaptive decoherence rate calculation

Status: Research prototype. Not integrated into brain.db consolidation yet.
"""

import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import sqlite3


@dataclass
class MemoryState:
    """A memory represented as a quantum-like state."""
    id: str
    embedding: np.ndarray  # 768-dimensional vector
    confidence: float  # Interpreted as |ψ|²
    temporal_class: str  # ephemeral, short, medium, long, permanent
    timestamp: float
    contradictions_count: int
    citation_count: int
    source_trust: float


def compute_memory_purity(memory: MemoryState, knowledge_graph: Dict) -> float:
    """
    Compute quantum purity of a memory state.

    Purity = (confidence)² + Σ_neighbors [edge_weight * neighbor_confidence]²

    Purity = 1: Pure state (well-defined, isolated from contradictions)
    Purity → 0: Mixed state (entangled with many competing beliefs)

    Args:
        memory: The memory state to evaluate
        knowledge_graph: Dict mapping memory_id -> {edges: [(neighbor_id, weight)]}

    Returns:
        Purity value in [0, 1]
    """
    # Diagonal term: memory's own confidence squared
    purity = (memory.confidence) ** 2

    # Off-diagonal: entanglement with neighbors
    edges = knowledge_graph.get(memory.id, {}).get('edges', [])

    for neighbor_id, edge_weight in edges:
        # Note: In real implementation, fetch neighbor.confidence from DB
        # For now, assume neighbors have avg confidence 0.7
        neighbor_confidence = 0.7  # Placeholder
        purity += (edge_weight * neighbor_confidence) ** 2

    # Normalize
    num_neighbors = len(edges)
    purity /= (1 + num_neighbors) if num_neighbors > 0 else 1

    return min(purity, 1.0)


def compute_decoherence_rate_base(temporal_class: str) -> float:
    """
    Base decoherence rate λ_base per temporal class.

    Current brain.db values (from Wave 1):
    - Ephemeral: λ = 0.5 (decay half-life ~ 1.4 seconds)
    - Short-term: λ = 0.2 (decay half-life ~ 3.5 seconds)
    - Medium-term: λ = 0.05 (decay half-life ~ 14 seconds)
    - Long-term: λ = 0.01 (decay half-life ~ 70 seconds)
    - Permanent: λ = 0 (no decay)
    """
    rates = {
        'ephemeral': 0.5,
        'short': 0.2,
        'medium': 0.05,
        'long': 0.01,
        'permanent': 0.0
    }
    return rates.get(temporal_class, 0.02)


def compute_adaptive_decoherence_rate(
    memory: MemoryState,
    alpha: float = 0.3,    # Contradiction amplification
    beta: float = 0.5,     # Citation protection
    gamma: float = 0.2     # Trust protection
) -> float:
    """
    Compute context-dependent decoherence rate λ_eff(t).

    λ_eff = λ_base × (1 + α × contradictions)
                    × (1 - β × citation_frequency)
                    × (1 - γ × source_trust)

    This predicts that:
    - Many contradictions → faster decoherence
    - Many citations → slower decoherence (protected by environment)
    - High source trust → slower decoherence

    Args:
        memory: The memory state
        alpha: Contradiction impact coefficient
        beta: Citation protection coefficient
        gamma: Trust protection coefficient

    Returns:
        Adaptive decoherence rate λ_eff
    """
    lambda_base = compute_decoherence_rate_base(memory.temporal_class)

    # Normalize counts (assume max 10 contradictions, 100 citations for scaling)
    contradiction_factor = memory.contradictions_count / 10.0
    citation_factor = min(memory.citation_count / 100.0, 1.0)
    trust_factor = memory.source_trust

    lambda_eff = (
        lambda_base
        * (1 + alpha * contradiction_factor)
        * (1 - beta * citation_factor)
        * (1 - gamma * trust_factor)
    )

    return max(0, lambda_eff)  # Clamp to non-negative


def predict_confidence_quantum_decay(
    memory: MemoryState,
    time_delta: float
) -> float:
    """
    Predict confidence decay using quantum decoherence model.

    Simple case: exponential decay with adaptive rate
    confidence(t) = confidence(t_0) × e^(-λ_eff × Δt)

    More complex case: would solve Lindblad equation numerically.

    Args:
        memory: Current memory state
        time_delta: Time elapsed (in seconds)

    Returns:
        Predicted confidence at time t_0 + time_delta
    """
    lambda_eff = compute_adaptive_decoherence_rate(memory)

    # Exponential decay with adaptive rate
    decayed_confidence = memory.confidence * np.exp(-lambda_eff * time_delta)

    return max(0, min(decayed_confidence, 1.0))  # Clamp to [0, 1]


def identify_pointer_states(
    memories: List[MemoryState],
    knowledge_graph: Dict,
    in_degree_threshold: int = 3,
    citation_threshold: float = 0.5
) -> List[str]:
    """
    Identify memories that are "pointer states" (resistant to decoherence).

    Pointer states are:
    - Heavily connected (high in-degree in knowledge graph)
    - Frequently cited (high recall/usage rate)
    - Protected by the environment through positive interactions

    Args:
        memories: List of memory states
        knowledge_graph: Knowledge graph with edge information
        in_degree_threshold: Minimum edge count to be a pointer state
        citation_threshold: Minimum citation frequency (0-1)

    Returns:
        List of memory IDs that are pointer states
    """
    pointer_states = []

    for memory in memories:
        edges = knowledge_graph.get(memory.id, {}).get('edges', [])
        in_degree = len(edges)

        # Compute citation frequency
        citation_frequency = min(memory.citation_count / 100.0, 1.0)

        # Criteria: high in-degree AND high citation
        is_pointer_state = (
            in_degree >= in_degree_threshold and
            citation_frequency >= citation_threshold
        )

        if is_pointer_state:
            pointer_states.append(memory.id)

    return pointer_states


def compute_error_syndrome(
    memory: MemoryState,
    contradicting_memories: List[MemoryState]
) -> np.ndarray:
    """
    Compute error syndrome from contradictions.

    In quantum error correction, the syndrome encodes information about
    what error occurred, without revealing the data.

    For memories, the contradiction pattern is our syndrome.

    Args:
        memory: The memory with potential errors (low confidence)
        contradicting_memories: Memories that contradict this one

    Returns:
        Syndrome vector (direction of contradictions)
    """
    if not contradicting_memories:
        return np.zeros_like(memory.embedding)

    # Average direction of contradictions
    syndrome = np.mean([m.embedding for m in contradicting_memories], axis=0)

    # Normalize
    syndrome_norm = np.linalg.norm(syndrome)
    if syndrome_norm > 0:
        syndrome /= syndrome_norm

    return syndrome


def reconstruct_from_syndrome(
    memory: MemoryState,
    source_memories: List[MemoryState],
    contradicting_memories: List[MemoryState],
    semantic_neighbors: List[MemoryState],
    alpha: float = 0.3
) -> Tuple[np.ndarray, float]:
    """
    Reconstruct a decohered memory using syndrome information.

    Quantum state tomography analogy:
    1. Collect observable outcomes (sources, contradictions, semantic neighbors)
    2. Use syndrome to weight them
    3. Reconstruct the original state

    Args:
        memory: The memory to reconstruct (currently low confidence)
        source_memories: Memories that cite this one as a source
        contradicting_memories: Memories that contradict this one
        semantic_neighbors: Semantically similar high-confidence memories
        alpha: Blend weight between reconstruction and original (0-1)

    Returns:
        (reconstructed_embedding, coherence_recovery_fraction)
    """
    # Weights for different observation types
    weights = {
        'source': 1.0,           # Sources are primary
        'semantic_neighbor': 0.3,  # Semantic similarity is weak signal
        'contradict': -0.5        # Contradictions point in opposite direction
    }

    reconstructed = np.zeros_like(memory.embedding)
    total_weight = 0

    # Observable 1: Source memories
    for source in source_memories:
        if source.confidence > 0.7:
            reconstructed += weights['source'] * source.embedding
            total_weight += abs(weights['source'])

    # Observable 2: Semantic neighbors
    for neighbor in semantic_neighbors:
        if neighbor.confidence > 0.8:
            reconstructed += weights['semantic_neighbor'] * neighbor.embedding
            total_weight += abs(weights['semantic_neighbor'])

    # Observable 3: Contradictions (point in opposite direction)
    for contradict in contradicting_memories:
        if contradict.confidence > 0.7:
            reconstructed += weights['contradict'] * contradict.embedding
            total_weight += abs(weights['contradict'])

    # Normalize
    if total_weight > 0:
        reconstructed /= total_weight

    # Smooth with original embedding
    original = memory.embedding / np.linalg.norm(memory.embedding)
    blended = alpha * reconstructed + (1 - alpha) * original

    # Normalize blended
    blended_norm = np.linalg.norm(blended)
    if blended_norm > 0:
        blended /= blended_norm

    # Compute coherence recovery: fidelity between original and reconstructed
    # Fidelity F(ρ, σ) = Tr(√(√ρ σ √ρ))²  (quantum fidelity)
    # Classical approximation: cosine similarity
    coherence_recovery = np.dot(original, blended)
    coherence_recovery = max(0, coherence_recovery)  # Clamp to [0, 1]

    return blended, coherence_recovery


def estimate_decoherence_rate_from_history(
    memory_history: List[Tuple[float, float]],  # [(time, confidence), ...]
    temporal_class: str
) -> float:
    """
    Estimate the decoherence rate from historical confidence measurements.

    Fits the data to exponential decay: confidence(t) = C0 × e^(-λt)

    Args:
        memory_history: List of (timestamp, confidence) tuples
        temporal_class: For comparison with theoretical base rate

    Returns:
        Estimated decoherence rate λ
    """
    if len(memory_history) < 2:
        return compute_decoherence_rate_base(temporal_class)

    times = np.array([t for t, _ in memory_history])
    confidences = np.array([c for _, c in memory_history])

    # Convert to log scale for linear regression
    # log(confidence) = log(C0) - λ*t

    valid_mask = confidences > 0  # Only positive confidences
    if np.sum(valid_mask) < 2:
        return compute_decoherence_rate_base(temporal_class)

    times_valid = times[valid_mask]
    log_conf = np.log(confidences[valid_mask])

    # Linear regression: log_conf = intercept - lambda * t
    coeffs = np.polyfit(times_valid, log_conf, 1)
    lambda_estimated = -coeffs[0]  # Slope is -λ

    return max(0, lambda_estimated)


def should_attempt_recovery(
    memory: MemoryState,
    coherence_threshold: float = 0.3
) -> bool:
    """
    Decide whether to attempt quantum error correction for a memory.

    Attempt recovery if:
    - Confidence is low (indicates decoherence)
    - But not so low that no signal remains
    - Memory had significant importance (source_trust > threshold)

    Args:
        memory: The memory to evaluate
        coherence_threshold: Minimum purity to attempt recovery

    Returns:
        True if recovery attempt is warranted
    """
    return (
        0 < memory.confidence < 0.4 and  # Low but not zero
        memory.source_trust > 0.5  # Was from trusted source
    )


# Example usage and integration test
if __name__ == "__main__":
    # Create sample memories
    memory_decohering = MemoryState(
        id="mem_1",
        embedding=np.random.randn(768),
        confidence=0.15,  # Low confidence — decohering
        temporal_class="medium",
        timestamp=1000,
        contradictions_count=5,
        citation_count=2,
        source_trust=0.8
    )

    memory_source = MemoryState(
        id="mem_source",
        embedding=np.random.randn(768),
        confidence=0.9,
        temporal_class="long",
        timestamp=950,
        contradictions_count=0,
        citation_count=50,
        source_trust=0.95
    )

    memory_neighbor = MemoryState(
        id="mem_neighbor",
        embedding=np.random.randn(768),
        confidence=0.85,
        temporal_class="medium",
        timestamp=980,
        contradictions_count=1,
        citation_count=15,
        source_trust=0.7
    )

    # Simple knowledge graph
    knowledge_graph = {
        "mem_1": {
            "edges": [("mem_source", 0.7), ("mem_neighbor", 0.5)]
        },
        "mem_source": {
            "edges": []
        },
        "mem_neighbor": {
            "edges": [("mem_source", 0.6)]
        }
    }

    # Test functions
    print("=== Quantum Decoherence Model Tests ===\n")

    # Test 1: Purity
    purity = compute_memory_purity(memory_decohering, knowledge_graph)
    print(f"Memory purity (low = mixed/decohering): {purity:.3f}")

    # Test 2: Adaptive decoherence rate
    lambda_base = compute_decoherence_rate_base(memory_decohering.temporal_class)
    lambda_eff = compute_adaptive_decoherence_rate(memory_decohering)
    print(f"Base λ (medium-term): {lambda_base:.4f}")
    print(f"Adaptive λ (5 contradictions, low citations): {lambda_eff:.4f}")

    # Test 3: Predicted decay
    time_delta = 100  # 100 seconds
    predicted = predict_confidence_quantum_decay(memory_decohering, time_delta)
    print(f"Predicted confidence after {time_delta}s: {predicted:.3f}")

    # Test 4: Pointer states
    memories = [memory_decohering, memory_source, memory_neighbor]
    pointer_states = identify_pointer_states(memories, knowledge_graph, in_degree_threshold=1, citation_threshold=0.15)
    print(f"Pointer states (high in-degree + citation): {pointer_states}")

    # Test 5: Recovery
    should_recover = should_attempt_recovery(memory_decohering)
    print(f"Should attempt error correction: {should_recover}")

    if should_recover:
        reconstructed, coherence = reconstruct_from_syndrome(
            memory_decohering,
            [memory_source],
            [],
            [memory_neighbor]
        )
        print(f"Reconstruction fidelity: {coherence:.3f}")

    print("\n=== End Tests ===")
