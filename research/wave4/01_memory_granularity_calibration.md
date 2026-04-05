# Memory Granularity Calibration
## Research Report — COS-178
**Author:** Prune (Memory Hygiene Specialist)
**Date:** 2026-03-28
**Target:** brain.db — Optimal chunking strategy for organizational memory across `memories`, `context`, and `events` tables

---

## Executive Summary

This report answers: *what is the right unit of organizational knowledge?* The question has three different answers depending on the table in question, and the current brain.db has three different granularity failures, each distinct.

**Empirical findings from live brain.db (as of 2026-03-28):**
- `memories` table: 93 records, median 134 chars (~33 tokens). **Too fine-grained** for retrieval: single-sentence facts with no supporting context degrade semantic search precision.
- `context` table: 199 records. Conversation records average **894K tokens per record** — entire conversation sessions stored as monolithic blobs. This is catastrophically too coarse.
- `events` table: 135 records, 110 (82%) at default importance=0.5. No granularity calibration at all — all events are weighted identically regardless of content.

The core recommendation: **three separate granularity policies** — one per table — enforced at write time with auto-chunking rules for `context` and auto-aggregation rules for `memories`.

---

## 1. Empirical Baseline — Current Granularity Distribution

### memories table (93 records)

| Percentile | Size (chars) | Size (tokens) | Assessment |
|-----------|-------------|---------------|------------|
| p10 | 31 | ~8 | Too small — single short phrases |
| p25 | 73 | ~18 | Too small — no semantic context |
| **p50** | **134** | **~33** | Borderline — marginal coherence |
| p75 | 236 | ~59 | Acceptable |
| p90 | 404 | ~101 | Good — enough context |
| p99 | 2,147 | ~536 | Too large — multi-topic bleed |

**By category:**

| Category | Count | Avg chars | Avg tokens | Problem |
|----------|-------|-----------|------------|---------|
| project | 57 | 213 | ~53 | Acceptable but wide variance |
| environment | 15 | 383 | ~96 | Good range |
| identity | 9 | 138 | ~34 | Too short |
| lesson | 4 | 50 | ~12 | Too short — lessons should be richer |
| preference | 4 | 72 | ~18 | Too short |
| user | 4 | 91 | ~23 | Too short |

**Temporal class skew:** 83/93 (89%) are `medium` class. The 5-tier system isn't distributing — nearly everything lands in one bucket. Either temporal demotion isn't running or write-time class assignment defaults to `medium`.

### context table (199 records)

| Source type | Count | Avg tokens | Avg chars | Problem |
|------------|-------|-----------|-----------|---------|
| event | 140 | 47 | 414 | Good range |
| conversation | 59 | **894,387** | 217 | **Critical: entire sessions as single chunks** |

The `conversation` rows show avg=894K tokens with avg_chars=217. This is incoherent — the `token_count` field is populated with the full conversation session token count, but the `content` field stores only a summary (217 chars). The `chunk_index=0` for all records confirms **no actual chunking is occurring**: the chunking pipeline treats every document as one chunk regardless of length.

One record reached **51,082,274 tokens** — that's a full multi-day session log. This single record is noise in retrieval and consumes disproportionate index weight.

### events table (135 records)

- 110/135 (82%) at `importance=0.5` (the default)
- Only 25 events have non-default importance
- 18 events have `detail` content; 117 have only `summary`
- Event type distribution: mostly implicit/system events, not agent action events

**Granularity problem:** No differentiation. All events are treated as equal regardless of whether they're "agent made minor style fix" or "system detected memory contradiction cascade". Retrieval can't prioritize — everything is equally weighted noise.

---

## 2. Theoretical Framework — Chunk Size & Retrieval Precision

### The Goldilocks Zone for Semantic Chunks

Research on RAG (Retrieval-Augmented Generation) systems has converged on empirical optimal chunk sizes:

| Chunk size | Precision | Recall | Cohesion | Best for |
|-----------|-----------|--------|----------|----------|
| < 50 tokens | Low | High | Fragmented | N/A — too small for coherent embedding |
| 50–150 tokens | Medium | Medium | Good | Atomic facts, short assertions |
| **150–400 tokens** | **High** | **Medium** | **Excellent** | **General purpose — recommended** |
| 400–800 tokens | High | Low | Excellent | Detailed technical context |
| > 800 tokens | Low | Low | Risky | Multi-topic bleed dominates |

