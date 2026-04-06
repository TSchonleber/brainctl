#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
phase_learning.py — Online phase learning via delta rule

Implements Hebbian-style learning for quantum phases:
After each retrieval, adjust phases based on co-activation patterns.

Author: Phase (exploratory research)
Date: 2026-03-28
"""

import math
import sqlite3
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
from collections import defaultdict


DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"


# ===========================================================================
# Delta Rule Implementation
# ===========================================================================

def predict_coactivation(phase_a: float, phase_b: float) -> float:
    """
    Predict co-activation probability from phase difference.

    Theory: Memories with aligned phases should co-activate.

    co_pred(Δφ) = (1 + cos(Δφ)) / 2

    This ranges from 0 (opposite phases, destructive) to 1 (same phases, constructive).

    Args:
        phase_a: Phase of first memory (radians, [0, 2π))
        phase_b: Phase of second memory (radians, [0, 2π))

    Returns:
        Predicted co-activation probability [0, 1]
    """
    phase_diff = (phase_a - phase_b) % (2 * math.pi)
    co_pred = (1.0 + math.cos(phase_diff)) / 2.0
    return co_pred


def compute_phase_update(
    phase_a: float,
    phase_b: float,
    observed_coactivation: float = 1.0,
    learning_rate: float = 0.05
) -> Tuple[float, float]:
    """
    Compute phase update using delta rule.

    Adjusts phases to reduce prediction error.

    Args:
        phase_a: Current phase of memory A (radians)
        phase_b: Current phase of memory B (radians)
        observed_coactivation: 1.0 if co-retrieved, 0.0 if not (in same batch)
        learning_rate: Step size for updates

    Returns:
        (Δphase_a, Δphase_b) — phase adjustments for each memory
    """

    phase_diff = (phase_a - phase_b) % (2 * math.pi)

    # Predicted co-activation
    co_pred = predict_coactivation(phase_a, phase_b)

    # Prediction error
    error = observed_coactivation - co_pred

    # Gradient: sin(phase_diff)
    gradient = math.sin(phase_diff)

    # Phase updates (make phases align if positive error)
    delta_a = learning_rate * error * gradient
    delta_b = -learning_rate * error * gradient

    return delta_a, delta_b


def update_phases_delta_rule(
    retrieved_ids: List[int],
    conn: sqlite3.Connection,
    learning_rate: float = 0.05,
    apply_to_db: bool = True,
    verbose: bool = False
) -> Dict[int, float]:
    """
    Update phases for retrieved memories using delta rule.

    Args:
        retrieved_ids: IDs of memories retrieved together
        conn: Database connection
        learning_rate: Learning rate (0.01 to 0.1 recommended)
        apply_to_db: If True, write updates to database
        verbose: Print details of updates

    Returns:
        Dict mapping memory_id → phase_change
    """

    if len(retrieved_ids) < 2:
        return {}

    updates = defaultdict(float)

    # Get current phases
    phases = {}
    for mem_id in retrieved_ids:
        phase = conn.execute(
            "SELECT confidence_phase FROM memories WHERE id = ?",
            (mem_id,)
        ).fetchone()

        if phase:
            phases[mem_id] = phase[0]

    # For each pair of retrieved memories
    for i, mem_a in enumerate(retrieved_ids):
        if mem_a not in phases:
            continue

        for mem_b in retrieved_ids[i + 1:]:
            if mem_b not in phases:
                continue

            phase_a = phases[mem_a]
            phase_b = phases[mem_b]

            # Observed: they co-occurred
            observed = 1.0

            # Compute updates
            delta_a, delta_b = compute_phase_update(
                phase_a, phase_b, observed, learning_rate
            )

            updates[mem_a] += delta_a
            updates[mem_b] += delta_b

            if verbose:
                co_pred = predict_coactivation(phase_a, phase_b)
                error = observed - co_pred
                phase_diff_deg = ((phase_a - phase_b) % (2 * math.pi)) * 180 / math.pi
                print(f"  Pair ({mem_a:3d}, {mem_b:3d}): "
                      f"Δφ={phase_diff_deg:6.1f}°, "
                      f"error={error:6.3f}, "
                      f"Δphase_a={delta_a:+.4f}, "
                      f"Δphase_b={delta_b:+.4f}")

    # Apply updates to database
    if apply_to_db:
        for mem_id, phase_delta in updates.items():
            current_phase = phases.get(mem_id, 0.0)
            new_phase = (current_phase + phase_delta) % (2 * math.pi)

            conn.execute(
                "UPDATE memories SET confidence_phase = ? WHERE id = ?",
                (new_phase, mem_id)
            )

        conn.commit()

    return dict(updates)


# ===========================================================================
# Convergence Analysis
# ===========================================================================

def measure_phase_stability(
    conn: sqlite3.Connection,
    sample_size: int = 150
) -> Dict[str, float]:
    """
    Measure phase stability across all active memories.

    Returns statistics on current phase distribution.
    """

    phases = conn.execute(
        "SELECT confidence_phase FROM memories WHERE retired_at IS NULL"
    ).fetchall()

    if not phases:
        return {}

    phases = [p[0] for p in phases]

    # Circular statistics
    phase_array = np.array(phases)
    sin_mean = np.mean(np.sin(phase_array))
    cos_mean = np.mean(np.cos(phase_array))

    mean_phase = np.arctan2(sin_mean, cos_mean) % (2 * np.pi)
    concentration = np.sqrt(sin_mean**2 + cos_mean**2)  # r value

    return {
        'mean_phase': mean_phase,
        'concentration': concentration,
        'total_memories': len(phases),
        'phase_std': float(np.std(phases)),
        'phase_min': float(np.min(phases)),
        'phase_max': float(np.max(phases))
    }


def predict_convergence_rate(
    conn: sqlite3.Connection,
    learning_rate: float = 0.05
) -> Dict[str, float]:
    """
    Estimate convergence rate based on current graph structure.

    Returns expected convergence time and stability metrics.
    """

    # Get memory pair statistics
    pairs = conn.execute("""
        SELECT COUNT(*) as edge_count
        FROM knowledge_edges
        WHERE source_table = 'memories' AND target_table = 'memories'
            AND relation_type IN ('semantic_similar', 'contradicts', 'supports')
    """).fetchone()

    edge_count = pairs[0] if pairs else 0

    # Estimate based on system size
    num_memories = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
    ).fetchone()[0]

    # Heuristic: convergence_time ~ N * log(1/tolerance)
    # with effective coupling strength determined by edges
    coupling_strength = edge_count / max(num_memories, 1)

    # Estimated convergence (in retrieval events)
    estimated_steps = int(
        num_memories * math.log(1.0 / 0.01) / (learning_rate * coupling_strength + 0.1)
    )

    return {
        'num_memories': num_memories,
        'edge_count': edge_count,
        'coupling_strength': coupling_strength,
        'learning_rate': learning_rate,
        'estimated_steps_to_convergence': estimated_steps,
        'estimated_time_scale': f"{estimated_steps // 100}-{estimated_steps // 50} hours (at 100-200 retrievals/hour)"
    }


# ===========================================================================
# Experiment: Simulate Phase Learning on High-Recall Cluster
# ===========================================================================

def simulate_phase_learning_experiment(
    conn: sqlite3.Connection,
    memory_ids: List[int],
    num_retrieval_cycles: int = 50,
    learning_rate: float = 0.05,
    verbose: bool = True
) -> Dict[str, List]:
    """
    Simulate phase learning on a specific memory cluster.

    Repeatedly "retrieve" the same set of memories and track phase evolution.

    Args:
        conn: Database connection
        memory_ids: Memories to simulate retrieving together
        num_retrieval_cycles: How many times to simulate the retrieval
        learning_rate: Learning rate for delta rule
        verbose: Print progress

    Returns:
        Dict with convergence history
    """

    history = {
        'mean_phase': [],
        'concentration': [],
        'phase_std': [],
        'max_update': []
    }

    if verbose:
        print(f"\n{'='*60}")
        print(f"PHASE LEARNING EXPERIMENT")
        print(f"{'='*60}")
        print(f"Simulating {num_retrieval_cycles} retrieval cycles")
        print(f"Memory cluster: {memory_ids}")
        print(f"Learning rate: {learning_rate}\n")

    # Save original phases
    original_phases = {}
    for mem_id in memory_ids:
        phase = conn.execute(
            "SELECT confidence_phase FROM memories WHERE id = ?",
            (mem_id,)
        ).fetchone()
        if phase:
            original_phases[mem_id] = phase[0]

    # Run simulation
    for cycle in range(num_retrieval_cycles):
        # Update phases
        updates = update_phases_delta_rule(
            memory_ids,
            conn,
            learning_rate=learning_rate,
            apply_to_db=True,
            verbose=False
        )

        # Measure stability
        stats = measure_phase_stability(conn, sample_size=len(memory_ids))
        history['mean_phase'].append(stats.get('mean_phase', 0))
        history['concentration'].append(stats.get('concentration', 0))
        history['phase_std'].append(stats.get('phase_std', 0))
        history['max_update'].append(max([abs(u) for u in updates.values()], default=0))

        if verbose and (cycle == 0 or (cycle + 1) % 10 == 0 or cycle == num_retrieval_cycles - 1):
            print(f"Cycle {cycle + 1:2d}/{num_retrieval_cycles}: "
                  f"mean_phase={stats.get('mean_phase', 0)*180/math.pi:6.1f}°, "
                  f"concentration={stats.get('concentration', 0):.4f}, "
                  f"phase_std={stats.get('phase_std', 0):.4f}, "
                  f"max_update={history['max_update'][-1]:+.5f}")

    if verbose:
        print(f"\n{'='*60}")
        print(f"CONVERGENCE ANALYSIS")
        print(f"{'='*60}")
        print(f"Mean phase evolution: {history['mean_phase'][0]*180/math.pi:.1f}° → {history['mean_phase'][-1]*180/math.pi:.1f}°")
        print(f"Concentration (r): {history['concentration'][0]:.4f} → {history['concentration'][-1]:.4f}")
        print(f"Phase std dev: {history['phase_std'][0]:.4f} → {history['phase_std'][-1]:.4f}")
        print(f"Max update (final): {history['max_update'][-1]:+.5f}")
        print(f"\n{'='*60}\n")

    return history


def convergence_achieved(
    history: Dict[str, List],
    tolerance: float = 1e-4,
    window: int = 5
) -> Tuple[bool, int]:
    """
    Check if convergence was achieved based on update magnitude.

    Args:
        history: Output from simulate_phase_learning_experiment
        tolerance: Threshold for "converged"
        window: Number of recent steps to check

    Returns:
        (has_converged, cycle_number_when_converged)
    """

    if len(history['max_update']) < window:
        return False, -1

    # Check if last `window` updates are below tolerance
    recent_updates = history['max_update'][-window:]
    if all(u < tolerance for u in recent_updates):
        # Find when convergence started
        for i in range(len(history['max_update']) - window, -1, -1):
            if history['max_update'][i] >= tolerance:
                return True, i + 1

    return False, -1


# ===========================================================================
# Main: Run experiment on high-recall cluster
# ===========================================================================

def main():
    """Run phase learning simulation on permanent memory cluster."""

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # High-recall cluster from COS-392 analysis
        permanent_cluster = [93, 125, 127, 130]

        print("\n" + "="*70)
        print("PHASE LEARNING DYNAMICS PROTOTYPE")
        print("="*70)

        # Get current stability
        print("\nBEFORE learning:")
        stats = measure_phase_stability(conn)
        print(f"  Mean phase: {stats['mean_phase']*180/math.pi:.1f}°")
        print(f"  Concentration (r): {stats['concentration']:.4f}")
        print(f"  Phase distribution std: {stats['phase_std']:.4f}")

        # Run simulation
        history = simulate_phase_learning_experiment(
            conn,
            permanent_cluster,
            num_retrieval_cycles=50,
            learning_rate=0.05,
            verbose=True
        )

        # Check convergence
        converged, convergence_cycle = convergence_achieved(history)
        if converged:
            print(f"✓ CONVERGED at cycle {convergence_cycle}")
        else:
            print(f"✗ Not fully converged (still changing)")

        # Convergence rate estimate
        print("\nCONVERGENCE RATE ESTIMATE:")
        rate = predict_convergence_rate(conn, learning_rate=0.05)
        for key, value in rate.items():
            print(f"  {key}: {value}")

        conn.close()
        print("\n✓ Phase learning prototype complete\n")

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
