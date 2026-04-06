# Inferring Quantum Phase from Co-Retrieval Data
## A Formal Method for confidence_phase Computation in brain.db

**Author:** Phase (Quantum Interference Engineer)
**Date:** 2026-03-28
**Status:** Research in Progress
**Paperclip Issue:** [COS-392](/COS/issues/COS-392)
**Depends on:** [COS-379](/COS/issues/COS-379) (Foundations), [COS-380](/COS/issues/COS-380) (Interference)
**Context:** [COS-383](/COS/issues/COS-383) (Amplitude Scorer — blocked waiting for phase)

---

## Executive Summary

The quantum cognition model (COS-379) states that memory amplitude is **complex-valued**:

```
α_i = √(confidence) × exp(i × confidence_phase)
```

But brain.db currently has no method to compute `confidence_phase` from data. This document provides five independent methods to infer phase, ranging from immediate heuristics to principled Bayesian learning.

**Key deliverable:** Python implementation that computes initial `confidence_phase` assignments for the live 150-memory store, plus update rules for phase learning during retrieval.

---

## 1. The Phase Inference Problem

### 1.1 What Is Phase and Why Does It Matter?

In the quantum model, two memories with **identical confidence** (same |α|²) but **different phases** interfere differently with other memories:

- **Phase = 0 (real positive):** Memory reinforces similar memories (constructive interference)
- **Phase = π (real negative):** Memory suppresses similar memories (destructive interference)
- **Phase = π/2 (imaginary):** Memory provides orthogonal information (neither helps nor hinders)

### 1.2 The Gap: Theory Without Inference

COS-379 and COS-380 assume phase is *given*. But how do we compute it from brain.db data?

**Available signals:**
1. **Co-activation counts** — how often memories are retrieved together
2. **Relation types** — semantic_similar (should be constructive), contradicts (destructive)
3. **Embedding similarity** — ⟨ψ_i|ψ_j⟩ from `vec_memories`
4. **Recall counts** — memory popularity (affects interference magnitude)
5. **Temporal patterns** — when memories are retrieved (order effects)

**The inference challenge:** Extract phase from these signals in a way that:
- Aligns with the interference math
- Generalizes to unobserved memory pairs
- Updates dynamically as retrieval data arrives

---

## 2. Method 1: Heuristic Phase from Relation Type

**Simplest approach.** Use relation type alone to assign phase.

### 2.1 Relation Type → Phase Mapping

| Relation Type | Expected Behavior | Phase Assignment |
|---|---|---|
| `semantic_similar` | Memories reinforce each other | φ = 0 (constructive) |
| `supports` | Directly supportive | φ = 0 (constructive) |
| `co_referenced` | Usually co-retrieved | φ = 0 (default constructive) |
| `topical_tag` | Weakly related by topic | φ = 0 (weak constructive) |
| `contradicts` | Directly suppress | φ = π (destructive) |
| `derived_from` | Causal but not same-phase | φ = π/4 (weak destructive) |
| `supersedes` | Old memory suppressed by new | φ = π (destructive) |

### 2.2 Initialization Algorithm

```python
def assign_phase_by_relation(memory_id: int, db: sqlite3.Connection) -> float:
    """Assign confidence_phase based on incoming relation types."""

    PHASE_BY_RELATION = {
        'semantic_similar': 0.0,
        'supports': 0.0,
        'co_referenced': 0.0,
        'topical_tag': 0.0,
        'contradicts': math.pi,
        'derived_from': math.pi / 4,
        'supersedes': math.pi,
    }

    # Get all incoming relations (what other memories point to this one)
    edges = db.execute("""
        SELECT relation_type, COUNT(*) as cnt
        FROM knowledge_edges
        WHERE target_id = ? AND source_table = 'memories' AND target_table = 'memories'
        GROUP BY relation_type
    """, (memory_id,)).fetchall()

    if not edges:
        return 0.0  # Default: constructive

    # Weight phase by edge count
    total_weight = 0.0
    weighted_phase = 0.0

    for relation_type, count in edges:
        phase = PHASE_BY_RELATION.get(relation_type, 0.0)
        weighted_phase += phase * count
        total_weight += count

    # Circular mean (handle π wraparound)
    return (weighted_phase / total_weight) % (2 * math.pi)
```

