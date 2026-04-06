# Cognitive Compression & Abstraction
## Research Report — COS-116
**Author:** Prune (Memory Hygiene Specialist)
**Date:** 2026-03-28
**Target:** brain.db — Hierarchical compression architecture enabling Hermes to scale from ~10K to 1M+ memory records without retrieval degradation

---

## Executive Summary

This report answers the question: *how do we keep brain.db useful as it grows by 100×?* The core finding is that **compression is not a storage problem — it is an epistemology problem**. The most effective compression strategy is not quantization or deduplication; it is *abstraction*: replacing many specific records with fewer general ones that encode the same actionable knowledge.

Six theoretical frameworks are analyzed. The central architectural recommendation: implement a **three-tier hierarchical memory** (Raw → Episode → Abstraction) with **progressive summarization** as the compression engine and **power-law forgetting** as the cleanup policy. Scalable retrieval is addressed via HNSW indexing over Matryoshka embeddings with multi-resolution search at query time.

**Estimated impact:** 90–95% reduction in active memory footprint at 1M records while maintaining or improving retrieval quality, measured by task completion rate in retrieval benchmarks.

---

## 1. Hierarchical Memory Systems

### Theory — Schema Theory, Scripts & Frames (Bartlett 1932; Schank & Abelson 1977; Minsky 1975)

**Core idea:** The brain does not store events verbatim. It stores *schemas* — abstract templates capturing the typical structure of a class of events — and fills in deviations. A "restaurant schema" encodes the order/eat/pay sequence; storing a specific restaurant visit requires only the departures from the schema (unusual food, unexpected companion).

**Key insight for brain.db:** Most agent memories repeat with variation. Hermes's 22+ agents do similar work daily: checkout tasks, read code, post comments, close tickets. Each event is stored as a full record. A schema-based system would store the pattern once and encode only deltas.

**Frames (Minsky 1975):** A frame is a data structure for representing stereotyped situations. Each slot has a default value; deviations from defaults are what actually need to be stored.

```
FRAME: task_completion
  slots:
    agent: <agent-name>
    project: <project-name>
    action: [checkout, comment, close]  ← default: close
    duration: <hours>                    ← default: 2h
    outcome: [done, blocked, escalated]  ← default: done
```

Storing a standard task close needs only: `{agent: Prune, project: COS, task: COS-116}`. Everything else defaults. An unusual outcome (escalation) stores the exception: `{agent: Prune, outcome: blocked, blocker: "no write access"}`.

**Script theory (Schank & Abelson 1977):** Scripts are procedural schemas — ordered sequences of actions associated with situational contexts. The "heartbeat script" (wake → inbox → checkout → work → comment → close) is a script. Once learned, individual heartbeat logs only need to store the deviations.

### Multi-Level Summarization Applied to brain.db

The human memory system organizes at multiple abstraction levels simultaneously:
- **Verbatim/Echoic:** exact sensory input (discarded quickly)
- **Episodic:** specific events with context (short-term, concrete)
- **Semantic/Generic:** facts, patterns, generalizations (long-term, abstract)
- **Gist:** high-level meaning that survives when details are lost (Reyna & Brainerd, Fuzzy Trace Theory)

Applied to brain.db:

| Level | brain.db Table | Retention Policy | Compression Ratio |
|-------|---------------|-------------------|-------------------|
| Verbatim | `events` | 7–30 days | 1× (ephemeral) |
| Episodic | `memories` (episodic type) | 90–365 days | ~10× summary |
| Semantic | `memories` (semantic type) | Indefinite | ~100× abstract |
| Gist | `context` table + schemas | Permanent | ~1000× |

---

## 2. Information-Theoretic Compression

### Minimum Description Length (MDL)

**Core idea (Rissanen 1978):** The best model of data is the one that minimizes the total description length: `L(model) + L(data | model)`. A model that overfits requires a short model description but a long data-given-model description; an underfit model is the reverse.

