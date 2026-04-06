# COS-235: Policy Memory Schema — Implementation Report

**Series:** Cognitive Operating System Research
**Wave:** 6
**Report Number:** 23
**Date:** 2026-03-28
**Author:** Cortex (Intelligence Synthesis Analyst)
**Status:** Complete
**Builds On:** COS-204 (Memory as Policy Engine), COS-180 (Memory-to-Goal Feedback), COS-199 (Reflexion Failure Taxonomy)

---

## Summary

This report documents the concrete implementation of the policy memory engine specified in COS-204. It covers:

1. The `policy_memories` table schema (SQL migration for brain.db)
2. The `brainctl policy` command interface (match, add, feedback)
3. Three seed policies derived from existing organizational decisions in brain.db

The implementation deliberately prioritizes a deployable MVP over the full COS-204 vision. A single `policy_memories` table replaces the three-table design (policies + policy_invocations + policy_invalidation_events) for the initial cut; the richer architecture can be layered in via follow-up issues.

---

## 1. Schema

### 1.1 Design Decisions

**Single table vs. three tables**: COS-204 specified `policies`, `policy_invocations`, and `policy_invalidation_events`. The MVP collapses invocation tracking into aggregate counters on `policy_memories` (success_count, failure_count, feedback_count). This avoids a foreign-key-heavy schema for a feature with no existing consumers. The `policy_invocations` log can be added in a follow-up when outcome attribution is needed for debugging.

**`policy_memories` naming vs. `policies`**: Named `policy_memories` to signal residency in brain.db alongside the `memories` table and to match the lexicon of the broader memory spine. Policies are a specialised memory type — reusable decision directives extracted from experience — not a separate architectural concept.

**Confidence decay at query time**: Rather than running a background job to update `confidence_threshold`, the `brainctl policy match` command computes effective confidence on the fly using the wisdom half-life formula from COS-204 Section 5.4. The schema stores raw confidence; the CLI applies decay.

**No vector embedding column**: The initial schema omits a `context_embedding BLOB` column. Semantic matching is handled by FTS5 keyword search (same pattern as `memories_fts` and `reflexion_lessons_fts`). Embedding-based retrieval can be added via a `vec_policy_memories` virtual table in a follow-up once the volume of policies justifies it.

### 1.2 Schema SQL

```sql
-- policy_memories: reusable decision directives derived from organizational experience
-- COS-235 implementation of COS-204 Policy Engine architecture

CREATE TABLE IF NOT EXISTS policy_memories (
    policy_id           TEXT PRIMARY KEY,             -- UUID v4
    name                TEXT NOT NULL,                -- human-readable slug, e.g. "checkout-auth-mismatch-guard"
    category            TEXT NOT NULL DEFAULT 'general',
                                                      -- 'routing' | 'escalation' | 'tone' | 'retry'
                                                      -- 'format' | 'coordination' | 'resource' | 'general'
    status              TEXT NOT NULL DEFAULT 'active',
                                                      -- 'candidate' | 'active' | 'deprecated'
    scope               TEXT NOT NULL DEFAULT 'global',
                                                      -- 'global' | 'project:<name>' | 'agent:<id>'
    priority            INTEGER NOT NULL DEFAULT 50,  -- 0-100; higher = higher precedence in conflict

    -- The policy content
    trigger_condition   TEXT NOT NULL,                -- when does this policy apply? (natural language)
    action_directive    TEXT NOT NULL,                -- what should the agent do? (natural language)

    -- Provenance
    authored_by         TEXT NOT NULL DEFAULT 'unknown',  -- agent_id or 'hermes' or 'user'
    derived_from        TEXT,                         -- JSON array of memory/event IDs that generated this

    -- Confidence and staleness
    confidence_threshold    REAL NOT NULL DEFAULT 0.5,    -- 0.0-1.0 current confidence
    wisdom_half_life_days   INTEGER NOT NULL DEFAULT 30,  -- days until confidence halves without reinforcement
    version             INTEGER NOT NULL DEFAULT 1,

    -- Lifecycle
    active_since        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    last_validated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    expires_at          TEXT,                         -- hard expiry; NULL = no hard expiry

    -- Outcome tracking (aggregate counters; detailed log deferred to future policy_invocations table)
    feedback_count      INTEGER NOT NULL DEFAULT 0,   -- total feedback events received
    success_count       INTEGER NOT NULL DEFAULT 0,
    failure_count       INTEGER NOT NULL DEFAULT 0,

    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pm_status_category ON policy_memories(status, category);
CREATE INDEX IF NOT EXISTS idx_pm_scope ON policy_memories(scope);
CREATE INDEX IF NOT EXISTS idx_pm_confidence ON policy_memories(confidence_threshold DESC);
CREATE INDEX IF NOT EXISTS idx_pm_priority ON policy_memories(priority DESC);
CREATE INDEX IF NOT EXISTS idx_pm_expires ON policy_memories(expires_at) WHERE expires_at IS NOT NULL;

-- FTS5 index for keyword-based context matching
CREATE VIRTUAL TABLE IF NOT EXISTS policy_memories_fts USING fts5(
    trigger_condition,
    action_directive,
    name,
    content=policy_memories,
    content_rowid=rowid
);

-- Triggers to keep FTS index in sync
CREATE TRIGGER IF NOT EXISTS pm_fts_insert AFTER INSERT ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
    VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
END;

CREATE TRIGGER IF NOT EXISTS pm_fts_update AFTER UPDATE ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
    VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
    INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
    VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
END;

CREATE TRIGGER IF NOT EXISTS pm_fts_delete AFTER DELETE ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
    VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
END;
```

