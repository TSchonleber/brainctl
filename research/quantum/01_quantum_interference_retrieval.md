# Quantum Interference in Memory Retrieval
## Constructive/Destructive Recall Patterns in brain.db

**Author:** Phase (Quantum Interference Engineer)
**Date:** 2026-03-28
**Task:** [COS-380](/COS/issues/COS-380) (re-filed from COS-370)
**Status:** Complete — Delivered

---

## Executive Summary

Memory retrieval in brain.db is currently a **commutative, additive** process: each candidate receives a salience score (0.45×sim + 0.25×recency + 0.20×confidence + 0.10×importance) and candidates are ranked independently. This model is fundamentally incapable of capturing a well-documented empirical phenomenon: **the order and co-presence of other candidates changes how any given candidate is scored**.

Quantum probability theory provides the formal machinery to model this. The central insight is that memory retrieval is not passive observation — it is **projection in Hilbert space**, and projections do not commute. Recalling memory A changes the probability of subsequently recalling memory B. This is not a metaphor; it maps directly to experimentally verified human cognition (Busemeyer & Bruza 2012) and to retrieval-induced forgetting documented in agent systems.

This document provides:
1. The formal quantum model for brain.db retrieval
2. Analysis of constructive and destructive interference patterns observable in the live 150-memory store
3. The double-slit retrieval thought experiment adapted to brain.db
4. A quantum-inspired interference scoring algorithm (see `quantum_interference_retrieval.py`)

---

## 1. Theoretical Foundations

### 1.1 The Hilbert Space Model

In classical retrieval, a query `q` returns a ranked list by independently scoring each memory `m_i`. The score function is a scalar product of static features — each memory is judged in isolation.

In the quantum model:
- The **memory store** is a Hilbert space `H` over ℝ^768 (the embedding dimension)
- Each **memory** `m_i` is a unit vector `|m_i⟩` in `H`
- A **query** is a unit vector `|q⟩` in `H`
- **Retrieval** is a sequence of projection operations

The retrieval probability of memory `m_i` given query `q` is:

```
P(m_i | q) = |⟨q|m_i⟩|²
```

This is the **Born rule** — the probability is the squared inner product (cosine similarity squared). This looks identical to classical cosine similarity until you introduce interference.

### 1.2 Why Interference Exists

The quantum model diverges from classical retrieval when **multiple candidates are present simultaneously**. A query `q` does not independently project onto each memory. Instead, the system is in a superposition of all possible retrievals:

```
|ψ_retrieval⟩ = Σ_i α_i |m_i⟩
```

where `α_i = ⟨m_i|q⟩` is the **probability amplitude** (not probability) for memory `m_i`. The total probability is `|α_i|²`, but crucially: **amplitudes add, not probabilities**. When two amplitudes point in the same direction (coherent), they constructively interfere and the combined probability is higher than the sum of individual probabilities. When they oppose, destructive interference reduces the combined probability below the classical sum.

The interference term between memories `m_a` and `m_b` for query `q`:

```
I(m_a, m_b | q) = 2 · Re(α_a · α_b*) · ⟨m_a|m_b⟩
                = 2 · ⟨q|m_a⟩ · ⟨q|m_b⟩ · ⟨m_a|m_b⟩
```

**This term is the key deliverable.** In brain.db terms:
- `⟨q|m_a⟩` = query-to-memory similarity (cosine, already computed)
- `⟨m_a|m_b⟩` = memory-to-memory similarity (partially captured by `semantic_similar` edges)
- The sign of the product determines constructive (positive) or destructive (negative) interference

### 1.3 Non-Commutative Projection: Order Effects

In classical retrieval, if you search for "billing" then "auth", you get the same top-k as searching "auth" then "billing". But agents do not retrieve in isolation — context from previous retrievals primes the state.

Formally, projection operators `P_A` and `P_B` satisfy:

```
P_A P_B ≠ P_B P_A  (in general)
```

This non-commutativity has been empirically measured in human cognition (Busemeyer & Bruza 2012, chapters 4-5) via **conjunction fallacy** and **question-order effects**. In brain.db:

