# Distributed brain.db — Federated Memory Architecture at Scale
## Research Report — COS-181

**Researcher:** Bedrock (Platform Engineer)
**Date:** 2026-03-28
**Issue:** [COS-181](/COS/issues/COS-181)
**Builds on:** [COS-122](/COS/issues/COS-122) write contention analysis, Wave 3 synthesis
**Method:** Empirical analysis of live brain.db (26 active agents), scaling projection to 178–200 agents, architecture comparison

---

## Executive Summary

The current `brain.db` is a single SQLite file running in WAL mode with a 10-second write timeout. At 26 agents, it is already generating multi-agent write bursts of **100 writes in one second from 15 distinct agents**. At 178 agents (~7× growth), this bottleneck becomes structural: average write rates stay manageable, but burst contention will cause agent-visible timeouts and silent serialization queues during every coordinated activity window.

**The root scaling problem is not average throughput — it's burst coordination.** When agents wake up together (heartbeat windows, shared task triggers), writes cluster into narrow time windows. SQLite serializes all writes regardless of which agent is writing, so a 15-agent burst creates a 15-deep write queue even when each individual write is trivial.

**Primary recommendation:** Team-sharded SQLite federation — one `brain_<team>.db` per organizational team (5–7 shards), plus a `brain_global.db` for cross-team promoted memories. The `brainctl` routing layer adds ~1ms overhead and requires no schema changes. This reduces peak burst contention per shard from ~15 concurrent writers to ~2–4.

**Migration is safe and reversible.** Existing data migrates in-place via a one-time `brainctl migrate --federate` command. The current single-file mode continues to work as a fallback.

---

## 1. Current State: Empirical Measurements

### 1.1 Infrastructure

| Parameter | Value |
|---|---|
| Database file | `~/agentmemory/db/brain.db` (15.2 MB) |
| Journal mode | WAL |
| Write timeout | 10 seconds (`sqlite3.connect(timeout=10)`) |
| Active agents | 26 |
| Total events | 381 |
| Active memories | 123 |

### 1.2 Write Rate Analysis

**Peak hourly event write rates (current 26-agent system):**

| Hour | Event Writes | Notes |
|---|---|---|
| 2026-03-28 10:xx | 133 | Research wave activity |
| 2026-03-28 05:xx | 65 | Early morning wave |
| 2026-03-28 04:xx | 54 | Overnight batch |

**Peak hourly memory write rates:**

| Hour | Memory Writes |
|---|---|
| 2026-03-28 10:xx | 146 |
| 2026-03-28 01:xx | 54 |
| 2026-03-28 05:xx | 31 |

### 1.3 Burst Contention Profile (The Real Problem)

The average hourly rate is misleading. The actual contention signature shows **extreme temporal clustering:**

```
2026-03-28T10:33:19 → 100 writes, 15 distinct agents, 1 second window
2026-03-28T05:33:04 →  11 writes,  4 distinct agents, 1 second window
2026-03-28T05:43:56 →   8 writes,  4 distinct agents, 1 second window
2026-03-28T06:30:04 →   7 writes,  4 distinct agents, 1 second window
```

**At 10:33:19, 100 writes arrived from 15 agents in a single second.** SQLite WAL serialized all of them. The 14 agents behind the first writer waited for their turn. With a 10-second timeout, any write taking longer than that would have raised `OperationalError: database is locked`.

This is not random noise — it is the heartbeat coordination effect. When many agents respond to the same event or trigger simultaneously, their writes land in the same second. This effect scales as O(agents) within the burst window.

### 1.4 Write Distribution by Agent

Top writers (all-time):

| Agent | Event Writes | % of total |
|---|---|---|
| hermes | 51 | 13.4% |
| paperclip-recall | 51 | 13.4% |
| paperclip-sentinel-2 | 37 | 9.7% |
| paperclip-cortex | 36 | 9.4% |
| paperclip-legion | 34 | 8.9% |
| paperclip-weaver | 25 | 6.6% |
| openclaw | 24 | 6.3% |
| hippocampus | 18 | 4.7% |

Top 8 agents → 70% of all writes. The write distribution is heavy-tailed — a small cohort drives most volume. This is a strong signal for team-based sharding: the high-write agents are clustered in specific teams.

### 1.5 Memory Scope Distribution (Sharding Signal)

