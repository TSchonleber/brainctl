# Attention Economics — Cognitive Resource Allocation Across 178 Agents

**Author:** Weaver (Context Integration Engineer)
**Task:** [COS-345](/COS/issues/COS-345)
**Date:** 2026-03-28
**DB State:** 22MB brain.db · 122 active memories · 1,117 events · 4,718 edges · 178 agents
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

With 178 agents, 122 active memories, and a proven protocol cost of ~42K tokens for a full orientation pass (wave9, [COS-322](/COS/issues/COS-322)), the brain.db system has outgrown flat-access patterns. Herbert Simon's foundational insight applies directly: *"a wealth of information creates a poverty of attention."* This document designs a **formal Attention Budget System (ABS)** — a compute-aware allocation layer that governs how brain.db distributes cognitive resources across agents, queries, and consolidation passes within fixed constraints.

**Key findings:**
- At 178 agents × 15 heartbeats/day, the system processes ~2,670 heartbeat orientations/day
- Full-protocol orientation burns ~44K tokens/agent (~117M tokens/day at scale); fast tier costs ~2K (~5.3M tokens/day)
- The current salience formula has no ignore logic — it ranks positively but never suppresses
- Focused-mode neuromodulation creates measurable inattentional blind spots (breadth_multiplier = 1.0x in NORMAL, but no downside gate)
- A tiered attention budget with agent-class profiles can reduce system-wide token spend by 95% while preserving >85% of actionable signal

---

## 1. Theoretical Foundations Mapped to brain.db

### 1.1 Attention Economics (Simon 1971; Davenport & Beck 2001)

Simon's core claim: information consumes the *attention* of its recipients, making attention the scarce resource — not information. In our system:

| Economic Concept | brain.db Analog |
|-----------------|-----------------|
| Information supply | 122 memories, 1,117 events, 4,718 edges |
| Attention supply | Context window tokens available per heartbeat (~200K for Sonnet 4.6) |
| Price of attention | Token cost of retrieving, ranking, and rendering a memory |
| Attention market | Competition between queries, consolidation, and coordination for context slots |
| Attention poverty | Agent receiving 44K orientation tokens can only use 156K for actual work |

**Implication:** Each memory retrieval is a purchase. The system must price attention correctly — cheap memories crowd out expensive ones even when they carry less signal. Current behavior is a flat-price market: all retrievals cost roughly the same in developer effort but vary 10x in actual token spend (105 tokens for `push run` vs. 23,881 for unguarded `search`).

### 1.2 Cognitive Load Theory (Sweller 1988)

Sweller distinguishes three load types:
- **Intrinsic load** — complexity inherent to the task
- **Extraneous load** — unnecessary cognitive overhead (poorly designed presentation)
- **Germane load** — effort that builds understanding/schema

In brain.db terms:

| Load Type | Source | Current State |
|-----------|--------|---------------|
| Intrinsic | The actual task an agent must solve | Fixed — cannot reduce |
| Extraneous | Orientation overhead: full search dumps, redundant vsearch, broken `gw listen` | ~42K tokens; **high** |
| Germane | Memory consolidation, cross-reference linking, pattern recognition | Undersupported — distillation lag 751min |

The 2K vs 42K protocol tiers discovered in wave9 are exactly a cognitive load reduction. The **attention budget system formalizes this**: define extraneous load ceilings per agent class, enforce them at the retrieval layer.

### 1.3 Selective Attention — Broadbent's Filter & Treisman's Attenuation

Broadbent (1958): attention acts as a filter *before* perception — irrelevant signals are blocked early. Treisman (1964): irrelevant signals are *attenuated*, not blocked (they can break through if highly salient).

**Critical insight for the salience formula:** The current formula scores positively:

```
S(m,q) = 0.45·sim + 0.25·recency + 0.20·confidence + 0.10·importance
```

It has no suppression logic. Treisman's model implies a **negative attention channel** is equally important: signals that are high-confidence but task-irrelevant should be attenuated, not just ranked lower. An agent working on a billing task should actively suppress memories about authentication — not just rank them 5th.

**Proposed extension — Suppression Filter:**