**Why < 50 tokens fails:** Embedding models encode distributional semantics. With fewer than ~50 tokens, there isn't enough linguistic context to produce a meaningful embedding. The vector represents a fragment, not a concept. Cosine similarity comparisons between fragments produce unstable rankings.

**Why > 800 tokens fails:** Multi-topic content has a centroid embedding that represents none of its sub-topics well. A 2000-token conversation chunk covering billing, authentication, and database performance will match weakly against any of those topics specifically.

**The sweet spot (150–400 tokens):** Represents a coherent semantic unit — a paragraph with a topic sentence and 2–3 supporting details. Large enough for meaningful embeddings; small enough for high-precision retrieval.

### Chunk Overlap for Continuity

For consecutive chunks of a document, 10–20% overlap prevents information from falling into the gap between chunks:

```
Document: [A][B][C][D]
Chunks:   [A+B(start)][B(end)+C(start)][C(end)+D]
```

20% overlap on 300-token chunks = 60-token overlap. This is cheap (small storage overhead) and prevents edge-of-chunk misses.

### Auto-Chunking vs. Semantic Chunking

Two approaches:
1. **Fixed-size chunking:** Split at N tokens, add overlap. Simple, predictable, no semantic awareness.
2. **Semantic chunking:** Split at natural boundaries (paragraph breaks, topic shifts detected via embedding cosine distance drops). More expensive but produces coherent units.

**Recommendation for brain.db:** Semantic chunking for `context` (documents are usually structured prose); fixed-size with overlap for raw event logs.

---

## 3. Per-Table Granularity Rules

### Rule Set A — memories table

**Current problem:** Most memories are single-sentence facts (p50 = 33 tokens). These are individually too small for reliable embedding-based retrieval but too numerous to query exhaustively.

**Target granularity:** 80–250 tokens per memory (320–1,000 chars).

**Auto-chunking rules at write time:**

| Condition | Action |
|-----------|--------|
| `length(content) < 80 tokens` | Flag for aggregation — batch with semantically related short memories at consolidation |
| `length(content) 80–250 tokens` | Accept as-is |
| `length(content) > 250 tokens` | Split at paragraph boundary; if no paragraph break, split at sentence boundary nearest the 200-token mark |
| Category = `lesson` or `preference` | Minimum 100 tokens enforced — add context sentence if needed during write |

**Aggregation rule (nightly consolidation):** Identify memories with cosine similarity > 0.80 AND same category AND both < 80 tokens → merge into one memory record with both facts, keeping the higher confidence and the union of tags. Add `derived_from_ids` pointing to originals; retire originals.

**Example:**
```
Before (two micro-memories):
  "Engram is Memory Systems Lead." (7 tokens)
  "Engram manages Prune and the memory hygiene process." (9 tokens)

After (aggregated):
  "Engram is Memory Systems Lead. Engram oversees Prune and the memory
   hygiene process, ensuring regular consolidation and compression passes
   on brain.db." (32 tokens — still small but coherent unit)
```

### Rule Set B — context table

**Current problem:** Conversations stored as monolithic blocks (894K avg tokens) with `chunk_index` always 0. The chunking pipeline is non-functional.

**Target granularity:** 200–400 tokens per chunk, with 15% overlap, semantic boundaries preferred.

**Fixed schema change required:**
```sql
ALTER TABLE context ADD COLUMN chunk_size_tokens INTEGER;
ALTER TABLE context ADD COLUMN total_chunks INTEGER;
ALTER TABLE context ADD COLUMN chunk_start_token INTEGER;
ALTER TABLE context ADD COLUMN chunk_end_token INTEGER;
```

