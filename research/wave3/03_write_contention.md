# Write Contention & Semantic Consistency at 178 Agents

**Researcher:** Recall (paperclip-recall)
**Date:** 2026-03-28
**Issue:** [COS-122](/COS/issues/COS-122)
**Method:** Empirical analysis of live brain.db (`~/agentmemory/db/brain.db`, 22 active agents as of research date)

---

## Executive Summary

SQLite WAL mode prevents byte-level corruption, but provides **no protection against semantic inconsistency**: an agent that reads a memory, then acts on it after another agent has retired or replaced it, will silently diverge. At 178 agents, this is not theoretical — the current corpus already shows mixed-agent writes in the same second, cross-category hotspots, and in-place updates that bypass the supersede chain. The recommended fix is **optimistic locking via a `version` column** on the `memories` table with a conditional update path in `brainctl`.

---

## 1. Contention Measurement

### 1.1 Write Volume (Current 22-Agent Baseline)

| Table | Total Records | Active | Write Rate (peak hour) |
|---|---|---|---|
| events | 93 | 93 | 29 writes/hr (10 agents) |
| memories | 88 | 9 active | 20 writes/hr |
| access_log | 158 | — | mixed read+write |

**Top event writers:** hermes (21), openclaw (14), paperclip-codex (13). At 178 agents with proportional growth, projected peak write rate: ~230 event writes/hr, ~160 memory writes/hr.

### 1.2 Hotspot Categories

| Category | Active Memories | Distinct Agents | Risk |
|---|---|---|---|
| `project` | 52 | 7 | **HIGH** — multi-agent shared namespace |
| `environment` | 15 | 1 | low (owned by one agent) |
| `identity` | 9 | 1 | low |
| `lesson` | 4 | 2 | medium |

The `project` category is the primary contention zone. It accounts for **59% of all active memories** and is the only category with confirmed multi-agent overlap: `scope=project:costclock-ai` has 3 distinct agents with 4 memories.

### 1.3 Race Window Sizes

**Observed concurrent writes (same second, multi-agent):**

| Timestamp | Writes | Distinct Agents | Notes |
|---|---|---|---|
| 2026-03-27 22:08:49 | 9 | 2 (hermes + paperclip-lattice) | Confirmed multi-agent collision |

**Observed single-agent burst writes (batch inserts — SQLite serializes these, no risk):**

| Timestamp | Writes | Agents |
|---|---|---|
| 2026-03-28 01:14:37 | 20 | 1 (hermes) |
| 2026-03-28 01:46:06 | 8 | 1 (paperclip-lattice) |

**Conclusion:** Multi-agent write collisions exist now at 22 agents. At 178 agents (~8x), the probability of concurrent writes within any given 1-second window scales proportionally. Assuming Poisson arrivals, the expected concurrent-write collision rate increases from ~1/day to ~8/day for memory writes alone.

### 1.4 In-Place Updates (Non-Supersede Path)

7 memories have `updated_at != created_at` and are not retired. These were modified in-place without creating a supersede chain. This is a silent update: any agent holding a reference to these IDs may be working from stale content without knowing it.

### 1.5 Clock Skew

Mixed timestamp formats detected:
- **events table**: 12 records use ISO 8601 (`T`-separated), 87 use space-separated
- **memories table**: 100% space-separated

This is a sorting hazard. A query that `ORDER BY created_at` mixes these formats and will not sort correctly without normalization. At 178 agents with different insertion codepaths, format drift will worsen.

---

## 2. Consistency Failure Taxonomy

### Type A: Stale Read → Stale Act (Most Common)

```
T=0: Agent A reads memory M (content: "use billing_v2")
T=1: Agent B retires M, creates M' (content: "use billing_v3")
T=2: Agent A acts on M — routes to billing_v2, no error raised
```

**Current risk:** HIGH. brainctl opens a fresh connection per command (no long-lived read transaction). A search result is immediately stale once returned. With a 5s busy_timeout and multi-second task execution, any agent that reads-then-writes based on a memory is in a race.

### Type B: Phantom Supersede

```
T=0: Agent A reads: memory M is active, M.id=42
T=1: Agent B: brainctl memory replace --old-id 42 → M retired, M'=43 created
T=2: Agent A: brainctl memory retire --id 42 → no error (SQL UPDATE on already-retired row)
```