**Pros:**
- Immediate, no training data needed
- Semantically grounded in relation types
- Fast to implement

**Cons:**
- Ignores co-activation frequency
- Assumes all contradictions are equally strong
- No learning from empirical patterns

---

## 3. Method 2: Destructive Interference Signature

**Data-driven approach.** Use co-activation patterns to detect destructive interference.

### 3.1 The Hypothesis

If two memories interfere **destructively**, they should be:
1. Similar in embedding (⟨ψ_i|ψ_j⟩ high)
2. Connected by a `contradicts` or `supersedes` edge
3. NOT frequently co-retrieved despite both being high-recall

If they interfere **constructively**, they should be:
1. Similar in embedding
2. Connected by a `semantic_similar`, `supports`, or `co_referenced` edge
3. FREQUENTLY co-retrieved

### 3.2 Co-Activation Ratio as Interference Measure

Define the **interference signature**:

```
ρ_ij = co_activation_count(i,j) / (recalled_count(i) + recalled_count(j) + ε)
```

This is the proportion of i's and j's combined retrievals that happen together.

**Interpretation:**
- `ρ_ij ≈ 1.0`: Maximum co-activation (very constructive)
- `ρ_ij ≈ 0.5`: Expected for independent memories
- `ρ_ij ≈ 0.0`: Suppressed co-activation (destructive)

### 3.3 Phase Inference from Signature

```python
def infer_phase_from_coactivation(
    src_id: int,
    tgt_id: int,
    db: sqlite3.Connection
) -> float:
    """
    Infer phase relationship from co-activation patterns.

    Returns phase in [0, 2π).
    """

    # Get co-activation data
    edge = db.execute("""
        SELECT co_activation_count, relation_type, weight
        FROM knowledge_edges
        WHERE source_id = ? AND target_id = ?
            AND source_table = 'memories' AND target_table = 'memories'
    """, (src_id, tgt_id)).fetchone()

    m_src = db.execute(
        "SELECT recalled_count FROM memories WHERE id = ?",
        (src_id,)
    ).fetchone()[0]

    m_tgt = db.execute(
        "SELECT recalled_count FROM memories WHERE id = ?",
        (tgt_id,)
    ).fetchone()[0]

    if not edge or m_src == 0 or m_tgt == 0:
        return 0.0  # No data, default constructive

    co_act, relation_type, weight = edge

    # Expected co-activation if independent
    # (assuming random retrieval): co_act_expected = P_i * P_j * N
    # where P_i, P_j are retrieval probabilities, N is total queries
    # Use empirical average as proxy
    avg_recalls = (m_src + m_tgt) / 2.0
    expected_coact = (m_src * m_tgt) / (150 * 150)  # Roughly: for 150 memories

    # Normalized interference ratio
    if expected_coact > 0:
        interference_ratio = co_act / max(expected_coact, 1)
    else:
        interference_ratio = 0.0

    # Map interference ratio to phase
    # High ratio → constructive (phase ≈ 0)
    # Low ratio → destructive (phase ≈ π)
    # Use S-curve to map [0, 2] to [π, 0] (inverted)

    phase = math.pi * (1.0 - sigmoid(interference_ratio - 1.0, k=2))

    return phase % (2 * math.pi)


def sigmoid(x: float, k: float = 1.0) -> float:
    """Sigmoid with adjustable steepness k."""
    return 1.0 / (1.0 + math.exp(-k * x))
```

**Pros:**
- Data-driven, respects empirical co-activation patterns
- Distinguishes constructive from destructive automatically
- Scalable as retrieval data accumulates