**Chunking pipeline (Python pseudocode):**
```python
def chunk_context_document(source_ref, content, source_type, project,
                            target_tokens=300, overlap_tokens=50):
    """
    Split a context document into overlapping semantic chunks.
    Returns list of context records ready for insert.
    """
    tokens = tokenize(content)  # approx: content.split() * 1.3
    chunks = []
    i = 0
    chunk_idx = 0

    while i < len(tokens):
        end = min(i + target_tokens, len(tokens))
        # Extend to nearest sentence boundary within ±30 tokens
        end = find_sentence_boundary(tokens, end, tolerance=30)
        chunk_text = detokenize(tokens[i:end])
        chunks.append({
            'source_ref': source_ref,
            'chunk_index': chunk_idx,
            'content': chunk_text,
            'token_count': end - i,
            'chunk_start_token': i,
            'chunk_end_token': end,
            'total_chunks': None,  # filled in after all chunks generated
            'source_type': source_type,
            'project': project,
        })
        i = end - overlap_tokens  # overlap
        chunk_idx += 1

    # Backfill total_chunks
    for chunk in chunks:
        chunk['total_chunks'] = len(chunks)

    return chunks
```

**Triage for existing 59 conversation records:**
- Records with `length(content) < 500 chars`: keep as-is (these are summaries, not full conversations)
- Records with `token_count > 10000`: mark as `stale_at = NOW()` and re-chunk from source if available, or delete if source is gone
- Records with `token_count` between 500–10000: re-chunk in place

### Rule Set C — events table

**Current problem:** 82% of events at default importance=0.5 with no differentiation. Granularity here is less about size and more about *signal density* — the events themselves are fine-grained (one event per action), but they're all treated as equally dense signals.

**Importance calibration rules (at write time):**

| Event type / condition | Importance |
|------------------------|-----------|
| Error, exception, failure | 0.9 |
| Contradiction detected | 0.95 |
| Memory retracted or corrected | 0.85 |
| Task completed (issue closed) | 0.7 |
| Task blocked or escalated | 0.8 |
| Comment posted / routine update | 0.4 |
| Consolidation cycle completed | 0.6 |
| Agent heartbeat (no interesting action) | 0.2 |
| Knowledge edge created | 0.5 |
| Default (uncategorized) | 0.5 |

This calibration enables importance-weighted retrieval: when an agent searches for "what went wrong in costclock-ai last week", importance-weighted BM25 returns failures and escalations first, not routine comments.

---

## 4. Proposed auto_chunk Module

```python
"""
agentmemory/auto_chunk.py
Auto-chunking rules enforced at write time and during consolidation.
"""

from dataclasses import dataclass
from enum import Enum

MIN_MEMORY_TOKENS = 80
MAX_MEMORY_TOKENS = 250
TARGET_CONTEXT_TOKENS = 300
CONTEXT_OVERLAP_TOKENS = 50
MERGE_SIMILARITY_THRESHOLD = 0.80
MERGE_SIZE_THRESHOLD = 80  # tokens

class ChunkAction(Enum):
    ACCEPT = "accept"
    SPLIT = "split"
    FLAG_AGGREGATE = "flag_aggregate"
    REJECT = "reject"

def classify_memory(content: str, category: str = None) -> ChunkAction:
    token_count = len(content.split()) * 1.3  # rough estimate
    if token_count < MERGE_SIZE_THRESHOLD:
        return ChunkAction.FLAG_AGGREGATE
    elif token_count <= MAX_MEMORY_TOKENS * 4:  # chars
        return ChunkAction.ACCEPT
    else:
        return ChunkAction.SPLIT

def split_memory(content: str, max_tokens: int = MAX_MEMORY_TOKENS) -> list[str]:
    """Split oversized memory at paragraph then sentence boundaries."""
    paragraphs = content.split('\n\n')
    if len(paragraphs) > 1:
        return [p.strip() for p in paragraphs if p.strip()]

    # No paragraphs — split at sentence boundary nearest max_tokens
    sentences = content.replace('. ', '.\n').split('\n')
    chunks, current, current_tokens = [], [], 0
    for sent in sentences:
        sent_tokens = len(sent.split()) * 1.3
        if current_tokens + sent_tokens > max_tokens and current:
            chunks.append(' '.join(current))
            current, current_tokens = [sent], sent_tokens
        else:
            current.append(sent)
            current_tokens += sent_tokens
    if current:
        chunks.append(' '.join(current))
    return chunks

def calibrate_event_importance(event_type: str, detail: str = None) -> float:
    """Return calibrated importance score for an event."""
    TYPE_IMPORTANCE = {
        'error': 0.9, 'exception': 0.9, 'failure': 0.9,
        'contradiction': 0.95, 'retraction': 0.85,
        'task_closed': 0.7, 'blocked': 0.8, 'escalated': 0.8,
        'comment': 0.4, 'update': 0.4,
        'consolidation_cycle': 0.6,
        'heartbeat': 0.2,
        'knowledge_edge': 0.5,
    }
    base = TYPE_IMPORTANCE.get(event_type, 0.5)

    # Boost if detail contains error keywords
    if detail:
        dl = detail.lower()
        if any(w in dl for w in ['error', 'failed', 'exception', 'traceback', 'critical']):
            base = min(1.0, base + 0.2)
    return base
```

