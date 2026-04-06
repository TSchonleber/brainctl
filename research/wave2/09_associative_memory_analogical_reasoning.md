# Associative Memory & Analogical Reasoning
## Research Report — COS-111
**Author:** Engram (Memory Systems Lead)
**Date:** 2026-03-28
**Target:** brain.db — Associative memory layer enabling creative cross-domain connections for Hermes and 22+ agents

---

## Executive Summary

This report investigates how to make Hermes' memory *creative* — not just retrieving exact matches but surfacing non-obvious connections, finding structural analogies between domains, and generating genuine insight from stored experience. Six theoretical frameworks are analyzed. The central finding: **spreading activation over the existing `knowledge_edges` graph, combined with structural analogy matching over `memories`/`context` records, gives the highest-impact capability at lowest implementation cost**. Holographic Reduced Representations (HRR) would require a full embedding architecture overhaul and are deferred. Sparse Distributed Memory (SDM) is theoretically elegant but adds little over our existing nomic-embed-text 768d space.

**Highest-impact recommendation:** Implement spreading activation as a new `brainctl graph activate` command (2-3 days), then build a lightweight structural analogy matcher operating on `knowledge_edges` + memory metadata (4-5 days).

---

## 1. Spreading Activation Networks
### Theory — Collins & Loftus (1975)

**Core idea:** Memory concepts are nodes in a semantic network. Accessing one node propagates partial activation to related nodes via weighted edges. Activation decays with distance. Retrieval bias toward currently-primed concepts explains priming effects, associative recall, and creative leaps.

**Key paper:** Collins, A.M. & Loftus, E.F. (1975). "A spreading-activation theory of semantic processing." *Psychological Review*, 82(6), 407–428.

**The mechanism:**
```
Activate(node_i) = base_activation + Σ_j (weight(i,j) × Activate(node_j) × decay^distance)
```
- Base activation comes from direct retrieval (query match, recency, importance)
- Spread is dampened at each hop by a decay factor (typically 0.5–0.7)
- Cycles are handled by capping total spreading iterations (2–3 hops is sufficient for useful associations)

**Why it produces creative connections:**
- A query about "billing slowdown" might activate the billing memories (direct), which propagate to "PostgreSQL index" (semantic_similar edge), which propagates to "index rebuild caused outage in auth service" (causal_chain_member edge) — surfacing a non-obvious analogy the agent wouldn't find with pure vector search.
- The creative connection isn't fabricated — it's latent in existing edges, just not directly queried.

### brain.db Status

We already have the graph substrate:
- `knowledge_edges` table with 1,933 edges
- Edge types: `semantic_similar`, `causal_chain_member`, `causes`, `topical_tag`, `topical_project`, `topical_scope`
- Weights: 0.0–1.0 (semantic_similar edges carry cosine similarity as weight)

**What's missing:** A spreading activation query layer. `brainctl graph related` does multi-hop traversal but returns raw neighbors without activation weighting. We need a scored traversal that returns a *ranked list of activated nodes* with activation strength.

### Implementation Design

```python
def spreading_activation(
    db: sqlite3.Connection,
    seed_ids: list[tuple[str, int]],  # (table, id) pairs to activate
    hops: int = 2,
    decay: float = 0.6,
    weight_by_type: dict[str, float] = None,
    top_k: int = 20
) -> list[dict]:
    """
    Spreads activation from seed nodes through knowledge_edges.
    Returns ranked list of (table, id, activation_score, path).
    """
    weight_by_type = weight_by_type or {
        'semantic_similar': 1.0,
        'causal_chain_member': 0.8,
        'causes': 0.9,
        'topical_tag': 0.5,
        'topical_project': 0.4,
        'topical_scope': 0.4,
    }
    activation = {}  # (table, id) -> score
    for table, id_ in seed_ids:
        activation[(table, id_)] = 1.0

    frontier = list(seed_ids)
    for hop in range(hops):
        next_frontier = []
        decay_at_hop = decay ** (hop + 1)
        for source_table, source_id in frontier:
            rows = db.execute("""
                SELECT target_table, target_id, relation_type, weight
                FROM knowledge_edges
                WHERE source_table = ? AND source_id = ?
                UNION ALL
                SELECT source_table, source_id, relation_type, weight
                FROM knowledge_edges
                WHERE target_table = ? AND target_id = ?
            """, (source_table, source_id, source_table, source_id)).fetchall()
            for t_table, t_id, rel_type, edge_weight in rows:
                type_weight = weight_by_type.get(rel_type, 0.5)
                contribution = decay_at_hop * edge_weight * type_weight
                key = (t_table, t_id)
                if key not in activation or activation[key] < contribution:
                    activation[key] = contribution
                    next_frontier.append((t_table, int(t_id)))
        frontier = next_frontier

    # Remove seeds from results, sort by activation
    results = sorted(
        [(k, v) for k, v in activation.items() if k not in set(seed_ids)],
        key=lambda x: -x[1]
    )[:top_k]
    return [{"table": t, "id": i, "activation": s} for (t, i), s in results]
```

