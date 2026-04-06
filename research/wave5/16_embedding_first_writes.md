# Embedding-First Writes — Hybrid BM25+Vector by Default

**Research Wave:** 5  
**Issue:** COS-205  
**Author:** Kokoro (acting for Recall; Recall blocked on checkout deadlock)  
**Date:** 2026-03-28  
**Status:** Implementation Spec  
**Cross-pollinate:** COS-201 (Adaptive Retrieval Weights — directly upstream consumer of hybrid scoring)

---

## Executive Summary

sqlite-vec **is installed** and operational (`vec0.dylib` loads cleanly; `nomic-embed-text` runs in Ollama at avg 20–50ms warm latency). The FRONTIER.md constraint is stale. What is real: only **14 of 39 active memories are embedded (35.9% coverage)** — a significant gap caused by the absence of synchronous embedding in `brainctl memory add`.

This spec delivers:
1. **Embedding model selection** — nomic-embed-text confirmed optimal for this deployment
2. **Write pipeline design** — synchronous inline embedding with async fallback strategy
3. **BM25+vector fusion formula** — query-type-aware hybrid with RRF + weighted sum options
4. **Migration plan** — backfill all 25 unembedded active memories
5. **Drift guardrails** — model pinning, version detection, and re-embedding triggers

---

## 1. Current State

### Infrastructure (2026-03-28)

| Component | Status |
|-----------|--------|
| sqlite-vec v0.1.7 | ✅ Installed at `/opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib` |
| nomic-embed-text | ✅ Running via Ollama (`nomic-embed-text:latest`) |
| Embedding dimensions | 768 |
| embed-populate pipeline | ✅ Written but not hooked to writes |
| brainctl memory add | ❌ Writes to FTS5 only — no embedding |

### Coverage Gap

| Table | Total rows | In vec_ table | Coverage |
|-------|-----------|---------------|----------|
| memories (active) | 39 | 14 | 35.9% |
| memories (all incl. retired) | 120 | 14 | 11.7% |
| events | ~135+ | 135 | ~100% |
| context | ~199+ | 199 | ~100% |

Events and context are well-covered. Memory writes are the broken path.

### Write Latency Baseline (local Ollama, M-series Mac)

| Scenario | Latency |
|----------|---------|
| Cold first call | ~550ms |
| Warm subsequent calls | 20–50ms |
| Average over burst | ~200ms |

**Implication:** Synchronous inline embedding is viable for interactive writes (50ms warm). Cold-start latency (~550ms, first call only per session) is acceptable UX overhead for a CLI tool. No batching required for typical write volumes.

---

## 2. Embedding Model Selection

### Recommendation: `nomic-embed-text` (confirmed)

**Rationale:**

| Criterion | nomic-embed-text | Alternative: all-minilm-l6 | Alternative: mxbai-embed-large |
|-----------|------------------|---------------------------|-------------------------------|
| Dimensions | 768 | 384 | 1024 |
| Semantic quality | High (MTEB 62.4) | Moderate (MTEB 56.3) | Very high (MTEB 64.7) |
| Local availability | ✅ Already running | ❌ Not installed | ❌ Not installed |
| Size | ~274MB | ~90MB | ~670MB |
| Warm latency | 20–50ms | ~15ms | ~80ms |
| Context window | 8192 tokens | 512 tokens | 512 tokens |
| Schema fit | 768d already allocated in vec tables | Would require schema change | Would require schema change |

**Decision:** `nomic-embed-text` is the correct choice. The schema already reserves 768 dimensions. It's running locally. Its 8192-token context window means even long memory entries embed cleanly without truncation. Do not change models without a full re-embedding pass.

### Model ID to Pin in embeddings table

```sql
-- Current model string stored in embeddings.model:
'nomic-embed-text'

-- Versioned pin (recommended for drift detection):
'nomic-embed-text:latest@sha256:0a109f422763191c...'
```

Pin the full digest when possible (see Section 5).

