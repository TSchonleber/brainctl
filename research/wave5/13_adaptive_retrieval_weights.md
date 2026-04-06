# Adaptive Retrieval Weights — Dynamic Scoring as Memory Store Scales

**Research Wave:** 5
**Issue:** COS-201
**Author:** Recall (Memory Retrieval Engineer)
**Date:** 2026-03-28
**Cross-pollinate:** Weaver (query routing context)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

Current retrieval scoring uses a fixed weight vector designed for a sparse,
early-stage store: `0.45×similarity + 0.25×recency + 0.20×confidence + 0.10×importance`.
At the present store size (39 memories, all written in the last 24 hours), this is
reasonable. At 10× scale (390+ memories, spanning weeks or months), the optimal
weights shift significantly. Recency should dominate more, similarity should weight
less relative to quality signals, and importance becomes a meaningful differentiator
once recalled_count statistics are non-trivial.

This report delivers:
1. **Weight sensitivity analysis** — how scoring changes at scale per memory store
   properties
2. **Proposed adaptive schema** — dynamic weight computation from store statistics
3. **Feedback loop design** — backpropagating retrieval outcomes into weight signals
4. **Query-type weight profiles** — separate vectors for temporal, factual, procedural
   queries

---

## 1. Current State Baseline

### Store Statistics (2026-03-28)

| Metric | Value |
|--------|-------|
| Active memories | 39 |
| Average confidence | 0.903 |
| Age range | ~8 hours (all created 2026-03-27 to 2026-03-28) |
| recalled_count > 0 | 1 of 39 (2.6%) |
| Category distribution | project(21), lesson(10), decision(5), env(2), identity(1) |
| Temporal class | medium(37), long(2) |

### Current Weight Vector (Wave 1)

```python
W_SIMILARITY  = 0.45   # cosine similarity via FTS5 (vec pending)
W_RECENCY     = 0.25   # exp(-0.1 * days_since_last_recall)
W_CONFIDENCE  = 0.20   # stored confidence score
W_IMPORTANCE  = 0.10   # log-normalized recalled_count
```

### Current Pathologies

**Confidence compression:** 36 of 39 memories have confidence ≥ 0.90. When
most memories cluster near 1.0, the 0.20 confidence weight is effectively
noise. It only differentiates the bottom 3 entries.

**Importance near-zero:** recalled_count is 0 for 97% of memories. The
0.10 importance weight is functionally dead. Importance is a dormant signal
waiting for usage history to exist.

**Recency bias too mild:** All memories are ≤ 8 hours old. At scale, with
memories spanning weeks, the 0.25 recency weight may over-surface stale
entries that are similarity-adjacent but operationally irrelevant.

---

## 2. Weight Sensitivity Analysis at Scale

### 2a. What changes at 10× (390 memories)

**Similarity** (currently 0.45): At higher density, more memories will be
semantically similar to any given query. The similarity signal becomes noisier
because a larger fraction of the corpus is "close enough." This argues for
**reducing W_SIMILARITY** slightly and compensating with quality signals.
Proposed range at 10×: 0.35–0.40.

**Recency** (currently 0.25): A corpus spanning 30+ days has real temporal
variation. Recency becomes meaningful. But the current decay function
`exp(-0.1 * days)` has a half-life of ~7 days — aggressive enough that a
30-day-old memory scores only 0.05. This is probably correct for tactical
decisions but too aggressive for policy/identity memories. Proposed: apply
recency only within the `medium` temporal_class; apply a much softer decay
(k=0.01) for `long`-class memories. Effective W_RECENCY: 0.25–0.30, but
class-gated.

**Confidence** (currently 0.20): Once the consolidation cycle has run for
30+ days, confidence will spread. The hippocampus decays low-value memories
and boosts recalled ones. Confidence becomes a meaningful discriminator.
At 10×, W_CONFIDENCE should rise to 0.25.

**Importance** (currently 0.10): recalled_count is the key signal. Once
memories have actual retrieval history, recalled_count differentiates hot
knowledge (project conventions, CEO identity) from cold storage (one-time
events). At 10×, W_IMPORTANCE should rise to 0.15.

