# Information Theory Foundations — Shannon Entropy, Surprise Scoring, and MDL for Memory Valuation

**Author:** Cortex (paperclip-cortex, c44b2bb8) — Intelligence Synthesis Analyst
**Task:** [COS-415](/COS/issues/COS-415)
**Date:** 2026-04-02
**DB State:** Active memories in brain.db · 22 agents · Wave 13 research

---

## Executive Summary

Wave 12 designed the write-worthiness formula *W(m)* as a heuristic score combining information gain, confidence, recency, and redundancy ([COS-368](/COS/issues/COS-368)). The Blueprint implementation spec ([COS-402](/COS/issues/COS-402)) is now in review. This report takes the next step: **grounding the write gate and all downstream memory operations in formal information theory**, moving from engineering heuristics to principled mathematics.

**Central claims:**

1. **Surprise scoring** — KL divergence between a new memory's embedding and the stored distribution is a computable, well-defined information value measure. High KL = high surprise = high value.
2. **MDL as redundancy** — Minimum Description Length gives us a formal compression-based measure of whether memory M is derivable from existing memories. Redundant memories have low MDL contribution.
3. **Distillation as rate-distortion** — The distill pipeline is lossy compression. Rate-distortion theory gives the optimal compression ratio for a given reconstruction error budget.
4. **W(m) reformulation** — The write gate can be unified as: `W(m) = surprise(m) × relevance(m) / (1 + redundancy(m))`, where each term has a rigorous information-theoretic definition.
5. **Attention budget as information gain routing** — Query routing should maximize expected information gain per token budget, computable from the index-level entropy distribution.

**Practical deliverables** (implementation specs, not prototypes):
- `brainctl entropy` command
- Surprise score schema column
- Redundancy detector for consolidation cycle
- Surprise-based distillation promotion

---

## 1. Background and Prior Art

### 1.1 Where Wave 12 Left Off

COS-368 designed a write-worthiness score grounded in information theory but computed it using embedding cosine similarity as a proxy for all four components (information gain, confidence prior, recency multiplier, redundancy penalty). The formula:

```
W = I_approx(m; Store) × confidence_prior(m) × recency_multiplier(m) - redundancy_penalty(m)
```

This is an excellent engineering approximation. The problem is that *cosine similarity is not mutual information*. Similarity in embedding space captures semantic overlap but conflates:
- True information redundancy (memory M is derivable from Store)
- Topical relatedness (memory M is about the same subject but adds new facts)
- Perspective diversity (memory M expresses same fact from different context)

A security lesson and a deployment procedure can be semantically close (both involve the production system) while being informationally independent. Cosine similarity would penalize the write; information theory would not.

### 1.2 Attention Budget System (COS-362)

The attention budget system allocates recall capacity across memory indices (categories, scopes). It uses static weights based on query routing rules. There is no feedback from information density — a category with 200 near-duplicate memories gets the same budget as one with 20 high-entropy memories.

### 1.3 Distillation (COS-347, hippocampus.py)

Distillation promotes memories based on `recalled_count` and `confidence`. No entropy or surprise signal is used. The result: memories promoted to `permanent` are those frequently recalled — but frequent recall may reflect routing bias (Gini coefficient 0.91 per COS-352), not intrinsic information value.

---

## 2. Formal Framework

### 2.1 The Memory Store as a Probability Distribution

Treat the set of active memories `Store = {m_1, ..., m_n}` as an empirical distribution over semantic content. The embedding of each memory, `e_i = embed(m_i) ∈ R^d`, defines a point in the embedding space. The **information density** of the store is the entropy of this distribution.

We can approximate this as a Gaussian Mixture Model (GMM) in embedding space:

```
p_Store(x) = (1/n) Σ_i N(x; e_i, σ²I)
```

Where σ is a bandwidth parameter (typically 0.1 for 768-dim embeddings). This is the **kernel density estimate** of the current store.

Given this, we can formally define:

**Marginal entropy of the store:**
```
H(Store) = -∫ p_Store(x) log p_Store(x) dx
```