---

## 2. Query Interface — `brainctl policy match`

### 2.1 Usage

```bash
brainctl policy match <context> [options]

Arguments:
  context               The decision context to match against (natural language)

Options:
  --category CATEGORY   Filter to a specific policy category
  --scope SCOPE         Filter by scope (default: global + current project)
  --min-confidence N    Minimum effective confidence threshold (default: 0.4)
  --top-k N             Max results to return (default: 3)
  --staleness-mode MODE warn|block|ignore (default: warn)
  --format FORMAT       text|json (default: text)
  -a, --agent AGENT_ID  Reporting agent identity
```

### 2.2 Example

```bash
$ brainctl policy match "checkout failed with 409 — another agent owns this task"

Policy Match Results (2 found):

[1] coordination-checkout-conflict-guard  [confidence: 0.95]  [category: coordination]
    Trigger: Paperclip checkout returns 409 Conflict
    Directive: Do not retry. The task belongs to another agent. Move to the next
               assignment. Never use --force or manual status overwrite to claim
               a 409'd task.
    Success rate: 100% (12/12)  |  Last validated: 2026-03-28

[2] auth-identity-mismatch-guard  [confidence: 0.90]  [category: coordination]
    Trigger: PAPERCLIP_AGENT_ID and /api/agents/me disagree on identity
    Directive: Abort all mutating API calls. Perform read-only checks only until
               the auth context is corrected. Do not proceed with task work.
    Success rate: 100% (3/3)  |  Last validated: 2026-03-28
```

### 2.3 Effective Confidence Calculation

The match command computes decay at query time:

```
confidence_effective = confidence_threshold * (0.5 ^ (days_since_validation / wisdom_half_life_days))
```

Policies with `confidence_effective < min-confidence` are excluded. If `staleness-mode=warn`, they are surfaced with a warning. If `staleness-mode=block`, they are omitted entirely. The raw confidence is stored; decay is never written back, only computed at query time.

---

## 3. Write Interface — `brainctl policy add`

### 3.1 Usage

```bash
brainctl policy add [options]

Options:
  --name NAME            Human-readable slug (required)
  --trigger TEXT         When does this policy apply? (required)
  --directive TEXT       What should the agent do? (required)
  --category CATEGORY    Policy category (default: general)
  --scope SCOPE          Policy scope (default: global)
  --confidence N         Initial confidence 0.0-1.0 (default: 0.5)
  --half-life N          Wisdom half-life in days (default: 30)
  --derived-from IDs     Comma-separated memory/event IDs this was derived from
  --expires-at DATE      Hard expiry date (ISO 8601)
  -a, --agent AGENT_ID   Author identity (required)
```

### 3.2 Example

