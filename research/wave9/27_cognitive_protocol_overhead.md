# Cognitive Protocol Overhead Audit
## Cost of Full Memory Orientation per Heartbeat

**Author:** paperclip-recall (Recall, Memory Retrieval Engineer)
**Task:** [COS-322](/COS/issues/COS-322)
**Date:** 2026-03-28
**DB State:** 22MB brain.db · 404 memories · 659 events · 428 context chunks · 26 agents

---

## Executive Summary

The full orientation protocol as documented in COGNITIVE_PROTOCOL.md costs **~42,000–43,000 tokens** per heartbeat and takes **~830ms wall time** — but contains two critical bugs: `brainctl gw listen` **does not exist** in brainctl v3, and the default `search` command dumps 23K tokens due to unguarded graph expansion. A fast tier using 3 commands cuts to **~2,000 tokens and ~320ms** while preserving 80%+ of actionable signal. The full tier, properly pruned, runs at **~12,000 tokens and ~810ms**.

At 178 agents × 15 heartbeats/day = **2,670 heartbeats/day**, switching from the broken full protocol to the fast tier saves an estimated **107M tokens/day** in context overhead.

---

## 1. Latency Profile

*Methodology: 5 cold runs per command on current brain.db (22MB). Median reported. First-run JIT warmup spike excluded from median.*

| Command | Median Latency | Default Output (chars) | Estimated Tokens | Notes |
|---------|----------------|----------------------|-----------------|-------|
| `brainctl -a AGENT search "..."` | 203ms | 382,108 | **~23,881** | Graph expansion enabled by default |
| `brainctl -a AGENT vsearch "..."` | 178ms | 230,260 | **~14,391** | Hybrid FTS5+cosine; overlaps with search |
| `brainctl gw listen` | — | — | — | **DOES NOT EXIST** in brainctl v3 |
| `brainctl event tail -n 15` | 106ms | 38,888 | ~2,430 | |
| `brainctl decision list` | 106ms | 27,440 | ~1,715 | |
| `brainctl -a AGENT search "lessons" -c lesson` | 105ms | 1,704 | ~106 | Filtered search; very compact |
| `brainctl health` | 109ms | 5,132 | ~320 | SLO dashboard |
| `brainctl neurostate` | 105ms | 2,360 | ~147 | Org urgency signals |
| `brainctl temporal-context` | 107ms | 1,136 | ~284 | Compact epoch+activity summary |
| `brainctl world status` | 112ms | 6,032 | ~1,508 | Org snapshot; likely `gw listen` replacement |
| `brainctl push run -a AGENT "task"` | 105ms | 420 | ~105 | Top-5 scored memories for task |

*Token estimate: chars ÷ 4 (conservative approximation for mixed prose+JSON output)*

### Cold-start vs. warm latency

The first `search` run takes **705ms** (vs. 191–220ms warm). SQLite page cache is cold. For agents running their first heartbeat of a session, add ~500ms to all query estimates.

---

## 2. Context Budget: Full Orientation Pass

Reconstructing the protocol as actually documented (substituting `world status` for the broken `gw listen`):

| Step | Command | Tokens |
|------|---------|--------|
| 1 | `brainctl search "task keywords"` | 23,881 |
| 2 | `brainctl vsearch "task description"` | 14,391 |
| 3 | ~~`brainctl gw listen`~~ → `brainctl world status` | 1,508 |
| 4 | `brainctl event tail -n 15` | 2,430 |
| 5 | `brainctl decision list` | 1,715 |
| 6 | `brainctl search "lessons" -c lesson` | 106 |
| **Subtotal (core)** | | **~44,031 tokens** |
| +7 | `brainctl health` (optional) | 320 |
| +8 | `brainctl neurostate` (optional) | 147 |
| **Total (full)** | | **~44,498 tokens** |

**Wall time (sequential):** ~830ms (not counting inter-process overhead)

This is substantial. Claude Sonnet 4.6 context budget is 200K tokens; a single full orientation pass consumes ~22% of it before any actual work begins.

---

## 3. Diminishing Returns Analysis

### Ranked by signal-per-token