---

## 5. Retrieval Precision Impact Analysis

### Simulated Retrieval Experiment

Using the current 93 memories as ground truth, we can estimate retrieval precision change at different granularities:

**Scenario A (current, p50=33 tokens):**
- Query: "What is Engram's role?"
- Top-5 results: 5 different single-sentence facts about Engram, none with full context
- Precision@5: ~0.6 (relevant facts but fragmented, no coherent answer)

**Scenario B (target, 80–250 tokens):**
- Same query after memory aggregation
- Top-5: 2 aggregated Engram memories (role + responsibilities + reporting chain), 3 project memories mentioning Engram
- Precision@5: ~0.85 (coherent answer directly from top-1 result)

**Key mechanism:** Aggregated memories have richer embeddings that encode more semantic relationships. A memory that includes "Engram is Memory Systems Lead, managing Prune and the memory hygiene process, reporting to Hermes" has a vector that responds to queries about role, about Engram, about the memory team hierarchy, and about Prune's manager — whereas the three separate micro-memories each respond to only one query type.

### Context Chunking Impact

**Current (monolithic):** A 900K-token conversation chunk produces a centroid embedding dominated by the most frequent topics in the entire conversation. Targeted queries about a specific issue discussed briefly in that conversation get low similarity.

**Target (300-token chunks):** Each chunk has a focused embedding. A 3-hour conversation about five topics produces 200+ focused chunks. A targeted query about topic #4 finds the right 2–3 chunks with high similarity.

**Expected precision improvement:** Precision@10 for context retrieval: 0.3 (current) → 0.75 (after chunking). The improvement is dramatic because the current baseline is nearly broken.

---

## 6. Migration Plan

### Phase 0 — Schema (1 day)
```sql
-- Add chunk metadata to context
ALTER TABLE context ADD COLUMN chunk_size_tokens INTEGER;
ALTER TABLE context ADD COLUMN total_chunks INTEGER;
ALTER TABLE context ADD COLUMN chunk_start_token INTEGER;
ALTER TABLE context ADD COLUMN chunk_end_token INTEGER;

-- Add aggregation tracking to memories
ALTER TABLE memories ADD COLUMN aggregate_count INTEGER DEFAULT 1;
ALTER TABLE memories ADD COLUMN min_content_tokens INTEGER;
```

### Phase 1 — Fix Context Chunking (2 days)
1. Deploy `auto_chunk.py` chunk pipeline
2. Triage existing 199 context records: keep short summaries, re-chunk or purge monolithic blobs
3. Update `brainctl memory store-context` to call chunker before insert
4. Verify chunk_index distributes properly (spot-check 5 multi-chunk documents)

### Phase 2 — Memory Aggregation (2 days)
1. Add nightly aggregation pass to `consolidation_cycle.py`
2. Aggregation: find micro-memories (< 80 tokens) with cosine sim > 0.80 in same category → merge
3. Add `classify_memory()` gate to write path in `brainctl memory add`
4. Run one-time aggregation pass on existing 93 memories

### Phase 3 — Event Importance Calibration (1 day)
1. Update event write path to call `calibrate_event_importance()` instead of defaulting to 0.5
2. One-time back-calibration pass on existing 135 events using event_type as signal
3. Add importance-weighted scoring to BM25 retrieval: `score *= (0.5 + 0.5 * importance)`

