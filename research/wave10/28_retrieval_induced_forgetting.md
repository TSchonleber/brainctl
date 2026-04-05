# Wave 10 Research: Retrieval-Induced Forgetting in brain.db
**COS-343 | Recall | 2026-03-28**

---

## Executive Summary

Retrieval-Induced Forgetting (RIF) — Anderson, Bjork & Bjork (1994) — is not merely a risk in our system: it is actively occurring, measurably, right now. The brain.db recall distribution shows a Gini coefficient of **0.91**, placing it in monopoly territory. More dangerously, the adaptive salience system was designed to compensate but instead *amplifies* the monopoly: high Gini raises `w_importance`, which further rewards high-recall memories. This is a feedback loop. Combined with exploitation_bias in focused_work mode (54.5% temporal boost for top memories), zero-recall memories face a **64.8% salience deficit** versus the store's most-recalled entry — for identical query relevance.

**Bottom line**: 83.6% of the store (102/122 memories) has zero recall exposure. These memories exist but are functionally invisible. We have designed blind spots into the retrieval engine.

---

## 1. Theoretical Grounding

### 1.1 Anderson et al. (1994) — Core Mechanism
Practicing retrieval of a subset of items from a category impairs subsequent recall of unpracticed items from the *same* category. The effect is not passive competition — it is active inhibition. Retrieving some items suppresses the rest.

### 1.2 Part-Set Cuing Inhibition
Providing some set members as retrieval cues impairs recall of remaining members. In our context: when `brainctl search` returns top-K results, it provides those items as cognitive cues for the requesting agent. The lower-ranked items suffer suppression — not because they're irrelevant, but because the practiced items crowd them out.

### 1.3 The Matthew Effect
"For to everyone who has, more will be given" (Matt. 25:29). In memory systems, frequent retrieval leads to confidence boosts, which lead to higher salience, which leads to more retrieval. High-recall memories compound advantages; zero-recall memories compound invisibility.

---

## 2. Empirical Evidence in brain.db

### 2.1 Recall Distribution (Active Memories, 2026-03-28)
```
Total active memories: 122
Never recalled (count=0): 102 (83.6%)
Recalled once:            3
Recalled 2-10 times:      7
Recalled 11-50 times:     5
Recalled 50+ times:       5

Max recalls: 93 (memory #93: "Agent memory spine current state")
Average recalls: 5.1
Gini coefficient: 0.9137  ← monopoly territory (1.0 = perfect monopoly)
```

The top 5 memories (4.1% of store) account for ~73% of all recall events.

### 2.2 Confidence Compounding
Each recall applies: `confidence = MIN(1.0, confidence + 0.15 * (1 - confidence))`

This is exponential convergence toward 1.0:
- Memory #93 (93 recalls): confidence = **1.0000** (saturated)
- Memory #127 (90 recalls): confidence = **1.0000**
- Average confidence of zero-recall memories: ~0.30-0.50

A memory at 0.5 confidence vs 1.0 confidence contributes 0.10 less salience via the `w_confidence` term — purely because it was never surfaced, not because it is less valid.

### 2.3 Importance Proxy Gap
`importance_proxy = log(1 + recalled_count) / log(1 + max_recalls)`

| recalled_count | importance_proxy |
|---|---|
| 0 | 0.0000 |
| 1 | 0.1526 |
| 5 | 0.3944 |
| 20 | 0.6701 |
| 93 | 1.0000 |

A zero-recall memory contributes 0 to the importance term. A maximally-recalled memory contributes 1.0. With `w_importance = 0.10` (default), this alone creates a 0.10 point salience gap — but confidence compounding adds another ~0.10. Combined: a **0.20 point** floor disadvantage for never-recalled memories.

### 2.4 The Backwards Gini Effect (Critical Bug)

The adaptive weight formula in `salience_routing.py`:
```python
w_importance = 0.05 + 0.15 * g_recall   # g_recall = Gini coefficient
```

With our Gini = 0.91: `w_importance = 0.05 + 0.15 * 0.91 = **0.187**` (vs. default 0.10).

**This is backwards.** High inequality means the store has monopolistic recall patterns. The correct response is to *reduce* importance weighting to break the monopoly. Instead, the formula increases it, locking in and amplifying the existing inequality. This is a design inversion.

Full salience delta with adaptive weights (same query relevance, recency):
- Top memory (confidence=1.0, recalled=93): salience = **0.7935**
- Zero-recall memory (confidence=0.5, recalled=0): salience = **0.4815**
- Disadvantage: **64.8%** lower salience for equivalent content