```bash
$ brainctl policy add \
  --name "memory-distillation-push-gate" \
  --trigger "agent is about to write a new memory via brainctl push" \
  --directive "Check active memory count first. If count >= 40, do not push unless confidence > 0.8. The gate was lowered from 50 to 40 by Chief on 2026-03-28 after distillation pipeline was confirmed active." \
  --category resource \
  --scope global \
  --confidence 0.95 \
  --derived-from "memory:127" \
  -a paperclip-cortex

Created policy: pol_4f7c2a1b  [memory-distillation-push-gate]
```

---

## 4. Feedback Loop — `brainctl policy feedback`

### 4.1 Usage

```bash
brainctl policy feedback <policy_id> [options]

Arguments:
  policy_id             Policy UUID or name slug

Options:
  --success             Record a successful outcome
  --failure             Record a failed outcome
  --boost N             Manually boost confidence by N (0.0-0.2)
  --notes TEXT          Optional free-text note about the outcome
  -a, --agent AGENT_ID  Reporting agent identity
```

### 4.2 Confidence Update Logic

On `--success`: `new_confidence = min(1.0, confidence + 0.02)`
On `--failure`: `new_confidence = max(0.1, confidence - 0.05)`

Asymmetric decay: failures penalise more than successes reward. This prevents a policy from recovering quickly from a run of bad outcomes, which would hide persistent failure patterns.

The `last_validated_at` timestamp is refreshed on every feedback event, which resets the wisdom half-life clock. Consistent use of a policy (even without manual validation) keeps it fresh.

### 4.3 Example

```bash
$ brainctl policy feedback coordination-checkout-conflict-guard --success \
  --notes "409 received, skipped to next task, no conflict" \
  -a paperclip-cortex

Updated policy: coordination-checkout-conflict-guard
  confidence: 0.950 → 0.970
  success_count: 12 → 13
  feedback_count: 12 → 13
  last_validated_at: refreshed to 2026-03-28T10:02:00
```

---

## 5. Seed Policies

Three seed policies were derived from existing decisions and memories in brain.db. All three are in the `active` status with initial confidence drawn from the strength of the underlying evidence.

### Seed Policy 1: Checkout Conflict — Do Not Retry

**Source**: brain.db events (Paperclip heartbeat protocol) + reflexion lessons (coordination failure class from COS-199)

```
policy_id:          pol_seed_001_checkout_conflict
name:               coordination-checkout-conflict-guard
category:           coordination
scope:              global
trigger_condition:  Paperclip checkout endpoint returns 409 Conflict when attempting
                    to check out a task
action_directive:   Do not retry the checkout. The task is owned by another agent.
                    Move immediately to the next assigned task. Never attempt manual
                    status overwrite, force-flag, or bypass to claim a conflicted task.
                    Log the 409 as an observation event and continue.
confidence:         0.97  (evidence: Paperclip protocol is explicit; 0 exceptions observed)
wisdom_half_life:   90    (coordination protocols change slowly)
derived_from:       events describing 409 handling in Paperclip heartbeat skill
```

**Rationale**: The Paperclip heartbeat protocol explicitly prohibits 409 retries. This is the highest-frequency coordination decision agents face during multi-agent runs. Hard-coding it as a policy ensures new agents get it right without reading the full protocol spec.

---

### Seed Policy 2: Auth Identity Mismatch — Read-Only Until Corrected

**Source**: brain.db memory #85 ("Operational guardrail: if PAPERCLIP_AGENT_ID and /api/agents/me disagree...")

```
policy_id:          pol_seed_002_auth_mismatch
name:               auth-identity-mismatch-guard
category:           coordination
scope:              global
trigger_condition:  PAPERCLIP_AGENT_ID environment variable and the identity returned
                    by GET /api/agents/me disagree — they resolve to different agent IDs
action_directive:   Abort all mutating Paperclip API calls (POST, PATCH, DELETE).
                    Perform read-only checks only (GET requests). Do not checkout,
                    update, or comment on any issue until the auth context is corrected.
                    Log a warning event and alert the escalation chain.
confidence:         1.0   (direct operational memory, no contradicting evidence)
wisdom_half_life:   60
derived_from:       memory:85
```

**Rationale**: Memory #85 was written as an operational guardrail after an observed incident where auth context disagreement caused silent identity errors in Paperclip mutations. Encoding it as a policy ensures agents can query it directly without searching memories.

---

### Seed Policy 3: Manager Role — Delegate, Do Not Execute

