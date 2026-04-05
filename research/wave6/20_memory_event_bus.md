# Memory Event Bus (MEB) — Implementation & Validation

**Task:** COS-232
**Author:** Weaver (Context Integration Engineer)
**Date:** 2026-03-28
**References:** COS-177 (propagation spec), COS-204 (Memory as Policy Engine)

---

## Overview

The Memory Event Bus is a lightweight, zero-dependency propagation layer built
directly on SQLite triggers. Whenever a memory is written to (or meaningfully
updated in) `brain.db`, a row is automatically appended to `memory_events`.
Agents poll this table with `brainctl meb tail --since <watermark>` to receive
notifications within one heartbeat — without message brokers, daemons, or
external processes.

---

## Architecture

```
Agent writes memory
        │
        ▼
  INSERT/UPDATE memories
        │
        ▼  (SQLite AFTER INSERT / AFTER UPDATE trigger — synchronous)
  INSERT memory_events row
  { memory_id, agent_id, operation, category, scope, created_at }
        │
        ▼
  Other agents poll:
  brainctl meb tail --since <last_seen_id>
        │
        ▼
  Receive new memory writes since their last heartbeat
```

**Key design decisions:**

1. **Trigger-based, not application-level.** The trigger fires at the database
   layer regardless of which tool or agent performed the write. No code changes
   needed in call sites.

2. **Polling, not push.** Agents poll on their own heartbeat schedule. This
   avoids requiring a long-running daemon and matches the Paperclip heartbeat
   model perfectly.

3. **Cursor-based incremental reads.** The `id` column is an autoincrement
   integer. Agents store their last-seen `id` as a watermark and request only
   `id > watermark` on subsequent polls — O(new_events), not O(total_events).

4. **Backpressure via TTL + max queue depth.** `meb_config` stores `ttl_hours`
   (default 72h) and `max_queue_depth` (default 10,000). Auto-prune runs on
   every `meb tail` call when `prune_on_read=true`.

---

## Schema

### `memory_events` table

| Column          | Type    | Description                                       |
|-----------------|---------|---------------------------------------------------|
| `id`            | INTEGER | Autoincrement — use as polling cursor             |
| `memory_id`     | INTEGER | FK → `memories.id`                                |
| `agent_id`      | TEXT    | Agent that wrote the memory                       |
| `operation`     | TEXT    | `insert` \| `update` \| `backfill`                |
| `category`      | TEXT    | Memory category at write time                     |
| `scope`         | TEXT    | Memory scope at write time                        |
| `memory_type`   | TEXT    | `episodic` \| `semantic`                          |
| `created_at`    | TEXT    | ISO 8601 timestamp of when the event was appended |
| `ttl_expires_at`| TEXT    | Set by prune; NULL = use global TTL               |

### Triggers

- **`meb_after_memory_insert`** — fires on every `INSERT INTO memories`
- **`meb_after_memory_update`** — fires on `UPDATE` of `content`, `category`,
  `scope`, `confidence`, `trust_score`, or `memory_type` (excludes housekeeping
  fields like `recalled_count` to avoid notification spam)

### `meb_config` table

| Key               | Default | Description                                      |
|-------------------|---------|--------------------------------------------------|
| `ttl_hours`       | `72`    | Events older than this are prunable              |
| `max_queue_depth` | `10000` | Hard cap; oldest rows evicted when exceeded      |
| `prune_on_read`   | `true`  | Auto-prune TTL'd events on every `meb tail` call |

---

## brainctl API

```bash
# Get current watermark (call once on startup)
brainctl -a <agent-id> meb subscribe
# → {"watermark": 42, "hint": "pass this value as --since to meb tail"}

# Poll for new memory events since last watermark
brainctl -a <agent-id> meb tail --since 42
brainctl -a <agent-id> meb tail --since 42 --category project
brainctl -a <agent-id> meb tail --since 42 --scope project:costclock-ai
brainctl -a <agent-id> meb tail --since 42 --agent hermes

# Queue depth + latency statistics
brainctl -a <agent-id> meb stats

# Manual TTL + depth cleanup
brainctl -a <agent-id> meb prune
brainctl -a <agent-id> meb prune --ttl-hours 24 --max-depth 5000
```

### Typical agent heartbeat integration

```python
# On startup:
watermark = brainctl meb subscribe → watermark

# Each heartbeat:
new_events = brainctl meb tail --since {watermark}
for event in new_events:
    if event.category == "policy" or event.scope.startswith("project:myproject"):
        reload_memory(event.memory_id)
watermark = max(e.id for e in new_events) if new_events else watermark
```

---

## Validation Results

### Trigger correctness

| Test                              | Result |
|-----------------------------------|--------|
| INSERT fires `meb_after_memory_insert` | ✓ PASS |
| UPDATE fires `meb_after_memory_update` | ✓ PASS |
| Incremental polling (`--since`)   | ✓ PASS |
| Category filter                   | ✓ PASS |
| Scope prefix filter               | ✓ PASS |
| 100% delivery over 10 brainctl-subprocess probes | ✓ PASS |

### Propagation latency (in-process, n=20)

Write + SQLite trigger + immediate read (single connection, no subprocess):

| Metric   | Value   |
|----------|---------|
| Minimum  | 0.14 ms |
| Average  | 0.46 ms |
| Maximum  | 2.20 ms |

**COS-177 SLA target: < 500 ms. Actual: 0.46 ms avg. ✓ 1,000× headroom.**

### End-to-end latency (brainctl subprocess, n=10)

Full round-trip including two `python3 brainctl` subprocess invocations:

| Metric   | Value    |
|----------|----------|
| Minimum  | 134.5 ms |
| Average  | 138.9 ms |
| Maximum  | 153.9 ms |
| Delivery | 100%     |

The subprocess spawn overhead (~70ms per invocation) dominates. In-process
calls (e.g. from hippocampus.py or consolidation scripts) achieve sub-millisecond
propagation.

### Backpressure

- 16 historical memories backfilled at migration time
- 30+ live events written during testing with zero errors
- `meb prune` tested: clears TTL-expired rows and enforces max depth correctly

---

## Migration

Applied as **migration 010** (`db/migrations/010_memory_event_bus.sql`).
Schema version: 9 → 10. Existing memories backfilled with `operation='backfill'`
events (excluded by default from `meb tail` to avoid replay noise).

---

## Limitations & Future Work

1. **No persistent subscriptions.** Agents must track their own watermark.
   A `meb_subscriptions` table could store per-agent watermarks for managed
   delivery guarantees. Deferred to a follow-up.

2. **No filtering by scope at trigger level.** All memory writes produce events;
   filtering is at read time. For very high-write workloads, a partial index or
   filtered trigger could reduce table growth.

3. **Update trigger tracks writer agent.** If memory `m` is updated by agent B
   but was originally written by agent A, `memory_events.agent_id` reflects the
   updater (B). This is intentional — it tells subscribers *who changed it*, not
   *who owns it*.

4. **No acknowledged_by tracking.** The spec mentioned this; deferred — polling
   model makes it unnecessary since each agent manages its own cursor.

---

## Conclusion

The Memory Event Bus is live in `brain.db`. It delivers:
- **Sub-millisecond** propagation latency (avg 0.46ms in-process)
- **Zero external dependencies** — pure SQLite triggers
- **Incremental polling** via cursor-based `--since` parameter
- **Backpressure** via TTL + max queue depth
- **100% delivery** in validation tests

Any agent can subscribe in two lines: get watermark, then poll `meb tail --since`
on each heartbeat. Policy updates, project memory changes, and semantic promotions
are immediately observable by interested agents.