### 2b. Proposed 10× weight vector (no adaptive algorithm)

```python
# Static improvement — good for 300–600 memory range
W_SIMILARITY  = 0.35
W_RECENCY     = 0.25   # but class-gated (softer for temporal_class='long')
W_CONFIDENCE  = 0.25
W_IMPORTANCE  = 0.15
```

### 2c. Deriving weights analytically from store statistics

A store's optimal weights can be approximated from three statistics:

1. **Confidence entropy** (`H_conf`): `H = -Σ p_i * log(p_i)` over
   discretized confidence bands. High entropy (diverse confidence) → increase
   W_CONFIDENCE. Low entropy (all clustered near 1.0) → reduce W_CONFIDENCE.

2. **Recency spread** (`R_spread`): std deviation of `days_since_created` across
   active memories. If R_spread < 1 day, recency is useless. If R_spread > 14
   days, recency is critical. Map: `W_RECENCY = 0.15 + 0.15 * clamp(R_spread/14, 0, 1)`.

3. **Recall Gini coefficient** (`G_recall`): Lorenz curve over recalled_count.
   High Gini (a few memories dominate retrieval) → increase W_IMPORTANCE.
   Low Gini (uniform access) → importance adds less signal.
   Map: `W_IMPORTANCE = 0.05 + 0.15 * G_recall`.

4. **Similarity** gets the remainder: `W_SIMILARITY = 1.0 - W_RECENCY - W_CONFIDENCE - W_IMPORTANCE`.

### 2d. Python implementation sketch

```python
import math
import sqlite3

def gini(values: list[float]) -> float:
    """Gini coefficient of a list of non-negative values."""
    n = len(values)
    if n == 0: return 0.0
    s = sorted(values)
    total = sum(s)
    if total == 0: return 0.0
    cumsum = 0
    lorenz = 0
    for i, v in enumerate(s):
        cumsum += v
        lorenz += cumsum
    return 1 - 2 * lorenz / (n * total)

def entropy(values: list[float], bins: int = 5) -> float:
    """Discretized entropy of confidence values."""
    if not values: return 0.0
    from collections import Counter
    buckets = [int(v * bins) for v in values]
    counts = Counter(buckets)
    total = len(values)
    return -sum((c/total) * math.log(c/total + 1e-9) for c in counts.values())

def compute_adaptive_weights(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("""
        SELECT confidence, recalled_count, created_at, temporal_class
        FROM memories WHERE retired_at IS NULL
    """).fetchall()

    if not rows:
        return dict(similarity=0.45, recency=0.25, confidence=0.20, importance=0.10)

    confidences = [r[0] for r in rows]
    recalls = [float(r[1]) for r in rows]

    # Recency spread in days
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    def parse_ts(s):
        try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except: return now
    ages = [(now - parse_ts(r[2])).total_seconds() / 86400 for r in rows]
    r_spread = (max(ages) - min(ages)) if ages else 0

    h_conf = entropy(confidences)
    g_recall = gini(recalls)

    w_recency = 0.15 + 0.15 * min(r_spread / 14.0, 1.0)
    w_importance = 0.05 + 0.15 * g_recall
    w_confidence = 0.15 + 0.10 * min(h_conf / 1.5, 1.0)
    w_similarity = max(1.0 - w_recency - w_importance - w_confidence, 0.20)

    # Normalize to sum to 1.0
    total = w_similarity + w_recency + w_confidence + w_importance
    return {
        "similarity": round(w_similarity / total, 3),
        "recency": round(w_recency / total, 3),
        "confidence": round(w_confidence / total, 3),
        "importance": round(w_importance / total, 3),
        "_r_spread_days": round(r_spread, 1),
        "_h_conf": round(h_conf, 3),
        "_g_recall": round(g_recall, 3),
    }
```

**Applied to current store (39 memories, 8-hour spread):**
- R_spread = ~0.3 days → W_recency = 0.15 (minimum, age uniformity makes recency useless)
- H_conf ≈ 0.25 (low entropy — confidence compressed near 1.0) → W_confidence = 0.15 (minimum signal)
- G_recall ≈ 0.98 (one memory has recall, rest zero — extreme Gini) → W_importance = 0.20
- W_similarity = 1 - 0.15 - 0.15 - 0.20 = 0.50 (similarity dominates sparse stores)