- `brainctl search "billing"` projects onto the billing subspace
- The result changes the agent's primed state `|ψ'⟩`
- Subsequent `brainctl search "auth"` operates on `|ψ'⟩`, not on `|ψ_0⟩`

**Measurement in brain.db terms:** Each `brainctl search` call should update a lightweight **agent state vector** representing the current primed subspace. The next search applies interference corrections based on the last-retrieved cluster.

---

## 2. Constructive Interference Patterns

### 2.1 Definition

Constructive interference occurs when two candidate memories `m_a` and `m_b` are both highly similar to query `q` **and** similar to each other. Their amplitudes reinforce:

```
P(m_a + m_b | q) > P(m_a | q) + P(m_b | q)
```

In brain.db: when a query activates a semantic cluster where multiple memories mutually support the same topic, each member's retrieval probability is boosted beyond what cosine similarity alone would predict.

### 2.2 Live Data Analysis

Brain.db currently has 4,718 edges with 742 `semantic_similar` edges (avg weight 0.9986 — near-identical embeddings) and 871 `topical_tag` edges. These are the primary constructive interference pathways.

**Constructive interference hubs** in the current store (memories with the most semantic_similar edges) are likely the `permanent` class memories that encode system-state summaries — these appear in every context retrieval because their topic coverage overlaps with almost any query.

### 2.3 Practical Example

Memory #93 (the "memory spine state" permanent memory, recalled 125×) constructively interferes with nearly all project-related queries because:
1. It has high embedding similarity to most project/environment/decision category queries
2. Its co-reference edges link it to hundreds of subsequent memories
3. Interference with adjacent memories in the `project` category (59 active) consistently boosts the whole cluster

In the quantum model, this memory is a **high-amplitude node** that constructively interferes with other cluster members. Its 125 recall count is partly this amplification effect, not just raw relevance.

---

## 3. Destructive Interference: Retrieval-Induced Forgetting

### 3.1 Definition

Destructive interference occurs when two memories `m_a` and `m_b` are both similar to query `q` but similar to each other in a way that **suppresses** mutual retrieval:

```
P(m_a + m_b | q) < P(m_a | q) + P(m_b | q)
```

This is the quantum model of **retrieval-induced forgetting (RIF)** — a well-replicated finding in human memory research (Anderson et al. 1994). Practicing recall of one category member suppresses recall of related-but-distinct members.

### 3.2 The Contradiction Edge as Destructive Interference

In brain.db, `contradicts` edges in `knowledge_edges` are the natural substrate for destructive interference. When memory `m_a` contradicts memory `m_b`, their probability amplitudes should carry opposing signs in queries where both are relevant:

```
If contradicts(m_a, m_b) with weight w:
  I(m_a, m_b | q) = -2w · ⟨q|m_a⟩ · ⟨q|m_b⟩ · |⟨m_a|m_b⟩|
```

**Current gap:** The contradiction detection module (Wave 1 `06_contradiction_detection.py`) detects contradictions and adds `contradicts` edges, but the retrieval scorer does not use these edges to apply interference corrections. The quantum model provides principled machinery to do this.

### 3.3 Supersession as Completed Decoherence

When `m_a.supersedes_id = m_b.id`, the old memory `m_b` has undergone **complete destructive interference** with `m_a` — its amplitude in any retrieval is effectively zero once it is retired. But the quantum model suggests a subtler treatment: a superseded (but not yet retired) memory should still contribute a **negative correction** to queries that might retrieve it, as its outdated content would mislead the agent if retrieved alone.

---

## 4. The Double-Slit Experiment in brain.db

### 4.1 The Analogy

In the quantum double-slit experiment:
- A photon passes through **two slits simultaneously**
- On the detection screen, the photon appears where amplitudes constructively interfere
- Crucially: **observing which slit the photon passes through destroys the interference pattern**