### 2.5 Exploitation Bias in Focused Work Mode
`neuromodulation_state['focused_work']['exploitation_bias'] = 0.4`

Applied as: `tw = tw * (1.0 + 0.4 * log1p(recalled_count) * 0.3)`

For recalled_count=93: multiplier = `1 + 0.4 * log(94) * 0.3 = 1.546` → **54.5% temporal boost**
For recalled_count=0: multiplier = `1.0` (no boost)

In focused_work mode — when agents need precise, deep recall — they get the most biased results. This is the worst possible time for exploitation bias. Focused work demands coverage, not popularity.

### 2.6 Graph Nodes: Permanent Second-Class Status
Code comment in brainctl (line ~2298):
```python
# Update recalled_count for direct (non-graph) memory hits only (COS-238, COS-274, COS-334)
for r in results.get("memories", []):
    if r.get("source") != "graph":
        db.execute("UPDATE memories SET recalled_count = recalled_count + 1 ...")
```

Graph-expanded neighbors are never credited. They can appear in every search result but their `recalled_count` remains permanently 0. Over time, graph traversal becomes the only path to these memories — and that path requires the graph seed to first win the direct competition. These memories can never bootstrap into the direct-retrieval tier.

### 2.7 Current Health Metrics Showing RIF Effects
```
Engagement rate:  16.4%  (only 16.4% recalled in last 30 days)
Category HHI:     0.633  RED — topic collapse (knowledge monopoly)
Avg confidence:   0.56   RED — dragged down by 102 never-recalled entries
```

The "topic collapse" alert is a direct RIF symptom: a few topic categories dominate because their memories win every retrieval contest.

---

## 3. Part-Set Cuing: The Top-K Suppression Mechanism

When `brainctl search` returns top-K results (default K=10-15), every agent's working context is loaded with the same high-salience champions. The requesting agent's subsequent reasoning, memory pushes, and task outputs are conditioned on those top-K items. Other memories, even highly relevant ones, are functionally absent from the agent's context window.

This is not just a retrieval miss — it shapes downstream cognition. An agent that always sees the same "Agent memory spine current state" entry as result #1 will continuously reinforce the framing in that entry rather than discovering potentially contradicting or updating entries.

**Specific observations:**
- Memory #93 ("Agent memory spine current state") appears in virtually every general search. It acts as a "hub" that crowds out more specific, nuanced memories.
- 102 "Potential connection: [...]" hypothesis memories (all recalled_count=0) were created by the graph system but have never entered direct retrieval. They represent hypothesized knowledge connections that no agent has ever accessed — a complete dead layer.

---

## 4. Does RIF Apply to Knowledge Graphs?

The `knowledge_edges` table has 2,675 edges. Spreading activation via `brainctl vsearch` or graph expansion flows from high-salience seeds. This means:

1. Popular memories are seeds for graph expansion.
2. Graph expansion follows edges from those seeds.
3. Memories only reachable via low-salience seeds are never graph-expanded.
4. The graph *amplifies* the rich-get-richer effect by giving popular memories two paths to dominance: direct retrieval AND graph seeding.

Inhibitory edges (COS-117 discussed but not implemented) would partially address this — a popular memory that is "incorrect in the current context" could inhibit its neighbors. Without them, spreading activation is purely excitatory.

---

## 5. Mitigation Design

### 5.1 Fix the Backwards Gini Effect (Priority: P0)
**Change**: When Gini > 0.7, *reduce* `w_importance` rather than increase it.

```python
# Current (broken):
w_importance = 0.05 + 0.15 * g_recall

# Proposed:
if g_recall > 0.7:
    # High inequality: suppress popularity signal to break monopoly
    w_importance = 0.05 + 0.15 * (1.0 - g_recall)
else:
    # Low inequality: safe to reward importance
    w_importance = 0.05 + 0.15 * g_recall
```

This inverts the formula above Gini=0.7. At Gini=0.91, `w_importance` drops from 0.187 to 0.064 — shifting weight toward similarity and recency, which are query-relevant.

### 5.2 Interleaved Diversity via Maximal Marginal Relevance (Priority: P1)
Replace pure ranking with MMR in the `_apply_recency_and_trim` step:

```
MMR(m) = λ * salience(m) - (1-λ) * max_{s∈Selected} similarity(m, s)
```

Default λ = 0.7 (relevance-biased). This prevents the top-K from being dominated by semantically similar high-recall memories. If memory A and memory B are near-duplicates, the second one is penalized by the `max similarity` term — leaving room for a distinct, lower-salience memory C.

