# Prospective Memory — Conditional Recall Triggers and Future-Oriented Cognition

**Author:** Weaver (Context Integration Engineer)
**Task:** [COS-364](/COS/issues/COS-364)
**Date:** 2026-03-28
**DB State:** 26 agents · 151 active memories · brain.db @ ~/agentmemory/db/brain.db
**Migration target:** `025_memory_triggers.sql`

---

## Executive Summary

Every memory in `brain.db` is **retrospective** — it records what happened. Biological agents also possess *prospective memory*: the ability to remember to surface information when a future condition is met. Our agent fleet has no equivalent.

The gap is concrete and costly:
- Governance rules in `brain.db` are passive. A rule saying "never push to main without Kokoro approval" only helps if an agent happens to recall it. It cannot self-surface when an agent is about to run `git push`.
- Time-sensitive memories (merge freeze by 2026-03-31) have no expiry-surfacing mechanism. They decay silently.
- Reflexion lessons from COS-320 require explicit recall. High-stakes lessons are never *pushed* to the agents who most need them.

This report designs a `memory_triggers` system: a table of conditional surfacing rules linked to existing memories, with three trigger modalities (temporal, contextual, event), integration with MEB (COS-232) and the World Model (COS-321), a `brainctl trigger` command family, and a priority-based fan-out protocol for simultaneous trigger fires.

**Core claim:** Prospective memory is not a new data store. It is a *routing layer* over existing memories — a set of conditions that say "when X happens, surface memory Y to agents Z." The right implementation is a trigger table + a polling daemon hook + two injection points (heartbeat pre-task scan, MEB subscriber).

---

## 1. Literature Review

### 1.1 Einstein & McDaniel (1990) — Defining Prospective Memory

McDaniel and Einstein introduced the term in their seminal 1990 paper *"Normal Aging and Prospective Memory"* (Journal of Experimental Psychology: Learning, Memory, and Cognition). Their core distinction:

- **Retrospective memory**: remembering *that X happened* (episodic) or *what X is* (semantic)
- **Prospective memory**: remembering *to do X at time T* or *when condition C is met*

They identified two subtypes that map directly to our architecture needs:

| Subtype | Definition | brain.db analogue |
|---|---|---|
| **Time-based PM** | Surface at a specific clock time or elapsed interval | `trigger_type = 'temporal'` |
| **Event-based PM** | Surface when a specific event/condition is encountered | `trigger_type = 'contextual'` or `'event'` |

Einstein & McDaniel found that event-based PM is more reliable than time-based PM in humans because environmental cues (the "retrieval cue") naturally interrupt processing at the right moment. Our analogue: a heartbeat pre-task scan (event-based) will be more reliable than a cron-based check (time-based) because it fires at the natural interruption point.

**Key finding:** PM failures are often *monitoring failures* — the subject never checks whether the condition has been met — not *storage failures*. Our design must therefore include a polling mechanism that actively checks trigger conditions, not just a passive index.

### 1.2 Guynn (2003) — The Noticing + Retrieval Framework

Guynn (2003, *"A Two-Process Model of Monitoring in Event-Based Prospective Memory"*, Memory & Cognition) proposed that event-based PM requires two distinct processes:

1. **Noticing** — detecting that the current context matches the trigger condition
2. **Retrieval** — recovering the associated intention/information from long-term memory

The noticing process is *attentionally costly*: if agents must continuously monitor all inputs for all trigger conditions, their available token budget collapses. Guynn's finding: noticing cost scales with the *number of active triggers*, not their specificity. A fleet with 500 active triggers is slower than one with 10, even if the extra 490 never fire.

**Design implication for brain.db:** Active triggers must be bounded. The `memory_triggers` table needs an `active` flag and a hard capacity limit per agent. Trigger expiry is not optional — it is a cognitive economics requirement.

### 1.3 Kliegel (2008) — The PRAM Framework

Kliegel, Martin, McDaniel & Einstein (2008, *"Prospective Memory and Aging"*) developed the **Prospective and Retrospective Memory Assessment (PRAM)** framework, describing PM as a four-phase sequence:

1. **Encoding** — forming the trigger-intention link ("when X, do Y")
2. **Retention** — maintaining the link during the delay period
3. **Noticing/Initiation** — detecting the trigger event
4. **Execution** — actually performing the intended action

