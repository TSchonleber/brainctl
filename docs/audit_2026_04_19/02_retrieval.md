# Retrieval + Search Stack — Audit 2026-04-19 (v2.4.6)

## Executive summary

Nine findings across the retrieval stack. Two are HIGH-severity logic bugs that
silently produce wrong results: the FTS-confidence relative-anchor gate is dead
code (always evaluates to False), and the FTS anchor flag mutates a shared
`hybrid` variable, incorrectly disabling vec fusion for the events and context
buckets after it fires on memories. A third HIGH bug is a table-name typo in
`graph_traversal` intent expansion (`"event"` instead of `"events"`). The CE
cold-start warmup cost (~40 s) is not guarded before the latency-budget window
opens, poisoning the rolling p95 for the first 8 calls. Several MEDIUM findings
cover FTS OR-expansion not applied consistently across all retrieval entry points
(`_reason_l1_search`, `cmd_push`, `Brain.orient`). One LOW finding covers a dead
`@lru_cache` stub in `rerank.py`. No critical security issues found. The FTS5
migration-050 fix (from 048 trigger split) appears complete for the
`memories_fts` trigger; no evidence of residual UPDATE paths that bypass the
retire guard.

---

## Methodology

Read `src/agentmemory/_impl.py` (18 652 lines) in full for the core retrieval
path (`cmd_search`, `_rrf_fuse`, `_reranker_signal_check`, FTS-confidence gate,
CE latency guard, intent dispatch, graph expansion), `rerank.py`, `embeddings.py`,
`code_ingest.py`, `brain.py`, and the migration-048 SQL. Cross-checked against
`docs/BENCHMARK_REPORT_2026_04_19.md` and `docs/RERANKER.md`. Grepped for all
`UPDATE memories` sites to verify FTS5 trigger coverage. No runtime was
available; all findings are static.

---

## Findings

---

### [HIGH] F-01: FTS-confidence relative-anchor gate is dead code — `rel_strong` never fires

**File:** `src/agentmemory/_impl.py:6815–6826`

**Claim:** When `len(fts_list) >= 3`, the gate should also trigger when the top
BM25 result is at least 1.5× stronger than the third result (a "relative
strong anchor"), disabling vec fusion. The comment on line 6820 correctly
states the intended check.

**Evidence:**

```python
if len(fts_list) >= 3:
    try:
        third_score = float(fts_list[2].get("fts_rank") or 0.0)
        if top_score < -0.2 and third_score > top_score * 0.67:   # line 6818
            # comment says: top is >= 1.5x BM25 strength of third
            rel_strong = top_score * 0.67 > third_score            # line 6823
    except (TypeError, ValueError):
        pass
```

Line 6818 enters the block only when `third_score > top_score * 0.67` (both
negative, so this means `|third| < |top| * 0.67`, i.e., top is already more than
1.5× stronger). Line 6823 then assigns `rel_strong = top_score * 0.67 >
third_score`, which is the exact opposite of line 6818's condition — inside this
block it is always `False`. `rel_strong` is never True.

**Impact:** The relative-anchor gate is entirely inoperative. Queries with a
dominant lexical anchor whose intent isn't `factual_lookup`/`general` (e.g.
`event_lookup` proper-noun queries) go through vec fusion unnecessarily,
diluting exact-match results. This is a direct contributor to the LongMemEval
Hit@1 regression (-1.58%) noted in the benchmark report.

**Recommended fix:** Remove the redundant inner assignment — the guard at
6818 already establishes the condition:

```python
if top_score < -0.2 and third_score > top_score * 0.67:
    rel_strong = True
```

---

### [HIGH] F-02: FTS anchor gate mutates shared `hybrid` variable, disabling vec for events and context

**File:** `src/agentmemory/_impl.py:6831, 6882, 6901`

**Claim:** When the FTS-confidence anchor gate fires for the memories bucket, it
sets the outer `hybrid = False` to skip vec fusion for memories. The events and
context buckets then check the same `hybrid` variable.

**Evidence:**

```python
# line 6831 — inside "if 'memories' in tables:" block
if _fts_strong_anchor and hybrid:
    hybrid = False              # ← mutates shared variable

# line 6882 — "if 'events' in tables:" block, later in same function scope
if hybrid:                      # ← reads the mutated value
    merged = _rrf_fuse(fts_list, vec_list)
else:
    merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_list]
```

`hybrid` is set once at line 6225 based on whether the vec extension is loaded
and the query embedded. There is no per-bucket reset between the memories, events,
and context processing blocks. After the FTS anchor fires for memories, events
and context lose vec candidates permanently for that query.