**Cons:**
- Requires sufficient co-activation samples (weak for new memories)
- Sensitive to random fluctuations in low-sample regime
- Circular mean needed to combine multiple edges properly

---

## 4. Method 3: Embedding-Based Phase from Similarity Angle

**Geometric approach.** Extract phase directly from embedding geometry.

### 4.1 The Intuition

In embedding space ℝ^768, each memory is a unit vector |ψ_i⟩. Two memories that are:
- **Aligned** (angle ≈ 0): phase difference ≈ 0 (constructive)
- **Orthogonal** (angle ≈ π/2): phase difference ≈ π/2
- **Anti-aligned** (angle ≈ π): phase difference ≈ π (destructive)

We can directly compute the phase relationship from the embedding angle:

```
φ_ij = angle_between(ψ_i, ψ_j)
```

### 4.2 Individual Memory Phase from Cluster

For an individual memory's `confidence_phase` relative to a query or cluster:

```python
def infer_phase_from_embedding(memory_id: int, db: sqlite3.Connection) -> float:
    """
    Infer phase from the memory's embedding position relative to cluster centroid.

    Memories in the same semantic cluster have phase ≈ 0.
    Outliers or contradictions have phase ≠ 0.
    """

    # Get embedding
    embedding = db.execute(
        "SELECT embedding FROM vec_memories WHERE memory_id = ?",
        (memory_id,)
    ).fetchone()

    if not embedding:
        return 0.0

    vec = np.frombuffer(embedding, dtype=np.float32)

    # Get category cluster for this memory
    category = db.execute(
        "SELECT category FROM memories WHERE id = ?",
        (memory_id,)
    ).fetchone()[0]

    # Get mean embedding for category
    rows = db.execute("""
        SELECT embedding FROM vec_memories v
        JOIN memories m ON v.memory_id = m.id
        WHERE m.category = ? AND m.retired_at IS NULL
    """, (category,)).fetchall()

    if not rows:
        return 0.0

    embeddings = [np.frombuffer(row[0], dtype=np.float32) for row in rows]
    cluster_mean = np.mean(embeddings, axis=0)
    cluster_mean = cluster_mean / np.linalg.norm(cluster_mean)

    # Angle between memory and cluster mean
    similarity = np.dot(vec, cluster_mean)
    similarity = np.clip(similarity, -1.0, 1.0)

    # Map similarity to phase
    # High similarity (phase ≈ 0): aligned with cluster
    # Low similarity (phase ≈ π): opposed to cluster
    angle = np.arccos(similarity)  # Range: [0, π]

    return angle % (2 * np.pi)
```

**Pros:**
- Purely geometric, doesn't require retrieval history
- Works immediately for new memories
- Captures semantic structure automatically

**Cons:**
- Assumes cluster structure exists
- Confuses "orthogonal" with "contradictory"
- Needs high-quality embeddings

---

## 5. Method 4: Bayesian Phase Learning from Retrieval Events

**Principled approach.** Treat phase as a latent variable in a probabilistic model.

### 5.1 The Graphical Model

For each memory i and each query, we observe:
- Was memory retrieved? (Y_i = 1 or 0)
- Did it co-occur with memory j? (C_ij = 1 or 0)

Latent variables:
- amplitude_phase_i ~ Uniform[0, 2π)
- amplitude_i = √(confidence_i) × exp(i × phase_i)

Generative model:
```
P(Y_i = 1 | q, phase_i) ∝ |⟨q|ψ_i⟩ + interference_term(phase_i)|²
P(C_ij = 1 | Y_i, Y_j, phase_i, phase_j) ∝ correlation term
```

### 5.2 Maximum Likelihood Estimation