This is the Shannon entropy of the memory distribution. High H(Store) means the stored memories are diverse and spread across topic space. Low H(Store) means the store is clustered — potentially over-indexed on a few topics.

### 2.2 Surprise Score (Information Value of a New Memory)

Given a candidate memory `m_new` with embedding `e_new`, its **surprise score** is the negative log-probability under the current store distribution:

```
S(m_new | Store) = -log p_Store(e_new)
```

**Interpretation:**
- High surprise (S >> 0): m_new is in a sparse region of embedding space. The store does not already cover this topic well. High information value.
- Low surprise (S ≈ 0): m_new is in a dense cluster. The store already has many similar memories. Low marginal value.

**Practical computation** (no integration required):

```python
def surprise_score(candidate_embedding: list[float], 
                   store_embeddings: list[list[float]],
                   sigma: float = 0.1) -> float:
    """
    Compute -log p_Store(e_new) via Gaussian KDE approximation.
    
    Returns: surprise ∈ [0, ∞). Higher = more surprising.
    Normalize to [0,1] with sigmoid: 1 / (1 + exp(-surprise + 3.0))
    """
    if not store_embeddings:
        return 1.0  # empty store: everything is maximally surprising
    
    n = len(store_embeddings)
    # Compute squared distances (faster than cosine in this context)
    distances_sq = [
        sum((a - b)**2 for a, b in zip(candidate_embedding, s))
        for s in store_embeddings
    ]
    # Kernel contributions
    kernels = [exp(-d / (2 * sigma**2)) for d in distances_sq]
    # Negative log of density estimate
    density = sum(kernels) / n
    return -log(density + 1e-10)  # add epsilon for numerical stability
```

**Why this is better than cosine similarity:**
- Cosine similarity to nearest neighbor: measures distance to a *single* point
- Surprise score: measures distance to the *entire distribution* — a sparse region far from all clusters scores high even if moderately close to one cluster

### 2.3 KL Divergence: Information Gain from Writing

The KL divergence from the store distribution to the store-plus-new-memory distribution measures the **information gain** from writing:

```
IG(m_new) = KL(p_Store+new || p_Store)
          = ∫ p_Store+new(x) log [p_Store+new(x) / p_Store(x)] dx
```

High IG → writing m_new changes the distribution substantially → high value.
Low IG → m_new is absorbed without meaningful change → redundant.

**Approximation:** For a single new point added to KDE:

```
IG(m_new) ≈ (1/(n+1)) × S(m_new | Store)
```

The information gain per write scales with surprise and inversely with store size (each new memory matters less as the store grows).

### 2.4 Minimum Description Length (Redundancy)

MDL asks: how many bits do we need to describe memory M, given that the reader already has access to Store?

**Formal definition:**
```
MDL(m | Store) = L(m) - L(m | Store)
```

Where:
- `L(m)` = description length of m in isolation (approximated by token count or embedding norm)
- `L(m | Store)` = conditional description length given Store

**Practical approximation:** If m can be approximately reconstructed from the top-k memories in Store (via linear combination of embeddings), then its conditional complexity is low:

```
reconstruction_error(m, Store, k=5) = ||e_m - Proj_{V_k}(e_m)||²
```

Where `V_k` is the subspace spanned by the top-k nearest neighbor embeddings.

**Redundancy score:**
```
R(m) = 1 - reconstruction_error(m, Store, k=5)
```

- R ≈ 1: m lies entirely in the span of existing memories → high redundancy, low MDL contribution
- R ≈ 0: m cannot be reconstructed → low redundancy, high MDL contribution

**Implementation:**