Requires vector embeddings already populated (vec_coverage=100%, confirmed in health report). Implementation is in the post-RRF trim pass.

### 5.3 Exploration Heartbeat / Curiosity Mode (Priority: P1)
Add a new neuromodulation state `curiosity` (or a periodic forced-exploration query):

```python
"curiosity": {
    "exploitation_bias": -0.3,  # negative = exploration bonus for low-recall
    "retrieval_breadth_multiplier": 2.0,
    "temporal_lambda": 0.005,   # almost no recency decay
}
```

The negative exploitation_bias would compute: `tw = tw * (1.0 + (-0.3) * log1p(recalled_count) * 0.3)` — applying a *penalty* to high-recall memories and a relative bonus to zero-recall ones. This is the "forced interleaving" mitigation from Anderson et al.

Scheduled trigger: run curiosity mode for 20% of Hippocampus consolidation cycles, or whenever Gini > 0.8.

### 5.4 Partial Recall Credit for Graph Nodes (Priority: P2)
Change the recalled_count update to credit graph neighbors at 50%:

```python
for r in results.get("memories", []):
    credit = 0.5 if r.get("source") == "graph" else 1.0
    if credit > 0:
        db.execute(
            "UPDATE memories SET recalled_count = recalled_count + ?, "
            "last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
            "confidence = MIN(1.0, confidence + ? * (1.0 - confidence)) "
            "WHERE id = ?",
            (credit, 0.15 * credit, r["id"])
        )
```

This lets graph-reachable memories accumulate influence over time, eventually bootstrapping into direct retrieval.

### 5.5 Recall Diversity Metric in `brainctl health` (Priority: P2)
Add a `recall_gini` SLO to the health dashboard:

- GREEN: Gini < 0.60
- YELLOW: Gini 0.60–0.80
- RED: Gini > 0.80 (monopoly / active RIF risk)

Currently at 0.91 — this would immediately fire a RED alert, prompting investigation. Pairs with the existing `category_hhi` metric.

### 5.6 `recalled_count` Decay (Priority: P3)
Apply a slow decay to `recalled_count` proportional to time since last recall. This prevents memories from accumulating "permanent prestige" from past relevance.

```sql
-- In hippocampus consolidation:
UPDATE memories
SET recalled_count = CAST(recalled_count * 0.95 AS INTEGER)
WHERE retired_at IS NULL
  AND last_recalled_at < datetime('now', '-7 days')
  AND recalled_count > 0;
```

This gives zero-recall memories a path to competitive parity over time, even if no active intervention is applied.

---

## 6. Risk Assessment

| Finding | Severity | Current State |
|---|---|---|
| Backwards Gini amplifies monopoly | HIGH | Active — amplification confirmed |
| 83.6% of memories never accessed | HIGH | Structural blind spots |
| Graph nodes permanently excluded from recall credit | MEDIUM | 102+ affected memories |
| Exploitation_bias worst in focused_work mode | MEDIUM | Inverted utility |
| Category HHI = 0.633, topic collapse | MEDIUM | Active alert already firing |
| No MMR diversity in retrieval | MEDIUM | Unimplemented |

---

## 7. Recommended Issue Filing

| Issue | Priority | Owner |
|---|---|---|
| Fix backwards Gini in compute_adaptive_weights | P0 | Recall |
| Add recall_gini SLO to brainctl health | P1 | Recall |
| Implement MMR diversity in _apply_recency_and_trim | P1 | Recall |
| Add curiosity exploration mode to neuromodulation | P1 | Recall/Hermes |
| Partial recall credit for graph-expanded nodes | P2 | Recall |
| recalled_count slow decay in hippocampus | P3 | Engram |

---

## 8. Conclusion

The brain.db memory spine has a confirmed, measurable case of computational RIF. The store is not merely unbalanced — the retrieval engine actively reinforces the imbalance through adaptive weight inversion and exploitation bias. The 102 zero-recall memories represent a large, silenced knowledge layer that agents cannot access in practice.

The Anderson et al. mechanism maps cleanly:
- **Practiced items** = top-20 high-recall memories
- **Unpracticed items** = 102 zero-recall memories
- **Inhibitory control analog** = exploitation_bias + backwards Gini
- **Part-set cuing** = top-K limit in every search

Fixes are concrete, implementable without schema changes (except the curiosity state), and high ROI. The backwards Gini fix alone would reduce the salience gap from 64.8% to approximately 20% — making the playing field far more competitive for less-recalled but potentially relevant memories.

---

*Filed by Recall (57854056) | COS-343 | Wave 10 Research*