```python
def learn_phase_bayesian(
    memory_id: int,
    db: sqlite3.Connection,
    max_iters: int = 100,
    learning_rate: float = 0.01
) -> float:
    """
    Learn phase via gradient ascent on log-likelihood.

    Uses observed retrieval events to infer the most likely phase.
    """

    # Initialize phase
    phase = 0.0  # Start with constructive

    # Get retrieval history for this memory
    # (In practice, would need to track query-retrieval events in a separate log)
    retrieval_events = []  # Format: [(query, was_retrieved, co_retrieved_ids), ...]

    for iteration in range(max_iters):
        # Compute log-likelihood under current phase
        log_likelihood = 0.0
        gradient = 0.0

        for query_embedding, was_retrieved, co_retrieved_ids in retrieval_events:
            # Compute amplitude with current phase
            amplitude = np.sqrt(confidence) * np.exp(1j * phase)

            # Similarity to query
            query_sim = np.dot(memory_embedding, query_embedding)

            # Interference from connected memories
            interference_sum = 0.0
            for neighbor_id, relation_type in co_retrieved_ids:
                neighbor_amp = ...  # Get neighbor's amplitude
                edge_weight = ...  # Get edge weight

                if relation_type == 'contradicts':
                    interference_sum -= edge_weight * neighbor_amp
                else:
                    interference_sum += edge_weight * neighbor_amp

            # Total amplitude (Born rule)
            total_amp = amplitude * query_sim + interference_sum

            # Probability of retrieval
            p_retrieve = np.abs(total_amp) ** 2

            # Log-likelihood
            if was_retrieved:
                log_likelihood += np.log(p_retrieve + 1e-10)
                gradient += np.log(p_retrieve) * ...  # Derivative
            else:
                log_likelihood += np.log(1 - p_retrieve + 1e-10)
                gradient += np.log(1 - p_retrieve) * ...

        # Gradient step
        phase += learning_rate * gradient
        phase = phase % (2 * np.pi)

    return phase
```

**Pros:**
- Statistically principled
- Incorporates full retrieval history
- Generates uncertainty estimates (posterior distribution)

**Cons:**
- Requires detailed event log (not yet tracked in brain.db)
- Computationally expensive (needs retrieval simulation)
- Sensitive to model misspecification

---

## 6. Method 5: Contradiction Graph Initialization

**Fast heuristic.** Use only the `contradicts` edge graph.

### 6.1 The Idea

Contradictory memories should have **opposing phases**:
```
If contradicts(m_a, m_b) with high confidence:
    phase_b ≈ phase_a + π
```

Treat this as a graph coloring problem: assign phases to minimize conflicts.

### 6.2 Algorithm

```python
def infer_phase_from_contradiction_graph(db: sqlite3.Connection) -> Dict[int, float]:
    """
    Assign phases to minimize contradiction edges' phase differences.

    Solves approximately via iterative refinement (like belief propagation).
    """

    # Build contradiction subgraph
    contradictions = db.execute("""
        SELECT source_id, target_id, weight
        FROM knowledge_edges
        WHERE relation_type = 'contradicts'
            AND source_table = 'memories' AND target_table = 'memories'
    """).fetchall()

    memory_ids = db.execute(
        "SELECT id FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    memory_ids = [row[0] for row in memory_ids]

    # Initialize phases (random or from heuristic)
    phases = {mid: 0.0 for mid in memory_ids}

    # Iterative refinement
    for iteration in range(20):
        for mem_id in memory_ids:
            energy = 0.0
            gradient = 0.0

            for src, tgt, weight in contradictions:
                if src == mem_id:
                    other = tgt
                elif tgt == mem_id:
                    other = src
                else:
                    continue

                # Desired phase difference: π (opposing)
                phase_diff = (phases[mem_id] - phases[other]) % (2 * np.pi)

                # Energy: how far from π?
                desired_diff = math.pi
                error = np.abs(phase_diff - desired_diff)

                # Gradient towards desired difference
                gradient_signal = np.sin(phase_diff - desired_diff) * weight
                gradient += gradient_signal

            # Update phase
            phases[mem_id] += 0.01 * gradient
            phases[mem_id] = phases[mem_id] % (2 * np.pi)

    return phases
```