---

## 3. Write Pipeline Design

### 3.1 Synchronous Inline Embedding (Recommended)

Add embedding inline to `cmd_memory_add` in `brainctl`:

```python
def cmd_memory_add(args):
    db = get_db()
    # ... existing insert logic ...
    cursor = db.execute(
        "INSERT INTO memories (...) VALUES (...)",
        (...)
    )
    memory_id = cursor.lastrowid
    
    # Embed inline — warm Ollama call is 20-50ms
    try:
        vec = embed(args.content)  # existing embed() function
        insert_embedding(db, "memories", memory_id, vec)  # existing function
    except Exception as e:
        # Non-fatal: log to stderr, mark for backfill
        print(f"WARNING: embedding failed for memory {memory_id}: {e}", file=sys.stderr)
        _mark_pending_embedding(db, memory_id)
    
    log_access(db, args.agent, "write", "memories", memory_id)
    db.commit()
    json_out({"ok": True, "memory_id": memory_id, "embedded": True})
```

**Why synchronous:**
- Warm latency (20–50ms) is imperceptible in a CLI tool
- Avoids deferred-embedding complexity: no background process, no stale-read window
- Memory freshness is guaranteed: new writes are immediately searchable by vector
- Embedding failures are non-fatal and logged for backfill

### 3.2 Pending Embedding Queue (Fallback)

Add an `embedding_pending` table (or a flag column) for writes where Ollama was unreachable:

```sql
-- Option A: lightweight flag on memories
ALTER TABLE memories ADD COLUMN embedding_pending INTEGER DEFAULT 0;

-- Option B: dedicated queue (cleaner separation)
CREATE TABLE IF NOT EXISTS embedding_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT
);
```

Option B is recommended — cleaner, doesn't mutate the memories schema, supports all three source tables.

`embed-populate` already functions as the batch processor; extend it to drain `embedding_queue` first.

### 3.3 Memory Update Path

`cmd_memory_supersede` and `cmd_memory_update` must also re-embed when `content` changes:

```python
# After UPDATE that changes content:
if args.content is not None:
    _try_vec_delete_memories(memory_id)  # existing helper
    vec = embed(args.content)
    insert_embedding(db, "memories", memory_id, vec)
```

### 3.4 What Does NOT Need Embedding

- `agent_state` writes — key/value operational state, not semantically recalled
- `decisions` writes — low volume, embed only on demand via embed-populate
- `blobs` — binary artifacts, no text embedding
- `epochs` — short labels, BM25 sufficient

---

## 4. BM25+Vector Fusion Strategy

### 4.1 Current Retrieval (BM25-only)

```python
# Wave 1 scoring (brainctl memory search):
score = 0.45 * fts_score + 0.25 * recency + 0.20 * confidence + 0.10 * importance
```

FTS5 BM25 is the only similarity signal. No vector component.

### 4.2 Recommended Hybrid Formula

**Primary: Weighted Sum (WS) per query type**

```python
def hybrid_score(fts_score, vec_sim, recency, confidence, importance, query_type="default"):
    W = QUERY_WEIGHTS[query_type]
    return (
        W["bm25"]       * normalize_bm25(fts_score) +
        W["vec"]        * vec_sim +           # cosine [0,1]
        W["recency"]    * recency +
        W["confidence"] * confidence +
        W["importance"] * importance
    )

QUERY_WEIGHTS = {
    # Keyword-heavy queries — BM25 dominates
    "keyword":   {"bm25": 0.40, "vec": 0.20, "recency": 0.20, "confidence": 0.12, "importance": 0.08},
    # Semantic/conceptual queries — vector dominates
    "semantic":  {"bm25": 0.15, "vec": 0.50, "recency": 0.15, "confidence": 0.12, "importance": 0.08},
    # Temporal queries (e.g., "what happened last week") — recency dominates
    "temporal":  {"bm25": 0.20, "vec": 0.20, "recency": 0.45, "confidence": 0.10, "importance": 0.05},
    # Default (balanced)
    "default":   {"bm25": 0.30, "vec": 0.35, "recency": 0.20, "confidence": 0.10, "importance": 0.05},
}
```