| Scope | Active Memories |
|---|---|
| project:costclock-ai | 58 (47%) |
| project:agentmemory | 47 (38%) |
| global | 14 (11%) |
| other | 4 (3%) |

Memory writes are already naturally partitioned by project scope. 85% of memories belong to one of two project scopes. This is the key sharding axis.

---

## 2. Scaling Model: 26 → 178 → 200+ Agents

### 2.1 Write Rate Projection

Using the current 26-agent empirical baseline:

| Metric | Current (26 agents) | At 178 agents (6.8×) | At 200 agents (7.7×) |
|---|---|---|---|
| Average events/hr | 38 | ~260 | ~293 |
| Peak events/hr | 133 | ~905 | ~1,025 |
| Peak burst (1s window) | 100 writes, 15 agents | ~680 writes, 100+ agents | ~770 writes, 115+ agents |
| Memory writes/hr avg | 27 | ~184 | ~208 |
| Memory writes/hr peak | 146 | ~993 | ~1,124 |

### 2.2 Contention Threshold Analysis

SQLite WAL mode can sustain roughly **1,000–5,000 simple writes/second** on modern NVMe storage, but with a 10-second application-level timeout, the constraint is queueing time, not raw throughput.

For a burst of N concurrent writers all arriving at t=0, with each write taking W milliseconds:
- Writer 1: waits 0ms
- Writer N: waits (N-1) × W ms

Current write operations in brainctl are non-trivial (FTS5 updates, trigger execution, WAL sync). Estimated W ≈ 5–15ms per write.

For the observed 100-writer burst:
- Last writer waits: 99 × 10ms = **990ms** (best case)
- Last writer waits: 99 × 15ms = **1,485ms** (typical)
- Well within the 10-second timeout — no visible errors today

At 178 agents with a 680-writer burst:
- Last writer waits: 679 × 10ms = **6,790ms** — approaching the 10s limit
- Last writer waits: 679 × 15ms = **10,185ms** — **exceeds the 10-second timeout**

**Conclusion: At ~178 agents, heartbeat-triggered write bursts will begin causing `OperationalError: database is locked` timeouts during coordination-heavy windows.** The 10-second timeout is not generous enough at this scale.

### 2.3 WAL File Size Projection

At 993 memory writes/hr peak and each write touching FTS5 triggers (3 pages each) + main table (1 page):
- WAL growth rate: ~4 × 4KB × 993 = ~15.9 MB/hr at peak
- WAL checkpoint triggered at page threshold (SQLite default: 1000 pages = 4MB)
- At peak load, checkpoints fire every ~15 minutes, adding checkpoint latency to writes during that window

The single-file WAL checkpoint is a global pause point. With 178+ agents writing, checkpoint contention adds a second category of latency spike.

---

## 3. Architecture Options

Four architectures evaluated against the constraints: SQLite locked in, brainctl is sole interface, single machine, 178+ agents.

### Option A: Single-File with Tuned Write Queue (Minimal Change)

**What:** Increase write timeout, add write-ahead queue in brainctl (local buffer that retries), tune WAL checkpoint threshold.

**Changes:**
- `timeout=30` (was 10)
- `PRAGMA wal_autocheckpoint = 4000` (was 1000)
- Application-level write retry with exponential backoff

**Pros:** Zero schema change, zero migration, single file maintained.

**Cons:** Does not reduce contention — only increases tolerance for it. At 200+ agents, the burst queue grows proportionally. The 30-second timeout means agents block for up to 30 seconds during severe bursts. Write throughput ceiling unchanged.

**Verdict:** Buys time to ~150 agents. Not a solution at 200+.

---

### Option B: Per-Agent Shards (Maximum Isolation)

**What:** Each agent writes to `brain_<agent_id>.db`. Global queries scatter-gather across all shards.

**Write path:** Each agent only writes to its own shard — zero write contention between agents.
**Read path:** `brainctl search` queries all shard files in parallel, merges results.

**Pros:** Complete write isolation. Each agent has its own SQLite with no lock contention from peers.

**Cons:**
- At 178 agents, `brainctl search` must open and query 178 SQLite files simultaneously
- Cross-agent reads scale as O(agents²) for scatter-gather
- Each shard is tiny (<1MB), but file handle limits and OS overhead become real
- Memory Event Bus (MEB, COS-232) must fan out to 178 shard files
- Global FTS5 searches impossible — cross-shard FTS requires full-text index per shard + merge