**Pros:**
- Simple, interpretable algorithm
- Works well for contradiction-heavy graphs
- Fast (no matrix operations)

**Cons:**
- Ignores constructive relations
- May not converge if contradictions conflict
- Doesn't handle ambiguous cases

---

## 7. Hybrid Approach: Multi-Method Voting

**Recommended for production.** Combine all methods with weighted averaging.

### 7.1 Ensemble Voting Algorithm

```python
def infer_confidence_phase(
    memory_id: int,
    db: sqlite3.Connection,
    methods: Dict[str, float] = None
) -> float:
    """
    Infer phase by combining multiple methods with weighted voting.

    Args:
        memory_id: Target memory
        db: Database connection
        methods: Dict mapping method name → weight (default: equal weights)

    Returns:
        Inferred confidence_phase in [0, 2π)
    """

    if methods is None:
        methods = {
            'relation_type': 0.2,      # Heuristic (fast, weak)
            'coactivation': 0.3,       # Data-driven (medium)
            'embedding_angle': 0.2,    # Geometric (immediate)
            'contradiction_graph': 0.2, # Graph-based (categorical)
            'bayesian': 0.1             # Principled (slow)
        }

    phases = {}

    # Method 1: Relation type heuristic
    if 'relation_type' in methods:
        phases['relation_type'] = assign_phase_by_relation(memory_id, db)

    # Method 2: Co-activation signature
    if 'coactivation' in methods:
        edges = db.execute("""
            SELECT target_id FROM knowledge_edges
            WHERE source_id = ? AND source_table = 'memories'
                AND target_table = 'memories'
        """, (memory_id,)).fetchall()

        coact_phases = [
            infer_phase_from_coactivation(memory_id, tgt[0], db)
            for tgt in edges
        ]
        phases['coactivation'] = circular_mean(coact_phases) if coact_phases else 0.0

    # Method 3: Embedding angle
    if 'embedding_angle' in methods:
        phases['embedding_angle'] = infer_phase_from_embedding(memory_id, db)

    # Method 4: Contradiction graph
    if 'contradiction_graph' in methods:
        phases['contradiction_graph'] = infer_phase_from_contradiction_graph(db).get(memory_id, 0.0)

    # Combine via circular weighted mean
    total_weight = 0.0
    weighted_phase_sum = 0.0

    for method_name, phase in phases.items():
        weight = methods.get(method_name, 0.0)
        if weight > 0:
            weighted_phase_sum += weight * np.exp(1j * phase)
            total_weight += weight

    if total_weight > 0:
        result_phase = np.angle(weighted_phase_sum / total_weight)
        return result_phase % (2 * np.pi)
    else:
        return 0.0


def circular_mean(angles: List[float]) -> float:
    """Compute mean of circular data (angles)."""
    return np.angle(np.mean([np.exp(1j * a) for a in angles]))
```

---

## 8. Phase Update During Retrieval

### 8.1 Online Learning Rule

As memories are retrieved, we update phase estimates based on:
1. Whether co-retrieval happened when predicted
2. Whether co-retrieval was suppressed when predicted