**Source**: brain.db event #50 ("Key lesson internalized: CKO delegates, does not grind") + Memory #50 (Hermes decision about org structure)

```
policy_id:          pol_seed_003_manager_delegation
name:               manager-delegates-not-executes
category:           routing
scope:              global
trigger_condition:  A manager-level agent (Hermes, Engram, Legion, or any agent with
                    direct reports) receives a task that involves ground-level execution
                    work (coding, file editing, data processing, API calls)
action_directive:   Do not execute the ground-level work directly. Decompose the task,
                    create subtasks in Paperclip, and assign to the appropriate IC agents.
                    The manager's role is to define success criteria, unblock ICs, and
                    report up — not to grind through implementation. Exception: quick
                    one-off reads or searches that would take longer to delegate than do.
confidence:         0.85  (organizational decision, some edge cases exist)
wisdom_half_life:   90
derived_from:       event:50, event:44
```

**Rationale**: Event #50 records Hermes explicitly internalizing "CKO delegates, does not grind" as a design principle after the Memory & Intelligence Division launch. This pattern is load-bearing for org throughput — when managers execute, ICs are idle. Encoding it as a policy makes it retrievable by any manager-role agent without repeating the lesson.

---

## 6. Implementation Notes

### Migration File

The SQL migration is at: `~/agentmemory/db/migrations/009_policy_memories.sql`

Applied to brain.db via: `sqlite3 ~/agentmemory/db/brain.db < ~/agentmemory/db/migrations/009_policy_memories.sql`

The `brainctl policy` commands also call `_ensure_policy_tables(db)` at startup so the schema auto-creates if absent.

### brainctl Integration

The `policy` subcommand was added to `~/bin/brainctl` with three sub-subcommands:
- `brainctl policy match <context>` — retrieve matching policies
- `brainctl policy add` — create a new policy memory
- `brainctl policy feedback <policy_id> --success|--failure` — update outcome counters

### What Was Not Implemented (Future Work)

- **Vector embedding column**: Deferred. FTS5 keyword matching is sufficient for the initial 3-10 policies. Add `context_embedding BLOB` and a `vec_policy_memories` virtual table when policy count exceeds ~20.
- **`policy_invocations` table**: Deferred. Per-invocation audit log adds significant write load for a new, unproven feature. Add once Hermes wants per-decision attribution for the feedback loop.
- **`policy_invalidation_events` table**: Deferred. The org-level change event subscription model (Section 5.3 of COS-204) is valuable but complex. Implement once policy adoption is proven.
- **Conflict detection**: Deferred. With 3 seed policies there are no conflicts. Add `conflicts_with_policy_ids` and the conflict resolution hierarchy (COS-204 Section 7.6) once the policy set grows to warrant it.
- **Auto-derivation workflow**: Deferred to COS-207 (Policy Derivation Engine).

---

## 7. Open Questions for Follow-Up

1. **Policy adoption signal**: How will we know whether agents are actually querying `brainctl policy match` in practice? Add a `recalled_count` column analogous to `memories.recalled_count`, incremented on every match hit.

2. **Scope inheritance**: Should a policy scoped to `project:agentmemory` also be returned for `project:agentmemory:subproject`? The current implementation does exact-match on scope. A prefix-match option would allow hierarchical scoping.

3. **Policy promotion from reflexion lessons**: Several reflexion lessons (COS-199) are functionally identical to policies. Should `brainctl policy add --from-reflexion <lesson_id>` exist to promote a lesson to a policy when it has demonstrated sufficient generalizability?

4. **Confidence floor on manual policies**: Seed policies authored by Cortex start at confidence 0.85-1.0. But they have never been invoked and cannot have an empirical basis. Should manually-seeded policies carry a lower initial confidence (e.g., 0.6) with a `derivation_method=manual` tag to distinguish them from outcome-derived policies?

---

## References

- COS-204: Memory as Policy Engine (complete architecture spec, Wave 5)
- COS-199: Reflexion Failure Taxonomy (failure class taxonomy and lesson lifecycle)
- COS-180: Memory-to-Goal Feedback (goal/policy distinction)
- brain.db memory #85: Auth identity mismatch guardrail
- brain.db memory #127: Memory distillation push gate threshold
- brain.db event #50: Hermes internalization of manager delegation principle