For our system, each phase has a failure mode:
- Encoding failure → trigger condition is ambiguous or too broad (e.g. `*` matches everything)
- Retention failure → trigger expires or confidence decays to zero before firing
- Noticing failure → polling interval is too coarse; trigger fires between heartbeats
- Execution failure → agent receives the surfaced memory but ignores it (no injection into context)

The PRAM framework argues execution failure is the least common in healthy agents. For our system, it is the *most dangerous*: a memory surfaced as a side-channel suggestion will be ignored without a protocol for mandatory pre-task injection.

**Design implication:** Surfaced triggers must be injected into the agent's context at task start, not delivered as optional suggestions. The `brainctl trigger fire` output must appear in the `brainctl search` results flow, not a separate command.

---

## 2. Schema Design

### 2.1 Core Table: `memory_triggers`

```sql
-- Migration 025: Prospective Memory — memory_triggers table
-- Author: Weaver (Context Integration Engineer)
-- Date: 2026-03-28
-- COS-364

CREATE TABLE memory_triggers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id           INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    created_by_agent_id TEXT    NOT NULL REFERENCES agents(id),
    trigger_type        TEXT    NOT NULL CHECK (trigger_type IN ('temporal', 'contextual', 'event')),
    condition_predicate TEXT    NOT NULL,   -- JSON spec (see §2.2)
    surface_to_agents   TEXT,              -- JSON array of agent_ids; NULL = broadcast
    priority            TEXT    NOT NULL DEFAULT 'medium'
                                CHECK (priority IN ('critical', 'high', 'medium', 'low')),
    fire_mode           TEXT    NOT NULL DEFAULT 'once'
                                CHECK (fire_mode IN ('once', 'recurring', 'until_acked')),
    active              INTEGER NOT NULL DEFAULT 1,
    triggered_count     INTEGER NOT NULL DEFAULT 0,
    last_triggered_at   TEXT,
    expires_at          TEXT,              -- NULL = no expiry; ISO 8601
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    acked_by_agents     TEXT               -- JSON array of agent_ids that have acknowledged
);

CREATE INDEX idx_triggers_memory_id  ON memory_triggers(memory_id);
CREATE INDEX idx_triggers_type       ON memory_triggers(trigger_type);
CREATE INDEX idx_triggers_active     ON memory_triggers(active) WHERE active = 1;
CREATE INDEX idx_triggers_expires_at ON memory_triggers(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_triggers_created_by ON memory_triggers(created_by_agent_id);
```

**New `fire_mode` column (beyond the issue spec):** Distinguishes:
- `once` — fires, sets `active = 0`. Suitable for one-time reminders.
- `recurring` — fires, increments `triggered_count`, remains active. Suitable for governance rules.
- `until_acked` — fires until all `surface_to_agents` entries have acknowledged (added to `acked_by_agents`). Suitable for critical safety rules.

### 2.2 `condition_predicate` JSON Spec

The predicate is a JSON object with a required `type` field and type-specific parameters:

#### Temporal predicates

```json
{
  "type": "temporal",
  "mode": "absolute",
  "at": "2026-03-31T09:00:00"
}
```

```json
{
  "type": "temporal",
  "mode": "relative",
  "after_event_type": "task_start",
  "delay_seconds": 3600
}
```

#### Contextual predicates

```json
{
  "type": "contextual",
  "match": "keyword",
  "keywords": ["git push", "main branch", "force push"],
  "match_threshold": 1
}
```

```json
{
  "type": "contextual",
  "match": "category",
  "categories": ["governance", "security"],
  "min_confidence": 0.7
}
```

```json
{
  "type": "contextual",
  "match": "semantic",
  "query": "modifying authentication code",
  "similarity_threshold": 0.82
}
```

#### Event predicates (MEB integration)

```json
{
  "type": "event",
  "event_types": ["memory_insert", "memory_update"],
  "category_filter": "governance",
  "agent_filter": null
}
```

```json
{
  "type": "event",
  "event_types": ["world_predict"],
  "condition_field": "subject_id",
  "condition_value": "main-branch-freeze"
}
```

### 2.3 `trigger_fire_log` — Audit Table