In brain.db retrieval:
- A query can "go through" multiple semantic clusters simultaneously (analogy: two slits = two topic clusters)
- The retrieval result should show interference between the clusters — the most relevant memories are those that bridge or reinforce both clusters
- **Observation** corresponds to the agent explicitly reading a specific memory: once read, it collapses to a definite state and subsequent retrievals are conditioned on that choice

### 4.2 Experimental Design for brain.db

**Setup:**
- Query `q` that has high similarity to **two distinct semantic clusters** (e.g., "consolidation cycle" sits at the intersection of `project` and `environment` categories)
- Cluster A: `project:costclock-ai` memories
- Cluster B: `environment:system` memories

**Classical prediction:** Top-k by cosine similarity returns mixed bag from both clusters based on independent scores.

**Quantum prediction:** Memories that are **semantically central to both clusters** receive constructive amplitude boost from two paths, appearing higher in the ranking. Memories at the periphery of one cluster while near the other receive destructive correction.

**Test protocol:**
```python
# Measure interference pattern (full quantum scoring)
results_quantum = quantum_interference_search(q, memories, edges)

# Measure "which path" (collapse to one cluster)
results_cluster_a = classical_search(q, filter=cluster_a_ids)
results_cluster_b = classical_search(q, filter=cluster_b_ids)

# Verify: quantum_overlap(A,B) < classical_overlap(A,B)
# Interference removed = higher precision, cleaner separation
```

### 4.3 Expected Empirical Finding

Given brain.db's 150 active memories with 742 semantic_similar edges and 1,686 causes edges, we expect:
1. **15-25 memories** that bridge multiple clusters (cross-category, high edge degree) to receive measurable constructive interference boosts
2. **Memory #130** (the 3-topic permanent memory with embedded costclock+neuro+paperclip content, recalled 116×) should receive the largest constructive interference correction of any memory in the store — its embedding similarity with queries spans 3 clusters simultaneously
3. Memories with `contradicts` edges to high-salience memories should show measurable destructive interference suppression

---

## 5. Quantum-Inspired Interference Scorer

### 5.1 Algorithm Design

The quantum-inspired scorer replaces the classical additive formula with an amplitude-based calculation that accounts for pairwise interference between candidate memories.

**Phase 1 — Classical candidate retrieval (unchanged):**
```
candidates = top_100_by_salience(q)  # existing FTS5+vec pipeline
```

**Phase 2 — Amplitude assignment:**
```
α_i = cosine_sim(q, m_i)  # standard embedding similarity
```

**Phase 3 — Interference correction matrix:**
For each candidate pair (i, j):
```
I_ij = edge_weight(m_i, m_j) * sign_from_edge_type(relation_type) * α_i * α_j
```

Where `sign_from_edge_type` is:
- `semantic_similar`, `supports`, `co_referenced`, `topical_tag`: **+1** (constructive)
- `contradicts`: **-1** (destructive)
- `supersedes`: **-0.5** (partial destructive — outdated, not contradictory)
- `causes`, `derived_from`: **+0.3** (weak constructive — related but not synonymous)

**Phase 4 — Interference-adjusted probability:**
```
P_i = α_i² + Σ_j I_ij   (where j ranges over top-k candidates)
```

Clamp to [0, 1]. Normalize.

**Phase 5 — Re-rank by P_i**

### 5.2 Computational Complexity

For top-k candidates (typically k=20-50):
- Interference matrix: O(k²) pairwise comparisons
- Edge lookup: O(k²) SQLite JOIN on knowledge_edges (indexed)
- Total additional cost per query: O(k²) ≈ 400-2500 operations at k=20-50

At 150 active memories and 4,718 edges, this is trivially fast in Python. At 10,000 memories, caching the interference matrix for frequently co-retrieved memory pairs would be needed.

### 5.3 Expected Benchmark Results

Against the current salience scorer (0.45×sim + 0.25×recency + 0.20×confidence + 0.10×importance):
- **Precision improvement**: +10-20% estimated based on cluster structure (742 semantic_similar edges creating strong interference pathways)
- **Recall neutral**: by design — we only re-rank the top-100 candidate pool, not gate retrieval
- **Contradiction suppression**: eliminates contradicted memories from top results when a more-confident superseding memory is in the candidate set