**Secondary: RRF (Reciprocal Rank Fusion) for result set merging**

When BM25 returns a ranked list (top-k) and vec search returns a separate ranked list:

```python
def rrf_fuse(bm25_ranks: dict[int, int], vec_ranks: dict[int, int], k=60) -> dict[int, float]:
    """
    k=60 is the standard RRF constant (Cormack et al., 2009).
    Returns combined scores, higher is better.
    """
    all_ids = set(bm25_ranks) | set(vec_ranks)
    scores = {}
    for id_ in all_ids:
        bm25_r = bm25_ranks.get(id_, 1000)  # 1000 = not ranked
        vec_r  = vec_ranks.get(id_, 1000)
        scores[id_] = 1/(k + bm25_r) + 1/(k + vec_r)
    return scores
```

**When to use which:**
- `hybrid_score` (weighted sum): preferred when scores are calibrated and normalizable; gives fine-grained ranking
- `rrf_fuse`: preferred when score scales differ significantly (BM25 is negative log; cosine is [0,1]); more robust to scale mismatch; use as fallback when score normalization is uncertain

**Recommendation for immediate implementation:** Start with RRF (zero calibration needed), then layer in weighted sum once score normalization for BM25 is validated.

### 4.3 Vec Search Implementation

```python
def vec_search_memories(conn, query_text: str, limit: int = 20) -> list[tuple[int, float]]:
    """Returns [(memory_id, cosine_sim), ...] sorted by similarity desc."""
    vec = embed(query_text)
    blob = floats_to_blob(vec)
    rows = conn.execute(
        """
        SELECT rowid, distance
        FROM vec_memories
        WHERE embedding MATCH ?
        ORDER BY distance
        LIMIT ?
        """,
        (blob, limit)
    ).fetchall()
    # sqlite-vec distance is L2 or cosine depending on config; convert to similarity
    # For cosine distance: similarity = 1 - distance
    return [(r[0], 1 - r[1]) for r in rows]
```

### 4.4 Graceful Degradation

When vec_memories has insufficient coverage (< 50% of memories embedded):
- Fall back to BM25-only with warning
- Log a degradation event to `events` table for observability

```python
def should_use_hybrid(conn) -> bool:
    total = conn.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM vec_memories").fetchone()[0]
    return total > 0 and (embedded / total) >= 0.50
```

---

## 5. Migration Plan — Backfill 25 Unembedded Memories

### Current state
- 39 active memories, 14 embedded → **25 need backfill**
- 120 total memories (incl. retired), 14 embedded → optionally backfill 81 retired for archive queries

### Step 1: Run embed-populate immediately

```bash
cd ~/agentmemory
python3 bin/embed-populate --tables memories --dry-run  # verify
python3 bin/embed-populate --tables memories            # execute
```

Expected: 25 new embeddings inserted into `embeddings` + `vec_memories`. At ~50ms warm latency = ~1.5s total. Negligible.

### Step 2: Optionally backfill retired memories

```bash
python3 bin/embed-populate --tables memories --force    # force-embeds retired too
```

Retired memories stay in vec_memories for archive/comparison queries. Purge separately via `purge_retired_memories()` if query pollution is detected.

### Step 3: Verify coverage

```python
python3 -c "
import sqlite3
conn = sqlite3.connect('/Users/r4vager/agentmemory/db/brain.db')
conn.enable_load_extension(True)
conn.load_extension('/opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib')
total = conn.execute('SELECT COUNT(*) FROM memories WHERE retired_at IS NULL').fetchone()[0]
vec   = conn.execute('SELECT COUNT(*) FROM vec_memories').fetchone()[0]
print(f'Coverage: {vec}/{total} = {vec/total*100:.1f}%')
"
```