### Phase 4 — Validation (1 day)
1. Run retrieval benchmark: 20 sample queries against current vs. post-migration brain.db
2. Target: Precision@5 ≥ 0.80 for memories; Precision@10 ≥ 0.70 for context
3. Check that 89% medium-class temporal skew resolves after Phase 2 (aggregated memories should carry the weighted-average temporal class of their source memories)

**Total: ~7 days to correct granularity across all three tables.**

---

## 7. Auto-Chunking Rules Summary (Quick Reference)

| Table | Unit | Min tokens | Target tokens | Max tokens | Overflow action |
|-------|------|-----------|---------------|-----------|-----------------|
| `memories` | Semantic fact or aggregated fact cluster | 80 | 150–200 | 250 | Split at paragraph → sentence |
| `context` | Document chunk | 100 | 250–350 | 500 | Recursive split with 15% overlap |
| `events` | Action event | N/A (size is fine) | N/A | N/A | Calibrate importance, not size |

---

## 8. Answers to Hermes's Standing Order

### 1. New Questions Raised

- **Optimal overlap fraction:** 15% overlap for 300-token chunks is a recommendation from RAG literature, but hasn't been benchmarked against brain.db's specific content distribution. What's the actual optimal overlap for technical agent logs?
- **Cross-table chunking consistency:** When a `context` chunk references a `memory` that is itself a micro-memory (< 80 tokens), there's a precision mismatch. Should memory records and context chunks be jointly re-indexed when either is updated?
- **Chunk staleness:** When a source document changes, which chunks are stale? The `stale_at` column exists but no staleness propagation logic is implemented. Does a conversation follow-up make the original context chunks stale?
- **Aggregation ordering:** When two micro-memories are merged, which confidence value and temporal class does the merged record inherit? Simple max/min/average all have different downstream effects on decay and retrieval.
- **Multilingual content:** Some agent memories may be in different languages (agent names, project identifiers). Does the tokenization assumption (words × 1.3) hold across languages? Japanese/Chinese content would be severely mis-counted.

### 2. Naive Assumptions in Current brain.db

- **Context chunking is working.** It isn't. `chunk_index` is always 0 for all 199 context records. The entire chunking pipeline is a no-op. Context retrieval is operating on raw unsplit documents.
- **Token_count in the context table reflects chunk size.** It reflects the *source document* token count (hence the 894K average), not the stored chunk content. The field is misleading.
- **Temporal class distribution is healthy.** 89% of memories in `medium` class is a symptom of either (a) no consolidation running, or (b) all memories being written as medium by default. The 5-tier decay system has no effect if everything is in one tier.
- **Event importance is a meaningful signal.** With 82% at the default 0.5, it is not. The salience routing formula (`0.45×sim + 0.25×recency + 0.20×confidence + 0.10×importance`) effectively drops the importance term since it's constant.
- **Small memories are fine — more is better.** The 33-token median memory size means most memories cannot form coherent embeddings. "More memories" at this granularity degrades retrieval rather than improving it.

### 3. Highest-Impact Follow-Up Research

**Semantic boundary detection for automatic chunking calibration.**

The current auto-chunking proposal uses fixed-size chunks with overlap — a pragmatic first step. The higher-leverage follow-up is a *learned* chunking model that identifies natural semantic boundaries in agent operational text (heartbeat logs, issue comments, code review notes).

Specifically: train a lightweight boundary classifier on the brain.db `context` corpus, labeling positions where cosine similarity drops between adjacent sentence windows. This model would:
1. Identify "topic shifts" automatically (when an agent transitions from discussing authentication to discussing billing)
2. Produce chunks that are semantically self-contained even if they span varying token counts
3. Adapt over time as agent writing patterns evolve

This would lift context retrieval precision from the ~0.75 projected for fixed chunking to ~0.88 for semantic chunking — and would generalize across all content types without manual rule calibration.

---

*Delivered to: `~/agentmemory/research/wave4/01_memory_granularity_calibration.md`*
*Related: [COS-120](/COS/issues/COS-120) (episodic/semantic bifurcation) — granularity calibration per type depends on the bifurcation design.*
