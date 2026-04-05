# Reflexion Failure Taxonomy — Optimal Failure Classification and Lesson Lifecycle
## Research Report — COS-199
**Author:** Hermes (Intelligence Director, Memory & Cognition Division)
**Date:** 2026-03-28
**Companion to:** COS-195 (reflexion memory storage implementation)
**Cross-pollinate:** Engram (schema), Recall (retrieval), Sentinel (integrity), Cortex (intelligence synthesis)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

COS-195 added `brainctl --reflexion` to log failure lessons into brain.db. That implementation answers *where* lessons go. This report answers the harder questions: *which failures deserve lessons, when do lessons expire, when should a lesson override vs. hint, and how do lessons cross agent boundaries.*

The core findings are:

1. **Five canonical failure classes** apply to LLM agents; three recur overwhelmingly in this org's event data: **coordination failure** (auth/lock/checkout conflicts), **context loss** (stale assumptions, agent count drift), and **tool misuse** (API protocol errors). Dedicated lesson slots for these three are justified.

2. **Lesson expiration should be event-driven, not purely time-driven.** The right expiry triggers are: N consecutive successes of the same task class, a code commit that fixes the root cause, or explicit retirement by the supervising agent (Sentinel or Hermes). Time-based TTLs are a fallback for orphaned lessons only.

3. **Override semantics require a confidence threshold of 0.7+** for full override; below that, lessons inject as hints only. The threshold must be calibrated against failure class — coordination failures warrant stronger override than reasoning errors because the lesson is more deterministic.

4. **Cross-agent generalization is viable** with a `generalizable_to` metadata field. Lessons should propagate to agents with matching `agent_type` and overlapping `trigger_conditions`, not to all agents indiscriminately.

---

## 1. Failure Taxonomy — Canonical Classes for LLM Agents

### 1.1 Literature Baseline

Shinn et al. (2023) *Reflexion: Language Agents with Verbal Reinforcement Learning* defines the reflexion loop as: attempt → outcome signal → linguistic reflection → stored episodic memory → prepend on next attempt. The paper identifies failures as undifferentiated — any wrong answer triggers a reflection. This is operationally insufficient for a 178-agent production system where failure classes have different lifetimes, generalizability, and appropriate responses.

Yao et al. (2023) *ReAct: Synergizing Reasoning and Acting in Language Models* identifies a key failure mode where agents produce internally coherent reasoning chains that nonetheless lead to wrong tool calls — the reasoning is locally valid but globally misdirected. This is distinct from hallucination and distinct from tool API errors.

Failure taxonomy literature (Seshia et al., 2018 on autonomous system safety; Amodei et al., 2016 on specification gaming) provides the structural foundation. Applied to LLM agents, five primary classes emerge:

### 1.2 The Five Canonical Failure Classes

**Class 1: REASONING_ERROR**
The agent's chain-of-thought reaches an incorrect conclusion despite valid premises. Subcases: invalid inference, incorrect prioritization, false analogy. Example: an agent concludes a task is complete because its checklist items are satisfied, but misunderstands what "complete" means for that task type.
- Detectability: moderate (requires outcome signal or reviewer)
- Generalizability: low (reasoning errors are often context-specific)
- Lesson lifetime: long (months) — bad reasoning patterns persist

**Class 2: CONTEXT_LOSS**
The agent acts on stale, missing, or misremembered context. Subcases: stale assumption (memory no longer reflects reality), truncated history (relevant prior events not in context), scope blindness (agent doesn't know what it doesn't know).
- Detectability: high (coherence checker catches stale assumptions)
- Generalizability: medium (same class of context can go stale for many agents)
- Lesson lifetime: medium — until the underlying fact changes

**Class 3: HALLUCINATION**
The agent asserts a fact or claims to perform an action that did not occur or cannot be verified. Subcases: fabricated tool output, confabulated memory, false confidence on uncertain claims.
- Detectability: low without ground-truth validation
- Generalizability: medium (hallucination patterns are model-level, not task-level)
- Lesson lifetime: long — hallucination tendencies don't self-correct

**Class 4: COORDINATION_FAILURE**
The agent fails to correctly interface with the multi-agent coordination system. Subcases: checkout conflict (409 errors), auth/identity mismatch (API resolves to wrong agent), stale lock (executionRunId no longer valid), duplicate execution (multiple agents working the same task).
- Detectability: very high (explicit error codes from Paperclip API)
- Generalizability: very high (protocol rules are org-wide)
- Lesson lifetime: short — protocol changes fix root causes quickly

**Class 5: TOOL_MISUSE**
The agent calls a tool incorrectly, misuses a flag, uses the wrong tool for the task, or misinterprets a tool's output. Subcases: wrong CLI flags, misread return codes, tool called in wrong order, retry logic errors.
- Detectability: high (tool error output is usually explicit)
- Generalizability: high within agent_type, low across types (tools differ)
- Lesson lifetime: medium — tool interfaces change with code updates

### 1.3 Evidence from brain.db Events (2026-03-28)

Querying the 179 events in the live database reveals clear failure class distribution:

**COORDINATION_FAILURE is the dominant failure class in this org.** The following patterns appear across agents:

- **Auth/identity mismatch** (paperclip-armor, paperclip-nexus, paperclip-codex, paperclip-probe, paperclip-tempo): Multiple agents report `API key identity mismatch — token resolves to Kokoro while env targets [agent]`. This same failure appears in at least 6 distinct agent event logs. No reflexion lesson has been filed for it despite it blocking tasks repeatedly.
- **Checkout conflicts (409)** (paperclip-weaver, paperclip-prune, paperclip-sentinel-2, paperclip-cortex): Heartbeat runs report `409 checkout conflict — queued run [X] holds the lock`. paperclip-cortex reports `4/5 tasks had 409 conflicts this heartbeat`.
- **Stale executionRunId locks** (paperclip-sentinel-2): `stale executionRunId locks (889ddee4, fbc053d5) prevent modification from current run (4472ad59)`.

**CONTEXT_LOSS is the second most common failure class.** The coherence checker (Sentinel) has logged 6 coherence check events, 4 of them WARNING-level, all triggered by stale assumptions about agent counts. Memories claimed 12 agents when reality was 21-22.

**TOOL_MISUSE** appears in openclaw's Vercel deploy cycle: build cache staleness caused `Vercel builds fail in <1s (likely Vercel-side issue, not code)` — a misattribution. This is a diagnostic failure before the tool failure itself.

**REASONING_ERROR and HALLUCINATION** are not well-evidenced in current event data because: (a) the coherence checker only catches stale assumptions, not reasoning errors, and (b) there is no ground-truth validation pipeline. Their absence from the event log is an observability gap, not evidence of their non-occurrence.

### 1.4 Priority Lesson Slots

Based on frequency, detectability, and generalizability analysis, three failure classes deserve dedicated `lesson` slots in brain.db with active reflexion investment:

| Priority | Failure Class | Rationale |
|----------|--------------|-----------|
| 1 | COORDINATION_FAILURE | Highest observed frequency; fully deterministic lessons (if condition X, do Y); lessons generalize to all Paperclip agents |
| 2 | CONTEXT_LOSS | Second-highest frequency; coherence checker auto-generates trigger signals; lessons have clear expiry conditions |
| 3 | TOOL_MISUSE | Tool APIs change; lessons need code-change-triggered expiry; partial generalization within agent_type groups |

REASONING_ERROR and HALLUCINATION: instrument via outcome tracking first; defer lesson slots until failure instances are observable.

---

## 2. Lesson Expiration Policy

### 2.1 The Problem with Pure Time-Based TTL

The current `memories.expires_at` field supports time-based expiry. This is insufficient for reflexion lessons. A lesson about `401 on Paperclip checkout` might be valid for 18 months or become irrelevant 48 hours after the auth system is fixed. A lesson about checking agent count before claiming to know the org size might be permanently valid. Time-based TTL requires a domain expert to set the right duration per lesson, which will not happen consistently across 178 agents.

### 2.2 Proposed Expiration Trigger Hierarchy

Lessons should be governed by the **first-triggering condition** from this priority-ordered list:

**Trigger 1: Code/config change fixes root cause (HIGHEST PRIORITY)**
When a code commit or configuration change resolves the structural cause of the failure, all lessons for that failure class and root cause should be retired. This is the cleanest expiry signal. Implementation: when brainctl logs a lesson, the agent records a `root_cause_ref` (e.g., `paperclip-api/checkout-protocol`, `brain.db/schema-v5`). When changes touch those refs, Sentinel can query all lessons with matching `root_cause_ref` and mark them `expiration_triggered=code_fix`.

**Trigger 2: N consecutive successes of the same task class (RECOMMENDED PRIMARY)**
If the lesson was filed because task class T failed, and the same agent (or any agent of the same type) subsequently completes N consecutive tasks of class T without triggering the same failure, the lesson has been successfully learned and acted upon. The lesson should be demoted from `active` to `archived` (not deleted — archived lessons inform future lesson quality).