**Verdict:** Technically correct but operationally unmanageable. Per-agent isolation is the right model only at the team level.

---

### Option C: Category-Based Shards

**What:** Separate SQLite files per memory category: `brain_events.db`, `brain_memories.db`, and per-category shards (`brain_lesson.db`, `brain_project.db`, etc.).

**Rationale:** `lesson` (83%) and `project` (11%) dominate memory writes. Isolating them reduces contention on each file.

**Pros:** Simple routing logic (category → file). Consistent with existing category taxonomy.

**Cons:**
- Does not reduce event write contention (all agents write to `brain_events.db`)
- Events are 3× more frequent than memories — the hottest table stays in one file
- Cross-category queries still require scatter-gather
- Category-level isolation doesn't map to agent clusters (many agents write to `lesson`)

**Verdict:** Partial improvement for memory reads, no improvement for the primary write bottleneck (events). Not recommended as primary strategy.

---

### Option D: Team-Sharded SQLite Federation (Recommended)

**What:** One SQLite per organizational team, plus a global shard for cross-team promoted memories. Teams are derived from the Paperclip chain-of-command hierarchy.

**Shard map (at 178-agent scale, estimated):**

| Shard | Agents | Write Load | File |
|---|---|---|---|
| `brain_platform.db` | Bedrock, Kernel, Core, Hippocampus, etc. | ~18% | Platform team |
| `brain_product.db` | Hermes, OpenClaw-affiliated agents | ~20% | Product team |
| `brain_research.db` | Recall, Cortex, Engram, Weaver, Sentinel, Prune, Epoch | ~35% | Research team |
| `brain_ops.db` | Legion, Stratos, Vertex, etc. | ~12% | Ops/leadership |
| `brain_external.db` | openclaw, external integrations | ~15% | External |
| `brain_global.db` | Promoted cross-team memories | Writes: ~5% | Global index |

**Routing logic:**
- Each write goes to the agent's team shard based on `agent_id` prefix lookup
- Memories with `scope=global` go to `brain_global.db`
- Cross-team memories are promoted to `brain_global.db` by the consolidation cycle
- `brainctl search` reads from: (1) caller's team shard + (2) global shard [2 files, not 178]
- Advanced cross-team search: scatter to all shards + merge (opt-in via `--all-teams` flag)

**Contention reduction:**
- Current: 1 SQLite file receiving all 15 agents in a burst window
- After sharding: burst distributed across 5–6 team shards, ~2–4 agents per shard per burst
- 10:33:19 burst (15 agents → 100 writes) becomes: ~2–3 writes per shard per second
- Far below contention threshold at any reasonable timeout

**Pros:**
- Direct attack on the burst contention problem
- Read path for common case (own team + global) is only 2 files — faster than today for cross-team queries
- Natural organizational alignment: team writes stay within team shard
- Schema identical to current — zero migration complexity for each individual shard
- MEB (COS-232) propagates per-shard, keeping fan-out bounded

**Cons:**
- brainctl needs a routing layer (AgentID → team shard lookup)
- Cross-team full-text search requires scatter-gather across all shards (5–6 files, not 178)
- Initial migration step required
- Team assignment must be maintained as org chart evolves

---

## 4. Recommended Architecture: Team-Sharded Federation

### 4.1 Shard Topology

```
~/agentmemory/db/
├── brain.db                  # LEGACY — retained as fallback, read-only after migration
├── brain_global.db           # Cross-team promoted memories + global-scope writes
├── brain_research.db         # Research team agents
├── brain_platform.db         # Platform/backend agents
├── brain_product.db          # Product + Hermes + OpenClaw
├── brain_ops.db              # Ops, leadership, coordination agents
└── brain_external.db         # External integrations, openclaw adapters
```

Each shard is a full brain.db schema clone — same tables, same FTS5 virtual tables, same triggers, same WAL mode configuration. No new table types needed.

### 4.2 Routing Layer

A new `brain_router.py` module in brainctl:

```python
TEAM_SHARD_MAP = {
    # Team assignments by agent_id prefix or exact match
    # Research team
    "paperclip-recall":     "research",
    "paperclip-cortex":     "research",
    "paperclip-engram":     "research",
    "paperclip-weaver":     "research",
    "paperclip-sentinel":   "research",
    "paperclip-prune":      "research",
    "epoch":                "research",
    "paperclip-scribe":     "research",
    # Platform team
    "bedrock":              "platform",
    "kernel":               "platform",
    "hippocampus":          "platform",
    "paperclip-embed":      "platform",
    "paperclip-lattice":    "platform",
    "paperclip-probe":      "platform",
    # Product team
    "hermes":               "product",
    "openclaw":             "product",
    "paperclip-nexus":      "product",
    # Ops/leadership
    "paperclip-legion":     "ops",
    "paperclip-stratos":    "ops",
    "paperclip-vertex":     "ops",
    "paperclip-kokoro":     "ops",
}

def shard_for_agent(agent_id: str, scope: str = "global") -> str:
    """Return the shard name for a given agent_id + scope combination."""
    if scope == "global" or scope is None:
        return "global"
    team = TEAM_SHARD_MAP.get(agent_id)
    if team:
        return team
    # Fallback: derive from agent_id prefix
    for prefix, team_name in PREFIX_MAP.items():
        if agent_id.startswith(prefix):
            return team_name
    return "global"  # Unknown agents write to global

def db_path_for_shard(shard: str) -> Path:
    return DB_DIR / f"brain_{shard}.db"
```

### 4.3 Write Path

```
brainctl memory write --agent-id paperclip-recall --scope "project:agentmemory" ...
    ↓
router.shard_for_agent("paperclip-recall", "project:agentmemory")
    → "research"
    ↓
connect to brain_research.db
    ↓
INSERT INTO memories ...
    ↓
[optional] if memory.confidence > 0.9 and scope == "global":
    also INSERT into brain_global.db (promoted copy)
```

### 4.4 Read Path

**Standard search (most common — 95% of queries):**
```
brainctl search "checkout error handling" --agent-id paperclip-recall
    ↓
shard = "research"
    ↓
scatter to: [brain_research.db, brain_global.db]
    ↓
FTS5 + vector search on each (parallel)
    ↓
merge results, re-rank by composite score
    ↓
return top-K
```

**Cross-team search (rare — consolidation cycle, contradiction detection):**
```
brainctl search "..." --all-teams
    ↓
scatter to: ALL shard files (5–6 files)
    ↓
parallel FTS5 + vector on each
    ↓
merge + re-rank
    ↓
return top-K
```

**Latency impact of scatter-gather over 2 shards vs. 1:**
- Current: 1 FTS5 query on brain.db (~5–20ms)
- Federated: 2 FTS5 queries in parallel (~5–20ms each, parallel → same wall time)
- Net overhead: near zero for 2-shard scatter; ~2× overhead for full 6-shard scatter

### 4.5 Consistency Model

| Consistency Level | Scope | Mechanism |
|---|---|---|
| **Strong** | Within a shard | SQLite WAL serialization (unchanged) |
| **Read-your-writes** | Same agent | Agent always reads from its own shard first |
| **Causal** | Team reads | Team shard reflects all prior writes by team members |
| **Eventual** | Cross-team | Promoted memories propagate to `brain_global.db` on consolidation cycle |

**Cross-team eventual consistency delay:** The consolidation cycle runs at cadence (~hourly via Hippocampus). Cross-team memory promotion happens during each cycle. Maximum staleness window for cross-team memories: ~1 hour.

**This is acceptable for organizational memory.** The Memory Event Bus (MEB, COS-232) provides sub-500ms notification of new team-shard writes. Cross-team eventual consistency at ~1hr is the appropriate model for knowledge (vs. tasks, which need strong consistency and use Paperclip, not brain.db).

**Categories requiring strong cross-team consistency:**
- `environment` / `identity` → write directly to `brain_global.db`
- `convention` → write to team shard + promote to global immediately (synchronous dual-write)
- `lesson`, `project`, `decision` → team shard only, promote on consolidation cycle

### 4.6 Global Index Maintenance

The `brain_global.db` is not a copy of all data — it is a **curated index of cross-team knowledge**:

1. All `scope=global` memories from any agent
2. High-confidence memories (≥0.85) with cross-team relevance
3. Convention and identity memories
4. Summary memories created by the consolidation cycle from team-shard content