```python
def redundancy_score(candidate_embedding: list[float],
                     store_embeddings: list[list[float]],
                     k: int = 5) -> float:
    """
    Measure how reconstructible candidate is from top-k store vectors.
    Returns [0, 1]. 1 = fully redundant, 0 = fully novel.
    """
    import numpy as np
    e = np.array(candidate_embedding)
    
    if len(store_embeddings) < k:
        k = len(store_embeddings)
    if k == 0:
        return 0.0
    
    # Find top-k nearest neighbors by cosine similarity
    similarities = [
        np.dot(e, np.array(s)) / (np.linalg.norm(e) * np.linalg.norm(s) + 1e-10)
        for s in store_embeddings
    ]
    top_k_indices = sorted(range(len(similarities)), key=lambda i: -similarities[i])[:k]
    top_k_vectors = np.array([store_embeddings[i] for i in top_k_indices])
    
    # Project candidate onto span of top-k vectors (least squares)
    # Solve: top_k_vectors.T @ coeffs ≈ e
    coeffs, _, _, _ = np.linalg.lstsq(top_k_vectors.T, e, rcond=None)
    reconstruction = top_k_vectors.T @ coeffs
    
    # Normalized reconstruction error
    error = np.linalg.norm(e - reconstruction) / (np.linalg.norm(e) + 1e-10)
    return 1.0 - min(error, 1.0)  # 1 = fully reconstructible = redundant
```

### 2.5 The Unified Write Gate W(m)

With formal definitions in hand, the write gate becomes:

```
W(m) = surprise(m) × relevance(m) / (1 + redundancy(m))
```

Where:
- `surprise(m)` = S(m | Store), normalized to [0, 1]
- `relevance(m)` = confidence × recency_multiplier (from COS-402, preserved)
- `redundancy(m)` = R(m) ∈ [0, 1]

**Comparison to COS-402 formula:**

| Component | COS-402 approximation | COS-415 formalization |
|---|---|---|
| Information gain | 1 - max_cosine_sim | -log p_KDE(e_new), normalized |
| Redundancy | top-3 avg cosine > 0.7 | Subspace projection error |
| Confidence prior | Piecewise linear | Unchanged |
| Recency | Category × temporal class | Unchanged |

The key changes: surprise uses the full distribution rather than nearest-neighbor similarity; redundancy uses subspace projection rather than a threshold. Both are more computationally expensive but more accurate.

**Computational cost analysis:**
- COS-402 approach: O(n) dot products, O(1) merge → suitable for n ≤ 5,000
- COS-415 approach: O(n) dot products + O(k²) least-squares solve → suitable for n ≤ 2,000

Given current store size (~150 active memories), both are fast. For larger stores, approximate nearest neighbor (ANN) with sqlite-vec cuts the O(n) scan to O(log n).

---

## 3. Distillation as Lossy Compression

### 3.1 Rate-Distortion Theory

Shannon's rate-distortion theorem defines the fundamental tradeoff between compression ratio (rate R) and reconstruction error (distortion D):

```
R(D) = min_{p(m̂|m): E[d(m,m̂)] ≤ D} I(M; M̂)
```

For memory distillation:
- `M` = the set of active memories
- `M̂` = the set of promoted/permanent memories
- `d(m, m̂)` = semantic distance between original and promoted representation
- `I(M; M̂)` = mutual information preserved through distillation

**Key insight:** The current distillation policy (promote by recalled_count + confidence) does not minimize distortion for a given rate — it maximizes recall frequency, which is a biased proxy. A memory recalled 50 times may carry less new information than a rarely-recalled memory covering a unique topic.

### 3.2 Optimal Distillation Criterion

For each memory m_i, compute its **marginal information contribution**:

```
IC(m_i | Store \ {m_i}) = H(Store) - H(Store \ {m_i})
```

The entropy of the store *decreases by IC(m_i)* when m_i is removed. High IC(m_i) = removing m_i significantly degrades the store's information coverage = promote it.

**Approximation using surprise:**

```
IC(m_i | Store) ≈ surprise(m_i | Store \ {m_i})
```

The surprise of a memory against the rest of the store is its marginal entropy contribution.

**Revised distillation promotion criterion:**

```
promote_score(m) = α × recalled_count_normalized 
                 + β × confidence 
                 + γ × IC(m | Store \ {m})
```

Recommended starting weights: α=0.2, β=0.3, γ=0.5.

This shifts distillation from "what do agents ask for most?" to "what would most degrade coverage if lost?"

