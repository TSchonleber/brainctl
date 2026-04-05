# Agent-to-Agent Knowledge Transfer Protocol
## Real-Time Learning Propagation Across Running Agents

**Research Task:** [COS-177](/COS/issues/COS-177)
**Researcher:** Weaver (Context Integration Engineer)
**Wave:** 4 — Critical Gap
**Date:** 2026-03-28
**Deliverable:** Architecture proposal with protocol spec, latency analysis, and brainctl implementation sketch

---

## Executive Summary

When Agent A learns something and writes to brain.db, agents B through Z currently see it only on their next `brainctl search` — which may be their *next heartbeat*, hours later. For organizational learning to be genuinely real-time, this gap must close to seconds.

This report proposes the **Memory Event Bus (MEB)**: a lightweight, file-based pub/sub system that propagates memory write events to running agents with <500ms end-to-end latency, zero external dependencies, and a design that degrades gracefully on a single SQLite-backed machine with 200+ agents.

**Core recommendation:** A SQLite trigger → shared event table → agent polling loop architecture. Not a full message broker. Not gRPC. Not a daemon. Three files and a 30-line Python hook.

---

## Problem Statement

```
t=0    Agent A learns: "Paperclip checkout API returns 409 when run ID conflicts"
t=0    Agent A writes memory M to brain.db
t=0    Agent B is mid-heartbeat, working on a checkout-related task
t=?    Agent B has no way to know M exists until it issues brainctl search
t=30m  Agent B's next heartbeat: issues brainctl search, finds M, incorporates it
```

**The gap is 30 minutes.** In the interim, Agent B may:
- Make a wrong decision based on stale knowledge
- Waste tool calls investigating something Agent A already figured out
- Post a comment contradicting Agent A's freshly-written conclusion
- Hit the same blocker Agent A just resolved

At 178+ agents running simultaneously, stale knowledge isn't an edge case — it's the steady state. Every significant write to brain.db creates a propagation gap.

---

## Investigative Framework

Three propagation models to evaluate:

| Model | Description | Latency | Complexity | Reliability |
|---|---|---|---|---|
| **Poll-on-search** | Current behavior. Agent sees new memories only when it queries | 0–30min | None | N/A — it's the baseline |
| **Push-on-write** | When A writes, MEB notifies B | <1s | Medium | High (synchronous) |
| **Invalidation signal** | When A writes, MEB marks B's cached context stale | <1s | Low | High |
| **Subscription** | B subscribes to topics; A's writes on those topics notify B | <1s | High | Medium (subscription state) |
| **Gossip protocol** | Agents propagate updates to random peers | <10s | High | High (eventual) |

**Verdict:** For this infrastructure (SQLite, single machine, no daemon), **invalidation signal via a shared event table** is the right choice. It's the simplest mechanism that closes the propagation gap without requiring a broker or persistent connections.

---

## Architecture Proposal: Memory Event Bus (MEB)

### Core Concept

A single new SQLite table — `memory_events` — acts as a lightweight message bus. Writes append events. Readers poll on their heartbeat cycle. No daemon required.

```
┌─────────────────────────────────────────────────────────────────┐
│                     MEMORY EVENT BUS (MEB)                      │
│                                                                  │
│   brain.db                                                       │
│   ┌─────────────────────┐    ┌──────────────────────────────┐   │
│   │   memories table    │───▶│     memory_events table      │   │
│   │  (existing)         │    │  • event_id (PK)             │   │
│   │                     │    │  • event_type (write/update/ │   │
│   └─────────────────────┘    │    delete/invalidate)        │   │
│           ▲                  │  • memory_id (FK)            │   │
│           │                  │  • author_agent_id           │   │
│   SQLite AFTER               │  • topic_tags (JSON)         │   │
│   INSERT/UPDATE trigger      │  • created_at (epoch ms)     │   │
│                              │  • ttl_until (epoch ms)      │   │
│                              └──────────────┬───────────────┘   │
│                                             │                   │
│                              ┌──────────────▼───────────────┐   │
│                              │     agent_subscriptions       │   │
│                              │  • agent_id                  │   │
│                              │  • topic_filter (JSON)       │   │
│                              │  • last_seen_event_id        │   │
│                              │  • last_polled_at            │   │
│                              └──────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Schema

```sql
-- Memory event log (write-ahead, append-only)
CREATE TABLE IF NOT EXISTS memory_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,           -- 'write' | 'update' | 'delete' | 'invalidate'
    memory_id INTEGER,                  -- FK to memories.id (nullable for bulk invalidations)
    author_agent_id TEXT NOT NULL,
    author_session TEXT,                -- which heartbeat run produced this
    topic_tags TEXT DEFAULT '[]',       -- JSON: categories/topics affected
    memory_body_preview TEXT,           -- first 200 chars (avoids full table join on poll)
    created_at INTEGER NOT NULL,        -- Unix epoch ms
    ttl_until INTEGER,                  -- when to stop propagating (optional, default +24h)
    FOREIGN KEY (memory_id) REFERENCES memories(id)
);

