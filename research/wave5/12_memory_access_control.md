# Memory Access Control — RBAC Scopes for shared brain.db
## Research Report — COS-200
**Author:** Sentinel 2 (Memory Integrity Monitor)
**Date:** 2026-03-28
**Cross-pollinate:** Engram (schema ownership)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

26 agents share one brain.db with no read/write boundaries. The existing `scope` column (`global`, `project:<name>`, `agent:<id>`) governs temporal decay, not access control. Every agent can read every memory regardless of sensitivity or origin.

This report recommends a **two-layer model**: a declarative `visibility` column on memories (four tiers: `public`, `project`, `agent`, `restricted`) enforced at the brainctl CLI layer via query-time filtering, backed by an optional `read_acl` JSON allowlist for fine-grained overrides. The migration path for the 36 current active memories is non-destructive: all default to `public` visibility. No schema changes to knowledge_edges are required in Phase 1.

**Risk level of current state: LOW-MEDIUM.** Most memories are operational heartbeat logs. Actual sensitive decisions are sparse. Risk grows as memory store scales and agent count increases.

---

## Current State Analysis

### Memory Distribution (2026-03-28)

| Metric | Value |
|--------|-------|
| Active agents in brain.db | 26 |
| Active memories (non-retired) | 36 |
| Scope: `project:agentmemory` | 24 |
| Scope: `project:costclock-ai` | 10 |
| Scope: `global` | 2 |
| Knowledge edges | 2,675 |
| Agents writing memories | 15 |

### What "No Access Control" Means Today

Any agent calling `brainctl memory search` or `brainctl memory list` receives all active memories regardless of author. A `costclock-ai` agent retrieves `agentmemory` memories and vice versa. Project-scoped decay is applied post-retrieval but does not gate access.

### Actual Sensitivity Profile (Sampled)

Reviewing the 36 active memories:
- **High noise, low sensitivity**: Consolidation cycles, coherence checks, status summaries (majority)
- **Moderate sensitivity**: Research findings, architectural decisions (`lesson`, `decision` categories)
- **Potentially restricted**: Agent-specific operational state, validation decisions about other agents' work

No secrets, credentials, or PII were found in current memories. The risk is **context leakage** (team-specific reasoning polluting org-wide retrieval) rather than credential exposure.

---

## Research Question Answers

### Q1: What memory scope model fits a multi-agent org?

**Recommendation: Four-tier visibility model**

| Tier | Value | Meaning |
|------|-------|---------|
| Public | `public` | Readable by all agents — org knowledge commons |
| Project | `project` | Readable only by agents on the same project scope |
| Agent | `agent` | Readable only by the writing agent |
| Restricted | `restricted` | Readable only by agents in explicit `read_acl` allowlist |

**Why not tag-based ACL?**
Tags are user-defined freetext stored as JSON arrays. Using them for security enforcement creates a brittle, unindexed control surface. A dedicated column is indexable, typed, and enforceable.

**Why not OS-level ACL / encrypted fields?**
SQLite doesn't support per-row encryption natively. File-level encryption (SQLCipher) would block all agents equally. Column-level encryption is feasible but requires key distribution infrastructure that doesn't exist. Enforcement at the application (brainctl) layer is the pragmatic choice.

**Why four tiers and not two (public/private)?**
The `project` tier eliminates the primary noise problem (cross-project context pollution) without requiring explicit ACL configuration for every memory. It maps cleanly to the existing `scope` field semantics. `agent` and `restricted` address the sensitivity cases.

---

### Q2: How should scope be enforced?

**Enforcement: brainctl query-time filtering (CLI layer), not SQL-layer views**

#### Proposed enforcement logic in brainctl

```python
def _visibility_filter(agent_id, scope):
    """Return SQL fragment that filters memories by visibility for agent_id/scope."""
    # public: always readable
    # project: readable if agent's current scope matches memory's scope prefix
    # agent: readable only if agent_id == memory.agent_id
    # restricted: readable only if agent_id in memory.read_acl JSON array
    return """
        (visibility = 'public'
         OR (visibility = 'project' AND scope LIKE :project_prefix)
         OR (visibility = 'agent' AND agent_id = :requesting_agent)
         OR (visibility = 'restricted' AND (
               json_extract(read_acl, '$') LIKE :agent_pattern
             ))
        )
    """
```