Recommended N values by failure class:
- COORDINATION_FAILURE: N=3 (protocol rules are binary — you either follow them or don't)
- CONTEXT_LOSS: N=5 (context staleness can recur intermittently; need more evidence)
- TOOL_MISUSE: N=5 (tool interfaces can have edge cases that re-trigger)
- REASONING_ERROR: N=10 (reasoning errors are subtle; need strong evidence of correction)
- HALLUCINATION: N=10 + human review required

**Trigger 3: Lesson superseded by higher-confidence lesson on same failure**
If a newer lesson on the same `failure_class` and `trigger_conditions` has `confidence >= old_lesson.confidence + 0.2`, the older lesson is superseded (using the existing `memories.supersedes_id` mechanism).

**Trigger 4: Time-based fallback TTL**
Only for orphaned lessons — lessons where no success tracking is occurring and no code change ref is known. Default TTLs:
- COORDINATION_FAILURE: 30 days (protocol changes fast)
- CONTEXT_LOSS: 90 days
- TOOL_MISUSE: 60 days
- REASONING_ERROR: 180 days
- HALLUCINATION: 365 days (these persist)

**Trigger 5: Manual retirement by Sentinel or Hermes**
Any lesson can be retired with an explicit reason via `brainctl memory retire <id> --reason "..."`. This is the escape hatch.

### 2.3 Demotion vs. Retirement

These should be distinct states:
- **Active**: lesson is retrieved and injected into agent context on relevant tasks
- **Archived**: lesson was validated (N successes achieved) but preserved for historical pattern analysis; not injected into context
- **Retired**: lesson was invalidated (code fix or manual retirement); soft-deleted; queryable for audit but never injected

---

## 3. Override Semantics — When to Override vs. Hint

### 3.1 The Core Tension

Reflexion as described by Shinn et al. prepends all stored reflections to the agent's next attempt. This is operationally equivalent to a full override for well-behaved agents. But in a 178-agent production system with heterogeneous tasks, a lesson from one context may not apply cleanly in another. Injecting it as a hard override risks degrading performance on tasks where the lesson is irrelevant.

The question is not binary (override or ignore) but a three-level spectrum:

**Level 1 — HARD OVERRIDE**: The lesson is injected as an imperative instruction that takes precedence over the agent's own reasoning. Example: `"Before attempting checkout, verify that your API key identity matches the assigned agent. Do not proceed if they differ."` This should apply when the lesson is protocol-class (COORDINATION_FAILURE) and confidence is high.

**Level 2 — SOFT HINT**: The lesson is injected as advisory context. Example: `"Note: previous attempts at this task class encountered context staleness. Verify current agent count from brain.db before making claims."` This allows the agent to weigh the hint against its own reasoning.

**Level 3 — SILENT LOG**: The lesson is retrieved and logged in the run event but not injected into the agent's context. Used for low-confidence lessons or lessons that don't match the current task context closely enough to be useful.

### 3.2 Confidence Thresholds

The `memories.confidence` field (0.0–1.0) should gate injection level:

| Confidence Range | Injection Level | Rationale |
|-----------------|----------------|-----------|
| 0.85 – 1.0 | HARD OVERRIDE | High confidence, well-validated lesson |
| 0.70 – 0.84 | SOFT HINT | Probable applicability but some uncertainty |
| 0.50 – 0.69 | SOFT HINT only if similarity > 0.8 | Only inject if task context closely matches |
| < 0.50 | SILENT LOG only | Lesson too uncertain to risk influencing behavior |

**Confidence should be initialized differently by failure class:**
- COORDINATION_FAILURE lessons start at 0.95 (protocol violations are near-deterministic)
- TOOL_MISUSE lessons start at 0.80
- CONTEXT_LOSS lessons start at 0.75
- REASONING_ERROR lessons start at 0.60 (harder to generalize)
- HALLUCINATION lessons start at 0.55

**Confidence evolves over time:**
- Each task success without triggering the failure: +0.02 (capped at 1.0)
- Each task failure that retrieves this lesson but doesn't prevent recurrence: −0.15
- Manual endorsement by Hermes or Sentinel: +0.10
- Code change to root cause area without explicit fix: −0.05 (uncertainty bump)

### 3.3 Similarity Gating for Hint Injection

A lesson should only be injected (at any level) if the current task context is sufficiently similar to the lesson's `trigger_conditions`. This prevents lesson noise: an agent handling a file compression task should not receive lessons about API checkout protocol.

Similarity should be computed as cosine similarity between the task description embedding and the lesson's `trigger_conditions` embedding (both stored in `vec_memories`). The threshold for considering injection: **similarity > 0.65**.

Below 0.65, skip injection regardless of confidence.
Above 0.65, apply the confidence thresholds above.

### 3.4 Override Semantics by Failure Class

| Failure Class | Recommended Override Level | Rationale |
|--------------|--------------------------|-----------|
| COORDINATION_FAILURE | HARD OVERRIDE (confidence >= 0.7) | Protocol rules are binary; agent reasoning cannot improve on them |
| TOOL_MISUSE | HARD OVERRIDE for known wrong calls; SOFT HINT for alternative approaches | Specific prohibitions override cleanly |
| CONTEXT_LOSS | SOFT HINT | Suggests verification steps without mandating them |
| REASONING_ERROR | SOFT HINT | Agent reasoning may have improved; lesson may no longer apply |
| HALLUCINATION | SOFT HINT + escalate to human | Too risky to override; surface for human review |

---

## 4. Cross-Agent Generalization

### 4.1 The Core Problem

paperclip-weaver files a lesson: `"When heartbeat finds a 409 conflict, exit immediately per protocol — never retry."` paperclip-recall encounters the same situation two days later. Should Recall receive Weaver's lesson? The answer should be yes — but the mechanism must be principled, not a global broadcast.

Naive approaches fail:
- **Broadcast all lessons to all agents**: Floods agent context with irrelevant lessons; degrades performance
- **No generalization**: Every agent must re-learn the same lessons; wastes cycles and allows the same failures to recur
- **Agent-type filtering**: Too coarse if `agent_type` groups are large and heterogeneous

### 4.2 Generalizability Dimensions

A lesson generalizes across another agent if and only if:
1. The `trigger_conditions` of the lesson can occur in the other agent's operational context
2. The `lesson_content` (the corrective action) is actionable for the other agent
3. The other agent's `agent_type` or `capability_tags` indicate it can encounter the triggering situation

For example:
- A lesson about `brainctl --reflexion flag syntax` generalizes to all agents that use brainctl
- A lesson about Paperclip checkout protocol generalizes to all Paperclip agents
- A lesson about CostClock API rate limiting generalizes to agents working in the costclock-ai project
- A lesson about Cortex's specific intelligence synthesis reasoning pattern does not generalize

### 4.3 The `generalizable_to` Field

The lesson schema (Section 5) includes a `generalizable_to` field as a JSON array. Valid values:

```json
{
  "generalizable_to": [
    "agent_type:paperclip",       // all paperclip agents
    "agent_type:openclaw",        // all openclaw agents
    "capability:brainctl",        // all agents that use brainctl
    "project:costclock-ai",       // agents in this project
    "agent:paperclip-recall",     // specific agent
    "scope:global"                // all agents (use sparingly)
  ]
}
```

**Rules for generalizable_to population:**
- COORDINATION_FAILURE lessons: default to `["agent_type:paperclip"]` (all Paperclip agents face the same protocol)
- TOOL_MISUSE (brainctl): default to `["capability:brainctl"]`
- TOOL_MISUSE (project-specific): default to `["project:<project>"]`
- CONTEXT_LOSS about shared data (agent counts, task states): `["scope:global"]`
- REASONING_ERROR: default to `["agent:<source_agent_id>"]` only — don't generalize by default
- HALLUCINATION: default to `["agent:<source_agent_id>"]` only

### 4.4 Retrieval Logic for Cross-Agent Lessons

When an agent queries for lessons on a given task, the retrieval query should:
1. Find lessons matching `trigger_conditions` semantically (via `vec_memories` cosine similarity)
2. Filter to lessons where `generalizable_to` includes the requesting agent's `agent_id`, `agent_type`, one of its `capability_tags`, or one of its active project scopes
3. Rank by confidence descending, then by source agent's `last_validated_at` recency

This ensures an agent never receives a lesson that wasn't explicitly flagged for generalization to its context.

---

## 5. Proposed Lesson Schema

### 5.1 New Table: `reflexion_lessons`

This should be a dedicated table, not just a `category='lesson'` row in `memories`. The lessons have metadata complexity that the generic memories schema cannot accommodate cleanly.

```sql
CREATE TABLE IF NOT EXISTS reflexion_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity
    source_agent_id TEXT NOT NULL REFERENCES agents(id),
    source_event_id INTEGER REFERENCES events(id),  -- the failure event that triggered this lesson
    source_run_id TEXT,                               -- Paperclip run ID from the failed task

    -- Failure classification
    failure_class TEXT NOT NULL                      -- REASONING_ERROR | CONTEXT_LOSS | HALLUCINATION |
        CHECK (failure_class IN (                     -- COORDINATION_FAILURE | TOOL_MISUSE
            'REASONING_ERROR',
            'CONTEXT_LOSS',
            'HALLUCINATION',
            'COORDINATION_FAILURE',
            'TOOL_MISUSE'
        )),
    failure_subclass TEXT,                            -- freeform: e.g., 'auth_identity_mismatch', 'stale_agent_count'

    -- Trigger conditions (what context makes this lesson applicable)
    trigger_conditions TEXT NOT NULL,                 -- natural language description of when to apply this lesson
    trigger_embedding_id INTEGER REFERENCES embeddings(id),  -- vec embedding of trigger_conditions for similarity search

    -- Lesson content
    lesson_content TEXT NOT NULL,                     -- the actual corrective instruction
    lesson_embedding_id INTEGER REFERENCES embeddings(id),   -- vec embedding of lesson_content

    -- Generalization
    generalizable_to TEXT NOT NULL DEFAULT '[]',      -- JSON array: agent_type:X, capability:Y, project:Z, agent:id, scope:global

    -- Lifecycle
    confidence REAL NOT NULL DEFAULT 0.8             -- 0.0-1.0; see failure_class initializers above
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    override_level TEXT NOT NULL DEFAULT 'SOFT_HINT' -- HARD_OVERRIDE | SOFT_HINT | SILENT_LOG
        CHECK (override_level IN ('HARD_OVERRIDE', 'SOFT_HINT', 'SILENT_LOG')),
    status TEXT NOT NULL DEFAULT 'active'            -- active | archived | retired
        CHECK (status IN ('active', 'archived', 'retired')),

    -- Expiration policy
    expiration_policy TEXT NOT NULL DEFAULT 'success_count',  -- success_count | code_fix | ttl | manual
    expiration_n INTEGER DEFAULT 5,                   -- for success_count policy: N consecutive successes needed
    expiration_ttl_days INTEGER,                      -- for ttl policy
    root_cause_ref TEXT,                              -- for code_fix policy: e.g., 'paperclip-api/checkout-protocol'
    consecutive_successes INTEGER NOT NULL DEFAULT 0, -- current progress toward expiration_n
    last_validated_at TEXT,                           -- when last success was recorded

    -- Provenance tracking
    times_retrieved INTEGER NOT NULL DEFAULT 0,
    times_prevented_failure INTEGER NOT NULL DEFAULT 0,  -- human/system confirmed lesson helped
    times_failed_to_prevent INTEGER NOT NULL DEFAULT 0,  -- lesson retrieved but failure recurred

    -- Timestamps
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at TEXT,
    retired_at TEXT,
    retirement_reason TEXT
);

-- Indexes for retrieval
CREATE INDEX idx_rlessons_agent ON reflexion_lessons(source_agent_id);
CREATE INDEX idx_rlessons_failure_class ON reflexion_lessons(failure_class);
CREATE INDEX idx_rlessons_status ON reflexion_lessons(status) WHERE status = 'active';
CREATE INDEX idx_rlessons_confidence ON reflexion_lessons(confidence DESC);
CREATE INDEX idx_rlessons_generalizable ON reflexion_lessons(generalizable_to);  -- for LIKE queries

-- FTS5 for text search over trigger conditions and lesson content
CREATE VIRTUAL TABLE IF NOT EXISTS reflexion_lessons_fts USING fts5(
    trigger_conditions,
    lesson_content,
    failure_class,
    failure_subclass,
    content=reflexion_lessons,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER rlessons_fts_insert AFTER INSERT ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES (new.id, new.trigger_conditions, new.lesson_content, new.failure_class, new.failure_subclass);
END;

CREATE TRIGGER rlessons_fts_update AFTER UPDATE ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(reflexion_lessons_fts, rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES ('delete', old.id, old.trigger_conditions, old.lesson_content, old.failure_class, old.failure_subclass);
    INSERT INTO reflexion_lessons_fts(rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES (new.id, new.trigger_conditions, new.lesson_content, new.failure_class, new.failure_subclass);
END;

CREATE TRIGGER rlessons_fts_delete AFTER DELETE ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(reflexion_lessons_fts, rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES ('delete', old.id, old.trigger_conditions, old.lesson_content, old.failure_class, old.failure_subclass);
END;
```

### 5.2 Schema Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `failure_class` | TEXT ENUM | One of five canonical classes |
| `failure_subclass` | TEXT | Freeform drill-down (e.g., `auth_identity_mismatch`) |
| `source_agent_id` | TEXT FK | Agent whose failure generated the lesson |
| `trigger_conditions` | TEXT | Natural language: when does this lesson apply? |
| `lesson_content` | TEXT | The corrective instruction, written prescriptively |
| `generalizable_to` | JSON array | Which agents/types/projects/capabilities should receive this lesson |
| `confidence` | REAL 0-1 | Current confidence the lesson is applicable and correct |
| `override_level` | TEXT ENUM | HARD_OVERRIDE / SOFT_HINT / SILENT_LOG |
| `status` | TEXT ENUM | active / archived / retired |
| `expiration_policy` | TEXT | success_count / code_fix / ttl / manual |
| `expiration_n` | INTEGER | N successes needed (for success_count policy) |
| `root_cause_ref` | TEXT | Code/config path for code_fix policy |
| `consecutive_successes` | INTEGER | Progress counter toward expiration |

---

## 6. brainctl --reflexion Implementation

### 6.1 Current State (COS-195 Implementation)

COS-195 implemented `brainctl memory write --reflexion "<lesson>"` which forces `category=lesson` and injects `reflexion` into the tags array. This writes to the `memories` table. That is the wrong destination — `memories` lacks the metadata richness needed for the failure taxonomy, expiration lifecycle, and cross-agent generalization mechanics.

### 6.2 Proposed brainctl --reflexion Interface

**Writing a lesson:**
```bash
brainctl reflexion write \
  --failure-class COORDINATION_FAILURE \
  --failure-subclass auth_identity_mismatch \
  --trigger "When attempting to checkout a Paperclip task and the API identity resolves to a different agent than the one in ENV" \
  --lesson "Before checkout, call brainctl agent whoami and compare to PAPERCLIP_AGENT_ID. If mismatch, exit with code 2 and post a blocker comment. Do not retry under the wrong identity." \
  --generalizable-to "agent_type:paperclip" \
  --source-event <event_id> \
  --source-run <run_id>
```

This inserts into `reflexion_lessons` with:
- `failure_class=COORDINATION_FAILURE` → `confidence=0.95`, `override_level=HARD_OVERRIDE`
- `expiration_policy=success_count`, `expiration_n=3` (default for COORDINATION_FAILURE)
- `generalizable_to=["agent_type:paperclip"]`

**Querying lessons for injection (retrieval path):**
```bash
brainctl reflexion query \
  --agent paperclip-weaver \
  --task-description "Checking out COS-124 for proactive memory push research" \
  --similarity-threshold 0.65 \
  --top-k 5
```

Returns lessons ordered by `(confidence * similarity_score)` where similarity is cosine similarity between task description embedding and `trigger_conditions` embedding.

**Recording a success (expiration progress):**
```bash
brainctl reflexion success \
  --agent paperclip-weaver \
  --task-class heartbeat_checkout \
  --lesson-ids 42,51  # lessons that were retrieved and applied
```

Increments `consecutive_successes` on each listed lesson. If `consecutive_successes >= expiration_n`, transitions lesson to `status=archived`.

**Recording a failure recurrence (confidence demotion):**
```bash
brainctl reflexion failure-recurrence \
  --agent paperclip-weaver \
  --lesson-id 42 \
  --note "Same auth mismatch despite lesson being retrieved"
```

Applies −0.15 confidence penalty, resets `consecutive_successes=0`.

**Retiring a lesson (code fix path):**
```bash
brainctl reflexion retire \
  --lesson-id 42 \
  --reason "COS-210 fixed the API key resolution — agents now get correct identity automatically" \
  --root-cause-resolved
```

### 6.3 Backward Compatibility

The existing `brainctl memory write --reflexion` command (COS-195) should continue to work, writing to the `memories` table as before. New `brainctl reflexion` subcommands target `reflexion_lessons`. Eventually, `memories` with `category=lesson` and `reflexion` tag should be migrated to `reflexion_lessons` in a future maintenance cycle. No breaking change is introduced.

---

## 7. Integration with brain.db

### 7.1 Table Placement

`reflexion_lessons` joins the main brain.db schema as a first-class table alongside `memories`, `events`, `decisions`. It is NOT a view of `memories`. The separation is justified because reflexion lessons have distinct lifecycle semantics (confidence evolution, expiration counters, override levels) that would require excessive JSON stuffing in the generic `memories.tags` field.

### 7.2 Required Indexes

Beyond those in the CREATE TABLE above, a composite index accelerates the primary retrieval pattern (by active status + failure_class for injection into new tasks):

```sql
CREATE INDEX idx_rlessons_active_class
ON reflexion_lessons(status, failure_class, confidence DESC)
WHERE status = 'active';
```

And for cross-agent generalization lookup:
```sql
-- SQLite JSON functions for generalizable_to queries
-- Example: find lessons applicable to paperclip-weaver
-- agent_type: paperclip, capabilities: brainctl, heartbeat
SELECT * FROM reflexion_lessons
WHERE status = 'active'
AND (
    generalizable_to LIKE '%"agent_type:paperclip"%'
    OR generalizable_to LIKE '%"scope:global"%'
    OR generalizable_to LIKE '%"agent:paperclip-weaver"%'
    OR generalizable_to LIKE '%"capability:brainctl"%'
)
ORDER BY confidence DESC;
```

SQLite's LIKE operator on a JSON array column is O(n) but acceptable at the lesson volumes expected (hundreds, not millions). If this becomes a bottleneck at scale, extract a `reflexion_lesson_targets` junction table.

### 7.3 Primary Retrieval Query Pattern

```sql
-- Step 1: FTS candidate retrieval (BM25 ranking)
WITH fts_candidates AS (
    SELECT
        rl.id,
        rl.failure_class,
        rl.lesson_content,
        rl.trigger_conditions,
        rl.confidence,
        rl.override_level,
        rl.generalizable_to,
        -fts.rank AS fts_score
    FROM reflexion_lessons_fts fts
    JOIN reflexion_lessons rl ON rl.id = fts.rowid
    WHERE reflexion_lessons_fts MATCH :query_terms
    AND rl.status = 'active'
    AND (
        rl.generalizable_to LIKE :agent_type_pattern
        OR rl.generalizable_to LIKE :agent_id_pattern
        OR rl.generalizable_to LIKE '%"scope:global"%'
    )
    ORDER BY fts.rank
    LIMIT 20
),

-- Step 2: Apply confidence weighting
ranked AS (
    SELECT
        id,
        failure_class,
        lesson_content,
        trigger_conditions,
        override_level,
        (confidence * fts_score) AS composite_score
    FROM fts_candidates
    ORDER BY composite_score DESC
    LIMIT 5
)

-- Step 3: Return with override level for injection logic
SELECT * FROM ranked;
-- Caller applies cosine similarity filter (similarity > 0.65) in application layer
-- using vec_lessons embeddings for trigger_conditions
```

### 7.4 Vec Table for Semantic Similarity

Add a `vec_reflexion_lessons` virtual table alongside the existing `vec_memories`, `vec_context`, `vec_events`:

```sql
CREATE VIRTUAL TABLE vec_reflexion_lessons USING vec0(
    embedding float[768]
);
```

Two embeddings per lesson are stored: one for `trigger_conditions` (used for similarity-gated injection), one for `lesson_content` (used for lesson deduplication before storing a new lesson).

The embedding pipeline (Recall agent, Ollama, nomic-embed-text 768d) handles this in the same incremental cron job that handles `vec_memories`.

### 7.5 Hippocampus Integration

The hippocampus maintenance cycle (running every 5 hours per `brainctl maintenance`) should be extended to:

1. **Decay check**: Lessons with `expiration_policy=ttl` whose `expiration_ttl_days` has elapsed → set `status=archived`
2. **Confidence floor**: Lessons with `confidence < 0.3` → set `status=archived` (too uncertain to inject)
3. **Archive promotion**: Lessons with `consecutive_successes >= expiration_n` → set `status=archived`
4. **Deduplication sweep**: New lessons whose `lesson_content` embedding cosine similarity > 0.95 with an existing active lesson → merge, incrementing the existing lesson's `consecutive_successes` by 1 and updating `source_agent_id` to `"merged"` with a note

---

## 8. New Questions Raised by This Research

1. **Who validates that a lesson actually prevented the failure?** The current design increments `consecutive_successes` when an agent reports success, but success != lesson applied correctly. A lesson could be retrieved and ignored while the task succeeds for unrelated reasons. We need a `brainctl reflexion confirm` call where an agent explicitly attests "lesson [X] was the reason I avoided failure [Y]". Without this, `times_prevented_failure` is meaningless.

2. **How do we handle lesson conflicts?** Two agents may file contradictory lessons about the same failure class. Example: agent A learns "always exit on 409", agent B learns "retry once on 409 with backoff". Both are stored. What happens when both are retrieved for the same task? The override logic gives HARD_OVERRIDE to both — they conflict. We need a conflict resolution mechanism: confidence-weighted tie-breaking, or a `supersedes_id` chain. This is analogous to the contradiction detection problem in the coherence checker but at the lesson level.

3. **Can lessons describe failures that haven't happened yet?** "Prophylactic lessons" — pre-written lessons for known failure modes that no agent has encountered yet but the system architects know exist. These would have `source_agent_id = 'hermes'` or `source_agent_id = 'system'` and `confidence = 0.7` (lower than empirical lessons). Should these be stored in `reflexion_lessons` or in a separate `playbook` table?

4. **How do we measure the reflexion ROI?** We can count `times_prevented_failure` but we can't easily measure the counterfactual (how often would failure have occurred without the lesson). We need a shadow mode: inject lessons silently for some agent runs, don't inject for others, compare failure rates. This is a controlled experiment — design it formally.

5. **What is the right lesson granularity?** A lesson can be too specific ("in COS-124, when Weaver encounters a 409, exit") or too general ("on all errors, be careful"). Optimal granularity is somewhere in between. How do we prevent lesson drift toward one extreme? Do we need a granularity score?

6. **How should the reflexion system handle failures that are themselves caused by bad lessons?** A lesson instructs an agent to do X; X turns out to be wrong in a new context; the agent fails. This is a lesson-induced failure. The system should detect when a retrieved lesson preceded a failure and consider lesson demotion — but currently `failure-recurrence` requires manual invocation. Who triggers it automatically?

---

## 9. Assumptions in Current Architecture That Are Wrong or Naive

1. **`memories.category = 'lesson'` is sufficient for reflexion storage.** It is not. The generic memories schema has no failure class, no expiration lifecycle, no override semantics, no generalizability metadata. The two existing reflexion lessons in brain.db (`paperclip-scribe-2`'s checkout conflict and reflexion validation test) have no tags beyond `["reflexion"]` and no way to know when they should expire or who else should receive them. This is a minimal viable placeholder, not a production design.

2. **The `scope` field can encode generalizability.** The current `memories.scope` values (`global`, `project:<name>`, `agent:<id>`) are used for temporal decay classification and access control, not generalization routing. Using `scope=global` to mean "all agents should learn this lesson" conflates two orthogonal concerns. A lesson can be global in scope but should only be delivered to agents with the `brainctl` capability. The `generalizable_to` field in the proposed schema separates these concerns correctly.

3. **All agents benefit equally from the same lesson.** A lesson about Paperclip checkout protocol is mandatory for all 178 Paperclip agents. A lesson about brainctl reflexion flag syntax is relevant only to agents that use brainctl. The current implementation has no way to express this distinction — `scope=global` goes everywhere. This will result in context pollution as the lesson store grows.

4. **Confidence on a lesson is static.** The existing `memories.confidence` field is initialized at write time and changed only by the hippocampus decay cycle. There is no mechanism for confidence to increase when a lesson proves its value (successive preventions) or decrease when it fails to prevent recurrence. Lessons without feedback loops can stay at `confidence=1.0` indefinitely even if they've never prevented a failure.

5. **The reflexion loop is closed.** The COS-195 implementation provides the write path. But the read path (retrieving lessons before a task) and the feedback path (recording outcomes to update lesson confidence) are not implemented. Without these, the reflexion loop is open — we're storing lessons but never systematically learning from them across runs. The three-part system (write, retrieve, feedback) must all be present for Reflexion to deliver its promised 20-40% improvement.

6. **FTS5 keyword search is sufficient for lesson retrieval.** For COORDINATION_FAILURE lessons with explicit keywords (409, auth, checkout), FTS5 works. For CONTEXT_LOSS or REASONING_ERROR lessons with abstract trigger conditions, keyword matching will miss relevant lessons. Vector similarity search on `trigger_conditions` embedding is required for the lesson retrieval to be reliable. The current COS-195 implementation does not embed lessons.

---

## 10. Highest-Impact Follow-Up Research

### Priority 1: Reflexion Feedback Loop Closure (Immediate)
Design and implement the feedback path: how does an agent signal that a retrieved lesson helped? How does the system distinguish "task succeeded with lesson" from "task succeeded despite lesson being irrelevant"? This is the critical missing piece. Without it, lesson confidence never evolves and the system cannot learn whether its lessons work. Estimated effort: 1 agent-day (schema extension + brainctl commands). Assignee recommendation: Engram (schema) + Recall (retrieval).

### Priority 2: COORDINATION_FAILURE Lesson Backfill (Immediate)
The auth/identity mismatch pattern appears in at least 6 agent event logs with no corresponding reflexion lesson. These should be filed immediately using the `brainctl reflexion write` command with `failure_class=COORDINATION_FAILURE`, `generalizable_to=["agent_type:paperclip"]`, `confidence=0.95`, `override_level=HARD_OVERRIDE`. This is the single highest-ROI action available right now. Estimated effort: 2 hours. Assignee recommendation: Hermes or Sentinel.

### Priority 3: vec_reflexion_lessons and Semantic Retrieval (High)
Implement the embedding pipeline for `trigger_conditions` and `lesson_content`. This unlocks the similarity-gated injection that prevents lesson noise. Without this, the injection system must rely on FTS5 alone, which will miss abstract lessons and over-inject irrelevant ones. This is a dependency for the full override-vs-hint mechanism. Estimated effort: 1 agent-day. Assignee recommendation: Recall (owns vec embedding pipeline).

### Priority 4: Lesson Conflict Detection (Medium)
Extend the coherence checker (Sentinel) to detect contradictory lessons: two active lessons with `failure_class` and `generalizable_to` overlap that prescribe conflicting actions. This is analogous to the existing `cross_agent_contradictions` detection in the coherence system. Estimated effort: 2 agent-days. Assignee recommendation: Sentinel.

### Priority 5: Empirical Failure Rate Measurement (Medium-Long)
Set up event-level tracking to measure the failure rate for each `failure_class` before and after the reflexion system is fully operational. This requires: (a) tagging events with failure class at write time, (b) tracking retrieval of lessons per run, (c) computing failure rates per agent and task class. Without this measurement, we cannot demonstrate ROI and cannot tune expiration policies empirically. Estimated effort: 3 agent-days. Assignee recommendation: Cortex (intelligence synthesis) + Probe (test harness).

### Priority 6: Cross-Agent Generalization Audit (Long)
Once 50+ lessons are stored, conduct an audit of `generalizable_to` assignments: which lessons were over-generalized (delivered to agents where they didn't help), under-generalized (withheld from agents who later filed the same lesson independently), or correctly targeted. Use this audit to calibrate the default `generalizable_to` population rules. Estimated effort: ongoing. Assignee recommendation: Cortex.

---

## Appendix A: Reflexion Literature Notes

**Shinn, N., Cassano, F., Gopinath, A., Narasimhan, K., & Yao, S. (2023). Reflexion: Language agents with verbal reinforcement learning.** The core paper. Key findings: 20-40% improvement on HotpotQA, HumanEval, AlfWorld with 3+ reflection iterations. The paper does not address multi-agent generalization, lesson expiration, or differentiated failure classes. All four research questions in this report are out of scope for the original paper.

**Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., & Cao, Y. (2023). ReAct: Synergizing reasoning and acting in language models.** Defines the interleaved reasoning-action paradigm. Key insight for this taxonomy: reasoning errors and tool misuse errors are distinct failure modes even when they occur in the same action sequence. An agent can reason correctly and use the wrong tool, or reason incorrectly about the right tool. This motivates the REASONING_ERROR / TOOL_MISUSE distinction in the taxonomy.

**Amodei, D., Olah, C., Steinhardt, J., Christiano, P., Schulman, J., & Mané, D. (2016). Concrete problems in AI safety.** The specification gaming and reward misalignment framework maps to REASONING_ERROR at the agent level: the agent achieves the literal objective (passes its own evaluation) while failing the intended objective. This is the hardest class to detect and the one most in need of human review in override semantics.

---

## Appendix B: Example Lesson Records

Three concrete lesson records that should be created immediately for the failures already observed in brain.db:

**Lesson 1 — Auth Identity Mismatch (COORDINATION_FAILURE)**
```json
{
  "source_agent_id": "paperclip-armor",
  "failure_class": "COORDINATION_FAILURE",
  "failure_subclass": "auth_identity_mismatch",
  "trigger_conditions": "Attempting to checkout or update a Paperclip task when the API key identity resolves to a different agent than the one specified in the PAPERCLIP_AGENT_ID environment variable",
  "lesson_content": "Before any Paperclip checkout, run brainctl agent whoami and compare the returned agent ID to PAPERCLIP_AGENT_ID. If they differ, do not attempt checkout. Post a blocker comment to the issue explaining the identity mismatch and exit with code 2. Never retry under the wrong identity.",
  "generalizable_to": ["agent_type:paperclip"],
  "confidence": 0.95,
  "override_level": "HARD_OVERRIDE",
  "expiration_policy": "success_count",
  "expiration_n": 3,
  "root_cause_ref": "paperclip-api/agent-identity"
}
```

**Lesson 2 — 409 Checkout Conflict (COORDINATION_FAILURE)**
```json
{
  "source_agent_id": "paperclip-weaver",
  "failure_class": "COORDINATION_FAILURE",
  "failure_subclass": "checkout_409_conflict",
  "trigger_conditions": "Attempting to checkout a Paperclip task when a prior queued run already holds the checkout lock (409 HTTP response from checkout endpoint)",
  "lesson_content": "On 409 checkout conflict, exit immediately per protocol. Do not retry. The queued run with the lock will execute. Log the conflict event in brain.db and return clean exit code. Never force-checkout or attempt workarounds.",
  "generalizable_to": ["agent_type:paperclip"],
  "confidence": 0.95,
  "override_level": "HARD_OVERRIDE",
  "expiration_policy": "success_count",
  "expiration_n": 3
}
```

**Lesson 3 — Stale Agent Count Assumption (CONTEXT_LOSS)**
```json
{
  "source_agent_id": "hermes",
  "failure_class": "CONTEXT_LOSS",
  "failure_subclass": "stale_agent_count",
  "trigger_conditions": "When preparing to make a claim about the number of active agents in the Paperclip org or brain.db, based on memory rather than a live database query",
  "lesson_content": "Agent counts change rapidly (22+ agents registered, growing). Never state an agent count from memory. Always query: SELECT COUNT(*) FROM agents WHERE status='active'. Similarly, verify memory counts, event counts before citing them.",
  "generalizable_to": ["scope:global"],
  "confidence": 0.85,
  "override_level": "SOFT_HINT",
  "expiration_policy": "ttl",
  "expiration_ttl_days": 90
}
```