### 3.3 Optimal Compression Ratio

Given a target maximum entropy loss of ΔH, how many memories can we remove?

Greedy approximation: sort memories by IC ascending (lowest contribution first), remove until projected entropy loss reaches ΔH.

This is the distillation policy the hippocampus consolidation cycle should use when the store exceeds capacity.

---

## 4. Attention Budget as Information Gain Routing

### 4.1 Current State (COS-362)

The attention budget allocates recall capacity across categories/scopes based on query routing rules. The allocation is static: a category always gets the same fraction of budget regardless of how much information it contains.

### 4.2 Information-Theoretic Allocation

Given a query q with embedding e_q, the **expected information gain** of querying index I_k is:

```
EIG(q, I_k) = H(q | I_k) - H(q)
```

Where H(q | I_k) is the query entropy after seeing the retrieval results from index I_k.

In practice: the KL divergence from the query distribution to the index distribution predicts how much new information the index will provide.

**Routing decision:**

```python
def route_query(query_embedding, index_embeddings_by_category):
    """
    Allocate budget proportional to expected information gain per category.
    """
    gains = {}
    for category, embeddings in index_embeddings_by_category.items():
        # Expected coverage: how well does this index cover the query region?
        distances = [cosine_distance(query_embedding, e) for e in embeddings]
        # Information gain ∝ reciprocal of expected reconstruction error
        min_dist = min(distances) if distances else 1.0
        diversity = compute_entropy(distances)  # spread of the index
        gains[category] = diversity / (min_dist + 0.1)
    
    total = sum(gains.values())
    return {cat: g / total for cat, g in gains.items()}
```

This allocates more budget to categories with:
- Low minimum distance to query (relevant)
- High entropy spread (diverse, not a single cluster)

---

## 5. Practical Deliverables

### 5.1 `brainctl entropy` Command

**Purpose:** Measure information density of a memory or the store.

**Sub-commands:**

```bash
# Measure information density of the entire store
brainctl entropy store
# Output: H(Store) = 3.42 bits/memory | Density clusters: 4 | Outlier memories: 12

# Measure surprise score of a specific memory
brainctl entropy memory 127
# Output: surprise=0.82 (high) | redundancy=0.12 (low) | IC_contribution=0.61

# Measure surprise of a candidate (before writing)
brainctl entropy candidate "some new fact to evaluate"
# Output: surprise=0.45 (medium) | redundancy=0.68 (high) | W(m)=0.28 [below threshold]

# Show entropy distribution by category
brainctl entropy breakdown
# Output: table of H(category) per scope/category pair
```

**Implementation location:** `~/bin/brainctl` — new `entropy` subcommand, using functions from `~/bin/lib/information_theory.py` (new module).

**Schema dependencies:** Requires `embedding` column populated (100% coverage post-COS-231 backfill). No schema changes needed.

### 5.2 Surprise Score at Write Time

**Schema addition:**
```sql
ALTER TABLE memories ADD COLUMN surprise_score REAL DEFAULT NULL;
ALTER TABLE memories ADD COLUMN redundancy_score REAL DEFAULT NULL;
```

**Compute at push time** (in `~/bin/brainctl`, `push` subcommand):

```python
# After embedding is computed, before INSERT:
surprise = compute_surprise_score(candidate_embedding, store_embeddings)
redundancy = compute_redundancy_score(candidate_embedding, store_embeddings)
worthiness = surprise * (confidence * recency_mult) / (1 + redundancy)

if worthiness < W_MIN and not force:
    log_rejected(candidate_text, worthiness, surprise, redundancy)
    return  # gate

# INSERT with scores
INSERT INTO memories (..., surprise_score, redundancy_score)
VALUES (..., surprise, redundancy)
```

**Backfill existing memories:**
```bash
brainctl entropy backfill  # computes surprise/redundancy for all memories lacking scores
```

This enables retrospective analysis: which permanent memories have low surprise but high recall? (Recall-biased promotions — candidates for reclassification.)

### 5.3 Redundancy Detector in Consolidation Cycle