This matches intuition: when the store is young, keyword/semantic match is the only real
signal. Recall and confidence haven't had time to differentiate.

---

## 3. Query-Type Weight Profiles

Not all queries should use the same weights. Three query types with distinct
optimal profiles:

### 3a. Temporal queries
*"What happened with X last week?"*, *"What changed?"*

Temporal cues: words like "yesterday", "last", "recent", "when", "since", dates.

```python
WEIGHTS_TEMPORAL = dict(similarity=0.25, recency=0.50, confidence=0.15, importance=0.10)
```

Recency dominates. Staleness is a critical failure mode — the user is asking about
recent state. Similarity matters less; even a tangential recent memory beats a
highly similar stale one.

### 3b. Factual queries
*"What is the production URL?"*, *"How many agents are there?"*

Factual cues: interrogatives (what, how many, where, which), proper nouns,
specific entities.

```python
WEIGHTS_FACTUAL = dict(similarity=0.45, recency=0.20, confidence=0.30, importance=0.05)
```

Similarity and confidence dominate. For facts, we want the most *correct*
answer, not the most recent. Confidence reflects how often this memory has been
validated or promoted through the consolidation cycle.

### 3c. Procedural / how-to queries
*"How do I deploy?"*, *"What's the workflow for X?"*

Procedural cues: action verbs, "how to", "what steps", "workflow", imperative mood.

```python
WEIGHTS_PROCEDURAL = dict(similarity=0.40, recency=0.15, confidence=0.20, importance=0.25)
```

Importance (recalled_count) rises — procedural memories that have been retrieved
frequently are more likely to be the canonical, validated procedure. A process that
nobody ever recalls is probably stale or edge-case.

### 3d. Query classification implementation

```python
import re

TEMPORAL_CUES = re.compile(
    r"\b(yesterday|last\s+\w+|recent|since|when|ago|latest|this\s+week|today)\b",
    re.IGNORECASE,
)
FACTUAL_CUES = re.compile(
    r"\b(what\s+is|where\s+is|how\s+many|which|what's|the\s+\w+\s+url|what\s+version)\b",
    re.IGNORECASE,
)
PROCEDURAL_CUES = re.compile(
    r"\b(how\s+to|how\s+do|steps?\s+to|workflow|deploy|run|install|configure|what\s+steps)\b",
    re.IGNORECASE,
)

def classify_query(query: str) -> str:
    """Return 'temporal', 'factual', 'procedural', or 'default'."""
    if TEMPORAL_CUES.search(query): return "temporal"
    if FACTUAL_CUES.search(query): return "factual"
    if PROCEDURAL_CUES.search(query): return "procedural"
    return "default"

QUERY_WEIGHTS = {
    "temporal":   dict(similarity=0.25, recency=0.50, confidence=0.15, importance=0.10),
    "factual":    dict(similarity=0.45, recency=0.20, confidence=0.30, importance=0.05),
    "procedural": dict(similarity=0.40, recency=0.15, confidence=0.20, importance=0.25),
    "default":    dict(similarity=0.45, recency=0.25, confidence=0.20, importance=0.10),
}
```

---

## 4. Feedback Loop Design

### 4a. Is a feedback loop viable?

Yes, with important caveats. The signal is available but noisy.

**Available feedback signals:**
1. `recalled_count` increment — brainctl already increments this on each retrieval.
   This is passive feedback: retrieval frequency as a proxy for value.
2. Explicit agent outcome linking — if an agent completes a task and the task
   used a specific memory (logged via `derived_from_ids` or event linkage),
   outcome can be attributed.
3. Memory retirement as negative signal — when a memory is retired (superseded or
   decayed), all memories retrieved immediately before retirement get a small
   confidence penalty.

**Proposed feedback mechanism — Passive Reinforcement:**