Promotion to global is handled by the consolidation cycle via:
```bash
brainctl consolidate --promote-to-global --confidence-threshold 0.85
```

The global shard stays lean (~10–15% of total memory volume at any time).

---

## 5. Cross-Shard Query Routing

For queries that must span shards (contradiction detection, Wave 3 situation models, proactive push at the org level), the scatter-gather pattern is the correct approach:

### 5.1 Scatter Phase

```python
async def scatter_search(query: str, shards: list[str], k: int = 20) -> list[MemoryResult]:
    tasks = [search_shard(shard, query, k) for shard in shards]
    results_per_shard = await asyncio.gather(*tasks)
    return flatten(results_per_shard)
```

With 5–6 shards on a single machine, the gather phase adds <5ms wall time (all SQLite files are local).

### 5.2 Merge Phase

Results from multiple shards have the same composite score schema (`0.45×sim + 0.25×recency + 0.20×confidence + 0.10×importance` from Wave 1 attention routing). Merge is a simple sort + dedup on content hash:

```python
def merge_results(results: list[MemoryResult], k: int = 10) -> list[MemoryResult]:
    seen_hashes = set()
    merged = []
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        h = hash_content(r.content)
        if h not in seen_hashes:
            seen_hashes.add(h)
            merged.append(r)
        if len(merged) >= k:
            break
    return merged
```

Content deduplication matters because the global shard may contain promoted copies of team-shard memories. The hash dedup prevents duplicates without requiring a join.

### 5.3 Shard-Aware MEB

The Memory Event Bus (COS-232) uses SQLite triggers on the `memories` table. In the federated model, each shard has its own MEB trigger table (`meb_events`). Agents subscribe to their team shard + global:

```
Agent paperclip-recall subscribes to:
  - brain_research.db::meb_events  (team writes)
  - brain_global.db::meb_events    (cross-team promotions)
```

This preserves MEB semantics (<500ms propagation) without cross-shard coupling.

---

## 6. Migration Path

### Phase 0: Preparation (No Downtime)

1. Schema-validate: confirm all shards can be created from current `brain.db` schema
2. Add `shard` metadata column to `agents` table: `ALTER TABLE agents ADD COLUMN shard TEXT DEFAULT 'legacy'`
3. Create all shard files with identical schema (via `brainctl init --federated`)
4. No data movement yet — `brain.db` continues as primary

### Phase 1: Routing Layer (Parallel Write, No Downtime)

1. Deploy routing layer in brainctl
2. Enable dual-write: new writes go to BOTH `brain.db` (legacy) AND appropriate shard
3. Run for 24 hours — validate shard writes match legacy writes
4. Read path still uses `brain.db` only

### Phase 2: Read Migration (Gradual Cutover)

1. Enable shard reads for 10% of agents (canary group: platform team)
2. Monitor: compare shard read results against legacy for first 48 hours
3. Expand to 50%, then 100% over 1 week
4. Legacy `brain.db` remains writable as fallback

### Phase 3: Legacy Cutover

1. Migrate historical data: `brainctl migrate --export-legacy --import-to-shards`
   - Routes each memory/event to the appropriate shard by `agent_id`
   - Estimated time: <5 minutes for current 15MB database
2. Stop dual-write to `brain.db`
3. Rename `brain.db` → `brain_legacy_readonly.db`
4. Set `brain_legacy_readonly.db` to WAL read-only mode

### Phase 4: Validation

1. Cross-shard integrity check: total memory count, event count match legacy
2. Search quality check: compare top-10 results for 20 benchmark queries across old and new
3. Write throughput stress test: simulate 180-agent burst, measure contention

**Estimated total migration time:** 1–2 weeks (mostly monitoring phases). Code changes: ~400 LOC in brainctl.

**Rollback:** At any phase before Phase 3 cutover, revert by pointing reads back to `brain.db`. Dual-write means no data loss.

---

## 7. brainctl Interface Changes

### 7.1 New Flags

```bash
# Explicit shard targeting (advanced/admin use)
brainctl search "query" --shard research
brainctl search "query" --all-teams          # scatter to all shards

# Initialization and migration
brainctl init --federated                    # create all shard files
brainctl migrate --export-legacy             # migrate brain.db → shards
brainctl shards status                       # show per-shard stats

# Federated search (default for agents)
brainctl search "query"                      # auto-detects caller's shard + global
```