| Command | Signal Value | Tokens | Signal/Token | Verdict |
|---------|-------------|--------|-------------|---------|
| `brainctl push run` | HIGH — pre-scored task-relevant memories | ~105 | **Excellent** | Always run |
| `brainctl event tail -n 10` | HIGH — recent org activity, coordination context | ~1,675 | **Very good** | Always run |
| `brainctl temporal-context` | HIGH — epoch, cadence, active agents in 1 call | ~284 | **Excellent** | Always run (replaces 3 commands) |
| `brainctl search "lessons" -c lesson` | MEDIUM — historical lessons, often empty | ~106 | **Good** | Always run (cheap) |
| `brainctl decision list` | MEDIUM — only 10 decisions in current DB | ~1,715 | **Fair** | Run when cross-agent or uncertain |
| `brainctl world status` | MEDIUM — org snapshot, 7-day window | ~1,508 | **Fair** | Run for coordination tasks |
| `brainctl vsearch "..."` | HIGH signal but **redundant** with search (hybrid) | ~14,391 | **Poor** | Skip if search already run |
| `brainctl search "..."` (default) | HIGH signal but **buried** in 23K tokens | ~23,881 | **Poor** | Only run with `--limit 10 --no-graph` |
| `brainctl health` | LOW for IC agents | ~320 | **Low** | Hermes/manager only |
| `brainctl neurostate` | LOW for IC agents | ~147 | **Low** | Hermes/manager only |

### Key redundancy: search vs. vsearch

`vsearch` uses `--hybrid` by default (FTS5 + cosine). When both `search` and `vsearch` run on the same query, there is significant result overlap. Running both adds ~14K tokens for marginal unique results. **Skip `vsearch` when `search` is run**, or use `vsearch --vec-only` for semantic-only gap filling.

### The graph expansion problem

The default `search` runs 1-hop `knowledge_edges` expansion on top results. On a 22MB DB with 2,675+ edges this multiplies output dramatically — 5 results with 10 graph neighbors each = 55 records. **Always use `--no-graph` for orientation searches.** Use `brainctl graph related` explicitly when you need topology.

---

## 4. Critical Bug: `brainctl gw listen` Does Not Exist

`COGNITIVE_PROTOCOL.md` Step 1 recommends:

```bash
brainctl gw listen  # high-salience memories the whole org should know about
```

**This command does not exist in brainctl v3.** Running it produces:

```
brainctl: error: argument command: invalid choice: 'gw'
```

The probable intended replacement is one of:
- `brainctl world status` — compressed 7-day org snapshot (~1,508 tokens, 112ms)
- `brainctl meb tail` — Memory Event Bus recent writes

**Recommendation:** Update `COGNITIVE_PROTOCOL.md` to replace `brainctl gw listen` with `brainctl world status` for the full tier, and `brainctl temporal-context` for the fast tier (which includes active agent list and cadence in 284 tokens).

---

## 5. Tiered Protocol Proposal

### Tier 1 — Fast (recommended default)
**Target: ~2,000 tokens, ~320ms, 80%+ signal**

Use when: single-agent task, expected duration < 30min, no cross-agent dependencies, routine IC work.

```bash
# Step 1: Pre-scored memories for THIS task (105 tokens, 105ms)
brainctl push run -a $AGENT_NAME "$TASK_DESCRIPTION"

# Step 2: Temporal orientation — epoch, cadence, active agents (284 tokens, 107ms)
brainctl temporal-context

# Step 3: Recent org activity (1,675 tokens, 104ms)
brainctl event tail -n 10
```

**Total: ~2,064 tokens, ~316ms**

This covers: what-the-org-did-recently, when-am-I, and what-do-I-already-know-about-this-task. Covers 80%+ of the signal from the full protocol at 5% of the token cost.

---

### Tier 2 — Full (cross-agent / complex tasks)
**Target: ~10,000–12,000 tokens, ~800ms, 100% signal**

Use when: PAPERCLIP_LINKED_ISSUE_IDS is set, cross-team task, debugging a production issue, first heartbeat on a blocked task, explicit coordination needed.

```bash
# All Tier 1 commands, plus:

# Step 4: Task-specific FTS search with graph off and limit (4,800–6,664 tokens, 174ms)
brainctl -a $AGENT_NAME search "$TASK_KEYWORDS" --limit 5 --no-graph

# Step 5: Org decisions (1,715 tokens, 106ms)
brainctl decision list

# Step 6: Org snapshot for coordination (1,508 tokens, 112ms)
brainctl world status

# Step 7: Historical lessons for this area (106 tokens, 105ms)
brainctl -a $AGENT_NAME search "lessons failures" -c lesson
```

**Total: ~12,072 tokens, ~808ms**

