# Proactive Memory Push — Anticipatory Context Delivery Before Agents Ask
## Research Report — COS-124
**Author:** Weaver (Context Integration Engineer)
**Date:** 2026-03-28
**Project:** Cognitive Architecture & Enhancement (Wave 3)
**Target:** brain.db + brainctl + Paperclip heartbeat architecture

---

## Executive Summary

All current memory retrieval in the 22-agent (scaling to 178-agent) brain.db system is **pull-based**: an agent explicitly calls `brainctl search` and receives results. This reactive model means agents can only retrieve what they already know to ask for — the unknown-unknown problem of memory retrieval. This report proposes a **push-based layer** that monitors agent task state and injects relevant memories proactively, before the agent issues its first query.

**Central finding:** A push-based system is viable and high-value, but must be strictly gated. The risk of push is not latency — it is **noise injection**: irrelevant context prepended to the agent's working window degrades performance more than no context at all. The right architecture is: *push a small, high-confidence set (≤5 chunks) at the single highest-signal moment (issue checkout), scored by a combined FTS + vector + graph activation pipeline, tracked for downstream utility.*

**Recommended implementation path:**
1. New `brainctl push` command: score + select top-K memories for a given task description
2. Paperclip post-checkout hook: invoke `brainctl push` and log results to events table
3. CLAUDE.md injection point: push output prepended to agent's context at heartbeat start
4. Utility tracking: correlate push IDs to recalled_count delta over the session

**Estimated impact:** 30–50% reduction in explicit `brainctl search` calls per heartbeat for well-scoped tasks, with a corresponding improvement in first-tool-call quality for tasks that touch established domains (billing, auth, deploy pipeline, schema changes).

---

## 1. Theoretical Foundation

### 1.1 Predictive Coding (Rao & Ballard, 1999)

Rao and Ballard's landmark paper demonstrated that the visual cortex does not passively receive signals — it *predicts* incoming input and only propagates the **prediction error** (the delta between expected and actual). The higher cortical layers constantly push their current model downward, pre-activating low-level processing before the stimulus arrives.

The agent memory analogy is direct:

| Neuroscience | Agent Memory |
|---|---|
| Higher cortical layers | Task context (issue title, goal, project) |
| Downward prediction signal | Pre-fetched memory set (push) |
| Prediction error | What the agent still needs to search for |
| Residual processing | Explicit `brainctl search` calls |

If the predictive signal is accurate, the agent's "search residual" shrinks — fewer explicit queries, more working time.

### 1.2 Clark (2015) — Surfing Uncertainty

Clark extends predictive coding to action and embodied cognition: the brain is an *inference engine* that generates hypotheses about its immediate future and uses perception to update them. The implication for agentic systems: the system should maintain a running forward model of what the agent is *about to need*, and pre-position resources accordingly.

In practical terms for our system: when Paperclip assigns issue `COS-124` to Weaver, the system knows (a) the task description, (b) Weaver's recent event history, (c) the project graph, and (d) historical memory access patterns for similar tasks. This is sufficient to generate a meaningful pre-fetch set before Weaver issues its first tool call.

### 1.3 Distinction from COS-112 (Wave 2 — Predictive Cognition)

COS-112 focused on *what* to predict — which memories are likely to become relevant, using forgetting curves, recency, and semantic similarity. COS-124 addresses the orthogonal question: *how* to deliver those predictions to the agent before they ask. The two research threads are complementary:

- COS-112: scoring model (what is likely relevant)
- COS-124: delivery mechanism (how to get it there proactively)

A production implementation would use COS-112's scoring output as the retrieval signal for COS-124's push channel.

---

## 2. Trigger Model

The key design question is: **at what moment does a proactive push produce the highest signal-to-noise ratio?**

### 2.1 Candidate Trigger Points

| Trigger | Signal Quality | Latency Budget | Noise Risk |
|---|---|---|---|
| Task assignment (Paperclip) | Medium — intent known, no agent context yet | High (seconds to minutes before work begins) | Low |
| **Issue checkout** | **High — agent has committed to work, has run context** | **Medium (checkout adds ~100ms)** | **Low** |
| First tool call in heartbeat | High — topic confirmed by action | Low (agent already working) | Low |
| Topic shift detection (mid-session) | Very high — confirmed pivot | Very low (post-hoc) | Medium |
| Every heartbeat (unconditional) | Low — no fresh signal | N/A | High |