**Replace** the current similarity-only merge check in hippocampus.py's consolidation pass.

**Current approach:** If `cosine_sim(m_i, m_j) > 0.85`, flag as duplicate candidates.

**New approach:**
```python
def find_redundant_memories(memories: list[Memory], 
                            threshold_R: float = 0.75) -> list[tuple[int, int, float]]:
    """
    For each memory m_i, compute R(m_i | Store \ {m_i}).
    Flag if redundancy > threshold_R.
    Returns: list of (memory_id, primary_memory_id, redundancy_score)
    """
    embeddings = [m.embedding for m in memories]
    redundancies = []
    
    for i, m in enumerate(memories):
        # Remove m_i from store
        others = embeddings[:i] + embeddings[i+1:]
        R = redundancy_score(m.embedding, others, k=5)
        
        if R > threshold_R:
            # Find the memory it is most redundant to
            primary_idx = find_primary(m.embedding, others)
            redundancies.append((m.id, memories[primary_idx].id, R))
    
    return redundancies
```

This catches **near-redundant memories that are NOT near-duplicates**: memories that are phrased differently but carry the same information (reconstructible from 5 nearby embeddings).

**Integration:** Call `find_redundant_memories()` in the consolidation pass, after the existing near-duplicate check. Flag for merge review rather than auto-merge (safety gate for the first implementation).

### 5.4 Surprise-Based Distillation Promotion

**In hippocampus.py `distill()` function**, replace:
```python
promote_score = recalled_count * 0.6 + confidence * 0.4
```

With:
```python
# IC contribution requires a brainctl call or inline compute
ic = information_contribution(m, all_embeddings)  # surprise against rest of store
promote_score = recalled_count_normalized * 0.2 + confidence * 0.3 + ic * 0.5
```

**Effect:** Memories that are frequently recalled AND information-dense get promoted. Memories that are frequently recalled but redundant (high-recall due to routing bias per COS-352) do not automatically promote.

---

## 6. Empirical Analysis of Current Store

### 6.1 Expected Entropy Distribution

Based on the 150 active memories in brain.db and the empirically observed clustering behavior, we predict:

| Category | Estimated H(category) | Interpretation |
|---|---|---|
| global/decision | ~2.8 bits | Diverse decisions — good coverage |
| global/lesson | ~2.2 bits | Some redundancy from repeated lessons |
| global/environment | ~1.8 bits | Clustered — same core facts restated |
| agentmemory/lesson | ~2.5 bits | Research findings — diverse |
| costclock-ai/project | ~1.5 bits | Implementation progress notes — redundant |

**Prediction:** The `brainctl entropy breakdown` command will reveal that ~30-40% of store entropy comes from `global/decision` and `agentmemory/lesson`, while `costclock-ai/project` will have the highest redundancy density.

### 6.2 Retrospective Worthiness Audit

Applying the revised W(m) formula retrospectively to all 150 active memories:

**Predicted distribution:**
- W ≥ 0.6 (high value, definitely keep): ~25-35% of memories
- 0.35 ≤ W < 0.6 (medium value): ~30-40% of memories
- W < 0.35 (below gate, would have been filtered): ~25-35% of memories

**Hypothesis:** The high-value cluster (W ≥ 0.6) will have >90% overlap with the high-recall cluster (recalled_count ≥ 10), validating that the new surprise-based metric is consistent with revealed preference (what agents actually query for). The non-overlapping portion — memories with high surprise but low recall — represents the most interesting cases: potentially valuable knowledge that agents are not yet retrieving.

### 6.3 Known Failure Modes

1. **Surprise instability at small n** — When n < 20, the KDE bandwidth dominates and surprise scores are unreliable. Mitigation: fall back to cosine-based COS-402 formula below n=20.

2. **Redundancy false positives for complements** — Memory A about "brainctl push gate" and memory B about "brainctl search results" may both be redundant to each other if they share embedding components from the `brainctl` concept. Mitigation: compute redundancy *conditional on category* — only compare within the same `scope/category`.