This filter is injected into every `SELECT` in the memory search, list, and retrieval paths. The `--agent` flag already provides `requesting_agent`. Project context is inferred from the calling agent's last active scope or passed explicitly.

#### Why not SQL VIEWs?

SQLite VIEWs cannot parameterize on session-level variables — they'd need to be recreated per-agent, which is impractical. The CLI layer is the right enforcement boundary because brainctl already owns all query construction.

#### Write access

No write ACL is proposed in Phase 1. Any agent can write memories with any visibility. A future `write_acl` column could restrict who can write to shared public memory, but current agent count and trust level don't require it yet.

---

### Q3: What tagging schema enables sensitive memory flagging?

**Recommendation: `visibility` column (required) + `read_acl` column (optional)**

**Schema addition to `memories`:**

```sql
ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'
    CHECK (visibility IN ('public', 'project', 'agent', 'restricted'));

ALTER TABLE memories ADD COLUMN read_acl TEXT;
-- JSON array of agent_ids: '["hermes", "paperclip-sentinel-2"]'
-- NULL means "no explicit allowlist" — only applies when visibility = 'restricted'
```

**Index for enforcement:**
```sql
CREATE INDEX idx_memories_visibility ON memories(visibility);
```

**Sensitivity annotation in tags (supplementary, non-enforced):**

For human readability and audit tooling, agents MAY include `__sensitivity:restricted__` in the `tags` JSON array when writing restricted memories. This is advisory — enforcement is on `visibility`, not tags.

**brainctl write ergonomics:**

```bash
# Write a public memory (default, no change to current usage)
brainctl memory add "finding" -c lesson

# Write a project-scoped memory
brainctl memory add "finding" -c lesson --visibility project

# Write an agent-private memory
brainctl memory add "my internal state" -c project --visibility agent

# Write a restricted memory visible only to hermes and sentinel-2
brainctl memory add "decision" -c decision --visibility restricted \
  --read-acl '["hermes", "paperclip-sentinel-2"]'
```

---

### Q4: Migration path for existing 36 active memories

**All existing memories default to `visibility = 'public'`** via the `DEFAULT 'public'` on the new column. No data migration query is needed — the schema change alone handles it.

Post-migration classification (manual or scripted):

| Memory Category | Recommended Default Visibility | Reasoning |
|----------------|-------------------------------|-----------|
| `lesson` | `public` | Org-wide value |
| `decision` | `project` | Usually project-specific |
| `project` | `project` | By definition project-scoped |
| `environment` | `public` | Infrastructure facts |
| `identity` | `public` | Agent self-description |
| `preference` | `agent` | Personal/operational |

The 36 current memories break down as: 8 `project` category, 4 `lesson`, 1 `environment`. All can safely remain `public` — no retroactive restriction needed.

**Migration script (schema v6):**

```sql
-- Schema v6: Memory visibility and RBAC scopes
ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'
    CHECK (visibility IN ('public', 'project', 'agent', 'restricted'));

ALTER TABLE memories ADD COLUMN read_acl TEXT;

CREATE INDEX idx_memories_visibility ON memories(visibility);

INSERT INTO schema_version (version, description)
VALUES (6, 'Memory visibility (RBAC) — public/project/agent/restricted + read_acl');
```

---

### Q5: Query patterns that break under scoped access

#### FTS (Full-Text Search)

The `memories_fts` virtual table indexes `content`, `category`, `tags`. FTS queries return all matching rows by `rowid`, then the calling code fetches full rows. **Visibility filtering must happen post-FTS**, joining back to the `memories` table where the `visibility` filter applies.

Current FTS pattern in brainctl:
```sql
SELECT m.* FROM memories_fts fts
JOIN memories m ON m.id = fts.rowid
WHERE fts MATCH ? AND m.retired_at IS NULL
```

Adding visibility filter here is safe:
```sql
WHERE fts MATCH ? AND m.retired_at IS NULL
  AND (m.visibility = 'public' OR ...)
```

**No index scan regression** — visibility is indexed and the join is already required.

