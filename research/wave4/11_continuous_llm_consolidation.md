# Continuous LLM Consolidation — Research Report

**Wave 4 Research** | [COS-183](/COS/issues/COS-183)
**Author:** Tensor
**Date:** 2026-03-28
**Builds on:** `05_consolidation_cycle.py`, `08_context_compression.py` (Wave 1)

---

## Root Question

> Can we eliminate garbage accumulation with continuous compression instead of batch cycles?

## Answer: Yes — via a hybrid event-driven + polling service

Continuous consolidation is feasible on the current SQLite stack. The key insight: **the batch cycle's architecture is sound; only its schedule needs to change**, plus two additions — write-time deduplication and incremental (running) summarization. Full meaning-based clustering is deferred until sqlite-vec is installed.

---

## The Problem with Nightly Batch

`05_consolidation_cycle.py` runs once per day. Between runs, garbage accumulates unchecked:

| Problem | Effect |
|---|---|
| 24-hour accumulation window | Potentially thousands of ephemeral/short memories pile up before any consolidation |
| Age-based clustering only | `(category, scope)` grouping misses cross-scope topic duplicates |
| Bursty LLM cost | All summaries requested in one cycle → latency spike, cost spike |
| Large batch transactions | One big SQLite write per cycle → higher lock contention with concurrent agents |
| Write-time no-op | Redundant memories are written and then cleaned up — never prevented |

With 22+ agents writing to `brain.db`, the nightly batch is structurally too slow.

---

## Architecture: Hybrid Continuous Consolidator

Two orthogonal mechanisms working in parallel:

```
Memory Write (any agent)
         │
    ┌────▼──────────────────────────┐
    │  Write-Time Dedup Hook        │  ← Prevents redundant inserts
    │  - FTS Jaccard similarity     │
    │  - If overlap ≥ 0.7: update   │
    │    confidence instead of INSERT│
    └────┬──────────────────────────┘
         │  (only genuinely new memories reach here)
         ▼
    brain.db memories table
         │
    ┌────▼──────────────────────────┐
    │  Background Poll Loop         │  ← Runs every POLL_INTERVAL (5 min)
    │  - Find clusters ≥ K          │
    │  - Age watermark sweep        │
    │  - Enqueue for consolidation  │
    └────┬──────────────────────────┘
         │
    ┌────▼──────────────────────────┐
    │  Incremental LLM Consolidator │
    │  - Update running summary     │
    │  - Retire source memories     │
    │  - Rate-limited (token bucket)│
    └────┬──────────────────────────┘
         │
    brain.db (consolidated memory, retired sources)
```

---

## Component Design

### 1. Write-Time Dedup Hook

Before inserting a new memory, check for near-duplicates in the same `(agent_id, category)`:

```python
def is_redundant(conn, new_content: str, agent_id: str, category: str,
                 threshold: float = 0.70) -> tuple[bool, int | None]:
    """
    Returns (True, existing_id) if new_content is ≥ threshold Jaccard
    overlap with an existing active memory in the same agent+category.
    Fast path: FTS token overlap. No LLM, no embeddings required.
    """
    new_toks = set(re.findall(r'\b\w{4,}\b', new_content.lower()))
    if not new_toks:
        return False, None
    rows = conn.execute("""
        SELECT id, content FROM memories
        WHERE agent_id = ? AND category = ? AND retired_at IS NULL
        ORDER BY created_at DESC LIMIT 50
    """, (agent_id, category)).fetchall()
    for row_id, content in rows:
        existing_toks = set(re.findall(r'\b\w{4,}\b', content.lower()))
        if not existing_toks:
            continue
        overlap = len(new_toks & existing_toks) / len(new_toks | existing_toks)
        if overlap >= threshold:
            return True, row_id
    return False, None
```

**Effect:** Stops garbage at the source. Redundant writes become confidence-bump updates on existing memories instead of new rows.

---

### 2. Background Poll Loop

Runs every `POLL_INTERVAL_SEC` (default: 300 seconds). Two sweep types:

**Cluster size sweep** — find (category, scope) groups with ≥ K un-consolidated members:
```sql
SELECT category, scope, agent_id, COUNT(*) as cnt
FROM memories
WHERE retired_at IS NULL
  AND temporal_class IN ('ephemeral', 'short')
  AND (julianday('now') - julianday(created_at)) * 1440 >= :min_age_min
GROUP BY agent_id, category, scope
HAVING cnt >= :min_cluster_size
```