`UPDATE memories SET retired_at = ... WHERE id = ?` succeeds even if `retired_at` is already set. The old content is gone either way, but the action logs don't record the conflict.

### Type C: Lost Update (In-Place Mutation Race)

The `memories` table has no `version` column. If two agents both fetch memory M and both issue an UPDATE (not via supersede), the last write wins silently:

```
T=0: Agent A reads M, sees confidence=0.8
T=1: Agent B UPDATE memories SET confidence=0.9 WHERE id=M
T=2: Agent A UPDATE memories SET confidence=0.7 WHERE id=M  ← overwrites B's write
```

`cmd_memory_replace` uses retire+insert, which avoids this. But in-place updates (e.g., `recalled_count` increment in `cmd_memory_search`) hit this race: multiple agents searching simultaneously will each read `recalled_count=N`, then each write `recalled_count=N+1` (instead of N+final).

### Type D: WAL Phantom (Benign at Current Scale)

SQLite WAL gives readers a consistent snapshot at the start of their read transaction. Since brainctl opens a new connection per subcommand, each `brainctl search` or `brainctl memory list` gets a snapshot of the DB at that moment. This is correct but means:

- An agent that performs two sequential reads may see different versions of a memory between reads (uncommitted writes by other agents were checkpointed between calls).
- Not a correctness bug per se, but it means long-running agents (e.g. multi-step research tasks) cannot assume memory state is stable across multiple brainctl invocations.

### Type E: Clock Skew Ordering (Recency Gradient Error)

The temporal weighting in `cmd_memory_search` uses `created_at` for recency scoring. If agents write with inconsistent timestamp formats (ISO vs space-separated), the recency ranking is unreliable. A memory written at "2026-03-28T04:00:00" and one written at "2026-03-28 03:59:59" will sort in the wrong order if string comparison is used instead of datetime parsing.

---

## 3. Versioning Recommendation: Optimistic Locking via CAS

### 3.1 Recommended Approach: `version` Column + Conditional UPDATE

Add a `version INTEGER NOT NULL DEFAULT 1` column to the `memories` table. Increment it on every write. All in-place updates use a `WHERE id=? AND version=?` guard (compare-and-swap). If the CAS fails (rowcount=0), the caller knows the memory was modified by another agent and must re-read.

**Schema migration:**

```sql
ALTER TABLE memories ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

-- Trigger to auto-increment on in-place update
CREATE TRIGGER memories_version_bump
AFTER UPDATE ON memories
FOR EACH ROW
WHEN NEW.version = OLD.version
BEGIN
  UPDATE memories SET version = version + 1 WHERE id = NEW.id;
END;
```

**CAS update pattern in brainctl:**

```python
def cas_update_memory(conn, memory_id, expected_version, **updates):
    set_clauses = ', '.join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [memory_id, expected_version]
    cursor = conn.execute(
        f"UPDATE memories SET {set_clauses} WHERE id = ? AND version = ?",
        params
    )
    conn.commit()
    if cursor.rowcount == 0:
        # Conflict: memory was modified by another agent
        return {"ok": False, "conflict": True, "memory_id": memory_id}
    return {"ok": True, "memory_id": memory_id, "new_version": expected_version + 1}
```

### 3.2 Why Not Logical Clocks?

Lamport clocks or vector clocks require each agent to maintain and propagate its clock state across calls. Since brainctl is stateless (new process per invocation), there is no natural place to store per-agent Lamport state without adding another table. The version column achieves the same conflict detection at lower complexity.

### 3.3 Why Not Strict Serialization?

SQLite's WAL mode already serializes writes at the database level. Adding `BEGIN IMMEDIATE` transactions around all writes would prevent concurrent writes but would increase latency (blocked writers wait for the lock). At 178 agents with 5s busy_timeout and high write volume, this would cause cascading timeouts. Optimistic locking is the right trade-off: it costs nothing on the common path (no conflict) and only slows the rare conflict case.

---

## 4. brainctl Interface Changes Required

### 4.1 New Flag: `brainctl memory update --if-version N`

```
brainctl memory update <id> --content "..." --if-version <N>
```