```python
def reinforce_retrieved_memory(conn, memory_id: int, outcome: str):
    """
    Adjust confidence of a memory based on retrieval outcome.
    outcome: 'positive' | 'negative' | 'neutral'
    """
    delta = {"positive": +0.05, "negative": -0.10, "neutral": 0.0}[outcome]
    if delta == 0: return
    conn.execute("""
        UPDATE memories
        SET confidence = MAX(0.1, MIN(1.0, confidence + ?)),
            updated_at = datetime('now')
        WHERE id = ?
    """, (delta, memory_id))
    conn.commit()
```

**Caution:** Explicit outcome signals require agents to emit them. This is a
significant behavioral change. The passive `recalled_count` signal is more
realistic in the short term — it's already being collected.

**Practical Phase 1:** No explicit backprop. Let `recalled_count` accumulate
naturally. After 30 days, the `importance` signal will have real distribution
and the Gini-based adaptive weight above will automatically upweight it.

**Phase 2 (if outcome linking is implemented):** Add a `retrieval_outcomes`
table tracking (memory_id, task_id, outcome) and run a weekly weight calibration
using the correlation between memory confidence and task success rate.

### 4b. Cost of adaptive weight computation

Computing adaptive weights from store statistics requires:
- 3 SQL aggregates (O(N) scan)
- No ML inference needed
- Negligible: < 2ms at 1000 memories

Recommendation: **compute on each query, cache for 60 seconds**. The store
changes slowly (memories are written on heartbeats, not per-token).

---

## 5. Recommendations

### Immediate (implement now)

1. **No change to current weights** at 39 memories. The current vector is
   appropriate for a sparse store. Wait for natural growth.

2. **Add `compute_adaptive_weights()` to brainctl** as a `weights` subcommand.
   Running `brainctl weights` emits the current optimal weights + the statistics
   that drove them. This gives observability without changing behavior yet.

3. **Add `classify_query()` to salience_routing.py** and select from
   `QUERY_WEIGHTS` based on query type. Low-risk improvement that is independent
   of store size.

### At 100+ memories (approximately 2–4 weeks at current write rate)

4. **Switch to adaptive weight vector** using the analytical formula in §2d.
   The weights will shift automatically as the store matures.

5. **Soft-gate recency by temporal_class**: memories with `temporal_class='long'`
   use `k=0.01` (half-life ~70 days). `medium` keeps `k=0.1`. This prevents
   long-class identity/strategy memories from fading out of retrieval.

### At 500+ memories (longer term)

6. **Introduce passive feedback loop**: read `recalled_count` distribution weekly
   and calibrate `W_IMPORTANCE` via the Gini coefficient.

7. **Phase 2 feedback loop**: if outcome signals exist, train a simple linear
   regression on (similarity, recency, confidence, importance) → retrieval_success.
   Use the learned coefficients as the adaptive weights.

---

## 6. Open Questions

1. **Embedding-first writes (COS-205)**: the similarity score currently uses FTS5
   BM25 rank, not semantic cosine distance. Once sqlite-vec is installed and
   embeddings are populated, W_SIMILARITY will measure a fundamentally different
   thing (semantic vs keyword). This will likely push W_SIMILARITY higher (semantic
   similarity is a stronger signal than BM25 for conversational memory) and should
   be re-evaluated at that time.

2. **Cross-agent weight consistency**: different agents may benefit from different
   default profiles. Hermes (consolidation-focused) should weight recency lower
   than a tactical agent running daily sprints. A `per_agent_profile` override in
   `openclaw.json` is worth considering.

3. **Category-specific decay**: `lesson` and `decision` memories may warrant slower
   recency decay than `project` memories. A per-category temporal_class default would
   be a low-cost improvement.

---

## Appendix A: Current FTS5 Scoring Note

brainctl currently returns `fts_rank` (negative, more negative = less relevant) as
the similarity signal. The actual salience_routing.py uses this rank as a proxy for
semantic similarity. This is a BM25 approximation — adequate for the current store
but not equivalent to vector cosine similarity. When COS-205 (embedding-first writes)
is implemented, the similarity signal will improve substantially and the weight
sensitivity analysis should be re-run with real cosine scores.

---

*Filed at: ~/agentmemory/research/wave5/13_adaptive_retrieval_weights.md*
*Cross-reference: [COS-205](/COS/issues/COS-205) (Embedding-First Writes)*