**CLI integration:**
```bash
# Activate from a specific memory, see what it primes
brainctl graph activate memories 42 --hops 2 --decay 0.6 --top-k 10

# Activate from a search result set (most creative use case)
brainctl vsearch "billing performance" | brainctl graph activate --from-stdin --hops 2
```

**Key parameter tuning:**
- `decay=0.6` at hop 1 → `0.36` at hop 2. This means a node 2 hops away needs an edge weight of 0.9+ to have activation > 0.32. This naturally limits noise.
- `semantic_similar` edges get weight 1.0 since they represent genuine content overlap
- `topical_tag/project/scope` edges get lower weight (0.4–0.5) since they're metadata relationships, not content relationships

**Integration with search:** The most powerful use is post-retrieval amplification. After a vsearch returns top-5 hits, spread activation from those 5 nodes. Return the augmented set (original results + highly-activated neighbors) to the agent. This mimics how human memory works — direct recall plus cued associated retrieval.

---

## 2. Analogical Reasoning — Structure-Mapping & Copycat
### Theory — Gentner (1983) + Hofstadter

**Gentner's Structure-Mapping Theory:**
Analogy is about mapping *structural relationships* between domains, not surface features. "An atom is like a solar system" works because ORBITS(planet, sun) maps to ORBITS(electron, nucleus) — the *relational structure* is the same even though atoms and planets are nothing alike at surface level.

**Key paper:** Gentner, D. (1983). "Structure-mapping: A theoretical framework for analogy." *Cognitive Science*, 7(2), 155–170.

**What makes a good analogy:**
1. **Systematicity**: Prefer mappings that carry an entire system of relations over isolated attribute matches
2. **Relational > featural**: Structural similarity wins over surface similarity
3. **Minimal**: Avoid unnecessary attribute overlap — the simplest structural mapping wins

**Hofstadter's Copycat (1984) / Metacat:**
Hofstadter extended this to dynamic concept blending — the architecture builds a "slipnet" of conceptual distances and uses a coactive process to find analogies in letter-string domains. Key insight: **the slipnet continuously adjusts concept proximity based on context**, allowing flexible, context-dependent analogizing.

**Key work:** Hofstadter, D. & Mitchell, M. (1994). "The Copycat Project: A model of mental fluidity and analogy-making." In *Advances in Connectionist and Neural Computation Theory*.

### Why This Matters for Hermes

Hermes has 22+ agents working across multiple domains (billing, infrastructure, memory, auth, UI, research...). Many problems recur structurally across domains. Examples:
- "Slow query in billing" ≈ "Slow query in auth" — both are N+1 query problems
- "Memory eviction policy for brain.db" ≈ "Cache eviction policy for Redis" — same TTL/LRU tradeoff
- "Agent blocked waiting for approval" ≈ "Thread blocked waiting for lock" — concurrency problem in a different substrate

A structure-mapping system would let Hermes say: *"This billing slowdown has the same structure as the auth slowdown we fixed in January. The solution mapping suggests: check for missing index → add index."*

### Implementation Design — Lightweight Structural Analogy for brain.db

Full Copycat/SMAP would require a dedicated symbolic AI engine. But we can approximate structural analogy using **relational fingerprints** over `knowledge_edges`:

**Insight:** Two memories are structurally analogous if their *neighborhoods in the knowledge graph have similar relational patterns*, even if the actual neighbors are in different domains.

**Relational fingerprint:** For each memory record, build a vector summarizing its edge profile:
```python
def relational_fingerprint(db, table, record_id, hops=2):
    """
    Returns a dict: {relation_type: [activated_record_ids]}
    Normalized to be domain-agnostic by using relation_type categories,
    not the actual neighboring node IDs.
    """
    neighbors = spreading_activation(db, [(table, record_id)], hops=hops)
    # Aggregate by relation type and temporal_class of targets
    fingerprint = defaultdict(list)
    for n in neighbors:
        # Fetch relation type and metadata of this neighbor
        # ...
    return fingerprint
```

