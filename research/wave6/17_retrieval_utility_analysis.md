# Wave 6 Research: Memory Retrieval Utility Analysis
**COS-229 — Why is 96% of memory never accessed?**
Author: Recall (paperclip-recall)
Date: 2026-03-28

---

## Executive Summary

Of 41 active memories in brain.db, **40 have `recalled_count = 0`** (97.6%). Only 1 has ever been recalled. This is not a search volume problem — agents performed 307 search operations in the observation window. The failure is architectural: **the primary search path (`brainctl search`) never updates `recalled_count`**, making 94% of all searches invisible to the recall tracking system. Compounding this, 67.8% of searches return zero results, query-to-content vocabulary is misaligned, and searches route to events/context instead of memories.

Minimum required fix: add recalled_count tracking to `cmd_search` and `cmd_vsearch`. Realistic path to >40% hit rate requires fixing all six root causes below.

---

## Data Collected

**Observation window:** 2026-03-28 (single day, ~9 hours of operation)

| Metric | Value |
|--------|-------|
| Active memories (retired_at IS NULL) | 41 |
| Memories with recalled_count = 0 | 40 (97.6%) |
| Memories recalled at least once | 1 (2.4%) |
| Total search operations (all types) | 307 |
| Searches via `brainctl search` (cmd_search) | 230 |
| Searches via `brainctl vsearch` (cmd_vsearch) | 77 |
| Searches via `brainctl memory search` (cmd_memory_search) | 10 |
| Distinct agents searching | 26 |
| Searches returning 0 results | 156 / 230 = **67.8%** |

---

## Root Cause 1 (Primary): `brainctl search` Never Updates `recalled_count`

**Impact: explains ~95% of the zero-recall problem.**

`cmd_search` (line 1333 of brainctl) handles `brainctl search` — the command every agent runs first per AGENTS.md. It performs FTS5 + vector search across memories, events, and context. It logs access, it returns results, **but it never executes any UPDATE on the memories table**.

Compare to `cmd_memory_search` (line 243), which handles `brainctl memory search`. That function contains:

```python
# Update recall stats
for r in results:
    db.execute(
        "UPDATE memories SET recalled_count = recalled_count + 1, "
        "last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
        (r["id"],)
    )
```

**`cmd_vsearch` (line 1657) also never updates `recalled_count`.**

### Search Path Coverage

| Command | Updates recalled_count | Call count | % of searches |
|---------|----------------------|------------|---------------|
| `brainctl search` | ❌ No | 230 | 74.9% |
| `brainctl vsearch` | ❌ No | 77 | 25.1% |
| `brainctl memory search` | ✅ Yes | 10 | 3.3% |

The single memory with recalled_count=1 (id=77, Hermes/project:costclock-ai/CostClock AI product description) was found by one of the 10 `memory search` calls. The other 297 searches were invisible to recall tracking.

---

## Root Cause 2: Searches Return Zero Results 67.8% of the Time

156 of 230 `brainctl search` calls returned 0 results. These searches failed to find anything across memories, events, AND context.

### Zero-Result Query Examples

| Query | Agent |
|-------|-------|
| `recent tasks` | paperclip-legion |
| `intelligence synthesis analysis pattern` | paperclip-cortex |
| `legion task assignment` | paperclip-legion |
| `weaver context ingestion` | paperclip-weaver |
| `COS sentinel-2 brainctl validate integrity` | paperclip-sentinel-2 |
| `sentinel brainctl validate` | paperclip-sentinel-2 |
| `recall assigned tasks search retrieval` | paperclip-recall |

**Analysis:** These queries use agent-specific terminology that doesn't appear in stored content. "Recent tasks" has no keyword overlap with any memory. "Intelligence synthesis analysis pattern" uses abstract composite terms not present in memory content. FTS5 requires word-level overlap — if the query words don't exist in the indexed content, no results.

---

## Root Cause 3: Query-to-Content Vocabulary Mismatch

Even searches that return results often return only semantic (vector) matches with no keyword hits (FTS5 tier 3: "weak-coverage"). Example from current session:

```
brainctl search "recall search retrieval FTS5 vector"
→ metacognition: tier=3, "No keyword matches; 10 semantic-only results"
```

The stored memories contain terms like "hybrid-rrf", "benchmark", "COS-86", "19/20 hit@5" — these don't appear in the query "recall search retrieval FTS5 vector".

**Structural cause:** Agents are trained to search by task concept ("what I'm doing"), but memories are written as outcome summaries ("what was accomplished"). These two vocabularies don't overlap at the keyword level.

### Query Taxonomy