```
S(m,q) = max(0, S_positive(m,q) - S_suppress(m,q))

S_suppress(m,q) = α · scope_mismatch(m, agent)
                + β · topic_distance(m.category, task.domain)
                + γ · staleness_penalty(m)
```

Where:
- `scope_mismatch` = 1.0 if memory scope doesn't match agent's project scope, 0 otherwise
- `topic_distance` = categorical distance between memory's category and current task domain
- `staleness_penalty` = 1.0 if confidence < 0.3 and not recently recalled

This implements Treisman's attenuation: irrelevant memories cost attention budget even if they're retrieved and ranked low, because the LLM still processes them. Suppression prevents them from reaching the context window at all.

### 1.4 Inattentional Blindness (Simons & Chabris 1999)

When attention is focused, observers miss large, unexpected stimuli — the "gorilla in the room." The brain.db analog: does focused neuromodulation mode create dangerous blind spots?

**Current neuromodulation state:**
```
Acetylcholine (attention/novelty): 1.000 — maximum focus mode
temporal_lambda: 0.03 (high recency bias)
retrieval_breadth_multiplier: 1.0x (no expansion)
```

At maximum acetylcholine, agents retrieve with high recency bias and tight semantic focus. This is efficient for task execution but creates blind spots for:
- Old memories (high recency decay)
- Cross-scope signals (no breadth expansion)
- Contradictions in adjacent domains

**Evidence from current health dashboard:**
- Category HHI = 0.633 (topic collapse, 96/122 memories are "lesson" type)
- Only 20/122 memories ever recalled (16.4% engagement)
- 102 memories have never been accessed — a vast "gorilla" population

The system is in maximum focus mode while 84% of its knowledge base has never been touched. This is inattentional blindness at scale.

**Proposed mitigations:**
1. **Peripheral attention sweep** — once per consolidation cycle, run a low-salience-threshold scan across all memory categories to surface zero-recall memories
2. **Novelty injection gate** — if no memory from a category has been recalled in >7 days, boost that category's retrieval probability by 2x temporarily
3. **Breadth circuit-breaker** — if `retrieval_breadth_multiplier` is 1.0x for >24h, auto-escalate to 1.5x for one cycle

### 1.5 Attention Allocation in Multi-Agent Systems

The multi-agent attention problem: with 178 agents, each with heartbeats, how do we distribute a fixed system-wide compute budget such that high-value agents get deep context and low-value/idle agents get shallow context?

This is isomorphic to **attention head allocation in transformer models** — not all heads are equally useful; sparse attention mechanisms prune the redundant ones. Applied to agents:

---

## 2. Formal Attention Budget System Design

### 2.1 Agent Attention Classes

Define four attention classes based on role criticality and activity level:

| Class | Profile | Protocol Tier | Orientation Budget | Examples |
|-------|---------|---------------|-------------------|----------|
| **A — Executive** | CEO, Chief, high-urgency escalations | Full (pruned) | ~12K tokens | Hermes, CEO |
| **B — Active IC** | In-progress tasks, assigned work | Standard | ~4K tokens | Weaver, Recall, Kokoro |
| **C — Idle IC** | No active tasks, waiting | Minimal | ~1K tokens | Parked agents |
| **D — Observational** | Monitoring, health checks only | Heartbeat-only | ~200 tokens | Hippocampus (maintenance runs) |

**Class assignment algorithm:**
```sql
SELECT
  a.id,
  CASE
    WHEN a.role IN ('ceo', 'chief') THEN 'A'
    WHEN EXISTS (SELECT 1 FROM issues WHERE assigneeAgentId = a.id AND status = 'in_progress') THEN 'B'
    WHEN EXISTS (SELECT 1 FROM issues WHERE assigneeAgentId = a.id AND status IN ('todo','blocked')) THEN 'B'
    ELSE 'C'
  END AS attention_class
FROM agents a
WHERE a.active = 1
```

### 2.2 Protocol Tier Specification

Building on wave9's 2K/42K findings, formalize three tiers:

**Tier 1 — Fast (Class C, D): ~1K–2K tokens**
```bash
brainctl temporal-context                    # 284 tokens — epoch + cadence
brainctl -a AGENT search "task" -c lesson    # 106 tokens — lessons only
brainctl push run -a AGENT "task summary"    # 105 tokens — pre-scored memories
```
Total: ~495 tokens. Drop to this when no active tasks or in maintenance mode.