**Recommendation: Issue checkout is the optimal trigger.**

Rationale:
- At checkout, the agent has accepted the task and the heartbeat context is already populated with issue title, description, and ancestor summaries
- The checkout event fires a deterministic API call (`POST /api/issues/:id/checkout`) — trivially hookable
- The latency budget is generous: checkout happens before the agent starts substantive work, so a 200–400ms push query does not block the critical path
- The task description is the highest-quality semantic signal available without requiring the agent to have done any work

**Assignment** is a secondary trigger. Push at assignment time has the advantage of pre-positioning context before the agent even wakes — but the agent may not wake for minutes or hours, and the project may have shifted. Stale pre-pushed context is worse than none. Prefer checkout.

**Every heartbeat** is explicitly rejected. Repeating the same push on every heartbeat with no task change is pure noise. The push should fire once per checkout, not once per heartbeat.

### 2.2 Trigger Architecture

```
Paperclip heartbeat wake
  └─ Agent calls: POST /api/issues/:id/checkout
       └─ [hook / post-checkout event fires]
            └─ brainctl push --task-id COS-124 --description "..." --agent weaver
                 └─ top-K memories/context returned
                      └─ injected into agent's working context (see §4)
```

The hook can be implemented in two ways:
1. **Client-side (agent-owned):** The agent runs `brainctl push` immediately after checkout, injects results into its working context manually. Simple, reliable, no infrastructure changes.
2. **Server-side (Paperclip webhook):** Paperclip fires a push event to a sidecar service on checkout. The sidecar pre-populates a per-agent push cache readable by `brainctl push --cached`. More complex, but allows push to begin before the agent's heartbeat clock starts.

For the current 22-agent scale, **client-side is recommended**. At 178+ agents with frequent checkouts, server-side with caching becomes worthwhile.

---

## 3. Relevance Scoring

The push channel is only as good as its relevance function. Pushing noise is actively harmful — it consumes context tokens and primes the agent toward wrong associations.

### 3.1 Three-Layer Scoring Pipeline

```
Input: task description D, agent_id A, issue metadata M

Layer 1: FTS5 keyword match (fast gate)
  → score_fts[i] = BM25(D, memory[i].content)
  → filter: score_fts < threshold → discard
  → candidate pool: top-50

Layer 2: Vector similarity (semantic gate)
  → embedding(D) → vsearch top-K from candidate pool
  → score_vec[i] = cosine_similarity(emb(D), emb(memory[i]))
  → filter: score_vec < 0.72 → discard
  → candidate pool: top-20

Layer 3: Graph activation bonus (context enrichment)
  → for each surviving candidate: spreading_activation(candidate, hops=1)
  → score_graph[i] = max(activation_score of neighbors that survived vec gate)
  → bonus: +0.1 per activated neighbor in candidate pool

Final score: 0.5×score_vec + 0.3×score_fts_normalized + 0.2×score_graph
Push set: top-5 by final score, confidence > 0.6, not retired
```

### 3.2 Agent-Specificity Filtering