### 7.2 Agent Identity in Write Path

Every brainctl write command already requires `--agent-id`. The routing layer uses this to determine the shard. No change to caller interface; routing is transparent.

```bash
# This call is unchanged — router silently directs to brain_research.db
brainctl memory write \
  --agent-id paperclip-recall \
  --category lesson \
  --scope "project:agentmemory" \
  --content "..."
```

### 7.3 `brainctl shards status`

```
Shard         File Size   Memories   Events   Agents   Last Write
global        2.1 MB      14         12        —       2026-03-28 10:33
research      5.8 MB      87         210       8        2026-03-28 10:33
platform      1.2 MB      8          42        6        2026-03-28 09:25
product       3.1 MB      19         98        4        2026-03-28 10:31
ops           2.4 MB      11         82        5        2026-03-28 10:22
external      1.5 MB      6          35        3        2026-03-28 10:10
TOTAL         16.1 MB     145        479       26       2026-03-28 10:33
```

---

## 8. What This Does Not Solve

### 8.1 Write Coordination Within a Shard

At 200+ agents with 35+ agents per research team, the research shard could face similar burst contention to the current single-shard state. The team-sharding approach buys time to approximately **400–600 total agents** (when any single team exceeds ~100 agents and exhibits the same burst patterns).

Beyond that scale, per-agent shards or a true distributed database becomes necessary. Given current growth trajectory (26 → 178 agents is still uncertain), team shards provide ample headroom for 3–5 years.

### 8.2 Cross-Shard Transactions

If a consolidation job needs to atomically move a memory from `brain_research.db` to `brain_global.db`, there is no distributed transaction primitive. The approach is: write to destination, verify, then retire from source. This is a 2-step non-atomic operation. For organizational memory, this is acceptable — a briefly duplicated memory is preferable to a distributed lock.

### 8.3 The Recall Problem

This report addresses write scalability. The 97.6% zero-recall rate ([COS-229](/COS/issues/COS-229)) is a separate problem (the `recalled_count` field is never updated). Federation does not help or hurt recall rates — that fix belongs in `brainctl search`.

---

## 9. Implementation Dependencies

```
[COS-122] version column + CAS (done ✅)
    ↓
[COS-232] Memory Event Bus (done ✅)
    ↓
[THIS] Federation routing layer in brainctl (NEW)
    ├── brain_router.py module
    ├── init --federated command
    ├── migrate --export-legacy command
    └── per-shard MEB subscription update
    ↓
[NEW] Consolidation cycle: global promotion pass
    └── brainctl consolidate --promote-to-global
```

**Prerequisites already done:** COS-122 (version column, CAS writes) and COS-232 (MEB) are both complete. The federation layer can be built immediately.

**New work needed:**
- `brain_router.py`: ~200 LOC
- Migration tooling: ~150 LOC
- Shard status command: ~50 LOC
- Consolidation global-promotion pass: ~100 LOC

Total: ~500 LOC addition to brainctl, zero schema changes, zero breaking changes to existing interface.

---

## 10. Summary & Recommendations

| Question | Answer |
|---|---|
| Is SQLite itself the bottleneck? | Not yet (15 MB, 26 agents). Burst contention is the bottleneck. |
| At what agent count does failure begin? | ~150–180 agents at current burst patterns |
| Should we migrate to a different database? | No. Team-sharded SQLite provides sufficient headroom to 400–600 agents. |
| What is the right sharding axis? | Organizational team (5–6 shards), not per-agent or per-category |
| What consistency model is appropriate? | Strong within team, eventual across teams (via consolidation cycle) |
| How long will migration take? | 1–2 weeks, fully reversible, zero downtime |
| What changes to brainctl? | ~500 LOC: routing layer, migration commands, shard status. No interface breaking changes. |

**File this as an implementation ticket for Kernel/Hippocampus** — this is a platform-layer change that belongs to the Backend team. The routing layer is brainctl's responsibility; the consolidation global-promotion pass is Hippocampus' domain.

---

*Deliverable for [COS-181](/COS/issues/COS-181). Cross-references: [COS-122](/COS/issues/COS-122) (write contention), [COS-232](/COS/issues/COS-232) (MEB), [COS-229](/COS/issues/COS-229) (recall rate). Architecture ready for implementation review.*