CREATE INDEX idx_memory_events_created ON memory_events(created_at DESC);
CREATE INDEX idx_memory_events_topics ON memory_events(topic_tags);  -- JSON index (SQLite 3.38+)

-- Agent subscription registry
CREATE TABLE IF NOT EXISTS agent_subscriptions (
    agent_id TEXT PRIMARY KEY,
    topic_filter TEXT DEFAULT NULL,     -- JSON array of topic patterns, NULL = all events
    last_seen_event_id INTEGER DEFAULT 0,
    last_polled_at INTEGER,
    active BOOLEAN DEFAULT TRUE
);

-- SQLite trigger: emit event on memory write
CREATE TRIGGER IF NOT EXISTS memory_write_event
AFTER INSERT ON memories
BEGIN
    INSERT INTO memory_events (event_type, memory_id, author_agent_id, topic_tags, memory_body_preview, created_at)
    SELECT
        'write',
        NEW.id,
        NEW.agent_id,
        json_array(NEW.category, NEW.temporal_class),
        SUBSTR(NEW.body, 1, 200),
        CAST(strftime('%s', 'now') * 1000 AS INTEGER);
END;

-- SQLite trigger: emit event on memory update
CREATE TRIGGER IF NOT EXISTS memory_update_event
AFTER UPDATE ON memories
BEGIN
    INSERT INTO memory_events (event_type, memory_id, author_agent_id, topic_tags, memory_body_preview, created_at)
    SELECT
        'update',
        NEW.id,
        NEW.agent_id,
        json_array(NEW.category, NEW.temporal_class),
        SUBSTR(NEW.body, 1, 200),
        CAST(strftime('%s', 'now') * 1000 AS INTEGER);
END;
```

### Protocol Spec: MEB-v1

**Event flow:**

```
1. WRITE_PHASE
   Agent A writes memory M to brain.db
   → SQLite trigger fires automatically
   → memory_events row inserted with event_id=N, topic_tags=[category, class]
   → (no other action required from Agent A)

2. PROPAGATION_PHASE (occurs on next poll, typically at next brainctl call)
   Agent B calls: brainctl events poll [--since last_seen_event_id] [--topics filter]
   → Returns events where event_id > last_seen_event_id AND topics match filter
   → Updates agent_subscriptions.last_seen_event_id = max(returned event_ids)
   → Returns: [{event_id, event_type, memory_id, author_agent_id, topic_tags, preview}]

3. INCORPORATION_PHASE (agent decision)
   Agent B receives events, decides whether to:
   a. Immediately retrieve: brainctl memory get <memory_id>
   b. Soft-flag for retrieval: note memory_id as "available but not loaded"
   c. Ignore: event not relevant to current task (by topic filter)
```

**brainctl commands added:**

```bash
# Subscribe to all events (agent registers interest)
brainctl events subscribe [--topics "security,auth,billing"]

# Poll for new events since last poll (used at start of heartbeat)
brainctl events poll [--since <event_id>] [--topics filter] [--limit 20]

# Manually emit an event (for bulk invalidations or manual broadcasts)
brainctl events emit --type invalidate --topics "auth" --body "Auth token format changed"

# Inspect event backlog (diagnostic)
brainctl events tail -n 20
```

**Integration with existing heartbeat startup:**

```bash
# Standard heartbeat preamble (runs before brainctl search):
brainctl -a $AGENT_ID events poll --limit 10 > /tmp/new_events.json
# If new_events.json non-empty, agent reviews before proceeding with task
```

---

## Subscription Models

### Model 1: Broadcast (simplest, recommended for initial implementation)
All events go to all agents. No subscription filtering. Each agent reads the full event log since last poll. Agents decide locally whether an event is relevant.

**Pros:** Zero subscription management. Always correct (no missed events).
**Cons:** At 200+ agents with high write rates, each agent reads many irrelevant events.
**Verdict:** Fine for current scale (<50 active memories, <20 concurrent agents). Revisit at 500+ memories/day write rate.

### Model 2: Topic-Filtered Subscription
Each agent declares interests (e.g., `["security", "auth", "billing"]`). Event polling filters by topic.

```sql
SELECT e.*
FROM memory_events e
JOIN agent_subscriptions s ON s.agent_id = $agent_id
WHERE e.event_id > s.last_seen_event_id
  AND (s.topic_filter IS NULL OR
       EXISTS (
           SELECT 1 FROM json_each(e.topic_tags) t1
           JOIN json_each(s.topic_filter) t2 ON t1.value = t2.value
       ))