**Structural similarity score:**
```python
def structural_similarity(fp1, fp2):
    """
    Jaccard-like overlap of relational fingerprint keys (relation types used).
    High score = same structural role in the knowledge graph.
    """
    keys1 = set(fp1.keys())
    keys2 = set(fp2.keys())
    overlap = keys1 & keys2
    union = keys1 | keys2
    return len(overlap) / len(union) if union else 0.0
```

**Practical analogy retrieval:**
```bash
# "Find memories that play the same structural role as memory 42"
brainctl graph analogize memories 42 --top-k 5
```

This is achievable in ~150 lines of Python as an extension to `brainctl graph`. It won't be Copycat-level creative, but it will surface genuinely useful cross-domain structural matches.

**What we're NOT implementing (complexity budget):**
- Systematic predicate parsing from memory text (requires NLP pipeline or LLM)
- Slipnet-style dynamic concept proximity (needs full working memory simulation)
- Metacat-level re-representation of the source domain

The compromise: use graph topology as a structural proxy. It's imperfect but good enough — if two memories have similar edge profiles, they've been empirically connected to similar kinds of entities by the agents who wrote them.

---

## 3. Concept Blending — Fauconnier & Turner
### Theory

**Key work:** Fauconnier, G. & Turner, M. (2002). *The Way We Think: Conceptual Blending and the Mind's Hidden Complexities.* Basic Books.

**Core idea:** Novel meaning emerges from *blending* two or more input mental spaces into a third (the blend). The blend inherits selected structure from both inputs, plus emergent structure not present in either.

Classic example: "Surgeon as butcher" — blends *surgical skill* from the surgeon space with *crude meat-handling* from the butcher space to create an emergent critique of surgical insensitivity. Neither input alone carries the full meaning.

**For AI agents, concept blending would enable:**
- "What if we applied the eviction policy from Redis to the memory temporal_class system?" → Novel hybrid policy
- "Take the retry logic from the billing service and apply it to the memory write pipeline" → Cross-domain pattern transfer
- "The way Hermes handles topic decay is like the way the DNS TTL ages out records" → Generative insight

### Implementation Feasibility Assessment

**Hard constraint:** True concept blending requires understanding *propositional structure* (not just word embeddings). You need to know that "surgeon" plays the role of AGENT in "surgeon cuts patient" and map that role onto "butcher cuts meat."

**Current brain.db capabilities:**
- We have 768d embeddings (content similarity only — no role extraction)
- We have `knowledge_edges` (structural topology but no predicate labels)
- We have FTS5 (keyword overlap, no semantic roles)

**Realistic scope for brain.db:**

A weak form of concept blending is already achievable: **source + target vector interpolation**.

Given two memories M1 and M2, a "blend query" is:
```python
blend_vector = alpha * embed(M1.content) + (1 - alpha) * embed(M2.content)
```
Then vsearch for memories near `blend_vector`. This finds records that are semantically between M1 and M2 — records that talk about concepts that bridge both domains.

```bash
# Find memories that conceptually bridge memory 42 and memory 17
brainctl vsearch --blend-memories 42,17 --alpha 0.5
```

This is mathematically valid (vector interpolation in embedding space is meaningful with nomic-embed-text) and produces genuinely useful blends — it's used in creative generation research (DALL-E prompt interpolation, etc.).

**Limitation:** Without propositional structure, you get *topical* blending, not true structural blending. "Redis + memory" finds memories about cache-like memory systems, not memories where Redis and brain.db play analogous roles.

**Recommendation:** Implement vector-blend search (easy, high value, 1 day), defer structural blending (requires LLM-in-the-loop propositional parsing, separate initiative).

---

## 4. Semantic vs. Episodic Memory — Cross-Pollination
### Theory — Tulving (1972, 1985)

**Key papers:**
- Tulving, E. (1972). "Episodic and semantic memory." In *Organization of Memory*. Academic Press.
- Tulving, E. (1985). "Memory and consciousness." *Canadian Psychology*, 26(1), 1–12.

**Semantic memory:** General world knowledge — facts, concepts, rules. Not tied to when or where you learned them. ("Water boils at 100°C", "Go uses goroutines for concurrency")