**Impact:** Silently degrades hybrid retrieval for events and context every time
a memory query has a strong lexical anchor. No `_debug_skips` key is emitted for
events/context, so there is no audit trail. Affects all intent labels other than
`factual_lookup`/`general` (which already bypass vec at an earlier gate).

**Recommended fix:** Capture `hybrid` before the memories block and restore per
bucket:

```python
_hybrid_base = hybrid  # save the initial value

if "memories" in tables:
    hybrid = _hybrid_base  # reset per bucket
    ...
    if _fts_strong_anchor and hybrid:
        hybrid = False

if "events" in tables:
    hybrid = _hybrid_base  # restore for this bucket
    ...

if "context" in tables:
    hybrid = _hybrid_base
    ...
```

---

### [HIGH] F-03: `graph_traversal` intent passes wrong `table_hint` for events to `_graph_expand`

**File:** `src/agentmemory/_impl.py:7007`

**Claim:** The graph-traversal intent expansion loop calls `_graph_expand` with
`table_hint = tbl_key.rstrip("s")`, intending to convert "memories" → "memories"
(special-cased), "events" → "event", "context" → "context".

**Evidence:**

```python
extra = _graph_expand(
    db, top_items,
    tbl_key.rstrip("s") if tbl_key != "memories" else "memories",
    already
)
```

`"events".rstrip("s")` = `"event"`.  Inside `_graph_expand` (line 6026–6034):

```sql
SELECT target_table as nb_table, target_id as nb_id, ...
FROM knowledge_edges
WHERE source_table=? AND source_id=?    -- called with 'event'
```

`knowledge_edges.source_table` stores the literal string `"events"` (plural).
The query matches zero rows silently. The `"context"` case is accidentally
correct — `"context".rstrip("s")` = `"context"`.

**Impact:** For `graph_traversal` intent queries, graph expansion of event
results produces zero neighbors. Any event-linked context (e.g., a decision
that references an event) is not retrieved.

**Recommended fix:** Drop the `rstrip` transform entirely — the table_hint
should match the stored `source_table` string:

```python
elif _intent == "graph_traversal" and not no_graph:
    _TABLE_HINT_MAP = {"memories": "memories", "events": "events", "context": "context"}
    for tbl_key in ("memories", "events", "context"):
        top_items = results.get(tbl_key, [])[:3]
        if top_items:
            already = {r["id"] for r in results.get(tbl_key, [])}
            extra = _graph_expand(db, top_items, _TABLE_HINT_MAP[tbl_key], already)
            results.get(tbl_key, []).extend(extra)
```

---

### [MEDIUM] F-04: CE warmup cost (~40 s) poisons rolling p95 on first call

**File:** `src/agentmemory/_impl.py:6686–6731`; `src/agentmemory/rerank.py:205–259`

**Claim:** The latency-guarded CE path records the wall-clock time of every
`rerank_timed` call into `_CE_LATENCY_SAMPLES_MS`. The first call pays the
model load cost (~40 s cold, per `docs/RERANKER.md`). That 40 000 ms sample
is added at line 6709, then `post_p95` is computed and the budget check fires,
discarding the CE result. The sample stays in the deque.

**Evidence:**

```python
# line 6689 — pre-call guard skips only when >= _CE_P95_MIN_SAMPLES (8) samples exist.
# On first call: len == 0, so guard passes.
...
reranked_head, ce_secs = _ce_rerank_timed(...)  # ~40 000 ms on cold start
ce_ms = max(0.0, ce_secs * 1000.0)
_CE_LATENCY_SAMPLES_MS.append(ce_ms)            # 40 000 recorded
# Budget check: ce_ms (40 000) > ce_budget_ms (350) → discard
```

With `_CE_P95_MIN_SAMPLES=8`, the p95 guard activates after 8 samples. Calls 2–8
continue to execute the model (warm now, ~50–600 ms), but the p95 includes the
40 000 ms outlier. After 8 samples p95 ≈ 40 000 ms → all subsequent calls are
pre-skipped until the deque window rotates past the cold-start sample (64 calls
by default).

**Impact:** Users who launch a fresh process and call `--rerank` see ~64
consecutive skip events, then suddenly CE activates. Difficult to diagnose
without `--debug`. No warmup documentation in `RERANKER.md` mentions this
interaction with the latency budget.

**Recommended fix (two options):**