ORDER BY e.event_id ASC
LIMIT 20;
```

**Pros:** Reduces noise at scale. Agents only process relevant events.
**Cons:** Subscription management overhead. Topics must be well-defined.
**Verdict:** Implement after Broadcast proves value. Use memory categories from existing schema as topic vocabulary.

### Model 3: Importance-Threshold Subscription
Agents only receive events where `importance >= threshold`. Threshold configurable per agent.

```sql
WHERE e.event_id > s.last_seen_event_id
  AND m.importance >= s.min_importance_threshold  -- join back to memories table
```

**Pros:** CEO/manager agents only see high-importance events.
**Cons:** Requires join on memories table; importance scoring must be accurate.
**Verdict:** Valuable for senior agents (Hermes, CEO). Add as optional flag.

---

## Invalidation Strategies

Three types of invalidation signals:

### 1. Content Invalidation
Memory M was updated. Agents who have M in their working context should reload it.

```python
# brainctl events poll output includes:
{"event_type": "update", "memory_id": 42, "preview": "Auth token format is now JWT..."}
# Agent B has memory 42 in context from earlier this heartbeat
# → Agent B knows to re-fetch before using it again
```

### 2. Topic Invalidation
A cluster of memories in topic X changed. Agents working on topic X should re-query.

```python
{"event_type": "invalidate", "memory_id": null, "topic_tags": ["auth", "security"]}
# Agent B is working on a security task
# → Agent B should run: brainctl search "auth security" to get fresh results
```

### 3. Dependency Invalidation (advanced)
Memory M depends on Memory N (via the knowledge graph from Wave 1). If N is updated, M may be stale. Emit invalidation for M's dependents.

```python
# COS-177 note: this requires the knowledge graph from 03_knowledge_graph.py
# Traverse reverse edges from updated node, emit invalidation events for dependents
# Depth limit: 2 hops max (beyond that, relevance too diluted)
```

---

## Event-Driven Memory Bus Alternatives (Evaluated)

### Alternative A: File-based pub/sub (inotify/FSEvents)
Use filesystem events on brain.db (or a sidecar file) to signal writes. Agents use `inotify_add_watch` (Linux) or `FSEventStreamCreate` (macOS) to detect changes.

**Analysis:** Platform-specific. Requires a background listener process per agent. Doesn't provide structured event data (what changed, by whom, which topics). Fragile on SQLite WAL checkpoints. **Not recommended.**

### Alternative B: SQLite WAL tail
Agents tail the Write-Ahead Log file directly to detect new writes. Parse WAL page headers to extract modified rowids.

**Analysis:** Extremely fragile. WAL format is undocumented and version-dependent. WAL files are transient and get checkpointed. Requires SQLite internals knowledge. **Not recommended.**

### Alternative C: Redis pub/sub
Use a local Redis instance as the message broker. Agents publish/subscribe via standard Redis pub/sub.

**Analysis:** Requires Redis running as a daemon. Adds external dependency. Messages are ephemeral (no replay). But: very fast (<1ms local delivery), battle-tested pub/sub semantics. **Viable if Redis is already in the stack. Not worth adding as new dependency for this problem alone.**

### Alternative D: UNIX domain sockets / named pipes
Each agent listens on a named socket. `brainctl memory add` broadcasts to all listening sockets.

**Analysis:** Requires each agent to run a listener daemon (not compatible with heartbeat model — agents don't run between heartbeats). Named pipes block on write if no reader. **Not compatible with heartbeat architecture.**

### Alternative E: SQLite shared event table (MEB — recommended)
As described above. Append-only event log, polled at heartbeat start.

**Analysis:** Works natively with existing SQLite infrastructure. No daemon required. Survives agent restarts. Replayable. Trivially inspectable (`SELECT * FROM memory_events ORDER BY created_at DESC LIMIT 20`). **Recommended.**

---

## Latency Analysis

### End-to-End Latency (MEB-v1)

```
Agent A writes memory:
  INSERT INTO memories → ~2ms SQLite write
  Trigger fires → INSERT INTO memory_events → ~1ms
  Total write overhead: ~3ms (vs current: ~2ms, +50% but negligible absolute)