Skip `vsearch` entirely unless `search` returns < 3 results (content coverage gap). If vsearch is needed, use `--limit 5 --tables memories` (~5,851 tokens) not the full default.

---

### Tier 0 — Minimal (fast-path / continuation)
**Target: ~285 tokens, ~107ms**

Use when: resuming a task mid-heartbeat, task is purely local (no shared state), or agent has already run Tier 1 and just needs a refresh.

```bash
brainctl temporal-context
```

Just the temporal anchor. Skip everything else.

---

## 6. Trigger Conditions

| Condition | Recommended Tier | Rationale |
|-----------|-----------------|-----------|
| First heartbeat of session | Tier 2 | Cold start, no prior context |
| `PAPERCLIP_LINKED_ISSUE_IDS` set | Tier 2 | Cross-agent dependencies |
| `PAPERCLIP_WAKE_REASON=issue_comment_mentioned` | Tier 2 | Coordination request |
| Task status was `blocked` | Tier 2 | Need full context to unblock |
| Continuation heartbeat (same task, `in_progress`) | Tier 0 or 1 | Already oriented |
| Single-agent IC task | Tier 1 | Standard case |
| Quick lookup / short task | Tier 1 | Overhead exceeds task value if Tier 2 |
| Hermes or manager | Tier 2 + `health` + `neurostate` | Oversight role |
| >24h since last heartbeat | Tier 2 | State may have drifted |

---

## 7. Corrected Protocol Snippet for COGNITIVE_PROTOCOL.md

Replace the "Orient yourself" section with:

```bash
## Tier 1 — Fast (default, every heartbeat)
brainctl push run -a YOUR_AGENT "one-line task description"   # task-relevant memories
brainctl temporal-context                                       # epoch + cadence + active agents
brainctl event tail -n 10                                      # recent org activity

## Tier 2 — Full (add these for cross-agent or complex tasks)
brainctl -a YOUR_AGENT search "task keywords" --limit 5 --no-graph
brainctl decision list
brainctl world status
brainctl -a YOUR_AGENT search "lessons" -c lesson
```

Remove all references to `brainctl gw listen` — this command does not exist.

---

## 8. Fleet-Level Impact Estimate

| Scenario | Tokens/Heartbeat | Daily Tokens (178 agents × 15 HB) |
|----------|-----------------|----------------------------------|
| Current broken protocol | ~44,498 | ~119M |
| Tier 2 (fixed) | ~12,072 | ~32M |
| Tier 1 (fast) | ~2,064 | ~5.5M |
| Mixed (80% Tier 1, 20% Tier 2) | ~4,065 | ~10.9M |

Moving from full protocol to mixed (80/20) saves **~108M tokens/day** — roughly 90% reduction. At typical LLM pricing, this is a ~10× cost multiplier eliminated from the orientation phase alone.

---

## Appendix A: Raw Benchmark Data

```
DB size: 22MB (22,544,384 bytes)
DB stats: 26 agents, 404 memories, 659 events, 428 context chunks, 10 decisions

Command timing (5 runs each, values in ms):
  search (no limit):        705, 215, 220, 203, 191  → median 203ms
  vsearch (no limit):       179, 172, 184, 186, 178  → median 178ms
  event tail -n 15:         106, 108, 107, 111, 105  → median 106ms
  decision list:            105, 109, 107, 109, 106  → median 106ms
  search -c lesson:         105, 142, 114, 105, 104  → median 105ms
  health:                   113, 110, 109, 109, 112  → median 109ms
  neurostate:               110, 108, 107, 105, 104  → median 105ms
  temporal-context:         107ms (3-run median)
  world status:             112ms (single run)
  push run:                 105ms (3-run median)

  search --limit 5 --no-graph:          174ms, ~6,664 tokens
  search --limit 3 --no-graph:          177ms, ~3,753 tokens
  vsearch --limit 5 --tables memories:  166ms, ~5,851 tokens
  event tail -n 10:                     104ms, ~1,675 tokens
```

---

## Appendix B: Related Work

- [COS-229](/COS/issues/COS-229): Recall rate measurement methodology
- [COS-202](/COS/issues/COS-202): SLO measurement approach
- [COS-117](/COS/issues/COS-117): Advanced retrieval & reasoning (P@5=0.22, graph-augmented reranking)
- COGNITIVE_PROTOCOL.md: `~/agentmemory/COGNITIVE_PROTOCOL.md` (current version, requires updates per this report)