Before scoring, apply pre-filters:
1. **Scope filter:** prefer memories with `scope = 'global'` or `scope = project_id` of the current task
2. **Agent-recency boost:** memories the current agent has recalled in the last 7 days get +0.05 (agent already knows them — probably don't re-push unless high score)
3. **Novelty preference:** memories the current agent has *never* recalled get a small boost (+0.03) — these are the highest-value pushes (agent hasn't seen them yet)
4. **Recency cap:** memories updated > 90 days ago require score > 0.85 to enter push set (prefer fresh over stale at equal relevance)

### 3.3 Anti-Noise Safeguards

- **Hard cap:** never push more than 5 memories/context chunks per checkout. If the top-5 average confidence is < 0.6, push nothing and let the agent search naturally.
- **Topic coherence check:** if the top-5 memories span > 3 distinct categories, the task is too broad to push usefully — skip and log.
- **Repetition guard:** if ≥ 3 of the push-5 were also pushed at the agent's last checkout, suppress the push (the agent likely didn't use them, or already knows them — see §5).

---

## 4. Push Channel Design

How does the proactively fetched context actually reach the agent?

### 4.1 Option A — System Prompt Prepend (Recommended)

The push output is formatted as a compact markdown block and prepended to the agent's working context at heartbeat start, before the agent reads the issue or calls any tools.

```markdown
<!-- PROACTIVE MEMORY PUSH — COS-124 checkout -->
**Pre-loaded context (5 items, relevance-ranked):**
1. [memory:88] Billing pipeline uses Stripe webhook idempotency keys — duplicate events are safe to replay (confidence: 0.91)
2. [context:44/chunk:3] Last billing deploy (2026-03-21): latency spike traced to missing index on `invoice_line_items.customer_id`
3. [memory:71] Weaver owns context ingestion — Hermes owns schema decisions (confidence: 0.87)
4. [event:312] COS-83 closed 2026-03-28: auto-route-events (Phase 3) shipped, 14 events routed in first sweep
5. [context:52/chunk:1] brainctl push pipeline not yet implemented (as of wave3 kickoff)
<!-- END PROACTIVE PUSH -->
```

**Advantages:**
- No new infrastructure. The agent reads it like any other context.
- The agent can ignore it if irrelevant (low noise cost when content is wrong).
- Immediately available to all current agents without code changes to brainctl.

**Disadvantages:**
- Consumes context tokens unconditionally (even for trivial tasks).
- No structured feedback path — the agent can't easily signal "I didn't use #3."

**Implementation:** Add a `brainctl push` command (see §6) that outputs the formatted block. The agent runs it after checkout and includes the output in its working context.

### 4.2 Option B — brainctl Injection Hook

A new event type `context_push` is logged to the `events` table immediately after checkout. The agent's startup script reads pending push events for its agent_id and surfaces them.

```bash
# Push event logged by hook:
brainctl event add "Pre-push for COS-124 checkout" \
  -t context_push \
  -p costclock-ai \
  --metadata '{"push_id":"...", "items":[...], "task":"COS-124"}'

# Agent reads at heartbeat start:
brainctl event list --type context_push --agent weaver --unread
```

**Advantages:**
- Structured, queryable. Push events are auditable.
- Enables async push (server-side pre-fetch before agent wakes).
- Feedback loop is natural: mark events as "used" when agent explicitly recalls the memory.

**Disadvantages:**
- Requires agents to actively read push events (adds heartbeat overhead).
- "Unread" state requires new schema field or agent_state tracking.

### 4.3 Option C — CLAUDE.md Injection (Minimal MVP)

The simplest possible implementation: add a section to the agent's CLAUDE.md that runs `brainctl push` on session start and surfaces the output.

```markdown
## On task checkout
Run: `brainctl push --agent $AGENT_ID --task-title "$TASK_TITLE"`
Review the output and use it as pre-loaded context for the current task.
```

**Disadvantages:** Requires the agent to remember to run it. Not automatic.

**Verdict:** Start with Option A (system prompt prepend via `brainctl push` command), add Option B (event logging) for auditability and feedback. Option C is a stop-gap MVP.

---

## 5. Feedback Loop

A proactive push system without a feedback loop becomes a static pre-fetch that decays in quality over time. The system must learn which push suggestions were actually useful.

### 5.1 Implicit Feedback Signals

| Signal | Interpretation | Confidence |
|---|---|---|
| Agent explicitly recalls pushed memory via `brainctl memory get` | Used it | High |
| Agent's event log references the pushed memory's content | Used it | Medium |
| Agent completes task without searching pushed topic at all | Pushed content was either self-sufficient (high utility) or wrong topic (low utility) | Ambiguous |
| Agent calls `brainctl search` with a query semantically identical to the pushed content | Push arrived but wasn't sufficient — agent needed to re-query | Low utility signal |
| Agent calls `brainctl search` with queries covering none of the pushed topics | Topics were wrong — likely noise injection | Negative signal |

**Recommended primary signal:** Track `recalled_count` delta on pushed memories between checkout and task completion. A pushed memory whose `recalled_count` increased during the task session was accessed — strong utility signal.

### 5.2 Feedback Schema

Add a `push_log` table to brain.db:

```sql
CREATE TABLE push_log (
  id          INTEGER PRIMARY KEY,
  push_id     TEXT NOT NULL UNIQUE,        -- UUID per push event
  agent_id    TEXT NOT NULL,
  task_ref    TEXT NOT NULL,               -- e.g. "COS-124"
  checkout_at TEXT NOT NULL,
  items       TEXT NOT NULL,               -- JSON array: [{table, id, score, category}]
  used_ids    TEXT,                        -- JSON array: item ids where recalled_count increased
  utility_score REAL,                      -- 0.0-1.0, computed post-task
  created_at  TEXT DEFAULT (datetime('now'))
);
```

### 5.3 Scoring Model Update

After task completion (done/blocked), a lightweight scorer runs:

```python
utility = len(used_ids) / len(items)  # fraction of pushed items actually used
```

Feed utility scores back to the relevance model:
- If a memory is pushed N times and used < 20% of the time across all agents: lower its push-priority score globally (it's a false attractor).
- If a memory is consistently used when pushed for tasks in category X: boost its category-conditional push score.

This creates a **push quality ratchet**: the system gets better at selecting push content over time without any manual tuning.

### 5.4 Cold Start

The first 30 days of push operation will have no historical utility data. During cold start:
- Use only FTS5 + vector scores (no graph bonus, no push-history penalty)
- Log everything to push_log but do not adjust scores
- Begin adjusting scores after 50+ completed push events

---

## 6. Cost/Benefit Analysis

### 6.1 Query Overhead at Checkout

| Step | Estimated Latency |
|---|---|
| FTS5 BM25 scan (50 candidate memories) | ~15ms |
| vsearch top-20 from candidate pool | ~30ms |
| Graph activation bonus (1-hop, 50 nodes) | ~20ms |
| Result formatting | ~5ms |
| **Total push query overhead** | **~70ms** |

At issue checkout, the agent has already incurred ~100–200ms of API round-trip time. An additional 70ms is a <50% overhead increase — well within acceptable bounds for a one-time cost per checkout.

Compare to: a typical `brainctl search` call within a heartbeat costs 50–150ms *plus* the agent's reasoning time to formulate the query. Proactive push eliminates 1–3 of these search cycles per heartbeat for well-matched tasks.

### 6.2 Token Cost per Heartbeat

At 5 push items, each ~50 tokens of formatted content: **~250 tokens** added to working context per checkout.

For a 200K context window (Claude Sonnet 4.6): 0.125% — negligible.
For a 32K context window agent: ~0.78% — acceptable.

Token cost is only meaningful if the push fires on *every* heartbeat. With the checkout-once model (§2), this cost is paid once per task, not once per heartbeat cycle.

### 6.3 Latency Budget Recommendation

| Scenario | Acceptable total push latency |
|---|---|
| Interactive task (user watching) | ≤ 200ms |
| Background heartbeat | ≤ 500ms |
| Async server-side pre-fetch | ≤ 2000ms |

The 70ms estimate comfortably fits all scenarios. If the push query exceeds 300ms (e.g., on a large brain.db with 10K+ memories), fall back to FTS5-only (no vsearch) to stay under budget.

### 6.4 Expected Net Benefit

For well-scoped tasks in established domains (billing, auth, deploy, schema):
- Estimated 30–50% reduction in explicit search calls
- Estimated 15–25% improvement in first-tool-call quality (agent starts with relevant context rather than cold-starting)
- Zero regression for tasks in domains with no established memories (push returns empty set, agent proceeds normally)

---

## 7. Implementation Sketch

### 7.1 New `brainctl push` Command

```
brainctl push --description "text" [--agent weaver] [--top 5] [--min-confidence 0.6]

Options:
  --description TEXT    Task description to score against (required)
  --agent TEXT          Agent ID for scope/recency filtering
  --top INT             Max items to push (default 5)
  --min-confidence REAL Minimum memory confidence (default 0.6)
  --format [markdown|json|none]  Output format

Output: formatted push block (see §4.1)
```

**Implementation path in brainctl:**

1. Add `push` subcommand to `brainctl/__main__.py`
2. Implement `push_score(description, agent_id, top_k)` in `brainctl/search.py`:
   - FTS5 BM25 candidate gate: `SELECT id FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT 50`
   - vsearch rerank: `SELECT rowid, distance FROM vec_memories WHERE ... ORDER BY distance LIMIT 20`
   - Graph bonus: query `knowledge_edges` for 1-hop neighbors of top-20, compute activation
   - Final score blend
3. Format output as markdown block
4. Log push event to `events` table with type `context_push`

### 7.2 Agent Integration (Heartbeat Procedure)

Add to the Weaver (and all agent) heartbeat procedure, immediately after Step 5 (Checkout):

```markdown
**Step 5b — Proactive push.**
After successful checkout, run:

```bash
brainctl push \
  --description "$ISSUE_TITLE: $ISSUE_DESCRIPTION_FIRST_200_CHARS" \
  --agent "$AGENT_ID" \
  --top 5
```

If output is non-empty, treat the push block as pre-loaded context for this task.
Log the push_id from the output to use in Step 9 (utility tracking).
```

### 7.3 Utility Tracking at Task Close

When marking a task `done` or `blocked`, the agent should optionally run:

```bash
brainctl push --log-utility --push-id "$PUSH_ID" --task "$TASK_ID"
```

This queries `recalled_count` deltas for pushed items and writes to `push_log`.

### 7.4 Push Quality Dashboard (Future)

A `brainctl push stats` command should eventually show:

```
Push performance (last 30 days):
  Total pushes:        142
  Average utility:     0.61 (3.1/5 items used)
  Top pushed category: project (42%)
  Worst category:      global (0.31 utility)
  Most useful memory:  [88] Billing idempotency (used 31/31 times pushed)
  Least useful:        [55] Schema version convention (used 2/18 times pushed)
```

### 7.5 Schema Change (Minimal)

One new table (`push_log`) as defined in §5.2. No changes to existing `memories`, `events`, or `context` tables. Fully additive — zero migration risk.

---

## 8. Integration with Other Wave 3 Research

| Report | Integration point |
|---|---|
| COS-120 (Episodic/Semantic Bifurcation) | Proactive push should weight semantic memories (stable facts) higher than episodic (events) — stable facts transfer better across contexts |
| COS-121 (Provenance & Trust) | Push should filter to memories with `trust_level ≥ 0.7` — don't proactively inject low-trust or unverified facts |
| COS-122 (Write Contention) | Push reads are non-mutating; no contention risk. But push scoring results should not be cached across heartbeats — fresh reads ensure version consistency |
| COS-111 (Associative Memory / Wave 2) | The graph activation bonus in Layer 3 scoring directly uses spreading activation from COS-111's design |

---

## 9. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Push injects irrelevant context, misleads agent | Medium | High | Hard cap at 5 items; coherence check; min-confidence gate |
| Push latency exceeds budget at large scale | Low | Medium | FTS5-only fallback if query > 300ms |
| Push quality degrades as brain.db grows without feedback | Medium | Medium | Utility tracking from day 1; push_log table |
| Agents skip the push step (it's optional) | Medium | Low | Encode in heartbeat procedure instructions (AGENTS.md) |
| Topic drift: task description evolves after checkout | Low | Low | Push fires once per checkout, not per heartbeat — no stale-push risk |

---

## 10. Recommendations

1. **Implement `brainctl push` command** as a first-class subcommand. Target: 1–2 days of development.
2. **Add push step to all agent AGENTS.md** heartbeat procedures (Step 5b), making it a standard part of the checkout flow.
3. **Create `push_log` table** on first deployment. Collect 30 days of cold-start data before tuning scores.
4. **Use checkout as the sole trigger** — not every heartbeat, not every tool call.
5. **Push semantic memories over episodic** (pending COS-120 merge). Episodic entries are high-noise for cross-agent proactive delivery.
6. **Trust-gate the push set** (pending COS-121 merge). Only push memories with verified provenance.
7. **Do not push for tasks with < 20 tokens of description.** These are too underspecified for meaningful retrieval.

---

## References

- Rao, R.P.N. & Ballard, D.H. (1999). Predictive coding in the visual cortex: a functional interpretation of some extra-classical receptive-field effects. *Nature Neuroscience*, 2(1), 79–87.
- Clark, A. (2015). *Surfing Uncertainty: Prediction, Action, and the Embodied Mind*. Oxford University Press.
- Collins, A.M. & Loftus, E.F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407–428.
- Wave 2 — Associative Memory & Analogical Reasoning: `~/agentmemory/research/wave2/09_associative_memory_analogical_reasoning.md`
- Wave 3 — Episodic/Semantic Bifurcation ([COS-120](/COS/issues/COS-120)): `~/agentmemory/research/wave3/01_episodic_semantic_bifurcation.md`
- Wave 3 — Provenance & Trust ([COS-121](/COS/issues/COS-121)): `~/agentmemory/research/wave3/02_provenance_trust.md`
- Wave 3 — Write Contention ([COS-122](/COS/issues/COS-122)): `~/agentmemory/research/wave3/03_write_contention.md`

---

*Document delivered to: `~/agentmemory/research/wave3/05_proactive_push.md`*
*Linked issue: [COS-124](/COS/issues/COS-124)*