**Age watermark sweep** — anything ephemeral older than `EPHEMERAL_MAX_AGE_MIN` regardless of cluster size:
```sql
SELECT id, agent_id, category, scope, content, confidence
FROM memories
WHERE retired_at IS NULL
  AND temporal_class = 'ephemeral'
  AND (julianday('now') - julianday(created_at)) * 1440 >= :max_age_min
ORDER BY created_at ASC
LIMIT :batch_size
```

Both feeds the same consolidation queue. The poll loop never calls LLM directly — it enqueues work for the rate-limited consolidator.

---

### 3. Incremental (Running) Summarization

Current batch approach: collect all N texts → one LLM call → single summary.

Continuous approach: maintain a **running summary** per cluster. Each new memory that joins the cluster updates the summary incrementally:

```
running_summary[t+1] = LLM(
    system: "Update this summary to incorporate the new memory. Be concise.",
    existing_summary: running_summary[t],
    new_memory: new_content
)
```

**Cost comparison:**

| Approach | LLM calls for N=10 memories | LLM calls for N=100 |
|---|---|---|
| Batch (current) | 1 call (all at once) | 1 call (all at once) |
| Running summary (new) | Up to 10 calls (one per new member) | Up to 100 calls |
| Running summary with K=3 gate | 1 call at 3, 1 at 6, 1 at 9 ... | ~33 calls |
| Map-reduce | log₂(10) ≈ 4 calls | log₂(100) ≈ 7 calls |

**Recommendation:** Use running summary with `SUMMARIZE_EVERY_K = 3` — only call LLM when the cluster grows by K new members since last summary. This bounds cost to O(N/K) calls rather than O(N).

The quality tradeoff: incremental summaries may lose precision for large clusters. Mitigate by re-summarizing from scratch whenever a cluster exceeds `FULL_RESUMMARY_THRESHOLD = 30`.

---

### 4. Rate Limiter (Token Bucket)

Always-on compression must not run unchecked:

```python
class RateLimiter:
    """Token bucket: max LLM_CALLS_PER_HOUR calls/hour."""
    def __init__(self, calls_per_hour: int = 60):
        self.capacity = calls_per_hour
        self.tokens = calls_per_hour
        self.last_refill = time.monotonic()
        self.refill_rate = calls_per_hour / 3600.0  # tokens/sec

    def acquire(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False  # caller must back off
```

Cluster cooldown prevents re-consolidating the same cluster too frequently:
```python
# In state table or in-memory dict:
last_consolidated: dict[str, float]  # cluster_key -> unix_timestamp
COOLDOWN_SEC = 600  # 10 minutes between re-consolidation of same cluster
```

---

## Event-Driven vs. Periodic Polling

SQLite has no native push notifications. True event-driven consolidation would require either:
- An in-process hook in brainctl intercepting every `memory add`
- A WAL log watcher (fragile, OS-dependent)
- A separate change-data-capture layer

**Recommendation: polling at 5-minute intervals is sufficient and simpler.**

Rationale:
- Maximum garbage accumulation = `POLL_INTERVAL × write_rate`
- At 5 min × 10 memories/min = 50 ephemeral memories maximum pending
- Current batch allows 24h × 10/min = 14,400 memories pending
- **5-min polling is a 288× reduction in accumulation ceiling**

Reserve event-driven triggers for a future phase when brainctl natively supports write hooks. The polling approach can be deployed today.

---

## Clustering: Meaning-Based Without sqlite-vec

sqlite-vec is not yet installed, so full embedding similarity is unavailable. Practical options today:

| Method | Similarity Basis | Cost | Quality |
|---|---|---|---|
| `(category, scope)` grouping (current) | Metadata only | O(1) | Low — misses cross-scope duplicates |
| FTS Jaccard token overlap | Lexical | O(k) per pair | Medium — good for exact/near-exact |
| FTS BM25 match score | Weighted lexical | O(log n) via index | Good — handles frequency weighting |
| Embedding cosine similarity | Semantic | O(n) + model call | Best — catches paraphrases |

**Deployed now:** FTS Jaccard (already used in dedup hook and `find_near_duplicates_simple`).
**Migration path:** When sqlite-vec is available, replace Jaccard with `vec_distance_cosine(embedding1, embedding2) < threshold` in the clustering query. The consolidation logic is unchanged.

---

## Comparison: Batch vs. Continuous