```sql
CREATE TABLE trigger_fire_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_id      INTEGER NOT NULL REFERENCES memory_triggers(id),
    memory_id       INTEGER NOT NULL REFERENCES memories(id),
    fired_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    surfaced_to     TEXT    NOT NULL,   -- JSON array of agent_ids actually notified
    surface_method  TEXT    NOT NULL,   -- 'heartbeat_injection' | 'meb_event' | 'manual'
    acked_at        TEXT,
    acked_by        TEXT
);

CREATE INDEX idx_fire_log_trigger ON trigger_fire_log(trigger_id);
CREATE INDEX idx_fire_log_memory  ON trigger_fire_log(memory_id);
CREATE INDEX idx_fire_log_fired   ON trigger_fire_log(fired_at DESC);
```

---

## 3. `brainctl trigger` Command Spec

### 3.1 `trigger set`

Create or update a trigger linking a condition to a memory.

```
brainctl trigger set \
  --memory-id <id> \
  --type <temporal|contextual|event> \
  --predicate '<JSON string>' \
  [--agents <agent_id,agent_id,...>]   # omit for broadcast
  [--priority <critical|high|medium|low>]
  [--fire-mode <once|recurring|until_acked>]
  [--expires-at <ISO8601>]
  -a <calling_agent_id>
```

Example — governance rule, broadcast, recurring:
```bash
brainctl trigger set \
  --memory-id 42 \
  --type contextual \
  --predicate '{"type":"contextual","match":"keyword","keywords":["git push","main"],"match_threshold":1}' \
  --priority critical \
  --fire-mode recurring \
  -a paperclip-weaver
```

Output:
```json
{
  "trigger_id": 7,
  "memory_id": 42,
  "type": "contextual",
  "status": "active",
  "fire_mode": "recurring",
  "expires_at": null
}
```

### 3.2 `trigger list`

```
brainctl trigger list [--type <type>] [--agent <agent_id>] [--active-only] [--json]
```

Output (default table):
```
ID  MEMORY_ID  TYPE         PRIORITY  FIRE_MODE   TRIGGERED  EXPIRES
7   42         contextual   critical  recurring   0          never
12  88         temporal     high      once        0          2026-03-31T09:00
15  103        event        medium    until_acked 2          never
```

### 3.3 `trigger fire`

Manually fire a trigger (for testing or forced surfacing):

```
brainctl trigger fire <trigger_id> [--agents <agent_id,...>] [--dry-run] -a <calling_agent_id>
```

In dry-run mode: prints which agents would be notified and the memory content, without writing to `trigger_fire_log`.

### 3.4 `trigger expire`

Deactivate a trigger. Does not delete it (fire log preserved).

```
brainctl trigger expire <trigger_id> -a <calling_agent_id>
brainctl trigger expire --all-expired   # auto-expire past expires_at
```

### 3.5 `trigger ack`

Agent acknowledges receipt of a `until_acked` trigger surfacing.

```
brainctl trigger ack <trigger_id> -a <calling_agent_id>
```

Adds the agent to `acked_by_agents` JSON. When all target agents have acked, sets `active = 0` (if fire_mode = `until_acked`).

### 3.6 `trigger check` — The Critical Integration Hook

Called at heartbeat start, injected into the `brainctl search` flow:

```
brainctl trigger check \
  --context "<task description or search query>" \
  --agent <agent_id> \
  [--limit 5]
```

Returns fired triggers whose memories should be injected into the context window *before* the agent proceeds. Output is structured identically to `brainctl search` results so callers treat triggered memories identically to searched memories — they don't need special handling.

```json
{
  "triggered": [
    {
      "trigger_id": 7,
      "memory_id": 42,
      "content": "CRITICAL: Never push to main without Kokoro approval. See COS-237.",
      "confidence": 1.0,
      "priority": "critical",
      "fire_reason": "keyword_match: 'git push'"
    }
  ],
  "check_duration_ms": 12
}
```

The `trigger check` output is rendered as a warning block at the top of any `brainctl search` result set when `triggered` is non-empty.

---

## 4. Integration with World Model (COS-321)

The World Model (`brainctl world predict`) logs forward-looking predictions about organizational state. These predictions are natural trigger sources: "if the predicted state X is about to become actual, surface memory Y."

### 4.1 World Model as Trigger Source

When an agent calls `brainctl world predict`, it writes a row to `world_model_snapshots` with `predicted_state` (JSON). A trigger with `type = 'event'` can watch for resolution events from the world model:

```json
{
  "type": "event",
  "event_types": ["world_predict_resolved"],
  "condition_field": "subject_id",
  "condition_value": "merge-freeze-2026-03-31"
}
```

When `brainctl world resolve` is called (resolving the prediction), the MEB trigger fires. This surfaces the linked memory to target agents at the moment the predicted world state is confirmed — the optimal intervention point.

### 4.2 Predicted State as Trigger Condition (Future: COS-321+)

A deeper integration (beyond this wave) would allow the trigger condition itself to reference a world model prediction:

```json
{
  "type": "world_model",
  "predict_query": "main branch freeze",
  "confidence_threshold": 0.8,
  "horizon_days": 3
}
```

This fires when the world model's confidence in "main branch freeze within 3 days" exceeds 0.8 — *before* the event occurs. This is the closest analog to biological prospective memory: proactive recall based on anticipated future state, not past events.

**Dependency:** This requires the World Model to expose a query API (`brainctl world predict --query "..."` returning probability scores). Current `brainctl world` supports `predict` as a logging command, not a query command. The extension would be filed as a COS-321 subtask.

### 4.3 Recommended Immediate Integration

Without the advanced world model query API, the practical integration today:

1. When any agent calls `brainctl world predict`, the MEB fires a `memory_event` of type `world_predict`.
2. Any trigger with `event_type = 'world_predict'` is evaluated against the new snapshot.
3. Matching triggers surface their memories to the agent that made the prediction.

This covers the most common case: an agent making a prediction that should remind it of a related constraint or lesson.

---

## 5. Integration with MEB (COS-232)

The Memory Event Bus captures all memory writes as `memory_events` rows. The trigger system plugs into MEB as a *subscriber* — it reads the MEB stream and evaluates `event`-type triggers on each new event.

### 5.1 MEB → Trigger Evaluation Pipeline

```
brainctl meb tail --since <last_watermark>
     ↓ (each new memory_event)
evaluate_event_triggers(event)
     ↓ (matching triggers)
fire_trigger(trigger_id, surface_to_agents, surface_method='meb_event')
     ↓
inject into target agents' next heartbeat via trigger_fire_log
```

The evaluator runs in the hippocampus cycle (already scheduled every 5h) as a lightweight pass after consolidation:

```python
def evaluate_event_triggers(db: sqlite3.Connection, since_event_id: int) -> int:
    """
    Pull new MEB events since last watermark.
    For each event, find matching active 'event' triggers.
    Fire them and update fire log + triggered_count.
    Returns count of triggers fired.
    """
    new_events = db.execute(
        "SELECT * FROM memory_events WHERE id > ? ORDER BY id ASC", (since_event_id,)
    ).fetchall()

    fired = 0
    for evt in new_events:
        triggers = db.execute("""
            SELECT mt.*, m.content, m.confidence, m.importance
            FROM memory_triggers mt
            JOIN memories m ON mt.memory_id = m.id
            WHERE mt.active = 1
              AND mt.trigger_type = 'event'
              AND json_extract(mt.condition_predicate, '$.event_types') LIKE ?
        """, (f'%{evt["operation"]}%',)).fetchall()

        for t in triggers:
            if _predicate_matches(t, evt):
                _fire_trigger(db, t, surface_method='meb_event')
                fired += 1

    return fired
```

### 5.2 MEB Trigger Events

When a trigger fires, it also writes a `memory_event` to MEB with `operation = 'trigger_fired'`. This means:
- Other agents subscribed to `meb tail` see trigger fires in their stream
- Trigger fires are themselves auditable via the MEB history
- Agents can subscribe to trigger-fired events as a secondary prospective signal

### 5.3 Real-time vs. Batch Trade-off

For `critical` priority triggers, the MEB evaluator should run in near-real-time (call from within `brainctl memory add` / `brainctl push`, at the point of memory write). For `medium` and `low` triggers, batch evaluation in the hippocampus cycle is sufficient.

Implementation: `brainctl memory add` already calls the MEB INSERT trigger via SQLite. Add a lightweight Python hook in `brainctl push` that calls `evaluate_event_triggers(db, since=current_max_id)` for critical-priority triggers only.

---

## 6. Priority Handling: Simultaneous Trigger Fires

**Scenario:** 10 triggers fire simultaneously (e.g., an agent starts a task that matches 10 active contextual triggers). What happens?

### 6.1 Priority Ranking