Event propagation gap:
  Worst case: Agent B just started its heartbeat, won't poll for ~2 minutes
  Best case: Agent B polls at heartbeat start, sees event immediately → <5ms

Effective propagation latency: 3ms (write) + 0–120s (poll interval)
```

**To achieve <30s propagation:** Add a mid-heartbeat poll point. When agent calls `brainctl search`, also check event log for new events on same topics.

**To achieve <5s propagation:** Requires an active polling loop or an external notification. Not feasible with heartbeat-only execution model without a lightweight sidecar.

**Practical target:** <120s (next heartbeat boundary). This closes the current 30-minute gap by 15×.

### SQLite Performance at Scale

```
Event writes: 1 per memory write
  At 50 writes/day → 50 rows/day (trivial)
  At 5000 writes/day → 5000 rows/day → 1.8M rows/year

Query per poll: SELECT WHERE event_id > N
  With index on event_id: ~0.5ms for any table size
  With topic filter: ~2ms (JSON index, SQLite 3.38+)

Cleanup: events with ttl_until < NOW() are safe to delete
  Run cleanup in consolidation cycle (Wave 1, already scheduled)
  Keep last 10,000 events regardless of TTL (rolling window audit)
```

**Conclusion:** SQLite handles this comfortably. The event table adds negligible overhead.

---

## Implementation Sketch for brainctl

### Phase 1: Schema + Triggers (1 day)
Add `memory_events` and `agent_subscriptions` tables to the brainctl migration.
Add SQLite triggers for INSERT and UPDATE on `memories`.

### Phase 2: `brainctl events poll` command (1 day)
```python
@cli.command()
@click.option('--since', type=int, default=None, help='Event ID to poll from')
@click.option('--topics', default=None, help='Comma-separated topic filter')
@click.option('--limit', default=20)
@click.pass_context
def poll(ctx, since, topics, limit):
    """Poll for new memory events since last check."""
    agent_id = ctx.obj['agent_id']
    db = ctx.obj['db']

    # Get last seen event if --since not specified
    if since is None:
        row = db.execute(
            "SELECT last_seen_event_id FROM agent_subscriptions WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        since = row['last_seen_event_id'] if row else 0

    # Build query
    base_query = """
        SELECT event_id, event_type, memory_id, author_agent_id,
               topic_tags, memory_body_preview, created_at
        FROM memory_events
        WHERE event_id > ?
        ORDER BY event_id ASC
        LIMIT ?
    """
    events = db.execute(base_query, (since, limit)).fetchall()

    # Filter by topics if specified
    if topics:
        topic_set = set(t.strip() for t in topics.split(','))
        events = [e for e in events
                  if topic_set & set(json.loads(e['topic_tags'] or '[]'))]

    # Update cursor
    if events:
        max_id = max(e['event_id'] for e in events)
        db.execute("""
            INSERT INTO agent_subscriptions (agent_id, last_seen_event_id, last_polled_at)
            VALUES (?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                last_seen_event_id = excluded.last_seen_event_id,
                last_polled_at = excluded.last_polled_at
        """, (agent_id, max_id, int(time.time() * 1000)))

    # Output JSON
    click.echo(json.dumps([dict(e) for e in events], indent=2))
```

### Phase 3: Heartbeat hook integration (0.5 days)
```bash
# In the post-checkout hook (Paperclip harness):
EVENTS=$(brainctl -a $AGENT_ID events poll --limit 10)
if [ -n "$EVENTS" ] && [ "$EVENTS" != "[]" ]; then
    echo "--- NEW MEMORY EVENTS ---"
    echo "$EVENTS" | jq -r '.[] | "[\(.event_type)] \(.author_agent_id): \(.memory_body_preview)"'
    echo "---"
fi
```

### Phase 4: Monitoring + cleanup (0.5 days)
Add `memory_events` cleanup to the consolidation cycle:
```sql
DELETE FROM memory_events
WHERE ttl_until < strftime('%s', 'now') * 1000
  AND event_id < (SELECT MAX(event_id) FROM memory_events) - 10000;
```

---

## Protocol Spec Summary (MEB-v1)

| Field | Value |
|---|---|
| **Transport** | SQLite table (memory_events) |
| **Event emission** | Automatic via SQL triggers on memories INSERT/UPDATE |
| **Polling** | `brainctl events poll` — at heartbeat start + mid-heartbeat search |
| **Subscription** | Row in agent_subscriptions (implicit on first poll) |
| **Topic vocabulary** | memory.category + memory.temporal_class (extensible to custom tags) |
| **Message retention** | 24h TTL + rolling 10k window |
| **Propagation guarantee** | At-least-once within one heartbeat cycle (~2 min worst case) |
| **Ordering guarantee** | Strict (AUTOINCREMENT event_id, cursor-based polling) |
| **Back-pressure** | Limit 20 events per poll; agents skip if overloaded |
| **Version** | MEB-v1 — single-node, SQLite-native |

---

## New Questions Raised

1. **Cross-node MEB:** When we shard brain.db across machines (Wave 4 candidate #5), the event bus needs a cross-shard relay. MEB-v1 assumes single SQLite file. How do we federate event propagation without a centralized broker? Answer may be: each shard has its own event table, and a lightweight replication agent copies high-importance events to peer shards.

2. **Event ordering across agents:** If Agent A and Agent B write to brain.db simultaneously, SQLite's single-writer lock ensures ordered writes, but from the perspective of Agent C polling, it sees both events in event_id order — which may not reflect causality. Does MEB need a vector clock or Lamport timestamp for causal ordering? Probably not at current scale, but worth flagging.

3. **What do agents actually DO with events mid-heartbeat?** Polling is easy. The harder problem is: an agent is 80% through its task, receives a "memory updated" event about something it used earlier in this heartbeat. Does it re-derive its conclusions? This is an agent behavior problem (agent rationality), not a protocol problem. MEB delivers the signal; incorporating it correctly requires agent-level logic.

4. **Can MEB carry non-memory signals?** The event table structure is generic. Could it carry: "Agent X finished task Y" signals? "Hermes published a new directive"? If yes, MEB becomes a general intra-agent message bus — very powerful but scope creep. Recommend: keep MEB memory-specific in v1, evaluate general event bus as a separate proposal.

---

## Assumptions That May Be Wrong

1. **"Agents don't need sub-second propagation"** — The 2-minute heartbeat cycle assumption means <2min propagation is "good enough." But some agents run long tasks (>30 min) without a heartbeat boundary. The post-checkout hook adds mid-task polling, but a task could run for an hour without any poll point if the agent doesn't call brainctl. Need to ensure heartbeat-aware polling at minimum every 5 minutes in long-running tasks.

2. **"All knowledge is in brain.db"** — Some agent learning happens in working memory (never written to brain.db). The MEB can only propagate what gets written. If agents are solving complex problems in context without writing to brain.db, we never capture or propagate that learning. The incentive to write good memories must be maintained.

3. **"SQLite handles concurrent reads well"** — With WAL mode, SQLite allows parallel reads. But if 20 agents are polling simultaneously, they all trigger the `SELECT WHERE event_id > ?` query. With proper indexing this is fine. Without the index, it's a full table scan per agent per poll. **Must ensure index creation is not optional.**

---

## Highest-Impact Follow-Up Research

**Single recommendation:** **Cross-Agent Belief Reconciliation** (Wave 4 candidate #3 in FRONTIER.md).

MEB ensures that when Agent A updates a memory, Agent B is notified. But notification doesn't guarantee reconciliation. Agent B may have drawn conclusions from the old version of that memory that are now incorrect — and those conclusions may already be written to other memories, or posted in comments that influenced other agents. The belief reconciliation problem asks: after an invalidation event, which downstream inferences need to be revisited? This is the hardest open problem in the cognitive architecture and the one that would complete the real-time learning propagation story that MEB starts.

---

## References

- Gray, J., & Helland, P. (2010). *The Five-Minute Rule for Trading Memory for Disk Accesses.* Applied to event propagation latency tradeoffs.
- Lamport, L. (1978). Time, clocks, and the ordering of events in a distributed system. *CACM.*
- SQLite documentation on WAL mode, triggers, and JSON functions.
- Oki, B., & Liskov, B. (1988). Viewstamped replication — for understanding ordered event propagation.
- Demers, A. et al. (1987). Epidemic algorithms for replicated database maintenance. *PODC.* — gossip protocol reference.
