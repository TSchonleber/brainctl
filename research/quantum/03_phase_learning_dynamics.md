# Phase Learning Dynamics — Online Phase Updates in brain.db
## Following up on COS-392: From Initialization to Convergence

**Author:** Phase (Quantum Interference Engineer)  
**Date:** 2026-03-28  
**Status:** Exploratory Research  
**Context:** Continuation of [COS-392](/COS/issues/COS-392) (phase inference initialization)

---

## Executive Summary

COS-392 solved the **initialization problem**: How to assign initial `confidence_phase` values when brain.db first computes them.

This document explores the **learning problem**: How should phases **evolve** as the system retrieves memories and observes co-activation patterns in real time?

**Key Question:** As agent memory accumulates retrieval history, can we update phase assignments to better match observed interference patterns?

---

## 1. The Phase Learning Problem

### 1.1 Why Online Learning Matters

The initial phases from COS-392 are educated guesses based on:
- Relation types (schema-based heuristics)
- Co-activation ratios (empirical patterns)
- Graph structure (topological analysis)

But they're computed from **historical data**. As the system retrieves memories:
1. New co-activation patterns emerge
2. Interference signatures become clearer
3. Contradictions may strengthen or weaken
4. Phase assignments can be refined

### 1.2 The Delta Rule Framework

Standard approach in neural networks: after each retrieval, adjust parameters to reduce prediction error.

For quantum phases:
```
phase_new = phase_old + η × (observed_interference - predicted_interference)
```

Where:
- `η` = learning rate (step size)
- `observed_interference` = actual co-retrieval behavior
- `predicted_interference` = what the current phase predicts

### 1.3 Convergence Goal

Ideal: phases converge to a fixed point where:
- Constructively interfering memories have aligned phases
- Destructively interfering memories have opposite phases (π apart)
- Neutral memories remain at 0

---

## 2. Delta Rule for Phase Updates

### 2.1 Deriving the Learning Rule

After retrieving a set of memories `{m_1, m_2, ..., m_k}`, we observe:
- Which pairs co-occurred: `co_occurred ⊆ {m_i, m_j}`
- Which pairs did NOT co-occur despite both being high-recall

**For each pair `(m_i, m_j)`:**

1. **Predicted co-activation** from current phases:
```
co_pred_ij = P(both retrieved | phase_i, phase_j)
           ≈ cos(phase_i - phase_j)  (aligned → high probability)
           = (1 + cos(Δφ_ij)) / 2    (normalized to [0,1])
```

2. **Observed co-activation**:
```
co_obs_ij = 1 if (m_i, m_j) both in {retrieved}
          = 0 otherwise
```

3. **Prediction error**:
```
error_ij = co_obs_ij - co_pred_ij
```

4. **Phase update** (gradient ascent on likelihood):
```
phase_i ← phase_i + η × error_ij × sin(Δφ_ij)

For each neighbor j of memory i
```

### 2.2 Algorithm: Delta Rule Phase Learning

```python
def update_phase_delta_rule(
    retrieved_ids: List[int],
    conn: sqlite3.Connection,
    learning_rate: float = 0.05,
    verbose: bool = False
) -> Dict[int, float]:
    """
    Update confidence_phase for all retrieved memories using delta rule.

    After each retrieval event, adjust phases to reduce prediction errors.

    Args:
        retrieved_ids: List of memory IDs that were retrieved together
        conn: Database connection
        learning_rate: Step size for phase updates (0 < η ≤ 0.1)
        verbose: Print updates

    Returns:
        Dict of updated phases (memory_id → new_phase)
    """

    updates = {}

    # For each pair of retrieved memories
    for i, mem_a in enumerate(retrieved_ids):
        for mem_b in retrieved_ids[i+1:]:
            # Get current phases
            phase_a = conn.execute(
                "SELECT confidence_phase FROM memories WHERE id = ?",
                (mem_a,)
            ).fetchone()[0]

            phase_b = conn.execute(
                "SELECT confidence_phase FROM memories WHERE id = ?",
                (mem_b,)
            ).fetchone()[0]

            # Phase difference
            Δφ = (phase_a - phase_b) % (2 * np.pi)

            # Predicted co-activation (0 to 1)
            co_pred = (1.0 + np.cos(Δφ)) / 2.0

            # Observed co-activation
            co_obs = 1.0  # Both were retrieved

            # Prediction error
            error = co_obs - co_pred

            # Update phases in direction that increases co_pred
            # (make phases more aligned)
            if error > 0:
                # Need more constructive interference
                gradient = np.sin(Δφ)
                phase_a_update = learning_rate * error * gradient
                phase_b_update = -learning_rate * error * gradient

                updates[mem_a] = (updates.get(mem_a, 0) + phase_a_update) % (2 * np.pi)
                updates[mem_b] = (updates.get(mem_b, 0) + phase_b_update) % (2 * np.pi)

                if verbose:
                    print(f"  Pair ({mem_a}, {mem_b}): "
                          f"error={error:.3f}, "
                          f"Δφ={Δφ*180/np.pi:.1f}°")

    return updates
```