Triggers are ordered by priority descending, then by `created_at` ascending (older triggers first within the same priority):

| Priority | Max simultaneous surface | Behavior if over limit |
|---|---|---|
| `critical` | Unlimited | All critical triggers always fire |
| `high`     | 5          | Top 5 by `created_at` ASC fire; rest deferred |
| `medium`   | 3          | Top 3 by `created_at` ASC fire; rest deferred |
| `low`      | 1          | Top 1 fires; rest deferred |

Total cap per heartbeat: **10 triggered memories** (critical unlimited + up to 9 from high/medium/low). This maps to Guynn's (2003) finding that noticing cost scales with active triggers — an agent cannot process more than ~10 prospective cues per context window without degrading task performance.

### 6.2 Deferred Fires

Triggers that are ranked out in a given heartbeat are not lost. They are queued in `trigger_fire_log` with `surfaced_to = '[]'` (empty — not yet delivered). The next `brainctl trigger check` call picks them up if the context still matches.

### 6.3 Fan-out Limit for Broadcast Triggers

When `surface_to_agents IS NULL` (broadcast), a trigger fires to all active agents. To prevent a single trigger from spamming all 26+ agents simultaneously:

- Broadcast fires are **staggered**: the first heartbeat surfaces to the top-3 agents by relevance (derived from `agent_capabilities` matching the trigger's memory category).
- Remaining agents receive the trigger on subsequent heartbeats.
- Agents that have already acked are excluded from future fire attempts.

### 6.4 Priority Override for Critical Governance Triggers

If a `critical` trigger fires, it takes priority over the agent's current task context. The `trigger check` response includes a `force_inject` flag for critical triggers. The agent's heartbeat protocol requires reading `trigger check` output *before* reading task context — so a critical governance reminder (e.g., "never merge without auth review") is always seen first, regardless of what task the agent is working on.

---

## 7. `brainctl trigger` Integration into Heartbeat Protocol

The recommended update to the agent heartbeat protocol (addition to `COGNITIVE_PROTOCOL.md`):

```
## Pre-Task Context Pull (Step 2.5 — after brain push, before task start)

Run BOTH of these at the start of every task:

    brainctl push run <agent_id> "<task description>" --limit 5
    brainctl trigger check --context "<task description>" --agent <agent_id>

The trigger check output is prepended to the search results.
Critical triggers MUST be read before proceeding.
For until_acked triggers: run `brainctl trigger ack <id>` after reading.
```

This ensures prospective memories are injected at the optimal PRAM phase-3 moment (initiation/noticing), when the agent is actively beginning a task and the environmental cue (the task description) is available.

---

## 8. Migration Script: `025_memory_triggers.sql`

```sql
-- Migration 025: Prospective Memory — memory_triggers + trigger_fire_log
-- Author: Weaver (Context Integration Engineer)
-- Date: 2026-03-28
-- COS-364
-- Schema version: 24 -> 25

CREATE TABLE IF NOT EXISTS memory_triggers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id           INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    created_by_agent_id TEXT    NOT NULL REFERENCES agents(id),
    trigger_type        TEXT    NOT NULL CHECK (trigger_type IN ('temporal', 'contextual', 'event')),
    condition_predicate TEXT    NOT NULL,
    surface_to_agents   TEXT,
    priority            TEXT    NOT NULL DEFAULT 'medium'
                                CHECK (priority IN ('critical', 'high', 'medium', 'low')),
    fire_mode           TEXT    NOT NULL DEFAULT 'once'
                                CHECK (fire_mode IN ('once', 'recurring', 'until_acked')),
    active              INTEGER NOT NULL DEFAULT 1,
    triggered_count     INTEGER NOT NULL DEFAULT 0,
    last_triggered_at   TEXT,
    expires_at          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    acked_by_agents     TEXT
);

CREATE INDEX IF NOT EXISTS idx_triggers_memory_id  ON memory_triggers(memory_id);
CREATE INDEX IF NOT EXISTS idx_triggers_type       ON memory_triggers(trigger_type);
CREATE INDEX IF NOT EXISTS idx_triggers_active     ON memory_triggers(active) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_triggers_expires_at ON memory_triggers(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_triggers_created_by ON memory_triggers(created_by_agent_id);

CREATE TABLE IF NOT EXISTS trigger_fire_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_id      INTEGER NOT NULL REFERENCES memory_triggers(id),
    memory_id       INTEGER NOT NULL REFERENCES memories(id),
    fired_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    surfaced_to     TEXT    NOT NULL DEFAULT '[]',
    surface_method  TEXT    NOT NULL DEFAULT 'heartbeat_injection'
                            CHECK (surface_method IN ('heartbeat_injection', 'meb_event', 'manual')),
    acked_at        TEXT,
    acked_by        TEXT
);

CREATE INDEX IF NOT EXISTS idx_fire_log_trigger ON trigger_fire_log(trigger_id);
CREATE INDEX IF NOT EXISTS idx_fire_log_memory  ON trigger_fire_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_fire_log_fired   ON trigger_fire_log(fired_at DESC);

-- Auto-expire triggers past their expires_at on each run
-- (lightweight; runs on brainctl trigger list/check)
CREATE VIEW IF NOT EXISTS active_triggers AS
SELECT *
FROM memory_triggers
WHERE active = 1
  AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now'));

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (25, strftime('%Y-%m-%dT%H:%M:%S', 'now'),
  'Prospective Memory: memory_triggers + trigger_fire_log — COS-364');

PRAGMA user_version = 25;
```

---

## 9. Implementation Roadmap

| Phase | Work | Owner | Effort |
|---|---|---|---|
| 1 | Apply migration 025 | Engram | 30m |
| 2 | `brainctl trigger set/list/fire/expire/ack` commands | Weaver | 1d |
| 3 | `brainctl trigger check` with heartbeat injection | Weaver | 0.5d |
| 4 | MEB evaluator: `evaluate_event_triggers` in hippocampus | Weaver | 0.5d |
| 5 | Priority fan-out + deferred fire queue | Weaver | 0.5d |
| 6 | `COGNITIVE_PROTOCOL.md` update for pre-task trigger check | Hermes | 0.5d |
| 7 | Seed initial critical governance triggers from existing memories | All agents | 1d |

---

## 10. Risk Analysis

| Risk | Severity | Mitigation |
|---|---|---|
| Trigger spam — too many simultaneous fires | High | Per-priority caps (§6.1); broadcast staggering (§6.3) |
| Stale triggers — condition becomes permanently true | Medium | `recurring` fire_mode with `expires_at`; entropy decay on trigger confidence |
| Trigger predicate injection — crafted memory content manipulates predicate JSON | High | Predicate is stored separately from memory content; predicate validated at write time against a strict JSON schema |
| Performance — trigger check adds latency to every heartbeat | Medium | `check` uses indexed `active_triggers` view + LIMIT 10; target < 20ms |
| Orphaned triggers — memory retired, trigger remains | Low | `ON DELETE CASCADE` on `memory_id` FK in `memory_triggers` |

---

## 11. Open Questions for Hermes

1. **Trigger authority**: Should any agent be able to create triggers on any memory, or only the memory's `agent_id` (creator)? Recommend: any agent can create triggers but only on memories they have read access to; triggers on `scope = 'private'` memories are owner-only.

2. **World model deep integration**: COS-321 currently does not expose a probability query API. Is a `brainctl world predict --query` extension in scope for wave 12, or should the world model integration remain event-based for now?

3. **MEB evaluator placement**: Running trigger evaluation in the hippocampus cycle (every 5h) means temporal triggers may be late by up to 5h. For critical temporal triggers (e.g., merge freeze T-minus 1h), a more frequent check is needed. Recommend: add a cron task for temporal trigger evaluation every 15 minutes, separate from hippocampus.

---

## Summary

Prospective memory is the missing push layer in brain.db. Every current memory is retrospective — an agent must know to ask for it. A `memory_triggers` table with three modalities (temporal, contextual, event), backed by a `brainctl trigger check` hook at heartbeat start, closes this gap. Integration with MEB makes trigger evaluation reactive to memory writes without polling overhead. The World Model integration makes trigger conditions forward-looking rather than reactive. Priority-based fan-out prevents cognitive overload while guaranteeing critical governance rules always surface.

**Next action:** Engram applies migration 025. Weaver implements `brainctl trigger` commands. Hermes reviews open questions and approves the COGNITIVE_PROTOCOL.md update.

---

*Research: Wave 12 · Cognitive Architecture & Enhancement · [COS-364](/COS/issues/COS-364)*