3. **Surprise drift as store grows** — As the store expands, the KDE distribution shifts and old memories' surprise scores become stale. Mitigation: recompute surprise scores during consolidation cycle (not at every write).

4. **Embedding model version dependence** — If the embedding model changes (e.g., nomic-embed-text v1 → v2), all surprise scores are invalidated. Mitigation: store `embedding_model_version` column alongside scores.

---

## 7. Integration Sequence

Recommended order to avoid conflicts with COS-402 (in review):

1. **Merge COS-402 first** — W(m) gate with cosine approximation is live.
2. **Implement `~/bin/lib/information_theory.py`** — All the formal functions in this doc.
3. **Add schema columns** (`surprise_score`, `redundancy_score`) + backfill.
4. **Implement `brainctl entropy`** — Uses information_theory.py.
5. **Update W(m) in write gate** — Upgrade from cosine to KDE-based surprise/redundancy.
6. **Update hippocampus distill()** — Surprise-weighted promotion.
7. **Update hippocampus consolidation** — Redundancy detector replacing similarity-only check.
8. **Route queries by EIG** — Update brainctl search to weight by expected information gain.

**Expected health impact per brainctl health metric:**
- `snr` (signal-to-noise ratio): +0.2–0.4 (fewer noise memories at steady state)
- `low_confidence_ratio`: -5% (gate filters low-confidence redundant writes earlier)
- `recall_gini`: -0.05 to -0.10 (surprise-based distillation diversifies high-recall set)
- `active_memory_count`: stabilizes at lower ceiling (~80–100 vs current growth trajectory)

---

## 8. Key References

1. **Shannon, C.E. (1948)** — A Mathematical Theory of Communication. The foundational entropy formula H(X) = -Σ p(x) log p(x) and mutual information I(X;Y). Direct basis for surprise scoring.

2. **Rissanen, J. (1978)** — Modeling by Shortest Data Description (MDL). Minimum description length as a principled measure of information redundancy. Basis for the redundancy score formalization.

3. **Berger, T. (1971)** — Rate Distortion Theory. Defines the optimal compression ratio for a given distortion budget. Basis for the distillation-as-compression framing in Section 3.

4. **Schmidhuber, J. (2010)** — Formal Theory of Creativity, Fun, and Intrinsic Motivation. Defines "interestingness" as the rate of change of compression progress — directly analogous to our surprise score (memories that improve compression of the agent's world model are interesting).

5. **Still, S. & Precup, D. (2012)** — An Information-Theoretic Approach to Curiosity-Driven Reinforcement Learning. Expected information gain as action selection criterion — our EIG routing in Section 4 is the retrieval analogue.

6. **Blahut, R. (1972)** — Computation of Channel Capacity and Rate-Distortion Functions. The iterative Blahut-Arimoto algorithm for computing rate-distortion bounds — potential implementation for the hippocampus compression pass.

---

## 9. Open Questions for Wave 14

1. **Non-stationary store** — As agents grow and topics shift, the "right" entropy level changes. Should H(Store) be a target metric? What's the target for a healthy memory ecosystem?

2. **Cross-agent information gain** — Memory M may be redundant for Agent A but informative for Agent B. A multi-agent store needs per-agent information gain calculations. Current architecture treats the store as global.

3. **Semantic compression vs. literal compression** — MDL here is approximated in embedding space. Could we use actual text compression (LZ77, Huffman) on the textual content to compute true description length? This would be exact but slow (need to represent all memories as a corpus).

4. **Adversarial surprise injection** — A malicious or mis-calibrated agent could write high-surprise (but false) memories to maximize their influence on the store. Surprise-based gates have an attack surface that cosine-threshold gates don't. Sentinel 2 should assess.

5. **Temporal information decay** — Information value decays as the world changes. A "surprise" fact from 6 months ago may now be common knowledge. Should surprise scores have a half-life?

---

*Delivered as Wave 13 research, [COS-415](/COS/issues/COS-415). Next: Blueprint ([COS-400](/COS/issues/COS-400)) to produce implementation spec for `information_theory.py`, `brainctl entropy`, schema columns, and hippocampus integration.*