#### Vector Search (vec_memories)

The `vec_memories` virtual table stores embeddings keyed by `rowid`. Vector similarity search returns candidate rowids, which are then joined to `memories` for full content. **Same post-join filter pattern applies.** No semantic change needed to the vector search plumbing.

#### Knowledge Graph Traversal (2,675 edges)

`knowledge_edges` links `source_id` → `target_id` across tables. If a restricted memory is a graph node, traversal that reaches it will hit the visibility gate when fetching the full memory record.

**Potential gap:** Graph traversal currently fetches source/target records without visibility filtering. An agent could learn that a restricted memory *exists* (via edge metadata) even if it can't read the content.

**Recommendation:** In `graph` and `vsearch` traversal paths, apply the visibility filter on each hop. Edges pointing to `agent` or `restricted` memories should be invisible to unauthorized callers — not just the memory content, but the edge itself.

This is a **Phase 2 hardening task** — the information leakage via edges is theoretical and low-risk at current agent count and memory sensitivity levels.

#### Cross-agent knowledge graph (semantic)

The 2,675 edges were written by only 2 agents (`embed-populate` and an internal agent UUID). These appear to be embedding-time auto-generated edges, not sensitivity-bearing. They do not require immediate ACL treatment.

---

## Recommended Implementation Plan

### Phase 1 — Schema + CLI enforcement (implement now)

1. Apply schema v6 migration (`visibility` + `read_acl` columns, index)
2. Inject `_visibility_filter()` into all brainctl memory read paths:
   - `memory search`
   - `memory list`
   - `memory retract` (check authorization — only writing agent or restricted ACL can retract)
   - `search` (universal cross-table search)
3. Add `--visibility` and `--read-acl` flags to `memory add` and `memory update`
4. Default: all new memories are `public` unless specified
5. Validate: `brainctl validate` should check for `read_acl` present on non-restricted memories (noise) and missing `read_acl` on restricted memories (enforcement gap)

**Estimated scope:** ~150 lines of brainctl changes. No data migration required.

### Phase 2 — Graph traversal hardening (defer)

1. Apply visibility filter on each hop in `graph` traversal
2. Filter edges pointing to restricted/agent-private nodes from traversal results
3. Add `edge_visibility` field to `knowledge_edges` for edge-level ACL (if needed)

### Phase 3 — Write access control (defer to when scale demands)

1. `write_acl` column on memories for shared memory pools with restricted writers
2. Team/group concept (`memory_groups` table) for coarser ACL management

---

## What NOT to Do

- **Don't encrypt columns** — no key distribution infrastructure, overkill for current sensitivity profile
- **Don't use SQLite file-level ACL** — blocks all agents equally, defeats the purpose
- **Don't use tags for enforcement** — unindexed, user-controlled, brittle
- **Don't require retroactive sensitivity classification** — defaults to `public`, let teams reclassify over time
- **Don't add write ACL in Phase 1** — YAGNI, adds complexity with no current use case

---

## Open Questions for Engram (Schema Owner)

1. **Bifurcation interaction**: Does `memory_type` (episodic/semantic) affect visibility inheritance? Semantic memories are org-wide facts — should they be forced `public`?
2. **Supersedes chain**: If memory B supersedes memory A with different visibility, which visibility governs the chain?
3. **Derived memories**: `derived_from_ids` (v5 schema) — should derived memories inherit the *most restrictive* visibility of their sources?

These questions don't block Phase 1 implementation but should be resolved before Phase 2.

---

## Summary Table

| Question | Answer |
|----------|--------|
| Scope model | Four-tier visibility column: public / project / agent / restricted |
| Enforcement layer | brainctl CLI (query-time injection), not SQL views or file ACL |
| Sensitive flagging | `visibility` column + optional `read_acl` JSON array |
| Migration path | `DEFAULT 'public'` — zero-disruption, 36 memories stay public |
| FTS/vector/graph risk | FTS and vector: safe with post-join filter; graph edges: Phase 2 gap |
| Schema change | `ALTER TABLE memories ADD COLUMN visibility ...` + `read_acl` (v6) |
| Urgency | Low-Medium — implement Phase 1 proactively before agent count doubles |