1. Exclude the cold-start sample from the rolling window: if `ce_ms > 10 *
   ce_budget_ms`, treat it as a warmup outlier and do not record it.
2. Add a `rerank.warmup_model(model)` call before the first budget-tracked
   rerank (requires exposing it from `_impl.py`). `embeddings.py` already has
   a `warmup_model()` pattern.

---

### [MEDIUM] F-05: `decision_lookup` early routing guard checks wrong container

**File:** `src/agentmemory/_impl.py:6207–6208`

**Claim:** The guard `"decisions" not in results` is meant to add `"decisions"`
to the `tables` list. But `results` is initialized as
`{"memories": [], "events": [], "context": [], "decisions": []}` — it always
contains the key `"decisions"`. The guard is always False.

**Evidence:**

```python
results = {"memories": [], "events": [], "context": [], "decisions": []}
# ...
if _intent_result and _intent_result.intent == "decision_lookup" and "decisions" not in results:
    tables = list(set(tables) | {"memories", "events", "context"})
```

The condition `"decisions" not in results` is never True because `results` is a
dict and the key `"decisions"` is always present.  Additionally, the body adds
`memories/events/context` (which the intent classifier likely already included)
rather than `"decisions"` itself, so even if the guard fired it would not add the
decisions table.

**Impact:** This guard is a no-op. The decisions table is eventually searched
via the alias-normalization block at line 6984 (after `_INTENT_ALIAS` maps
`decision_rationale` → `decision_lookup`), but only via LIKE-based search on
raw `query`, not the OR-expanded FTS query. The `"decisions"` table is never
added to the initial `tables` list for `decision_lookup` intent via the builtin
classifier, so it depends entirely on the external classifier and the alias map.

**Recommended fix:**

```python
# Fix 1: check `tables` not `results`
if _intent_result and _intent_result.intent == "decision_lookup" and "decisions" not in tables:
    tables = list(set(tables) | {"decisions"})
```

---

### [MEDIUM] F-06: `_reason_l1_search` and `cmd_push` use AND-semantics FTS (no OR expansion)

**File:** `src/agentmemory/_impl.py:15411` and `13612`

**Claim:** These entry points call `_sanitize_fts_query(query)` but not
`_build_fts_match_expression()`, so multi-word natural-language queries get FTS5
implicit-AND semantics. `cmd_search` fixed this at line 6218 (I1 rollout) but
the fix was not applied to the reasoning and push paths.

**Evidence:**

```python
# _reason_l1_search (line 15411):
fts_query = _sanitize_fts_query(query)   # "what does Alice prefer" → AND

# cmd_push (line 13612):
_raw_fts = _sanitize_fts_query(task_desc)
fts_query = re.sub(r'[,:+<>]', ' ', _raw_fts).strip()  # still AND
```

`cmd_search` at line 6218:
```python
fts_query = _build_fts_match_expression(_sanitize_fts_query(query))  # → OR
```

**Impact:** Neuro-symbolic reasoning (L1 search) and prospective push matching
miss memories that contain any individual token but not all tokens. Multi-word
queries like "how does authentication work" return zero or few results via L1
even when relevant memories exist.

**Recommended fix:** Wrap both with `_build_fts_match_expression`:

```python
# _reason_l1_search:
fts_query = _build_fts_match_expression(_sanitize_fts_query(query))

# cmd_push (after existing extra-sanitize step):
fts_query = _build_fts_match_expression(re.sub(r'[,:+<>]', ' ', _raw_fts).strip())
```

---

### [MEDIUM] F-07: `Brain.orient()` memory search uses `_safe_fts` (legacy), not `_build_fts_match_expression`

**File:** `src/agentmemory/brain.py:635`

**Claim:** `Brain.orient()` performs its own FTS5 query when a search hint is
provided. It uses the older `_safe_fts()` from `brain.py`, not the
`_build_fts_match_expression` pipeline used by `cmd_search`.

**Evidence:**

```python
# brain.py _safe_fts (line 121):
def _safe_fts(query: str) -> str:
    safe = re.sub(r'[^\w\s]', ' ', query).strip()
    return " OR ".join(safe.split()) if safe else ""

# brain.py orient() line 635:
fts_q = _safe_fts(search_q)
```

`_safe_fts` joins all tokens with OR (correct direction), but it does not filter
stopwords. A query like `"what does Alice prefer"` produces
`"what OR does OR Alice OR prefer"` — the stopwords `what`, `does` match
thousands of rows and dilute signal. `_build_fts_match_expression` filters
these.

