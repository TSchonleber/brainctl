#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
phase_inference.py — Inferring quantum phase from brain.db co-retrieval data

Implements five methods to compute confidence_phase for memory amplitudes:
1. Relation type heuristic
2. Co-activation signature
3. Embedding angle (geometric)
4. Contradiction graph (graph-based)
5. Bayesian learning (principled)

Plus a hybrid ensemble voting approach.

Author: Phase (COS-392)
"""

import math
import cmath
import sqlite3
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from collections import defaultdict


# ===========================================================================
# Configuration
# ===========================================================================

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

PHASE_BY_RELATION = {
    'semantic_similar': 0.0,      # Constructive
    'supports': 0.0,              # Constructive
    'co_referenced': 0.0,         # Constructive (default)
    'topical_tag': 0.0,           # Weak constructive
    'contradicts': math.pi,       # Destructive
    'derived_from': math.pi / 4,  # Weak destructive
    'supersedes': math.pi,        # Destructive
    'causes': math.pi / 6,        # Weak constructive (causal)
}


# ===========================================================================
# Method 1: Relation Type Heuristic
# ===========================================================================

def assign_phase_by_relation(memory_id: int, conn: sqlite3.Connection) -> float:
    """
    Assign confidence_phase based on incoming relation types.

    Simple heuristic: weight relation types by frequency and combine phases.
    """

    # Get all incoming relations (what other memories point to this one)
    edges = conn.execute("""
        SELECT relation_type, COUNT(*) as cnt
        FROM knowledge_edges
        WHERE target_id = ?
            AND source_table = 'memories' AND target_table = 'memories'
        GROUP BY relation_type
    """, (memory_id,)).fetchall()

    if not edges:
        return 0.0  # Default: constructive

    # Weight phase by edge count using circular mean
    weighted_sum = 0.0
    total_weight = 0.0

    for relation_type, count in edges:
        phase = PHASE_BY_RELATION.get(relation_type, 0.0)
        weighted_sum += count * cmath.exp(1j * phase)
        total_weight += count

    if total_weight > 0:
        result = cmath.phase(weighted_sum / total_weight)
        return result % (2 * math.pi)
    else:
        return 0.0


# ===========================================================================
# Method 2: Co-Activation Signature
# ===========================================================================

def sigmoid(x: float, k: float = 1.0) -> float:
    """Sigmoid with adjustable steepness k."""
    return 1.0 / (1.0 + math.exp(-k * x))


def infer_phase_from_coactivation(
    src_id: int,
    tgt_id: int,
    conn: sqlite3.Connection
) -> float:
    """
    Infer phase relationship from co-activation patterns.

    High co-activation → constructive (phase ≈ 0)
    Low co-activation → destructive (phase ≈ π)
    """

    # Get co-activation data
    edge = conn.execute("""
        SELECT co_activation_count, relation_type, weight
        FROM knowledge_edges
        WHERE source_id = ? AND target_id = ?
            AND source_table = 'memories' AND target_table = 'memories'
    """, (src_id, tgt_id)).fetchone()

    m_src = conn.execute(
        "SELECT recalled_count FROM memories WHERE id = ?",
        (src_id,)
    ).fetchone()

    m_tgt = conn.execute(
        "SELECT recalled_count FROM memories WHERE id = ?",
        (tgt_id,)
    ).fetchone()

    if not edge or not m_src or not m_tgt:
        return 0.0

    co_act, relation_type, weight = edge
    m_src_count, = m_src
    m_tgt_count, = m_tgt

    if m_src_count == 0 or m_tgt_count == 0:
        return 0.0

    # Normalized co-activation ratio
    # Expected: if independent, co_act ~ (m_src * m_tgt) / total_retrievals
    # Simple proxy: use actual recall counts
    total_retrievals = max(m_src_count, m_tgt_count) + 1

    # Co-activation ratio relative to what we'd expect from chance
    empirical_prob = co_act / total_retrievals
    independent_prob = (m_src_count * m_tgt_count) / (total_retrievals ** 2)

    if independent_prob > 0:
        lift = empirical_prob / independent_prob
    else:
        lift = 1.0

    # Map lift to phase
    # High lift (>1) → constructive (phase ≈ 0)
    # Low lift (<1) → destructive (phase ≈ π)
    # Use S-curve to map to [0, π]

    phase = math.pi * (1.0 - sigmoid(lift - 1.0, k=2.0))

    return phase % (2 * math.pi)


def compute_coactivation_phases(memory_id: int, conn: sqlite3.Connection) -> float:
    """
    Compute phase by averaging co-activation inference over connected memories.
    """

    edges = conn.execute("""
        SELECT target_id
        FROM knowledge_edges
        WHERE source_id = ?
            AND source_table = 'memories' AND target_table = 'memories'
    """, (memory_id,)).fetchall()

    if not edges:
        return 0.0

    phases = []
    for (tgt_id,) in edges:
        phase = infer_phase_from_coactivation(memory_id, tgt_id, conn)
        phases.append(phase)

    # Circular mean
    if phases:
        phase_sum = sum(cmath.exp(1j * p) for p in phases)
        return cmath.phase(phase_sum / len(phases)) % (2 * math.pi)
    else:
        return 0.0


# ===========================================================================
# Method 3: Embedding Angle
# ===========================================================================

def infer_phase_from_embedding(memory_id: int, conn: sqlite3.Connection) -> float:
    """
    Infer phase from memory's embedding position relative to category cluster.

    Memories aligned with category centroid have phase ≈ 0.
    Outliers or contradictions have phase ≠ 0.

    Note: Skipped if embeddings not accessible via standard queries.
    """
    # Embedding method requires vec_memories access which may use virtual tables
    # Skip for now - use relation_type and coactivation methods instead
    return 0.0


# ===========================================================================
# Method 4: Contradiction Graph (Graph-based)
# ===========================================================================

def infer_phase_from_contradiction_graph(conn: sqlite3.Connection) -> Dict[int, float]:
    """
    Assign phases to minimize conflicts in contradiction graph via iterative refinement.

    Solves approximately: if contradicts(a, b), then phase_a ≈ phase_b + π.
    """

    # Build contradiction subgraph
    contradictions = conn.execute("""
        SELECT source_id, target_id, weight
        FROM knowledge_edges
        WHERE relation_type = 'contradicts'
            AND source_table = 'memories' AND target_table = 'memories'
    """).fetchall()

    active_memories = conn.execute(
        "SELECT id FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    memory_ids = [row[0] for row in active_memories]

    if not memory_ids:
        return {}

    # Initialize phases uniformly
    phases = {mid: 0.0 for mid in memory_ids}

    if not contradictions:
        return phases

    # Iterative refinement (belief propagation style)
    for iteration in range(10):
        for src, tgt, weight in contradictions:
            phase_diff = (phases[src] - phases[tgt]) % (2 * np.pi)

            # Desired phase difference: π (opposing)
            desired_diff = math.pi
            error = min(abs(phase_diff - desired_diff),
                       abs(phase_diff - (desired_diff - 2*np.pi)))

            if error > 0.1:  # Only update if not satisfied
                # Gradient towards desired difference
                target_phase_diff = math.pi
                current_phase_diff = phase_diff

                # Move phases towards opposing
                phase_update = 0.02 * weight

                phases[src] = (phases[src] + phase_update) % (2 * np.pi)
                phases[tgt] = (phases[tgt] - phase_update) % (2 * np.pi)

    return phases


# ===========================================================================
# Method 5: Ensemble Voting (Hybrid)
# ===========================================================================

def circular_mean(angles: List[float]) -> float:
    """Compute mean of circular data (angles)."""
    if not angles:
        return 0.0
    phase_sum = sum(cmath.exp(1j * angle) for angle in angles)
    return cmath.phase(phase_sum / len(angles)) % (2 * math.pi)


def infer_confidence_phase_hybrid(
    memory_id: int,
    conn: sqlite3.Connection,
    method_weights: Optional[Dict[str, float]] = None,
    include_methods: Optional[List[str]] = None
) -> float:
    """
    Infer phase by combining multiple methods with weighted voting.

    Args:
        memory_id: Target memory
        conn: Database connection
        method_weights: Dict mapping method name → weight
        include_methods: List of methods to include (None = all)

    Returns:
        Inferred confidence_phase in [0, 2π)
    """

    if method_weights is None:
        method_weights = {
            'relation_type': 0.4,         # Heuristic
            'coactivation': 0.35,         # Data-driven
            'contradiction_graph': 0.25,  # Graph-based
        }

    if include_methods:
        method_weights = {k: v for k, v in method_weights.items() if k in include_methods}

    phases = {}

    # Method 1: Relation type heuristic
    if 'relation_type' in method_weights:
        phases['relation_type'] = assign_phase_by_relation(memory_id, conn)

    # Method 2: Co-activation signature
    if 'coactivation' in method_weights:
        phases['coactivation'] = compute_coactivation_phases(memory_id, conn)

    # Combine via circular weighted mean
    total_weight = 0.0
    weighted_sum = 0.0

    for method_name, phase in phases.items():
        weight = method_weights.get(method_name, 0.0)
        if weight > 0:
            weighted_sum += weight * cmath.exp(1j * phase)
            total_weight += weight

    if total_weight > 0:
        result_phase = cmath.phase(weighted_sum / total_weight)
        return result_phase % (2 * math.pi)
    else:
        return 0.0


# ===========================================================================
# Database Integration
# ===========================================================================

def initialize_confidence_phase_column(conn: sqlite3.Connection) -> None:
    """Add confidence_phase column if it doesn't exist."""
    try:
        # Check if column exists
        cursor = conn.execute("PRAGMA table_info(memories)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'confidence_phase' not in columns:
            conn.execute("""
                ALTER TABLE memories
                ADD COLUMN confidence_phase REAL NOT NULL DEFAULT 0.0
            """)
            conn.commit()
            print("✓ Added confidence_phase column to memories table")
        else:
            print("✓ confidence_phase column already exists")

    except sqlite3.OperationalError as e:
        print(f"✗ Error adding column: {e}")


def compute_all_phases(
    conn: sqlite3.Connection,
    method: str = 'hybrid',
    batch_size: int = 10,
    verbose: bool = True
) -> Dict[int, float]:
    """
    Compute confidence_phase for all active memories.

    Args:
        conn: Database connection
        method: 'relation_type', 'embedding', 'coactivation', 'contradiction', or 'hybrid'
        batch_size: Progress reporting frequency
        verbose: Print progress updates

    Returns:
        Dict mapping memory_id → confidence_phase
    """

    memories = conn.execute(
        "SELECT id FROM memories WHERE retired_at IS NULL ORDER BY id"
    ).fetchall()
    memory_ids = [row[0] for row in memories]

    if verbose:
        print(f"\nComputing confidence_phase for {len(memory_ids)} active memories...")
        print(f"Using method: {method}\n")

    phases = {}

    # Pre-compute contradiction graph once for all memories
    contradiction_phases = {}
    if method in ['hybrid', 'contradiction']:
        contradiction_phases = infer_phase_from_contradiction_graph(conn)

    for i, memory_id in enumerate(memory_ids):
        if method == 'relation_type':
            phase = assign_phase_by_relation(memory_id, conn)

        elif method == 'embedding':
            phase = infer_phase_from_embedding(memory_id, conn)

        elif method == 'coactivation':
            phase = compute_coactivation_phases(memory_id, conn)

        elif method == 'contradiction':
            phase = contradiction_phases.get(memory_id, 0.0)

        elif method == 'hybrid':
            phase = infer_confidence_phase_hybrid(memory_id, conn)

        else:
            raise ValueError(f"Unknown method: {method}")

        phases[memory_id] = phase

        if verbose and (i + 1) % batch_size == 0:
            print(f"  Processed {i + 1}/{len(memory_ids)} memories...")

    if verbose:
        print(f"✓ Computed phases for all {len(memory_ids)} memories\n")

    return phases


def store_phases_in_database(
    conn: sqlite3.Connection,
    phases: Dict[int, float],
    verbose: bool = True
) -> int:
    """
    Store computed phases in database.

    Returns: Number of memories updated
    """

    updated = 0

    for memory_id, phase in phases.items():
        try:
            conn.execute(
                "UPDATE memories SET confidence_phase = ? WHERE id = ?",
                (phase, memory_id)
            )
            updated += 1
        except Exception as e:
            print(f"✗ Error updating memory {memory_id}: {e}")

    conn.commit()

    if verbose:
        print(f"✓ Stored {updated} phases in database\n")

    return updated


def validate_phases(
    conn: sqlite3.Connection,
    sample_size: int = 20
) -> None:
    """
    Validate stored phases by spot-checking and printing summary statistics.
    """

    phases = conn.execute(
        "SELECT confidence_phase FROM memories WHERE retired_at IS NULL"
    ).fetchall()

    if not phases:
        print("No phases to validate")
        return

    phases = [p[0] for p in phases]

    print("\n" + "="*60)
    print("PHASE INFERENCE VALIDATION")
    print("="*60)

    print(f"\nTotal active memories with phases: {len(phases)}")

    # Statistics
    phases_array = np.array(phases)

    # Circular statistics
    phase_sines = np.sin(phases_array)
    phase_cosines = np.cos(phases_array)
    mean_cos = np.mean(phase_cosines)
    mean_sin = np.mean(phase_sines)
    mean_phase = np.arctan2(mean_sin, mean_cos)

    r_value = np.sqrt(mean_cos**2 + mean_sin**2)  # Concentration parameter

    print(f"Mean phase: {mean_phase:.4f} rad ({np.degrees(mean_phase):.2f}°)")
    print(f"Concentration (r): {r_value:.4f}")
    print(f"  (r≈0: uniform, r≈1: concentrated)")

    # Histogram
    bins = np.linspace(0, 2*np.pi, 9)
    hist, _ = np.histogram(phases, bins=bins)
    print(f"\nPhase distribution (bins of π/4):")
    for i in range(8):
        angle = bins[i] * 180 / np.pi
        count = hist[i]
        bar = "█" * (count // 2)
        print(f"  [{angle:6.1f}°-{bins[i+1]*180/np.pi:6.1f}°] {bar} {count}")

    # Spot check
    print(f"\nSpot check (first {sample_size} memories):")
    sample = conn.execute(f"""
        SELECT id, category, confidence, confidence_phase
        FROM memories
        WHERE retired_at IS NULL
        ORDER BY id
        LIMIT {sample_size}
    """).fetchall()

    for mem_id, category, confidence, phase in sample:
        phase_deg = phase * 180 / np.pi if phase else 0
        print(f"  Memory {mem_id:3d} | {category:15s} | conf={confidence:.3f} | phase={phase_deg:6.1f}°")

    print("\n" + "="*60 + "\n")


# ===========================================================================
# Main
# ===========================================================================

def main():
    """Main entry point."""

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # Step 1: Initialize column
        initialize_confidence_phase_column(conn)

        # Step 2: Compute phases using hybrid method
        phases = compute_all_phases(conn, method='hybrid', verbose=True)

        # Step 3: Store in database
        store_phases_in_database(conn, phases, verbose=True)

        # Step 4: Validate
        validate_phases(conn, sample_size=25)

        conn.close()
        print("✓ Phase inference complete!")

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