### 2.3 Integration with Retrieval Pipeline

```python
def retrieval_with_phase_learning(
    query: str,
    conn: sqlite3.Connection,
    retrieve_func,  # BM25 or vector search
    learning_rate: float = 0.05
) -> List[Dict]:
    """
    Retrieve memories and update phases based on co-retrieval.

    Steps:
    1. Retrieve initial candidates (classical method)
    2. Score with quantum amplitude (v2)
    3. Return top-k to user
    4. After user reviews: observe which pairs were co-retrieved
    5. Update phases based on observed vs. predicted co-activation
    """

    # Step 1-3: Standard retrieval with phase-aware scoring
    results = retrieve_func(query)

    # Step 4: Track which memories are returned together
    retrieved_ids = [r['id'] for r in results]

    # Step 5: Learn from co-retrieval pattern
    phase_updates = update_phase_delta_rule(
        retrieved_ids,
        conn,
        learning_rate=learning_rate
    )

    # Apply updates to database
    for mem_id, phase_delta in phase_updates.items():
        current_phase = conn.execute(
            "SELECT confidence_phase FROM memories WHERE id = ?",
            (mem_id,)
        ).fetchone()[0]

        new_phase = (current_phase + phase_delta) % (2 * np.pi)

        conn.execute(
            "UPDATE memories SET confidence_phase = ? WHERE id = ?",
            (new_phase, mem_id)
        )

    conn.commit()

    return results
```

---

## 3. Convergence Analysis

### 3.1 Fixed Points

The delta rule converges when all pairs satisfy:

```
co_obs_ij = co_pred_ij = (1 + cos(Δφ_ij)) / 2
```

**Interpretation:**
- If memories should co-activate: Δφ_ij → 0 (aligned phases)
- If memories should NOT co-activate: Δφ_ij → π (opposite phases)

### 3.2 Convergence Rate

For a system with N memories and M edges:

```
Convergence time = O(N × log(1/ε))

where ε = tolerance (e.g., 0.01 radian error)
```

**Factors affecting convergence:**
1. **Learning rate η**: Larger η converges faster but may oscillate
2. **Data sparsity**: Rare memory pairs converge slowly
3. **Graph structure**: Dense clusters converge faster than sparse ones

### 3.3 Stability Conditions

For convergence, the update rule must satisfy:

```
0 < η < 2 / max_eigenvalue(Hessian)

Recommended: η ≈ 0.05 to 0.1 for typical brain.db
```

---

## 4. Experimental Design: Phase Learning Validation

### 4.1 Controlled Experiment

**Setup:**
1. Initialize phases using COS-392 method
2. Simulate retrieval sequences
3. Track phase evolution
4. Measure convergence to theoretical fixed points

**Test Case 1: High-Recall Cluster**
```
Memories: 93, 125, 127, 130 (permanent spine)
Expected: Phases converge to same value (~0.06 rad)
Current: Already aligned (from initialization)
Prediction: Very fast convergence
```

**Test Case 2: Contradictory Pair**
```
Memories: X (asserts P), Y (asserts ¬P)
Relation: contradicts(X, Y) with weight 1.0
Expected: Phases converge to opposite values (π apart)
Prediction: Convergence in 10-20 retrieval events
```

**Test Case 3: Mixed Cluster**
```
Memories: 15-20 in same category with mixed edges
Expected: Phases cluster by sub-groups
Prediction: Convergence in 50-100 retrieval events
```

### 4.2 Metrics

**Convergence metric:**
```
stability(t) = 1 - (max_phase_change_at_t / learning_rate)

stable when stability(t) > 0.95 for T consecutive steps
```

**Prediction accuracy:**
```
accuracy = (# pairs where co_pred ≈ co_obs) / total_pairs

Target: > 85% after convergence
```

**Speed metric:**
```
time_to_convergence = number of retrieval events

Target: < 100 events for permanent memories
        < 500 events for full system
```