**Impact:** `orient()` returns lower-precision memory suggestions when called
with stopword-heavy natural-language queries. This affects every session-start
call where `query=` is passed.

**Recommended fix:** Import and use `_build_fts_match_expression` from `_impl`:

```python
from agentmemory._impl import _build_fts_match_expression, _sanitize_fts_query
...
fts_q = _build_fts_match_expression(_sanitize_fts_query(search_q))
```

---

### [LOW] F-08: `code_ingest.py` module-dedup uses O(n²) set comprehension per import

**File:** `src/agentmemory/code_ingest.py:408, 476, 587`

**Claim:** Every time an import edge is emitted, the code checks
`if tgt not in {nd.name for nd in ex.nodes}`, rebuilding a set from the full
node list.

**Evidence:**

```python
# Line 408 (Python extractor):
if tgt not in {nd.name for nd in ex.nodes}:
    ex.nodes.append(...)
```

Same pattern at lines 476 (TypeScript) and 587 (Go). For a file with `k`
imports and a total of `n` nodes, this is `O(k * n)` per file.

**Impact:** Low in practice — typical source files have tens of imports and
hundreds of nodes at most, so the overhead is milliseconds. For generated files
approaching `MAX_FILE_BYTES` (1 MB) with dense imports this could become noticeable.

**Recommended fix:** Build a `seen_names: set[str]` once per extractor call and
maintain it incrementally:

```python
seen_names: set[str] = {nd.name for nd in ex.nodes}  # initial set before walk
# Inside _emit_python_import:
if tgt not in seen_names:
    ex.nodes.append(...)
    seen_names.add(tgt)
```

---

### [LOW] F-09: `rerank.py` contains a dead `@lru_cache` stub (`_cached_score`)

**File:** `src/agentmemory/rerank.py:274–283`

**Claim:** `_cached_score` is decorated with `@lru_cache(maxsize=1000)` and
always returns `None`. The comment says "This function exists purely so
functools.lru_cache gives us a stable per-process cache" but `score_pairs`
never calls `_cached_score` — it calls `_cache_get` which reads from a separate
`_score_cache` dict.

**Evidence:**

```python
@lru_cache(maxsize=1000)
def _cached_score(...) -> Optional[float]:
    return None   # always None — never populated

# score_pairs uses:
scores: List[Optional[float]] = [_cache_get(k) for k in keys]  # reads _score_cache dict
```

`_cached_score` is imported nowhere and called from nowhere except implicitly
through the decorator machinery (which is also unused since no one calls it).

**Impact:** Zero functional impact. Maintenance confusion — the function and its
comment suggest the lru_cache IS the backing store, but it isn't.

**Recommended fix:** Remove `_cached_score` and the `@lru_cache` import if no
other usage exists, or delete just the decorator and body if the name is used
elsewhere.

---

## Changes Made

None — this is a read-only audit. All findings are recommendations only.

---

## Appendix: FTS5 trigger coverage for UPDATE paths (migration 050 scope)

Migration 048 established the split-trigger pattern:
- `memories_fts_update_delete`: fires `AFTER UPDATE ... WHEN old.indexed = 1`
- `memories_fts_update_insert`: fires `AFTER UPDATE ... WHEN new.indexed = 1 AND new.retired_at IS NULL`

Grepped all `UPDATE memories` sites in `_impl.py`. The UPDATE paths that change
`content`, `category`, or `tags` (the FTS5-indexed columns) are:

- Line 3539 (`cmd_memory_update`): updates `content` — triggers fire correctly
  because `indexed=1` on active memories.
- Line 3685 (`cmd_memory_retract`): sets `retracted_at` but not `retired_at` —
  the FTS row is NOT removed by the retire trigger. However the MATCH query
  in `cmd_search` already uses `WHERE m.retired_at IS NULL`, so retracted-only
  memories are still returned by FTS. **UNCERTAIN** whether `retracted_at` is
  intended to behave like `retired_at` for search exclusion — needs confirmation.
  Experiment: `SELECT * FROM memories WHERE retracted_at IS NOT NULL AND retired_at IS NULL` 
  on a live brain.db to see if retracted memories appear in FTS results.
- Lines updating only non-FTS columns (trust_score, confidence, alpha, beta,
  recalled_count, etc.): trigger fires delete+insert but content is unchanged
  — minor overhead but functionally correct.

No evidence of a remaining UPDATE path that would corrupt FTS5 in the way
migration 050 fixed (the old single-trigger re-inserting retired rows).