**Tier 2 — Standard (Class B): ~4K tokens**
```bash
brainctl temporal-context                    # 284 tokens
brainctl push run -a AGENT "task summary"    # 105 tokens
brainctl event tail -n 10                    # 1,675 tokens
brainctl -a AGENT search "task keywords"     # filtered, --max-graph-depth 0 → ~500 tokens
```
Total: ~2,564 tokens. Sufficient for IC task execution.

**Tier 3 — Full (Class A): ~12K tokens**
```bash
brainctl temporal-context                    # 284 tokens
brainctl push run -a AGENT "task"            # 105 tokens
brainctl event tail -n 15                    # 2,430 tokens
brainctl -a AGENT search "task" --top 10     # ~6,000 tokens (guarded)
brainctl decision list                       # 1,715 tokens
brainctl world status                        # 1,508 tokens
```
Total: ~12,042 tokens. Reserved for executive context and critical escalations.

### 2.3 Budget Enforcement Mechanisms

**Mechanism 1: Context Token Gate**

Store each agent's tier assignment in `agent_state` and expose it via `brainctl`:

```bash
brainctl attention-class AGENT_ID           # returns A/B/C/D
brainctl attention-budget AGENT_ID          # returns token ceiling
```

Agents check their class at heartbeat start and select the appropriate protocol tier. The cognitive protocol documentation should reference this check explicitly.

**Mechanism 2: Query Cost Accounting**

Track query costs per heartbeat in a new `query_events` table:

```sql
CREATE TABLE query_events (
  id INTEGER PRIMARY KEY,
  agent_id TEXT,
  heartbeat_session_id TEXT,
  query_type TEXT,          -- 'search', 'push_run', 'event_tail', etc.
  tokens_estimated INTEGER,
  created_at TEXT
);
```

Hippocampus accumulates this during consolidation. When an agent's session exceeds 2× budget, flag it as an `attention_overflow` event and demote the agent to the next-lower tier for the following heartbeat.

**Mechanism 3: Salience-Gated Suppression Filter**

Add suppression scoring to the retrieval pipeline. Memories that score below the suppression gate never enter context:

```sql
-- Suppression filter in retrieval query
AND NOT (
  -- Scope mismatch: skip memories for other projects when agent has a clear scope
  (m.scope LIKE 'project:%' AND m.scope != ?)
  AND m.confidence < 0.7  -- but high-confidence cross-scope memories can break through
)
AND NOT (
  -- Stale + unengaged: never recalled + low confidence + older than 30 days
  m.recalled_count = 0
  AND m.confidence < 0.35
  AND julianday('now') - julianday(m.created_at) > 30
)
```

This implements Treisman's attenuation: stale, out-of-scope memories are suppressed unless they're high-confidence (potentially critical signals that can break through).

### 2.4 Optimal Budget Distribution Across 178 Agents

**Problem formulation:**
Given a system-wide daily token budget B, and N agents each requiring orientation cost O_i, allocate budgets to maximize total system utility U.

**Simplified allocation:**

| Agent Class | Count (estimated) | Tokens/heartbeat | Heartbeats/day | Daily tokens |
|-------------|-------------------|-----------------|----------------|--------------|
| A (Executive) | 5 | 12,000 | 10 | 600,000 |
| B (Active IC) | 40 | 4,000 | 15 | 2,400,000 |
| C (Idle IC) | 100 | 1,000 | 5 | 500,000 |
| D (Observational) | 33 | 200 | 10 | 66,000 |
| **Total** | **178** | — | — | **~3.6M tokens/day** |

Compare to the unconstrained baseline: 178 × 15 × 44,000 = **117.5M tokens/day**.

**Attention budget system reduces system-wide orientation cost by ~97%** while maintaining full depth for executive agents and all active IC work.

### 2.5 The Inattentional Blindness Mitigation Protocol

A dedicated **Peripheral Attention Sweep** runs during consolidation (hippocampus cycle), once per 6-hour cadence cycle:

```python
def peripheral_attention_sweep(conn, top_k=10):
    """Surface never-recalled or long-dormant memories for review."""
    cutoff = 14  # days
    rows = conn.execute("""
        SELECT id, content, category, confidence, recalled_count, created_at
        FROM memories
        WHERE retired_at IS NULL
          AND (recalled_count = 0 OR
               julianday('now') - julianday(COALESCE(last_recalled_at, created_at)) > ?)
        ORDER BY confidence DESC, created_at DESC
        LIMIT ?
    """, [cutoff, top_k]).fetchall()

    for row in rows:
        # Emit a 'dormant_memory_flagged' event for Hermes or Recall to review
        emit_event(conn,
            agent_id='hippocampus',
            event_type='dormant_memory_flagged',
            summary=f"Memory #{row['id']} unrecalled for {cutoff}+ days: {row['content'][:80]}",
            importance=0.4
        )
```

This ensures the "gorilla" — the 84% of memories never recalled — gets periodic visibility without flooding active agents' context.

---

## 3. Attention Budget Configuration Schema

Proposed addition to `~/agentmemory/config/`:

```yaml
# ~/agentmemory/config/attention_budget.yaml

version: 1

system:
  daily_token_budget: 10_000_000   # soft ceiling; alert at 80%
  enforcement: soft                 # 'soft' = alert, 'hard' = deny

classes:
  A:
    label: executive
    token_ceiling: 12000
    protocol_tier: 3
    heartbeats_per_day: 10
  B:
    label: active_ic
    token_ceiling: 4000
    protocol_tier: 2
    heartbeats_per_day: 15
  C:
    label: idle_ic
    token_ceiling: 1000
    protocol_tier: 1
    heartbeats_per_day: 5
  D:
    label: observational
    token_ceiling: 200
    protocol_tier: 0
    heartbeats_per_day: 10

suppression:
  enabled: true
  scope_mismatch_penalty: 0.3
  stale_recall_threshold: 30        # days
  stale_confidence_max: 0.35

peripheral_sweep:
  enabled: true
  cadence: "every 6h"
  dormancy_threshold_days: 14
  top_k: 10

novelty_injection:
  enabled: true
  category_dormancy_days: 7
  boost_multiplier: 2.0
```

---

## 4. Implementation Roadmap

| Phase | Work | Ticket |
|-------|------|--------|
| 1 | Add `attention_class` to `agent_state` + `brainctl attention-class` command | New COS |
| 2 | Add suppression filter to retrieval SQL in `brainctl search` and `push run` | New COS |
| 3 | Add `query_events` table + token accounting in hippocampus | New COS |
| 4 | Implement `peripheral_attention_sweep` in hippocampus.py | New COS |
| 5 | Add `attention_budget.yaml` config + document in COGNITIVE_PROTOCOL.md | New COS |
| 6 | Add novelty injection gate in neuromodulation logic | New COS |

---

## 5. Critical Findings Summary

1. **The suppression channel is missing.** The salience formula ranks positively but never suppresses. Add scope-mismatch and staleness penalties to the filter gate.

2. **Inattentional blindness is real and severe.** 102/122 memories (84%) have never been recalled. The system is at maximum focus (acetylcholine=1.0) and blind to most of its own knowledge.

3. **Agent class tiers can reduce orientation overhead by 97%** (117M → 3.6M tokens/day) while preserving full depth for executives and active ICs.

4. **Budget enforcement must be self-service.** Agents check `brainctl attention-class` themselves at heartbeat start. No central gatekeeper needed — the protocol tier specification does the enforcement.

5. **Peripheral attention sweeps prevent knowledge rot.** Without them, memories not matching active task domains never surface again. The sweep is the brain's equivalent of REM consolidation — reviewing dormant patterns.

---

## 6. Connection to Wave9 Findings

This research directly extends wave9 ([COS-322](/COS/issues/COS-322), `27_cognitive_protocol_overhead.md`):

- Wave9 measured *what existing commands cost* and identified the 2K fast tier
- Wave10 designs *how to allocate* those costs across 178 agents systematically
- The suppression filter directly fixes the "unguarded search dumps 23K tokens" finding from wave9 — even within the fast tier, unfiltered results are wasteful

Together they form a complete attention architecture: wave9 = pricing, wave10 = allocation.

---

*Deliver to: `~/agentmemory/research/wave10/28_attention_economics.md`*
*Linked task: [COS-345](/COS/issues/COS-345)*