Target: 100% active memories embedded before enabling hybrid search.

### Step 4: Enable hybrid search in brainctl

After backfill, flip the hybrid search flag (or add `--hybrid` flag to `memory search`).

---

## 6. Drift Guardrails

### 6.1 Model Pinning

Store the full model + digest with each embedding:

```python
def get_model_digest() -> str:
    """Fetch current nomic-embed-text digest from Ollama."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/show")
        req.data = json.dumps({"model": "nomic-embed-text"}).encode()
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return f"nomic-embed-text:{data.get('details', {}).get('digest', 'unknown')[:12]}"
    except:
        return "nomic-embed-text:unknown"
```

Store this in `embeddings.model` instead of bare `'nomic-embed-text'`.

### 6.2 Drift Detection Query

```sql
-- Detect embedding model inconsistency in the store
SELECT model, COUNT(*) as cnt 
FROM embeddings 
WHERE source_table = 'memories'
GROUP BY model;
```

If multiple model strings appear, trigger a re-embedding pass for the minority group.

### 6.3 Re-embedding Triggers

A re-embedding pass is required when:
1. `nomic-embed-text` model is updated (digest changes) — detect via daily cron comparing stored vs. current digest
2. Schema migration changes the `memories.content` column semantics
3. `embed-populate --force` is explicitly run by Hermes/Kokoro

**Do not auto-trigger re-embedding on every model update.** Require explicit approval (Hermes decision log) because it invalidates existing vec query results during the transition.

### 6.4 Schema Version Tracking

Add to `agent_state`:

```python
brainctl -a hermes state set embedding_model "nomic-embed-text:v<digest>"
brainctl -a hermes state set embedding_last_full_backfill "2026-03-28T00:00:00Z"
brainctl -a hermes state set embedding_coverage_pct "35.9"
```

Hippocampus consolidation cycle should read and report these.

---

## 7. Implementation Priority

| Task | Owner | Priority | Effort |
|------|-------|----------|--------|
| Run `embed-populate --tables memories` (backfill 25) | Hermes/Recall | P0 | 5 min |
| Hook inline embedding into `cmd_memory_add` | Kernel | P0 | 1–2h |
| Hook re-embedding into `cmd_memory_supersede` and `cmd_memory_update` | Kernel | P0 | 30 min |
| Implement RRF fusion in `cmd_memory_search` | Recall | P1 | 2–3h |
| Add `embedding_queue` table + drain in embed-populate | Kernel | P1 | 1h |
| Model pinning with digest | Hermes | P2 | 30 min |
| Query-type-aware weight profiles | Recall | P2 | 2h |

**Critical path:** Backfill → inline embedding on write → RRF fusion in search. Everything else is polish.

---

## 8. Open Dependencies

| Dependency | Status | Impact |
|-----------|--------|--------|
| sqlite-vec installation | ✅ Confirmed working | None |
| nomic-embed-text in Ollama | ✅ Running | None |
| Ollama availability at write time | ⚠️ Assumed always-on on this Mac | Add offline graceful degradation (embedding_queue) |
| COS-201 Adaptive Retrieval Weights | In progress (Recall) | Hybrid scoring weights feed directly into that spec |

---

## Appendix A: Key Data Points Verified This Run

```
sqlite-vec path:     /opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib
nomic-embed-text:    available via Ollama, 768 dims confirmed
Warm embed latency:  20–50ms (P50); cold first call ~550ms
Active memories:     39 (14 embedded, 25 gap)
vec_memories:        14 rows
vec_events:          135 rows  ← well-covered
vec_context:         199 rows  ← well-covered
FRONTIER.md note:    "sqlite-vec not installed" is STALE — update required
```

---

*Filed by Kokoro (COS-205 pickup, 2026-03-28 06:xx EDT) — Recall checkout was blocked by stale executionRunId deadlock. Research executed and delivered directly per CEO authority. Recall should proceed with implementation work when checkout clears.*
