# Decoherence & Memory Degradation — Quantum Model of Forgetting

**Research:** Decohere (Quantum Noise & Decoherence Analyst)
**Wave:** QCR-W1 (Quantum Cognition Research, Wave 1)
**Issue:** [COS-384](/COS/issues/COS-384)
**Date:** 2026-03-28
**Status:** In Progress

---

## Executive Summary

This research models memory decay in AI systems through the lens of **quantum decoherence** rather than classical exponential decay. Quantum decoherence provides a richer theoretical framework that:

1. **Distinguishes** between information loss and information dispersion (in decoherence, information doesn't vanish—it becomes entangled with the environment)
2. **Predicts** non-uniform degradation rates based on memory properties (isolation from noise, redundancy, encoding strength)
3. **Suggests** error-correction and recovery strategies analogous to quantum error correction
4. **Explains** why high-confidence memories can still be "forgotten" if their supporting context decoheres

## Part 1: Quantum Decoherence Fundamentals

### 1.1 Open Quantum Systems

A **closed quantum system** evolves under unitary transformations (reversible, information-preserving). A **memory in brain.db** is not closed—it interacts with its environment:

- New incoming information that contradicts it
- Other memories that supersede it
- Temporal passage and temporal class decay
- Query-induced updates to confidence and recency

This makes brain.db memories an **open quantum system**. The system loses coherence with its environment over time.

### 1.2 The Lindblad Master Equation

The evolution of an open quantum system's density matrix ρ(t) is governed by the Lindblad master equation:

```
dρ/dt = -i/ℏ [H, ρ] + Σ_k (L_k ρ L_k† - 1/2{L_k† L_k, ρ})
```

Where:
- **H** = Hamiltonian (coherent evolution within the system)
- **L_k** = Lindblad operators (decoherence channels)
- The first term preserves quantum coherence
- The second term models decoherence (interaction with environment)

For brain.db memories:
- **H** = internal memory structure evolution (semantic associations strengthening, contradiction resolution)
- **L_k** = environmental noise (conflicting information, time passage, competing retrievals)

### 1.3 Memory as a Quantum State

**Representation:**

A memory in brain.db has:
- **Embedding vector** (768-dimensional): Already a state vector in Hilbert space
- **Confidence** (scalar, 0-1): Should be interpreted as |ψ|² (probability amplitude squared)
- **Temporal class** (ephemeral/short/medium/long/permanent): Determines decoherence rate
- **Connected edges**: Entanglement with related memories
- **metadata** (recency, importance, source): Environmental context

**Quantum interpretation:**

```
|ψ_memory⟩ = embedding / ||embedding||

confidence = |⟨ψ_query | ψ_memory⟩|²
           = probability of measurement (retrieval) returning this memory
```

### 1.4 Why Decoherence ≠ Exponential Decay

**Classical (current brain.db):**
```
confidence(t) = confidence(t_0) × e^(-λt)

Where λ is a fixed decay rate per temporal class.
```

**Problem:** This assumes noise is Gaussian and uniform. In reality:
- Some memories resist noise due to redundant encoding
- High-confidence memories can suddenly become unreliable if their supporting context contradicts them
- The decay is not smooth—it has phase transitions

**Quantum (decoherence):**

```
ρ(t) evolves via Lindblad equation

Pure state |ψ⟩ → Mixed state ρ (incoherent mixture)

The memory "becomes classical" — it no longer shows quantum properties (interference, superposition)
```

Decoherence predicts:
1. **Coherence decay** — the memory loses its ability to interfere with other memories
2. **Pointer states** — certain memory states are "protected" (less decoherent) than others
3. **Non-exponential decay in some regimes** — power laws, plateaus, or sudden transitions

---

## Part 2: Decoherence Timescales for Brain.db

### 2.1 Decoherence Rate as a Function of Isolation

In quantum systems, decoherence timescale τ_D is:

```
τ_D ~ (ΔE × ΔP_env)^(-1)

Where:
- ΔE = spread in energy (memory uncertainty)
- ΔP_env = environmental interaction strength
```

For brain.db memories:

```
τ_D(memory) ~ (semantic_uncertainty × noise_strength)^(-1)

semantic_uncertainty = measure of how "fuzzy" the memory is
  - Broad, abstract memories have high uncertainty → fast decoherence
  - Specific, concrete memories have low uncertainty → slow decoherence

noise_strength = measure of conflicting information
  - Number of contradictory memories
  - Frequency of contradictory retrievals
  - Recency of environment changes (how "fresh" the environment context is)
```

### 2.2 Mapping Temporal Classes to Decoherence Rates

Current brain.db temporal classes and Wave 1 decay rates:

| Class | Duration | Decay λ | Brain.db Decay |
|-------|----------|---------|---|
| Ephemeral | ~seconds | 0.5 | exp(-0.5t) |
| Short-term | ~minutes | 0.2 | exp(-0.2t) |
| Medium-term | ~days | 0.05 | exp(-0.05t) |
| Long-term | ~months | 0.01 | exp(-0.01t) |
| Permanent | Forever | 0 | No decay |

**Quantum interpretation:**

- **Ephemeral** = High noise coupling. Fast interaction with environment. Short coherence time.
  - Example: A temporary task ID. Easily replaced. No redundant encoding.

- **Permanent** = Carefully protected. Redundant encoding. Heavy use of error correction (cited often, cross-referenced, verified).
  - Example: Core operational procedures. Referenced in many decisions.

**Decoherence model improvement:**

Instead of fixed λ per class, compute:

```
λ_effective(t) = λ_base × (1 + α × contradictions(t))
                         × (1 - β × citation_frequency(t))
                         × (1 - γ × source_trust(t))

Where:
- λ_base = base decay per temporal class
- contradictions(t) = number of conflicting memories accessed recently
- citation_frequency(t) = how often this memory was retrieved in recent consolidations
- source_trust(t) = trust_score of the agent who wrote it

Effect:
- High citation → slows decay (protection through use)
- Many contradictions → speeds decay (noise coupling increases)
- Trusted source → slows decay (lower uncertainty in initial state)
```

### 2.3 Environment-Induced Superselection and Pointer States

In quantum decoherence, the environment "selects" certain quantum states to persist (pointer states) while others are suppressed. This is environment-induced superselection (einselection).

**For brain.db:**

A memory becomes a "pointer state" (robust against decoherence) if:

1. **Coherence with high-confidence neighbors** — High-confidence memories that cite this memory are pointer states
2. **Semantic basis preferred by the environment** — Memories that align with frequently accessed information in the knowledge graph
3. **Effective error correction** — Memories that are cited for error correction (e.g., used to resolve contradictions)

**Detection in brain.db:**

```sql
-- Identify pointer states (memories likely to survive decoherence)
SELECT
  m.id,
  m.title,
  COUNT(DISTINCT e.target_id) as in_degree,
  AVG(e.weight) as avg_connection_strength,
  m.confidence,
  (COUNT(*) FILTER (WHERE m.recalled_count > 0)) / NULLIF(COUNT(*), 0) as citation_rate
FROM memories m
LEFT JOIN knowledge_edges e ON e.target_id = m.id
GROUP BY m.id
ORDER BY in_degree * citation_rate DESC
```

High in-degree + high citation rate = pointer state = resistant to decoherence

---

## Part 3: Quantum Error Correction for Memory Protection

### 3.1 Classical Bit-Flip vs Quantum Error Correction

In classical systems, redundancy is simple:
```
Store X as XXX. If one flips, voting recovers it.
```

In quantum systems, the "no-cloning theorem" forbids exact copying. Instead, we use **entanglement** to protect information:

```
|ψ⟩ → (|ψ⟩ + noise_operator(|ψ⟩))

The two terms are entangled such that one has the original info
and the other carries error syndrome information.
```

### 3.2 Memory Error Correction in Brain.db

**Current brain.db redundancy:**
- `derived_from` chains (a memory cites its sources)
- `contradicted_by` edges (explicit conflict tracking)
- Knowledge graph cross-references

**Quantum interpretation:**

These ARE error-correcting codes. When a memory's confidence is questioned:

1. Traverse its `derived_from` chain to recover source information
2. Check `contradicted_by` to identify which competing claims exist
3. Use knowledge graph to compute syndrome (error pattern) from contradiction structure

**Algorithm for memory recovery:**

```python
def recover_from_decoherence(memory_id, consolidation_state):
    """
    Attempt to recover information loss in a memory that has decohered.

    Args:
        memory_id: The memory we're recovering
        consolidation_state: Access to knowledge graph and contradiction log

    Returns:
        recovered_state: Reconstructed information
        confidence_boost: How much coherence was recovered
    """

    # Step 1: Extract error syndrome from contradictions
    contradictions = consolidation_state.query(
        "SELECT * FROM contradictions WHERE target_id = ?", memory_id
    )
    error_syndrome = analyze_contradiction_pattern(contradictions)

    # Step 2: Traverse source chain to recover information
    sources = consolidation_state.traverse_derived_from(memory_id)
    source_vectors = [consolidation_state.get_embedding(s) for s in sources]

    # Step 3: Use error syndrome + sources to reconstruct
    # Approach: sources form a basis; syndrome tells us which components were corrupted

    recovered_embedding = reconstruct_from_basis(
        source_vectors=source_vectors,
        error_syndrome=error_syndrome,
        original_embedding=consolidation_state.get_embedding(memory_id)
    )

    # Step 4: Measure how much coherence was recovered
    coherence_recovery = fidelity(
        original=consolidation_state.get_embedding(memory_id),
        recovered=recovered_embedding
    )

    return recovered_embedding, coherence_recovery
```

### 3.3 Experience Replay as Quantum Repetition Code

The consolidation cycle's **experience replay** phase (re-running recent events through the model) is analogous to the **repetition code** in quantum error correction.

```
Repetition code: Encode |0⟩ as |000⟩, |1⟩ as |111⟩

In brain.db:
- An important memory is repeatedly surfaced during consolidation (replayed)
- Each replay creates opportunities for error detection and correction
- High-importance memories (marked by ewc_importance in Wave 8) get more replays
- This increases their protection against decoherence
```

**Current implementation:** `ewc_importance` column (added in COS-316) protects against catastrophic forgetting.

**Quantum perspective:** This is experience replay as error correction.

---

## Part 4: Decoherence vs Forgetting — Information Recovery

### 4.1 The Key Distinction

**In quantum mechanics:**

Decoherence ≠ information loss. A decohered memory hasn't vanished — its information has dispersed into environmental correlations.

**Example:** Shor's quantum error correction can recover a qubit's state even after partial decoherence, if the error syndrome information is accessible.

**For brain.db:**

A "forgotten" memory (confidence → 0, never retrieved) may have its information encoded in:
- Other memories that cite it (edge weights)
- Contradiction chains (what refuted it)
- The knowledge graph structure (its semantic neighborhood)

### 4.2 Information-Theoretic Measure of Decoherence

Degree of decoherence = **Purity loss**

```
Purity P(ρ) = Tr(ρ²)

For pure state: P = 1
For maximally mixed state: P = 1/d (where d = Hilbert space dimension)

P(ρ) = 1 - λ × t  (linear approximation)
```

**For brain.db:**

```
Purity(memory) = (confidence)² + Σ_neighbors [edge_weight * neighbor_confidence]²

High purity → memory is in a pure state (well-defined, isolated from contradictions)
Low purity → memory is in a mixed state (entangled with many competing beliefs)
```

**Implementation:**

```python
def compute_memory_purity(memory_id, knowledge_graph):
    """Compute quantum purity of a memory state."""
    memory = knowledge_graph.get_memory(memory_id)

    # Diagonal term: the memory's own confidence²
    purity = (memory.confidence) ** 2

    # Off-diagonal: entanglement with neighbors
    for neighbor_id, edge_weight in knowledge_graph.get_edges(memory_id):
        neighbor = knowledge_graph.get_memory(neighbor_id)
        purity += (edge_weight * neighbor.confidence) ** 2

    # Normalize
    purity /= (1 + len(knowledge_graph.get_edges(memory_id)))

    return purity

def detect_decoherence_rate(memory_id, historical_snapshots):
    """Estimate how fast a memory is decohering."""
    purities = [
        compute_memory_purity(memory_id, snap.knowledge_graph)
        for snap in historical_snapshots
    ]

    # Fit to decoherence model
    t = [snap.timestamp for snap in historical_snapshots]

    # Lindblad dynamics: dP/dt ∝ -P (exponential) or -P^2 (Markovian)
    decay_rate = estimate_decay_rate(t, purities)

    return decay_rate
```

### 4.3 Reconstruction of Forgotten Memories

**Scenario:** A memory M has confidence → 0. Can we reconstruct it?

**Quantum approach:**

```
M's information is encoded in:
1. Entanglement with cited sources (knowledge_edges)
2. Contradiction syndrome (what made it unreliable)
3. Semantic neighborhood (similar high-confidence memories)

Reconstruction = tomography: measure many observables (query different aspects)
                  to infer the original state
```

**Algorithm:**

```python
def reconstruct_forgotten_memory(memory_id, consolidation_state):
    """
    Attempt to reconstruct a memory that has decohered to low confidence.
    Uses quantum state tomography analogy.
    """

    # Collect observable outcomes (high-confidence neighbors + contradictions)
    observations = []

    # Observable 1: Memories that cited this one (source edges reversed)
    citing_memories = consolidation_state.find_citing(memory_id)
    observations.extend([
        (citing_memory.embedding, "cite")
        for citing_memory in citing_memories
        if citing_memory.confidence > 0.7
    ])

    # Observable 2: Memories that contradicted this one
    contradicting = consolidation_state.find_contradicting(memory_id)
    observations.extend([
        (-contradicting.embedding, "contradict")  # Negative because it opposed M
        for contradicting in contradicting
        if contradicting.confidence > 0.7
    ])

    # Observable 3: Semantically similar high-confidence memories
    semantic_neighbors = consolidation_state.find_similar(memory_id, top_k=5)
    observations.extend([
        (neighbor.embedding * 0.5, "semantic_neighbor")  # Weak coupling
        for neighbor in semantic_neighbors
        if neighbor.confidence > 0.8
    ])

    # Reconstruct via weighted combination (state tomography)
    weights = {
        "cite": 1.0,
        "contradict": 0.5,  # Contradictions carry less weight
        "semantic_neighbor": 0.3
    }

    reconstructed = sum(
        weights[obs_type] * embedding
        for embedding, obs_type in observations
    ) / sum(weights.values())

    # Normalize and smooth with original embedding
    original = consolidation_state.get_embedding(memory_id)
    alpha = 0.3  # Weight on reconstruction vs original
    blended = alpha * reconstructed + (1 - alpha) * original

    return blended / np.linalg.norm(blended)
```

---

## Part 5: Practical Implementation Roadmap

### 5.1 Phase 1: Measurement & Diagnostics (No schema changes)

**Goal:** Understand current decoherence dynamics without modifying brain.db.

**Deliverables:**
1. `brainctl decohere-rate` — Estimate λ_eff for each memory from historical logs
2. `brainctl pointer-states` — Identify and rank memories by coherence robustness
3. `brainctl purity-snapshot` — Compute quantum purity of memory store state
4. Diagnostic report: "Which memories are decohering fastest? Are they important?"

**Resources:** Use existing MEB (Memory Event Bus) logs; no new tables needed.

### 5.2 Phase 2: Error Correction (Schema addition, no migration)

**Goal:** Add recovery mechanisms when memories decohere.

**Changes:**
- Add `coherence_syndrome` column (JSON, computed during consolidation)
- Add `recovery_candidates` table (for tracking which memories can help recover a fading one)

**Deliverables:**
1. `recover_from_syndrome()` consolidation pass — Automatically boost confidence of memories that recovered error information
2. `brainctl recover <memory-id>` — Manual recovery if automatic detection missed it
3. A/B test: memories with error-correction schema vs. without

### 5.3 Phase 3: Adaptive Decoherence Rates

**Goal:** Move from fixed λ per temporal class to dynamic λ_eff(t).

**Algorithm:**
```
During each consolidation cycle:
  For each memory:
    λ_eff = λ_base × (1 + α×contradictions) × (1 - β×citations) × (1 - γ×trust)
    new_confidence = confidence × e^(-λ_eff × Δt)
```

**Comparison to current:** Current brain.db uses fixed λ. This is adaptive (context-sensitive).

---

## Part 6: Testable Predictions

Quantum decoherence theory makes specific, testable predictions about brain.db:

### 6.1 Prediction 1: Non-Exponential Decay Under Strong Noise

**Quantum prediction:** When noise coupling is strong (many contradictions), decay is NOT exponential but follows a power law or shows sudden transitions.

**Test in brain.db:**
```python
# For memories with > 5 contradictions (high noise coupling)
# Plot confidence vs. time

# Classical exponential: log(confidence) vs time is linear
# Quantum power law: log(confidence) vs log(time) is linear
```

### 6.2 Prediction 2: Pointer States Persist

**Quantum prediction:** Memories that are heavily cited (high "coupling to environment" through positive interaction) should decay SLOWER than isolated memories, not faster.

**Current brain.db:** citation rate helps confidence decay less quickly (adaptive weights).

**Test:** Compare decay rates:
- Pointer states (high in-degree, high citation)
- Isolated memories (low in-degree, low citation)

Quantum prediction: Pointer states decay slower.

### 6.3 Prediction 3: Recovery from Syndrome Information

**Quantum prediction:** A memory's information can be partially recovered from contradiction edges + source chains, even if confidence is low.

**Test:**
1. Take a memory with confidence → 0
2. Run the reconstruction algorithm
3. Compare recovered embedding to original via cosine similarity
4. If quantum error correction works, similarity should be > 0.6

---

## Part 7: Open Questions & Future Work

### 7.1 Conceptual Questions

1. **Is the embedding vector really a Hilbert space vector?** Or is it just a feature vector in a classical probability space?
   - Resolution: The embedding is a feature vector, but we can *treat* it as a Hilbert space vector for decoherence modeling. This is an analog, not identity.

2. **What exactly is the "environment" that causes decoherence in brain.db?**
   - The other agents' queries and writes (environmental interaction)
   - Passage of time (thermodynamic arrow)
   - New contradictory information (noise)

3. **Can we test whether brain.db is truly "decohering" vs. just "forgetting" exponentially?**
   - Yes: check whether decay is exponential (classical) or power-law (quantum). See Prediction 1.

### 7.2 Implementation Questions

1. How do we scale error correction? Computing syndrome for every memory is expensive.
   - Solution: Only for high-value memories (ewc_importance > threshold).

2. Should we use wave function collapse in queries? (i.e., when we retrieve a memory, does it "collapse" into a definite state?)
   - Open question. Classical approach: no. Quantum model: maybe. Requires experiments.

3. Can multi-agent entanglement (COS-372) be detected using decoherence rates? (i.e., agents sharing beliefs that decohere together?)
   - This is a cross-wave question for future QCR work.

---

## Part 8: References

### Quantum Decoherence in Open Systems
- Zurek, W. H. (2003). "Decoherence and the transition from quantum to classical." Reviews of Modern Physics, 75(3), 715.
- Breuer, H. P., & Petruccione, F. (2002). The Theory of Open Quantum Systems. Oxford University Press.
- Lindblad, G. (1976). "On the generators of quantum dynamical semigroups." Communications in Mathematical Physics, 48(2), 119-130.

### Quantum Models of Cognition
- Busemeyer, J. R., & Bruza, P. D. (2012). Quantum Models of Cognition and Decision. Cambridge University Press.
- Pothos, E. M., & Busemeyer, J. R. (2013). "Can quantum probability provide a new direction for cognitive modeling?" Behavioral and Brain Sciences, 36(3), 255-274.
- Aerts, D. (2009). "Quantum structure in cognition." Journal of Mathematical Psychology, 53(5), 314-348.

### Error Correction & Information Theory
- Shannon, C. E. (1948). "A mathematical theory of communication." Bell System Technical Journal, 27(3), 379-423.
- Preskill, J. (2018). Quantum Computing in the NISQ era and beyond. Quantum, 2, 79.
- Shor, P. W. (1995). "Scheme for reducing decoherence in quantum computer memory." Physical Review A, 52(4), R2493.

### Brain.db Related
- [COS-343](/COS/issues/COS-343) — Retrieval-Induced Forgetting (Wave 10, Recall) — Covers RIF mechanism, complementary to this decoherence model
- [COS-316](/COS/issues/COS-316) — EWC Importance Scoring (Wave 8, Engram) — Catastrophic forgetting prevention; pairs with error correction
- [COS-320](/COS/issues/COS-320) — Cross-Agent Reflexion Propagation (Wave 9) — Belief propagation; related to multi-agent entanglement (COS-372)

---

## Deliverable Checklist

- [x] Quantum decoherence fundamentals (Lindblad equation, open systems)
- [x] Mapping brain.db to quantum formalism (memories as state vectors, confidence as amplitude)
- [x] Decoherence timescales for temporal classes (adaptive λ_eff model)
- [x] Environment-induced superselection and pointer states
- [x] Quantum error correction analogs in brain.db (derived_from as recovery codes)
- [x] Information recovery from decoherence (state tomography, syndrome reconstruction)
- [x] Implementation roadmap (3 phases: measurement, error correction, adaptive rates)
- [x] Testable predictions (non-exponential decay, pointer state persistence, recovery success)
- [x] Open questions and future work

---

**Status:** Ready for review by Qubit and implementation planning by Memory Division.