See `quantum_interference_retrieval.py` for the full implementation.

---

## 6. Connection to Retrieval-Induced Forgetting in Agent Systems

The destructive interference model directly addresses a known pathology in brain.db: when multiple agents retrieve the same cluster of related memories, they strengthen the most-recalled memories while leaving related-but-distinct memories in the same cluster with diminishing recall counts. This creates a **retrieval attractor** — the same memories are always returned, starving adjacent memories of recall boosts.

**Mitigation via the quantum model:**
- Interference-aware scoring detects when a high-recall memory (`α_i` large) is semantically similar to lower-recall candidates
- When candidate `m_i` has large `recalled_count` and high `semantic_similar` edge weight to `m_j`, the interference term I_ij boosts `m_j`'s probability even when its standalone amplitude is lower
- This models the **spreading activation enhancement** from constructive interference

This is the inverse of destructive interference — rather than suppressing, it redistributes probability mass toward under-recalled cluster members, preventing attention attractor lock-in.

---

## 7. Order Effects: A Practical Recommendation

Brain.db does not currently track within-session retrieval order. The quantum model suggests a **lightweight session state vector** that accumulates projections:

```
|ψ_session⟩ = normalize(Σ_retrieved_memories  recalled_embedding_i)
```

After each retrieval, bias subsequent queries toward this accumulated state by interpolating:

```
q_effective = (1 - λ) * q + λ * ψ_session  (λ = 0.2–0.3)
```

This implements **memory priming**: each search biases subsequent searches toward contextually coherent memories rather than treating each search as independent. The `recalled_count` boost on retrieval already does this weakly across sessions; the session state vector does it strongly within a session.

---

## 8. Relationship to CQT (Classical-Quantum-Transition)

The quantum model should not replace classical retrieval. The right architecture is:

1. **High decoherence** (ephemeral/short memories, weak edges): use classical scoring — interference corrections are small and noisy
2. **Low decoherence** (permanent/long memories with strong semantic_similar or contradicts edges): apply full quantum interference correction — signal is clean and corrections are meaningful

The 8 permanent memories and 4 short memories in the active store are the **natural starting point** for quantum interference scoring. They have the highest embedding quality, the most recall data, and the most knowledge edges — all conditions for meaningful interference rather than noise amplification.

---

## 9. Summary: Implementation Roadmap

| Phase | Action | Impact | Effort |
|-------|--------|--------|--------|
| 1 | Add `interference_weight` to retrieval output | Observability | 1 hour |
| 2 | Apply destructive interference for `contradicts` edges | Precision | 2 hours |
| 3 | Apply constructive interference for `semantic_similar` edges | Recall quality | 2 hours |
| 4 | Add session state vector (within-session priming) | Coherence | 4 hours |
| 5 | Implement full amplitude matrix for top-50 candidates | Full quantum | 1 day |

The Python prototype in `quantum_interference_retrieval.py` covers Phases 2-3 completely and Phases 4-5 as proof-of-concept.

---

## References

- Busemeyer, J.R. & Bruza, P.D. (2012). *Quantum Models of Cognition and Decision*. Cambridge University Press.
- Pothos, E.M. & Busemeyer, J.R. (2013). Can quantum probability provide a new direction for cognitive modeling? *Behavioral and Brain Sciences*, 36(3), 255–274.
- Sordoni, A., et al. (2013). Modeling latent topic interactions using quantum interference for information retrieval. *CIKM 2013*.
- Uprety, S., et al. (2020). A survey of quantum theory inspired approaches to information retrieval. *ACM Computing Surveys*, 53(5).
- Anderson, M.C., Bjork, R.A., & Bjork, E.L. (1994). Remembering can cause forgetting: retrieval dynamics in long-term memory. *Journal of Experimental Psychology: Learning, Memory, and Cognition*, 20(5), 1063–1087.
- Wave 1 COS-370 (original filing) — full spec
- Brain.db at heartbeat: 150 active memories, 825 total, 4,718 knowledge edges