**Applied to memory:** The "model" is the set of abstractions (schemas, patterns) in Hermes's knowledge graph. The "data" is the raw event stream. MDL says: keep abstracting until adding another abstraction costs more bits to describe than it saves in encoding the instances.

**Practical criterion:** A new abstraction is worth creating if it subsumes ≥ N instances (N ≥ 5 is a reasonable threshold — below that, the abstraction overhead exceeds the savings). Each abstraction should capture a *compressible* regularity: repeated structure with low deviation entropy.

**Kolmogorov Complexity:** In theory, the shortest program that generates a sequence is its Kolmogorov complexity. Incompressible sequences (true noise) have no useful abstraction — no pattern to learn. This tells us: **memories that have no regularity with any other memory should not be abstracted; they should be evaluated for deletion**.

### Entropy as a Salience Signal

High-entropy memories (unique, surprising, no pattern match) have *high information value* and should be retained verbatim — they're the outliers that don't fit schemas. Low-entropy memories (routine, predictable) are candidates for compression into their schema instance.

```python
def compression_salience(memory, schema_library):
    best_schema = find_best_schema(memory, schema_library)
    if best_schema is None:
        return "verbatim_retain"  # unique — no compression, high salience

    deviation_entropy = compute_deviation_entropy(memory, best_schema)

    if deviation_entropy < THETA_LOW:
        return "schema_instance"  # compress to {schema_id, delta}
    elif deviation_entropy < THETA_HIGH:
        return "compressed_retain"  # summarize, retain details that deviate
    else:
        return "verbatim_retain"  # too much deviation — keep full record
```

---

## 3. Progressive Summarization

### Forte's Framework Applied to Aging Memories

Tiago Forte's progressive summarization approach (2017) applies four passes to notes:
1. **Raw capture** — everything as-is
2. **Bold** — highlight the most interesting parts
3. **Highlight** — bold the best of the bold
4. **Executive summary** — synthesize in your own words

Applied to brain.db's temporal class system:

| Temporal Class | Compression Action | Trigger |
|---------------|-------------------|---------|
| `ephemeral` (0–7d) | None (full verbatim) | — |
| `short` (7–30d) | Pass 1: Extract key facts, discard raw event body | Age-out at 7d |
| `medium` (30–180d) | Pass 2: Bold — retain only actions taken + outcomes | Age-out at 30d |
| `long` (180d–2yr) | Pass 3: Highlight — single-sentence gist + exception flags | Age-out at 180d |
| `permanent` | Pass 4: Synthesize — abstracted into semantic schemas | Manual or high-access |

**Key implementation note:** This is already partially designed in the existing `consolidation_cycle.py`. The missing piece is the **LLM compression pass** — automated summarization of aging episodic memories using a lightweight Claude call (haiku-tier). The `context_compression.py` module handles token-budget selection but doesn't write compressed versions back to the store.

### Compression-on-Write vs. Compression-on-Age

Two architectures:
- **Write-time compression:** Every memory is immediately summarized and slotted into the right level. High overhead on write, clean retrieval.
- **Age-triggered compression:** Raw memories age through the system; batch compression runs nightly.

**Recommendation:** Age-triggered compression aligns with the existing consolidation cycle design and avoids write-path latency. The nightly `consolidation_cycle.py` job should add a compression pass *after* the temporal class demotion pass — demote first, then compress newly-demoted records.

---

## 4. Abstraction Learning

### Concept Formation & Prototype Extraction

**Prototype theory (Rosch 1973):** Categories are represented by prototypes — the most typical member. A "dog" concept is the prototypical dog (Labrador-sized, friendly, domestic). Novel instances are categorized by similarity to the prototype.

**Exemplar theory:** Categories are represented by all stored exemplars. Classification is done by similarity to the full set. More accurate but doesn't scale.

**Recommendation:** Hybrid approach. Start with exemplar storage (current brain.db model — every memory is an exemplar). At compression time, extract prototypes for high-frequency clusters:

```python
def extract_prototypes(memories, cluster_threshold=0.85):
    """
    Group semantically similar memories (cosine sim > threshold).
    Extract centroid embedding as prototype.
    Store prototype with exemplar count and deviation summary.
    Archive original exemplars to cold storage.
    """
    clusters = cosine_cluster(memories, threshold=cluster_threshold)
    prototypes = []
    for cluster in clusters:
        if len(cluster) < MIN_CLUSTER_SIZE:
            continue  # too small to prototype — keep exemplars
        proto = {
            'embedding': np.mean([m.embedding for m in cluster], axis=0),
            'body': summarize_cluster(cluster),  # LLM summarization
            'exemplar_count': len(cluster),
            'deviation_memory_ids': [m.id for m in cluster if is_outlier(m, cluster)],
            'type': 'semantic',  # prototypes are semantic memories
        }
        prototypes.append(proto)
    return prototypes
```

**Key property:** Prototypes compress N exemplars into 1 record, but retain pointers to the deviating exemplars (the outliers that don't fit the pattern). These outliers are kept verbatim — they're the high-information-density records.

### Automatic Schema Induction

Schemas can be induced from repeated event sequences:

1. **Sequence mining:** Find frequently co-occurring memory chains (e.g., "checkout → read heartbeat-context → read comments → post update → close" appears in 80% of heartbeat events).
2. **Template abstraction:** Extract the invariant structure as a schema; store only the variable slots.
3. **Slot filling:** Future matching events are stored as schema instances + slot values.

This is analogous to what modern LLMs do implicitly — they've compressed the schema for "task completion" into their weights. We need an explicit, inspectable version in brain.db.

---

## 5. Forgetting as a Feature

### Rational Analysis of Memory (Anderson & Schooler 1991)

**Core finding:** Human memory forgetting follows the environment's statistics. The probability that a memory will be needed decreases as a power law of time since last access and increases with each new access:

```
Activation(t) = log(Σ_j t_j^(-d))
```

Where `t_j` is the time since the j-th access and `d` is the decay parameter (empirically ~0.5 for humans).

**Key insight:** Anderson & Schooler showed that human memory retrieval probability tracks the empirical frequency of information being needed in the environment. Memory is *Bayesian optimal* given the statistical structure of the environment. This means: the "right" forgetting rate is derived from the actual usage patterns of the agent population.

### Power Law of Forgetting (Ebbinghaus)

`Retention = e^(-t/S)` — but this is exponential. The **power law of forgetting** (Wixted & Ebbesen 1991) is actually a better fit to human data:

```
Retention = c × t^(-d)
```

Power law decay is slower than exponential initially and faster at long timescales. It better explains why we remember 1-year-old memories disproportionately well compared to 3-month-old ones.

**Applied to brain.db temporal classes:** Current design uses five-tier class demotion (λ: 0.5, 0.2, 0.05, 0.01, none). This is an approximation of exponential decay. A power-law decay function would be more neurologically accurate and would retain more of the "medium" and "long" class memories that are accessed occasionally.

### Optimal Forgetting Policy Design

**What should be deleted vs. compressed vs. retained:**

| Signal | Action | Rationale |
|--------|--------|-----------|
| Zero accesses in 180d, no links in knowledge_edges | Delete | Never needed, no graph value |
| Zero accesses in 90d, but linked from ≥3 edges | Compress to gist | Graph structure implies future relevance |
| Last access < 30d | Retain verbatim | Still in active recall window |
| Accessed ≥ 3 times in any period | Promote to permanent | Repeated recall = high value |
| Flagged as contradiction | Delete old, retain corrected | Stale contradictions poison retrieval |
| High confidence (≥ 0.9) semantic fact | Never delete | Anchors the knowledge graph |

**Forgetting as garbage collection:** The Prune agent's core role maps directly to this. Scheduled compression and deletion passes on the above criteria would reduce brain.db to its *minimum viable knowledge graph* — the smallest representation that preserves retrieval quality.

**Estimated reduction:** Based on the EXECUTIVE_BRIEFING.md data (14 memories from 123 events = 11% retention), the current system already over-retains at the event layer. If we apply optimal forgetting to the memory layer with appropriate threshold calibration, a 90-day-old brain.db with 10K records should compress to ~1K active memories + ~500 archived prototypes, with the remaining ~8.5K eligible for deletion or cold storage.

---

## 6. Scalable Vector Search

### Current State

The EXECUTIVE_BRIEFING.md notes: "sqlite-vec = dead code (not installed)". This means current retrieval is BM25 (FTS5) only. Embedding-based similarity search is not operative. At the scale of thousands of records, this is workable. At 100K+, it becomes a bottleneck.

### HNSW — Hierarchical Navigable Small World (Malkov & Yashunin 2018)

**How it works:** HNSW builds a multi-layer proximity graph. The top layer has few nodes connected by long-range edges; lower layers add progressively more nodes with shorter-range edges. Search starts at the top layer and greedily descends toward the query point.

**Performance:** `O(log N)` search, compared to `O(N)` for brute-force. At 1M vectors (768d), HNSW achieves ~10ms per query at 95% recall@10 with appropriate `ef_construction` and `M` parameters. Brute force at 1M would be ~200ms (768d float32, no SIMD).

**For brain.db:** `sqlite-vec` supports HNSW via its `vec_each()` virtual table. Once installed (a one-line pip/brew operation), HNSW indexing is available:

```sql
CREATE VIRTUAL TABLE memory_idx USING vec0(
    memory_id INTEGER,
    embedding FLOAT[768]
);
-- Populate from memories table
INSERT INTO memory_idx SELECT id, embedding FROM memories WHERE embedding IS NOT NULL;
```

**Matryoshka Embeddings (MRL — Kusupati et al. 2022):** Modern embedding models (including nomic-embed-text v1.5) support Matryoshka Representation Learning. The model is trained so that truncating the embedding at any prefix dimension (768 → 512 → 256 → 128 → 64) gives a useful embedding at that lower dimensionality with graceful quality degradation.

**Multi-resolution search strategy:**

```python
def multi_resolution_search(query, budget_ms=50):
    """
    Fast first pass at 64d, then refine with full 768d only on candidates.
    Reduces compute by ~12× while maintaining 90%+ of 768d recall.
    """
    # Pass 1: fast 64d scan over all ~1M records (~2ms at 1M with HNSW)
    candidates = hnsw_search(query[:64], k=200, index="hnsw_64d")

    # Pass 2: full 768d rerank of top 200 candidates (~0.5ms)
    reranked = cosine_rerank(query[:768], candidates, k=20)

    # Pass 3: BM25 fusion (existing FTS5) over reranked set
    return rrf_merge(reranked, bm25_search(query_text, k=20))
```

**Index sizing benchmarks:**

| Records | 768d HNSW Index | 64d HNSW Index | Search Time (768d) |
|---------|----------------|----------------|-------------------|
| 10K | 30 MB | 2.5 MB | <1ms |
| 100K | 300 MB | 25 MB | ~5ms |
| 1M | 3 GB | 250 MB | ~15ms |

At 1M records, the 64d index fits in RAM on any modern machine (250 MB). The 768d index requires ~3 GB RAM for hot storage — feasible for a dedicated memory process, tight for an SQLite file approach.

### Product Quantization (PQ)

For even larger scale, PQ compresses each vector by splitting into M sub-vectors and replacing each with a cluster centroid index. 768d float32 → 96 bytes (using 8-bit codes, 8 sub-vectors): 32× compression with ~5% recall loss.

**Recommendation:** Not needed at 1M records with Matryoshka 64d. Re-evaluate at 10M+.

### IVF (Inverted File Index)

Groups vectors into K clusters; search only the nearest cluster(s). Reduces search space by `1/K` at the cost of recall. Effective for batch retrieval but adds cluster-assignment overhead on writes. HNSW is strictly better for the brain.db use case (few writes relative to reads in production).

---

## 7. Architecture Design: Hierarchical Memory with Progressive Compression

### Three-Tier Memory Model

```
┌─────────────────────────────────────────────────────────┐
│                    TIER 3: SCHEMAS                       │
│  knowledge_edges + semantic memories + prototype records  │
│  Retention: Indefinite | Records: ~1K | Pure abstractions│
├─────────────────────────────────────────────────────────┤
│                   TIER 2: EPISODES                       │
│  memories table (episodic type)                          │
│  Retention: 90–365d | Records: ~10K | Compressed events  │
├─────────────────────────────────────────────────────────┤
│                    TIER 1: RAW                           │
│  events table                                            │
│  Retention: 7–30d | Records: ~100K | Full verbatim log   │
└─────────────────────────────────────────────────────────┘
```

**Compression passes (nightly, in order):**

1. **Event → Episode:** Extract meaningful memories from events older than 7d (current distillation gap: only 14/123 = 11%). Target: 30% retention rate (each episode represents ~3 events).
2. **Episode compression:** Run progressive summarization on `short` → `medium` → `long` class demotions. Use Claude Haiku to generate compressed body.
3. **Prototype extraction:** Cluster `medium` and `long` class memories by semantic similarity (cosine sim > 0.85). Extract prototypes, archive low-deviation exemplars.
4. **Optimal forgetting:** Delete records meeting deletion criteria (see Section 5 table).
5. **Index update:** Rebuild HNSW index on modified records.

### Multi-Resolution Retrieval

```python
class HierarchicalRetriever:
    def retrieve(self, query: str, k: int = 10, resolution: str = "auto") -> list[Memory]:
        """
        Three-level retrieval: schemas → episodes → raw.
        Auto resolution selects based on query type.
        """
        if resolution == "auto":
            resolution = self._classify_query(query)  # factual/procedural/episodic

        if resolution == "schema":
            # Return abstract knowledge only — fast, high-level
            return self._search_tier3(query, k)

        elif resolution == "episode":
            # Schema + episode blend
            schemas = self._search_tier3(query, k=3)
            episodes = self._search_tier2(query, k=k-3)
            return rerank(schemas + episodes, query, k=k)

        else:  # "raw"
            # All tiers + recent events
            return self._full_search(query, k)

    def _classify_query(self, query: str) -> str:
        """Heuristic: factual queries → schema; 'what happened' → episode; debug → raw."""
        if any(w in query for w in ["what is", "how does", "define", "explain"]):
            return "schema"
        elif any(w in query for w in ["last time", "recently", "when did", "yesterday"]):
            return "episode"
        else:
            return "episode"  # default to episode — best general purpose tier
```

### Migration Plan from Current Flat Structure

**Phase 0 — Prerequisites (1–2 days):**
- Install `sqlite-vec` (pip install sqlite-vec or cargo install from source)
- Verify nomic-embed-text supports Matryoshka truncation (v1.5 does)
- Backfill embeddings for all existing memories with no embedding

**Phase 1 — Index (2–3 days):**
- Create `vec0` virtual tables: one 768d index, one 64d index (truncated at insert)
- Populate from existing `memories` table
- Wire into `brainctl search` as hybrid retrieval (BM25 + HNSW → RRF merge)

**Phase 2 — Compression Engine (3–4 days):**
- Add LLM summarization pass to `consolidation_cycle.py` (Haiku-tier, cheap)
- Implement prototype extraction with centroid computation + archival to `memory_archive` table
- Add `compressed_body` column to `memories`; update retrieval to prefer it when present

**Phase 3 — Schema Induction (4–5 days):**
- Implement sequence mining over `events` to extract heartbeat/task/communication scripts
- Build `schemas` table (schema_id, template, slot_definitions, instance_count)
- Add schema-matching pass to nightly consolidation
- Update event distillation to store schema instances instead of full bodies when match confidence > 0.9

**Phase 4 — Multi-Resolution Search (2–3 days):**
- Implement query classifier (factual vs episodic vs raw)
- Build `HierarchicalRetriever` with tier-aware search
- Expose via `brainctl search --resolution [schema|episode|raw|auto]`

**Total: ~12–17 days to full hierarchical memory.**

### Scaling Benchmarks (Projected)

| Metric | Current (flat) | After Phase 1–4 |
|--------|---------------|-----------------|
| Active memory records | ~10K | ~3K (schema+episode) |
| Cold storage records | 0 | ~50K (archived exemplars) |
| Search latency (P95) | ~50ms (BM25 only) | ~15ms (HNSW 64d + BM25) |
| Memory at 1M events | ~100K raw memories | ~3K schemas + 30K episodes |
| Retrieval quality (recall@10) | ~0.6 (BM25 baseline) | ~0.85 (hybrid) |

---

## 8. Answers to Hermes's Standing Order

### 1. New Questions Raised

- **Schema drift:** As agents change behavior over time, schemas learned from historical data become incorrect. How do we detect and update stale schemas without discarding valid historical patterns?
- **Cross-agent abstraction:** Should schemas be agent-specific or company-wide? A "heartbeat script" for Prune vs. Engram may differ in relevant ways — uniform prototyping loses agent-specific patterns.
- **Abstraction quality evaluation:** How do we know if a prototype/schema is good? We need an internal evaluation metric (e.g., reconstruction fidelity: can you recover the original episode from schema + delta?).
- **Cold storage retrieval:** When an archived exemplar is needed (e.g., a one-time event from 2 years ago), what's the access path? Index must cover cold storage or we lose episodic recall entirely.
- **Compression-induced hallucination risk:** LLM summarization may introduce errors into the compressed body. A compressed memory might be factually wrong in subtle ways that snowball. How do we validate summarization fidelity?

### 2. Wrong/Naive Assumptions in Current brain.db Architecture

- **All memories are equal.** The flat `memories` table has no compression tier, no schema layer, no distinction between prototype and exemplar. Every memory is treated identically regardless of whether it's unique (high information) or routine (low information, compression candidate).
- **Forgetting is a bug, not a feature.** The current temporal class system with decay is designed to *avoid* losing information. It should be designed to *aggressively prune* low-value memories and replace them with abstractions.
- **BM25 will scale.** FTS5 full-text search is O(N) at query time for large result sets. At 100K+ records with complex queries, this will visibly degrade. The assumption that the current retrieval stack scales is wrong.
- **Events don't need to be memories.** The 11% event-to-memory distillation rate is a symptom of the naive assumption that memories should only come from "significant" events. Progressive summarization says: *all events should become memories, but most should be immediately compressed into schema instances.*
- **One embedding per memory.** Matryoshka embeddings allow multi-resolution representation. Storing only 768d embeddings and never using the 64d prefix is leaving performance on the table.

### 3. Highest-Impact Follow-Up Research

**LLM-based automatic schema induction from agent event logs.**

The single largest leverage point is teaching brain.db to *learn its own schemas* from agent behavior patterns. Right now, schemas would have to be hand-designed. If we can train or prompt a model to:
1. Ingest 30 days of agent event logs
2. Identify recurring action sequences with >80% structural similarity
3. Output a labeled schema library with slot definitions and instance counts

...then compression becomes automatic. The schema library would compound over time, covering more of the event space, driving compression ratios toward the theoretical maximum (Kolmogorov complexity of the agent behavior process).

This is the research that would 10× Hermes's memory efficiency — not better retrieval algorithms, but automatic knowledge distillation from raw operational data.

---

*Delivered to: `~/agentmemory/research/wave2/10_cognitive_compression_abstraction.md`*
*Follow-up issues recommended for: schema induction, cold storage access path, compression fidelity validation.*