| Query Type | FTS5 Match Rate | Example |
|------------|----------------|---------|
| Concept-level task description | Low | "intelligence synthesis analysis pattern" |
| Acronym/identifier queries | High | "COS-218", "brainctl" |
| Outcome term queries | Medium | "retrieval benchmark hit rate" |
| Full-sentence queries | Zero | "CostClock is an AI workspace..." |
| Combined keyword phrases | Medium-High | "metacognition gap detection" |

---

## Root Cause 4: The "Unknown" Agent — 89 Searches, All Zero Results

89 of 230 searches were logged from agent_id="unknown". These searches used full sentence strings as queries (Hermes's identity/belief statements like "CostClock is an AI workspace for financial operations..." and "The Operator is the unique differentiator. Everything feeds it.").

These are almost certainly the para-memory-file system (PARA memory / Claude.md project memory) dumping stored belief/identity statements into brainctl as queries. Full sentences fail FTS5 completely because they contain stop words and FTS5 rejects queries where all terms are suppressed.

**89 zero-result searches from a single pattern = 57% of all zero-result searches.**

---

## Root Cause 5: Searches Hit Events/Context First, Bypass Memory Layer

`brainctl search` returns results from all three tables: memories, events, context. The access_log `result_count` includes totals across all three. So a search returning 72 results may include 0 memories, 40 events, and 32 context chunks.

Agents satisfy their context need from events/context (which contain operational recaps, COS issue references, heartbeat summaries) and never surface memories. Memory becomes vestigial when events/context are more query-dense.

Evidence: The memory that WAS recalled (id=77, CostClock product description) is global/product knowledge — exactly the type of information NOT covered by events. Agents do use memory for stable background knowledge.

---

## Root Cause 6: Secondary Bug in `cmd_memory_search` — Filter-After-Recall

In `cmd_memory_search`, recalled_count is updated **before** scope/category filters are applied:

```python
# Update recall stats (line 299)
for r in results:
    db.execute("UPDATE memories SET recalled_count = recalled_count + 1 ...")

# Scope filter (lines 311-314 — AFTER the update)
if args.scope:
    results = [r for r in results if r["scope"] == args.scope]
```

Memories that match the FTS query but fail the scope filter get their recalled_count incremented even though the caller never sees them. This inflates recall counts for irrelevant memories. Minor issue given that `memory search` is used rarely, but a correctness bug.

---

## Query-to-Memory Mismatch Taxonomy

| Category | Description | Frequency | Impact |
|----------|-------------|-----------|--------|
| **Agent-role queries** | Agent searches for its own task area ("weaver context ingestion") — nothing stored about this agent's work yet | High | Total miss |
| **Concept queries** | Abstract multi-word concepts with no overlap to narrative memory content | High | Total miss |
| **Full-sentence queries** | Long descriptive sentences (from para-memory system) | Medium | Total miss |
| **Scope isolation** | Memory stored in `project:agentmemory` scope, agent searches without scope filter | Medium | Recall not credited |
| **Stale content** | Memory written, but system is <1 day old; no accumulated recall history | Universal | Baseline distortion |
| **Content vocabulary** | Memory written with COS identifiers; query uses semantic terms | Medium | Tier-3 match only |

---

## Retrieval Surface Area Audit

### Where `brainctl search` is called:
- Agent AGENTS.md: "Before starting work: `brainctl -a YOUR_AGENT_NAME search "keywords about your task"`" — primary pre-work step
- para-memory-file skill: dumps belief/identity items as queries (unknown agent)
- All Memory Division agents on heartbeat startup

### Where `brainctl memory search` is called:
- Same AGENTS.md template: "brainctl -a YOUR_AGENT_NAME memory search "relevant topic"" — secondary step
- Executed 10 times vs 230 for `brainctl search` — agents skip or deprioritize this

### Where memory should be searched but isn't:
- **During task work** (mid-execution, not just at startup): agents check brain once at start, then proceed without re-querying as the task context evolves
- **Route-context / --expertise context**: expertise directory exists but doesn't pull memories
- **Before writing a new memory**: no "does this already exist?" search before `memory add`
- **After receiving a blocked message**: agents don't search for known fixes to common blockers

---

## Recommendations — Priority-Ordered

### P0 (Fix Now): Add `recalled_count` Tracking to `cmd_search` and `cmd_vsearch`

Both functions already iterate over result memories. Add the same UPDATE block used in `cmd_memory_search`:

```python
# In cmd_search, after building results["memories"]:
for r in results.get("memories", []):
    if r.get("id") and r.get("source") != "graph":  # skip graph-expanded neighbors
        db.execute(
            "UPDATE memories SET recalled_count = recalled_count + 1, "
            "last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
            (r["id"],)
        )

# Same pattern for cmd_vsearch
```

**Expected impact:** ~95% of recalled_count = 0 problem resolved. Hit rate should immediately reflect true access patterns.

### P0 (Fix Now): Fix Scope Filter Bug in `cmd_memory_search`

Move scope/category filters to BEFORE the recalled_count update:

```python
# Apply filters FIRST
if args.scope:
    results = [r for r in results if r["scope"] == args.scope]
if args.category:
    results = [r for r in results if r["category"] == args.category]

# THEN update recall stats
for r in results:
    db.execute("UPDATE memories SET recalled_count = recalled_count + 1 ...")
```

### P1 (This Week): Fix the Unknown Agent / Para-Memory Query Pattern

89 searches sending full Hermes identity sentences to brainctl return 0 results. Options:
1. The para-memory skill should extract 2-5 keywords from beliefs before querying
2. Or para-memory should use `vsearch` (semantic) instead of `search` (FTS5) for long-form content
3. Or para-memory should not query brainctl at all (it has its own storage layer)

### P1 (This Week): Keyword Guidance for Agent Queries

Update AGENTS.md query guidance to favor identifier terms and concrete nouns:

```
# Bad (too abstract):
brainctl search "intelligence synthesis analysis pattern"

# Good (COS identifiers, concrete nouns):
brainctl search "COS-218 gap detection"

# Good (outcome terms from memory content):
brainctl search "hippocampus consolidation cycle"
```

Add query formation examples to AGENTS.md.

### P2 (This Month): Address 67.8% Zero-Result Rate

Three sub-fixes:
1. **FTS5 vocabulary expansion**: when a query returns 0 FTS results, auto-extract entity terms (COS identifiers, project names, agent names) from the query and retry with subset
2. **Content writing standards**: memory authors should include 3-5 keywords in the first sentence of every memory that agents are likely to search for
3. **Fallback to vsearch**: when cmd_search returns 0 memory results, automatically fall back to semantic vsearch for memories

### P2: Route `brainctl search` Results by Table Salience

Current behavior: result_count logs total across all tables. Agents can't tell if they got memory hits or just event hits. Add per-table counts to the output and access_log, so agents know when their search returned memories vs. only events.

### P3 (Architecture): Mid-Task Memory Polling

Agents currently search memory once at heartbeat start. High-value memories (architecture decisions, standing rules, project context) should be accessible throughout the task. Options:
- Add memory poll to route-context so relevant memories surface at task checkout time
- Implement `brainctl push --task <identifier>` auto-trigger on checkout (COS-124 foundation exists)

### P3: Memory Content Design Audit

40 memories never accessed suggests the content itself doesn't match what agents need to know during work. Proposed quarterly review:
- For each memory: what query would an agent realistically issue that would hit this memory?
- Rewrite memories whose most likely query returns 0 FTS hits
- Tag memories with searchable keywords in a `tags` field (column already exists)

---

## Predicted Hit Rate After Fixes

| Fix Applied | Estimated recalled_count > 0 | Notes |
|-------------|------------------------------|-------|
| Baseline (current) | 1/41 (2.4%) | Only `memory search` updates count |
| P0: Recall tracking in cmd_search | ~30/41 (73%) | Most memories are topically relevant to running searches |
| P0 + P1: Fix query patterns | ~33/41 (80%) | Reduces 89 wasted zero-result queries |
| P0+P1+P2: Fix zero-hit rate | ~37/41 (90%) | FTS vocabulary bridging |
| All fixes | >40/41 (>97%) | Threshold goal exceeded |

Note: the 40% threshold in the task definition is achievable with P0 alone, assuming the observation window extends beyond a single day and agents continue normal operation.

---

## Conclusion

The 97.6% zero-recall rate is primarily a **measurement failure**, not a retrieval failure. The primary search path has never updated `recalled_count`. Fix cmd_search and cmd_vsearch to record recalls (P0), and the metric will immediately reflect true retrieval behavior.

The secondary problem — 67.8% zero-result rate from vocabulary mismatch and malformed queries — is a genuine retrieval failure that needs addressing via query discipline, content writing standards, and FTS fallback strategies.

Memory itself is sound. The architecture, scoring, and temporal weighting are working correctly. The pipeline just has a tracking gap at the most-used entry point.

---

*Research output: ~/agentmemory/research/wave6/17_retrieval_utility_analysis.md*
*Benchmark context: ~/agentmemory/benchmarks/retrieval_benchmark_v1.py*
*Related: COS-201 (adaptive weights), COS-205 (embedding coverage), COS-218 (gap detection)*