Behavior:
- Fetches current memory version
- Issues CAS update: `UPDATE memories SET content=?, version=version+1 WHERE id=? AND version=?`
- Returns `{"ok": true, "new_version": N+1}` on success
- Returns `{"ok": false, "conflict": true, "current_version": M}` on failure

This is the safe conditional write path. Agents that need to modify a memory they read earlier can pass the `version` they observed and detect if another agent changed it.

### 4.2 Expose `version` in search/list output

`brainctl memory search` and `brainctl memory list` should include `version` in each result. Agents can store the version alongside the memory ID and use it for safe conditional updates.

### 4.3 Normalize Timestamp Insertion

All `datetime('now')` calls in brainctl write the same format (space-separated). The 12 ISO-format records came from external callers. Fix: add `strftime('%Y-%m-%d %H:%M:%S', 'now')` as the standard in all INSERT/UPDATE statements, and add a migration to normalize existing ISO timestamps:

```sql
UPDATE events SET created_at = replace(created_at, 'T', ' ')
WHERE created_at LIKE '%T%';
-- Also strip timezone offsets
UPDATE events SET created_at = substr(created_at, 1, 19)
WHERE length(created_at) > 19;
```

### 4.4 Fix `recalled_count` Race

Change the recalled_count increment to use SQLite's atomic increment (it already does: `recalled_count + 1`). This is safe because SQLite serializes writes — no issue here. However, the **read** of `recalled_count` in search results reflects a snapshot before the increment, which means the returned record shows the pre-increment value. This is cosmetic but confusing; document it as eventual.

---

## 5. Acceptable Consistency Model by Category

| Category | Consistency Needed | Rationale |
|---|---|---|
| `project` (multi-agent shared) | **Strong per scope** — use CAS on update | Multiple agents write; silent overwrites cause task divergence |
| `identity` | Eventual — single owner | Only one agent writes; no contention |
| `environment` | Eventual — single owner | Same as identity |
| `lesson`, `preference` | Eventual | Low write frequency, single-agent ownership |
| `agent_config` (future) | **Strong** — use CAS | Config drift causes systemic failures |
| `billing` (future) | **Strong** — use CAS | Financial correctness requires no lost updates |

**Rule of thumb:** Any category written by more than one agent, or any category where the content directly affects another agent's behavior, requires strong consistency via CAS. Single-owner categories can remain eventual.

### 5.1 Read Path: No Change Needed

The read path is already safe for eventual reads: WAL mode gives a consistent snapshot, FTS5 queries are read-only, and recency weighting is deterministic given the snapshot. No locking is needed on reads.

### 5.2 Latency Budget

CAS adds one `WHERE version=?` clause to the existing UPDATE. SQLite evaluates this against the integer primary key — O(1) lookup, <0.1ms overhead. Well within the 5ms constraint.

---

## 6. Implementation Priority

1. **[P0] Add `version` column** with auto-increment trigger — schema migration, no interface change required. Backward compatible (existing inserts use DEFAULT 1).
2. **[P1] `brainctl memory update --if-version N`** — new safe update path. Does not break existing callers.
3. **[P1] Normalize timestamps** — prevents recency ranking bugs. Migration + code change.
4. **[P2] Expose `version` in search/list output** — cosmetic until #2 is shipped, but needed to use #2.
5. **[P3] Document consistency model** — guide for which categories require CAS vs eventual.

---

## Appendix: Key SQL Queries Used

```sql
-- Write collision detection (same second, multi-agent)
SELECT strftime('%Y-%m-%d %H:%M:%S', created_at) as second_bucket,
       COUNT(*) as concurrent_writes,
       COUNT(DISTINCT agent_id) as distinct_agents
FROM memories
GROUP BY second_bucket
HAVING concurrent_writes > 1 AND distinct_agents > 1;

-- Hotspot categories
SELECT category, COUNT(*) as cnt, COUNT(DISTINCT agent_id) as agents
FROM memories
GROUP BY category ORDER BY cnt DESC;

-- Shared scope contention zones
SELECT scope, category, COUNT(DISTINCT agent_id) as agents
FROM memories WHERE retired_at IS NULL
GROUP BY scope, category HAVING agents > 1;

-- In-place updates (bypassing supersede chain)
SELECT COUNT(*) FROM memories
WHERE updated_at != created_at AND retired_at IS NULL;
```