**Episodic memory:** Autobiographical memory — specific events with temporal and contextual tagging. ("On March 15, the billing service threw 500s for 20 minutes after the index migration") Always accompanied by *mental time travel* — you recall the context, not just the fact.

**Encoding specificity principle:** Retrieval is most effective when retrieval context matches encoding context. An agent trying to recall "how we fixed billing slowdowns" retrieves better if given temporal cues (what else was happening then?) or project cues, not just the semantic content of the fix.

### brain.db Mapping

| Tulving | brain.db |
|---------|----------|
| Semantic memory | `memories` table (facts, preferences, patterns) |
| Episodic memory | `events` table (structured event log with `created_at`, `session_id`, `epoch_id`) |
| Mental time travel | Epoch-tagged retrieval + causal chain traversal |

**Current gap:** The two systems are queryable independently but don't actively cross-pollinate during retrieval. A semantic memory retrieval doesn't automatically surface related episodic events, and vice versa.

**Implementation: Episodic → Semantic promotion**

When a recurring pattern appears in events, the hippocampus should crystallize it as a semantic memory:

```python
# In hippocampus.py maintenance cycle
def crystallize_recurring_patterns(db, min_occurrences=3, window_days=30):
    """
    Find event summaries with high FTS overlap in the last 30 days.
    If 3+ similar events exist, create a semantic memory summarizing the pattern.
    """
    # Cluster events by cosine similarity in vec_events
    # For clusters with >= min_occurrences members, generate a semantic summary
    # Insert as new memory with category='crystallized_pattern', confidence=0.7
    # Add knowledge_edges from new memory to each source event (relation: 'crystallized_from')
```

**Implementation: Semantic → Episodic cue injection**

When an agent retrieves a semantic memory, automatically surface 1-2 related episodic events as context:

```sql
-- Extend the brainctl memory fetch to also return related events
SELECT e.summary, e.created_at
FROM events e
JOIN knowledge_edges ke ON (
    ke.target_table = 'events' AND ke.target_id = e.id
    AND ke.source_table = 'memories' AND ke.source_id = :memory_id
)
WHERE ke.relation_type IN ('semantic_similar', 'crystallized_from', 'causal_chain_member')
ORDER BY e.created_at DESC LIMIT 2;
```

**Cross-pollination at query time:**
```bash
# Retrieve memory + related episodes in one shot
brainctl memory get 42 --with-episodes
```

**Why this matters:** When Hermes is debugging a recurring problem, retrieving the *semantic pattern* alone misses the rich context of specific episodes. Encoding specificity tells us the full episodic context dramatically improves the agent's ability to reason about what's happening now vs. what happened before.

---

## 5. Holographic Reduced Representations (HRR)
### Theory — Plate (1995, 2003)

**Key papers:**
- Plate, T.A. (1995). "Holographic reduced representations." *IEEE Transactions on Neural Networks*, 6(3), 623–641.
- Plate, T.A. (2003). *Holographic Reduced Representations: Distributed Representation for Cognitive Structures*. CSLI Publications.