| Dimension | Batch (current) | Continuous (proposed) |
|---|---|---|
| Consolidation latency | Up to 24 hours | ≤ 5 minutes |
| LLM cost pattern | Bursty (all at once) | Smooth (spread over time) |
| Garbage accumulation ceiling | ~14,400 memories (24h window) | ~50 memories (5 min × 10/min) |
| Clustering basis | Age + (category, scope) | Semantic similarity (Jaccard now, vec later) |
| Failure blast radius | Miss cycle = 24h debt | Miss poll = 5 min debt |
| Write amplification | Same (N retired + 1 consolidated) | Same, but spread out |
| SQLite lock contention | One large txn/day | Many small txns throughout day |
| Complexity | Simple cron | Background service with state |
| Compression by meaning | Partial | Full (after sqlite-vec) |

---

## Phased Rollout Plan

| Phase | Change | Effort | Dependency |
|---|---|---|---|
| **Phase 1** | Reduce batch cron from 24h → 5 min | Trivial (config change only) | None — deploy today |
| **Phase 2** | Add write-time dedup in brainctl `memory add` | Small — ~50 lines | brainctl source access |
| **Phase 3** | Implement running summary with SUMMARIZE_EVERY_K=3 | Medium — LLM integration | LLM hook in `consolidate_cluster` already exists |
| **Phase 4** | Replace polling with brainctl write hooks (event-driven) | Large | brainctl plugin API |
| **Phase 5** | Embedding-based clustering | Medium | sqlite-vec installed ([COS-205](/COS/issues/COS-205)) |

Phase 1 alone answers the root question: **yes, continuous compression can eliminate garbage accumulation** — it's primarily a scheduling problem, not an algorithmic one.

---

## Resource Budget

Recommended configuration for always-on operation:

```python
ContinuousConsolidatorConfig = {
    "poll_interval_sec": 300,        # 5-minute polling cycle
    "min_cluster_size": 3,           # same as batch (don't consolidate singletons/pairs)
    "ephemeral_max_age_min": 30,     # sweep ephemeral after 30 minutes regardless of cluster size
    "short_max_age_hr": 6,           # sweep short-class after 6 hours
    "llm_calls_per_hour": 60,        # max 60 LLM calls/hour (token bucket)
    "cooldown_sec": 600,             # 10-min cluster cooldown to avoid thrash
    "summarize_every_k": 3,          # incremental re-summary every 3 new members
    "full_resummary_threshold": 30,  # full re-summary when cluster exceeds 30
    "dedup_jaccard_threshold": 0.70, # write-time dedup threshold
    "similarity_threshold": 0.40,    # clustering assignment threshold
    "batch_size_per_poll": 200,      # max memories processed per poll cycle
}
```

**Expected steady-state load:**
- CPU: < 1% (mostly sleeping, SQLite queries are fast)
- LLM: 10-20 calls/hour under normal agent activity (well within 60/hr cap)
- SQLite writes: ~5-15 small transactions/poll cycle
- Memory footprint: < 10 MB (cluster state fits in-process)

---

## Blockers

| Blocker | Impact | Mitigation |
|---|---|---|
| sqlite-vec not installed | Meaning-based clustering limited to Jaccard | Deploy with FTS Jaccard now; swap to vec later |
| LLM summarizer not wired in `consolidate_cluster` | Phase 3 requires actual LLM integration | `summarizer_fn` hook exists; wire `brainctl` or Anthropic SDK |
| brainctl write serialization | Continuous small writes increase contention with 22+ agents | Use WAL mode; serialize writes through brainctl queue |
| Cold-start debt | Existing accumulated memories need one final batch run | Run `05_consolidation_cycle.py` once before switching to continuous mode |

---

## Prototype

See `wave4/11_continuous_llm_consolidation.py` — reference implementation of the poll loop, write-time dedup hook, running summary state, and rate limiter. No LLM dependency in the critical path; LLM is injected via callback (same pattern as `05_consolidation_cycle.py`).

---

## Connections to Other Wave 4 Work

- **[COS-178](/COS/issues/COS-178) — Memory Granularity Calibration**: Continuous mode makes granularity more important — if memories are too fine-grained, consolidation will thrash. Calibration work should inform the `min_cluster_size` and `summarize_every_k` parameters.
- **[COS-205](/COS/issues/COS-205) — Embedding-First Writes**: Phase 5 of this roadmap. When embeddings are stored at write time, the clustering in Phase 4 becomes semantic-first rather than lexical-first.
- **[COS-184](/COS/issues/COS-184) — Causal Event Graph**: Continuous consolidation produces events at higher frequency. Causal graph should be updated incrementally in the same poll cycle.
- **[COS-122](/COS/issues/COS-122) — Multi-Agent Write Contention**: Continuous consolidation adds a new write source. Must be accounted for in the write serialization design.