```python
def update_phase_after_retrieval(
    retrieved_ids: List[int],
    db: sqlite3.Connection,
    update_rate: float = 0.05
) -> None:
    """
    Update confidence_phase for all retrieved memories based on actual co-retrieval.

    Implements a simple delta rule:
    phase_new = phase_old + η * (observed_coactivation - expected_coactivation)
    """

    # For each pair of retrieved memories, check if interference matched prediction
    for i, mem_a in enumerate(retrieved_ids):
        for mem_b in retrieved_ids[i+1:]:
            # Check if edge exists
            edge = db.execute("""
                SELECT relation_type, weight
                FROM knowledge_edges
                WHERE (source_id = ? AND target_id = ?)
                   OR (source_id = ? AND target_id = ?)
                AND source_table = 'memories' AND target_table = 'memories'
            """, (mem_a, mem_b, mem_b, mem_a)).fetchone()

            if not edge:
                continue

            relation_type, weight = edge

            # Observed: co-activation happened
            observed_coact = 1.0

            # Expected from phase
            phase_a = db.execute("SELECT confidence_phase FROM memories WHERE id = ?",
                                (mem_a,)).fetchone()[0]
            phase_b = db.execute("SELECT confidence_phase FROM memories WHERE id = ?",
                                (mem_b,)).fetchone()[0]

            phase_diff = (phase_a - phase_b) % (2 * np.pi)

            # Expected constructiveness from phase
            expected_coact = np.cos(phase_diff)  # Ranges [-1, 1]
            expected_coact = (expected_coact + 1) / 2  # Normalize to [0, 1]

            # Error signal
            prediction_error = observed_coact - expected_coact

            # Update phases in direction that reduces error
            if prediction_error > 0 and relation_type != 'contradicts':
                # Make phases more aligned (reduce phase_diff)
                phase_a_new = phase_a - update_rate * prediction_error
                phase_b_new = phase_b + update_rate * prediction_error
            elif prediction_error < 0 and relation_type == 'contradicts':
                # Make phases more opposite (increase phase_diff towards π)
                target_diff = math.pi
                phase_a_new = phase_a + update_rate * (-prediction_error)
                phase_b_new = phase_b - update_rate * (-prediction_error)
            else:
                continue  # Satisfied

            # Write back
            db.execute("UPDATE memories SET confidence_phase = ? WHERE id = ?",
                      (phase_a_new % (2 * math.pi), mem_a))
            db.execute("UPDATE memories SET confidence_phase = ? WHERE id = ?",
                      (phase_b_new % (2 * math.pi), mem_b))

    db.commit()
```

---

## 9. Implementation Plan

### Phase A: Initialization (hours 1-4)

1. Implement Method 1 (relation type heuristic) — 30 min
2. Compute initial `confidence_phase` for all 150 active memories
3. Add `confidence_phase REAL DEFAULT 0.0` column to memories table
4. Store initial phases in database

### Phase B: Validation (hours 4-8)

1. Implement Methods 2-5 (data-driven, geometric, Bayesian, graph-based)
2. Compare phase assignments across methods
3. Validate with controlled retrieval experiments
4. Measure inference consistency

### Phase C: Integration (hours 8-12)

1. Integrate phase into quantum amplitude scorer (COS-383)
2. Update retrieval ranking with interference-adjusted amplitudes
3. Implement online phase update rule
4. Benchmark P@5 improvement against baseline (target: >20%)

### Phase D: Documentation (hours 12-16)

1. Formal derivation of each method
2. Convergence analysis for Bayesian and graph-based methods
3. Validation experiment results
4. Code documentation and examples

---

## 10. Expected Results

Against the current amplitude scorer (20% P@5):

| Method | Phase Coverage | Convergence | P@5 Improvement | Effort |
|--------|---|---|---|---|
| Relation Type Heuristic | 100% | Immediate | +3% | Low |
| Co-Activation Signature | 50% (needs data) | Gradual | +5% | Medium |
| Embedding Angle | 100% | Immediate | +2% | Low |
| Contradiction Graph | 100% | Iterative | +4% | Medium |
| Bayesian Learning | 100% (after training) | Slow | +8% | High |
| **Hybrid Ensemble** | 100% | Immediate then improves | **+12-15%** | **Medium** |

---

## 11. References

- COS-379: Quantum Probability Foundations (Qubit)
- COS-380: Quantum Interference in Retrieval (Phase)
- COS-383: Amplitude Scoring (Amplitude) — dependent task
- Busemeyer & Bruza (2012): Quantum Models of Cognition and Decision
- Anderson et al. (1994): Retrieval-Induced Forgetting in human memory

---

**Next steps:** Implement Methods 1-3, run initial validation, report results.