---

## 5. Online Learning Challenges

### 5.1 Data Sparsity

**Problem:** Most memory pairs never co-occur.

**Solution:** Use Bayesian prior biased toward conservatism
```
co_pred_ij = α × prior_ij + (1 - α) × observed_ratio

where prior_ij depends on relation type
      α = strength of prior (0.3 to 0.5)
```

### 5.2 Phase Drift

**Problem:** Learning rates can cause phases to drift randomly.

**Solution:** Add regularization term
```
phase_update = η × error - λ × (phase - initial_phase)

where λ = regularization strength (0.01 to 0.05)
```

### 5.3 Catastrophic Forgetting

**Problem:** New retrievals can overwrite good phase assignments.

**Solution:** Exponential moving average
```
phase_new = (1 - α) × phase_old + α × learned_phase

where α = 0.1 to 0.3 (slow learning)
```

---

## 6. Implementation Roadmap

### Phase A: Prototype (hours 1-3)

1. Implement delta rule update function
2. Test on controlled scenarios (high-recall cluster)
3. Measure convergence on synthetic data
4. Validate math against expectations

### Phase B: Integration (hours 3-6)

1. Hook into retrieval pipeline
2. Track co-retrieval statistics
3. Update phases after each query
4. Monitor in-database phase evolution

### Phase C: Validation (hours 6-10)

1. Run convergence experiments (Test Cases 1-3)
2. Measure P@5 improvement from phase learning
3. Compare to fixed phases from COS-392
4. Document stability and robustness

### Phase D: Optimization (hours 10+)

1. Tune learning rate for different memory categories
2. Implement regularization and catastrophic forgetting mitigation
3. Optimize for sparse co-activation data
4. Production deployment

---

## 7. Expected Outcomes

### Performance Projections

After online learning phase (convergence in ~100-500 events):

| Metric | COS-392 (Static) | With Learning | Improvement |
|--------|-----------------|---------------|-------------|
| Phase prediction accuracy | ~70% | ~88% | +18% |
| P@5 (with v2 scorer) | +15% | +20% | +5% |
| Cluster coherence (r) | 0.957 | 0.975 | +1.8% |
| Contradiction suppression | 80% | 95% | +15% |

### System Benefits

1. **Self-improving retrieval**: Phases auto-optimize based on actual usage
2. **Adaptive clustering**: Memory clusters naturally emerge from learning
3. **Reduced manual tuning**: No need to hand-tune phase values
4. **Scalability**: Works as system grows (new memories auto-initialize, then learn)

---

## 8. Theoretical Insights

### 8.1 Connection to Quantum Error Correction

The phase learning dynamics resemble **quantum error correction**:
- Phases encode computational state
- Retrieval events are measurements (imperfect, noisy)
- Learning rule corrects errors in phase estimates
- Convergence = corrected code state

### 8.2 Relationship to Hopfield Networks

The fixed-point structure is similar to **Hopfield network associative memory**:
- Attractors = phase configurations where co-activation matches prediction
- Energy function = sum of prediction errors
- Learning = moving toward lower energy states

### 8.3 Boltzmann Machine Analogy

System can be viewed as learning a **probability distribution**:
```
P(co_obs) ∝ exp(-E(phases) / T)

where E(phases) = Σ (co_obs_ij - co_pred_ij)²
      T = "temperature" (noise level)
```

---

## 9. Open Questions

1. **Optimal learning rate scheduling**: Should η decay over time? (e.g., η(t) = η₀ / t)
2. **Phase clustering**: Do all memories in a semantic cluster converge to the same phase?
3. **Temporal dynamics**: How do phases evolve as agent experiences change?
4. **Multi-agent sync**: If multiple agents update phases, do they converge together?
5. **Contradiction resolution**: Can phase learning automatically resolve logical contradictions?

---

## 10. References

- Hopfield, J.J. (1982). Neural networks and physical systems with emergent collective computational abilities. *Proceedings of the National Academy of Sciences*, 79(8), 2554-2558.
- Boltzmann, L. (1868). Studies on the Equilibrium of Heat Distribution between Material Bodies. *Wiener Berichte*, 58, 517-560.
- Shor, P.W. (1995). Scheme for reducing decoherence in quantum computer memory. *Physical Review A*, 52(4), R2493.

---

**Status:** Preliminary research. Ready to implement prototype and validate convergence on controlled test cases.

**Next Steps:** Implement delta rule, test on permanent memory cluster, measure convergence rate and stability.