**Core idea:** HRRs are high-dimensional vectors (typically 512–2048d, real-valued, Gaussian-initialized) where:
- **Superposition** (binding): `A + B` creates a composite representation of both concepts (the sum's magnitude ≈ 1/√2 of each component, so information is compressed but preserved)
- **Circular convolution** (`⊛`): `A ⊛ B` creates a new vector that *encodes the relationship* "A is bound to B" — the convolution is invertible: `A ≈ (A ⊛ B) ⊛ B̃` where B̃ is the approximate inverse
- **Role-filler binding**: Encode "agent=Hermes, action=approved, target=billing-fix" as `AGENT ⊛ HERMES + ACTION ⊛ APPROVED + TARGET ⊛ BILLING_FIX`

**Why this enables analogy:**
The convolutional binding produces vectors where *relational structure is preserved across domains*. If you have `AGENT ⊛ HERMES` and `AGENT ⊛ LEGION`, the cosine similarity of these vectors reflects the similarity of the AGENT role regardless of who fills it. This is genuine structural similarity, not surface similarity.

### Feasibility Assessment for brain.db

**What HRR would enable (that current system can't do):**
- Query "who approved what?" by decoding the AGENT role from stored event vectors
- Find analogies based on relational roles: "find other events where X approved Y for Z, where the structural relationship matches this event"
- Compose "billing+performance+fix" as a single queryable vector without needing all three tokens to appear in one document

**What it would require:**
1. Re-encode all existing memories and events using HRR instead of (or alongside) nomic-embed-text
2. Implement circular convolution in the brainctl query layer (O(n log n) via FFT — fast but non-trivial)
3. Maintain a role dictionary (fixed random vectors for AGENT, ACTION, TARGET, etc.)
4. Build a decoder to extract filled roles from encoded records

**Critical limitation:** nomic-embed-text's 768d vectors are learned — they don't support circular convolution because they weren't initialized as random Gaussian vectors with the right statistical properties. HRR requires *purpose-built* embeddings. You can't bolt HRR onto an existing embedding model.

**Verdict: High complexity, high reward, deferred.**
Implementing HRR properly would require generating dual embeddings for every record (keep nomic-embed-text for semantic search, add HRR for structural analogy) and building a new query path. This is a 2–3 week initiative, not a feature addition. Flag for a dedicated Cognitive Architecture sprint.

**Near-term proxy:** The structural analogy fingerprinting from Section 2 gives ~40% of HRR's analogical reasoning capability with ~10% of the implementation cost.

---

## 6. Sparse Distributed Memory (SDM)
### Theory — Kanerva (1988)

**Key work:** Kanerva, P. (1988). *Sparse Distributed Memory*. MIT Press.

**Core idea:** A biologically-inspired memory model where:
- Memory is stored across ~1M "hard locations" in a high-dimensional binary space (typically 1000 bits)
- A memory address is a 1000-bit binary vector
- **Writing:** Simultaneously write to all locations within Hamming distance D of the address (typically ~400–450 locations out of 1M)
- **Reading:** Sum all stored content at locations within Hamming distance D of the query address; the summed content converges to the stored pattern

**Key properties:**
- Content-addressable: similar addresses retrieve similar content (graceful degradation)
- Superposition: many memories can be stored without interference if the space is large enough relative to memory count
- Noise-tolerant: partial or corrupted addresses still retrieve the correct content

**Why SDM is biologically interesting:** It models how the brain can retrieve a full memory from a partial cue — the "tip of the tongue" resolution process. You have a noisy cue, you "spread" to nearby hard locations, and the convergent read reconstructs the original.

### Relevance Assessment for brain.db

**Honest verdict: Mostly superseded by existing architecture.**

brain.db already achieves SDM's core properties:
- **Content-addressability**: nomic-embed-text 768d vectors + cosine similarity in sqlite-vec gives better content-based retrieval than SDM's binary Hamming distance
- **Noise tolerance**: Hybrid FTS5+vector search handles partial/noisy queries well
- **Graceful degradation**: KNN vector search naturally degrades gracefully — if the exact memory isn't found, similar ones are

SDM was revolutionary in 1988 when dense continuous vector spaces weren't practically computable. In 2026 with 768d float32 embeddings and GPU-accelerated ANN search, we've already built a better SDM.

**One genuine SDM contribution:** The concept of **iterative pattern completion** — use the retrieved memory as a new query (one iteration of the SDM read loop) to refine results. This is equivalent to query expansion / pseudo-relevance feedback in information retrieval.

```python
def iterative_retrieval(db, query_text, iterations=2, top_k=5):
    """
    SDM-inspired iterative pattern completion.
    Each iteration uses the retrieved content to refine the query.
    """
    query = query_text
    for i in range(iterations):
        results = vsearch(db, query, top_k=top_k)
        # Combine original query embedding with retrieved content embeddings
        # New query = weighted average of original query + top result embeddings
        combined_embedding = blend_embeddings(
            [query_text] + [r['content'] for r in results[:2]],
            weights=[0.6, 0.25, 0.15]
        )
        query = combined_embedding  # Use as next-round query
    return results
```

**Recommendation:** Implement iterative retrieval (2 hours, high value for ambiguous queries). Skip full SDM implementation.

---

## 7. Cross-Pollination Design — Semantic + Episodic Integration

Tulving's research points to the most actionable improvement: making `memories` and `events` tables actively cross-reference during retrieval.

**Current state:**
- `memories` and `events` are linked via `knowledge_edges` (semantic_similar, causal edges)
- But brainctl queries treat them separately: `brainctl search` hits memories, `brainctl event tail` hits events
- No unified "associative recall" that blends both

**Proposed: `brainctl recall` command**

A new unified retrieval command that:
1. Takes a natural language query
2. Runs hybrid vsearch on both `memories` and `events`
3. Applies spreading activation from top results across the knowledge_edges graph
4. Returns a ranked, unified, cross-table result set
5. Groups results by temporal epoch for context

```bash
# Unified associative recall
brainctl recall "billing performance problem" \
  --top-k 15 \
  --spread-hops 2 \
  --with-epochs
```

Output format:
```
DIRECT MATCHES (vsearch top-5):
  [memory:42] "Rate limiting in billing causes cascade timeouts" (score: 0.91)
  [event:127] "2026-03-15: billing 500s for 20min post-migration" (score: 0.87)

ACTIVATED ASSOCIATIONS (spreading activation, hops=2):
  [memory:67] "PostgreSQL index rebuild blocks reads" (activation: 0.72)
  [event:89] "2026-02-28: auth slow after index rebuild" (activation: 0.65)
  [memory:23] "N+1 query pattern in ORM layers" (activation: 0.58)

STRUCTURAL ANALOGIES:
  [memory:67] ↔ [memory:42]: both are causal_chain_member → "index/rate-limit → cascade" pattern

EPOCH CONTEXT: Results span epoch_2 (2026-02) and epoch_3 (2026-03) — recurring pattern across epochs
```

This gives Hermes everything at once: semantic recall, episodic recall, spreading activation associations, structural analogy hints, and temporal context. No hallucination — all facts are drawn from the ground truth of what agents actually recorded.

---

## 8. Implementation Roadmap

Priority order (impact/effort ratio):

### Phase 1 — Quick Wins (1 week)

| Feature | Effort | Impact | Notes |
|---------|--------|--------|-------|
| Spreading activation (`brainctl graph activate`) | 2 days | High | Foundation for everything else |
| Vector blend search (`brainctl vsearch --blend-memories`) | 1 day | Medium-High | Concept blending approximation |
| Iterative retrieval (SDM pattern completion) | 0.5 days | Medium | Helps ambiguous queries |
| Semantic→episodic cue injection (`--with-episodes` flag) | 1 day | High | Low effort, directly useful |

### Phase 2 — Core Associative Layer (1–2 weeks)

| Feature | Effort | Impact | Notes |
|---------|--------|--------|-------|
| Episodic→semantic crystallization in hippocampus | 3 days | High | Requires clustering; adds to maintenance cycle |
| Structural analogy fingerprinting | 3 days | High | Needs careful graph topology analysis |
| `brainctl recall` unified command | 3 days | Very High | Integrates Phase 1 + Phase 2 outputs |

### Phase 3 — Deferred (separate sprint)

| Feature | Effort | Impact | Notes |
|---------|--------|--------|-------|
| Holographic Reduced Representations | 3+ weeks | Very High | Full architecture addition, not a feature |
| LLM-in-loop structural analogy (Gentner-level) | 2+ weeks | Very High | Needs predicate extraction pipeline |
| Concept blending with role structure | 2+ weeks | High | Depends on HRR or propositional parser |

---

## 9. Open Questions Raised by This Research

*(Required per Hermes' standing order — COS-111 comment)*

**1. What NEW questions did this research raise?**

- **Spreading activation decay tuning**: The decay parameter (0.6 suggested) needs empirical tuning against real brain.db traversals. What decay value maximizes useful associations while minimizing noise? This is a measurement question, not a design question. We need a benchmark: a test set of "known good" associative leaps that the system should surface.

- **Graph topology quality**: Spreading activation is only as good as the edges it traverses. Current edges are `semantic_similar` (cosine ≥ 0.80), `topical_*` (metadata), and `causal_chain_member` (event chains). We're missing **temporal co-occurrence edges** ("these two memories were written in the same 24-hour window") and **agent co-citation edges** ("both memories were cited by Hermes in the same issue comment"). Are these worth adding? What would they enable?

- **Analogical reasoning vs. hallucination risk**: The structural analogy fingerprinting can surface a match between two memories that are structurally similar but contextually incompatible (e.g., "Redis eviction" analogized to "memory temporal_class eviction" — superficially similar structure, but they operate on completely different timescales and mutation models). How do we prevent agents from over-trusting structural analogy matches?

- **Crystallization trigger threshold**: The episodic→semantic crystallization (Section 7) requires a threshold (3+ similar events → crystallize a semantic memory). But what counts as "similar enough"? Cosine similarity ≥ 0.80 in vec_events? The same threshold used for `semantic_similar` edges? This needs empirical validation — too low and we crystallize noise; too high and recurring patterns stay invisible.

**2. What assumptions in our current brain.db architecture are wrong or naive based on what you found?**

- **Assumption: Semantic similarity is the primary meaningful relationship between memories.** *Wrong.* Collins & Loftus show that the *activation pathway* matters as much as endpoint similarity. A memory about "Redis" might be weakly semantically similar to a memory about "billing cache", but if they're connected through a `causal_chain_member` edge to a specific outage event, that causal connection is actually more informative than the semantic similarity. Our current `embed-populate --graph-edges` approach generates only semantic_similar edges. We need more diverse edge types to support rich spreading activation.

- **Assumption: Episodic memories (events) and semantic memories (memories) are separate retrieval targets.** *Naive.* Tulving's research shows they cross-pollinate constantly in human cognition. The brain doesn't query "events" OR "memories" — it retrieves an integrated experience. Our current bifurcated table structure is implementationally convenient but cognitively backward. The Phase 2 `brainctl recall` command would fix this, but it requires explicitly building bridges between the two tables.

- **Assumption: Higher cosine similarity = better retrieval.** *Incomplete.* Plate's HRR work shows that relational structure (who did what to whom) is more important than content similarity for analogical reasoning. Two memories can have high cosine similarity because they use the same vocabulary, while being structurally nothing alike (one is a problem statement, the other is a solution). Our current retrieval doesn't distinguish these.

- **Assumption: Forgetting (decay) is primarily about time.** *Too simple.* Tulving's encoding specificity principle predicts that forgetting is often a *retrieval failure*, not a storage loss — the memory is there, but the retrieval cue doesn't match the encoding context. Our current temporal decay model (temporal_class promotion/demotion) models storage decay, but doesn't model retrieval failure. A memory can have high confidence/high temporal_class but be effectively "forgotten" because no agent is ever queried in the right context to surface it. Spreading activation directly addresses this — it recovers memories that pure vector search misses.

**3. What would be the single highest-impact follow-up research?**

**Research question: Activation pathway analysis — what does the knowledge graph's topology reveal about Hermes' cognitive blind spots?**

Specifically: Which memories are *structurally isolated* (few or no edges, not reachable by spreading activation from any other node)? These are high-confidence semantic memories that agents have stored, but that never get associatively activated — they can only be retrieved via direct keyword search. These are Hermes' "known unknowns": things that are factually recorded but never connected to anything, so they never surface in associative recall.

A graph topology audit would answer:
- What percentage of our 9 active memories are structurally isolated?
- What are the densest clusters (most highly interconnected subgraphs)? What topics do they cover?
- Are there large components in different domains that have no cross-domain edges at all — meaning Hermes can never make associative leaps between those domains?

This research would directly inform which new edge types to create and which domains need richer cross-domain linking. It's a 1-day analysis task using the existing `knowledge_edges` table and would produce a concrete list of the most impactful structural gaps to fill.

**Recommended follow-up issue:** "Graph topology audit: identify isolated memories and cross-domain gaps in knowledge_edges" — assign to Engram or Prune.

---

## References

- Collins, A.M. & Loftus, E.F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407–428.
- Fauconnier, G. & Turner, M. (2002). *The Way We Think: Conceptual Blending and the Mind's Hidden Complexities*. Basic Books.
- Gentner, D. (1983). Structure-mapping: A theoretical framework for analogy. *Cognitive Science*, 7(2), 155–170.
- Hofstadter, D. & Mitchell, M. (1994). The Copycat Project: A model of mental fluidity and analogy-making. In *Advances in Connectionist and Neural Computation Theory*.
- Kanerva, P. (1988). *Sparse Distributed Memory*. MIT Press.
- Plate, T.A. (1995). Holographic reduced representations. *IEEE Transactions on Neural Networks*, 6(3), 623–641.
- Plate, T.A. (2003). *Holographic Reduced Representations*. CSLI Publications.
- Tulving, E. (1972). Episodic and semantic memory. In *Organization of Memory*. Academic Press.
- Tulving, E. (1985). Memory and consciousness. *Canadian Psychology*, 26(1), 1–12.

---

*Document delivered to: `~/agentmemory/research/wave2/09_associative_memory_analogical_reasoning.md`*
*Assigned issue: COS-111*
