# Adversarial Robustness & Memory Integrity

**Task:** COS-115
**Researcher:** Sentinel 2 — Memory Integrity Monitor
**Wave:** 2 (Conceptual Foundations)
**Date:** 2026-03-28
**Status:** Complete

---

## Executive Summary

This report analyzes six threat vectors against brain.db and proposes a layered defense system. Key findings:

- **Content-addressable hashing** detects unauthorized modification of memory records with O(1) verification cost
- **Embedding poisoning** is the highest-risk attack vector — adversarial insertions can hijack vector search without triggering keyword-based checks
- **Byzantine fault tolerance** for 22+ agents requires a 2-of-3 validation quorum model, not full BFT consensus
- **Self-healing** should operate on a 3-tier escalation: auto-repair (contradictions), flag-for-review (trust anomalies), quarantine (integrity violations)
- The existing trust/retraction system (COS-121/COS-189) provides the foundation — this report adds the **attack prevention** layer

---

## 1. Threat Model

### 1.1 Attack Surface

brain.db serves 22+ agents with varying privilege levels. Attack vectors:

| Vector | Severity | Likelihood | Current Defense |
|--------|----------|------------|-----------------|
| Direct DB modification | Critical | Low | File permissions, WAL mode |
| Memory poisoning (bad content) | High | Medium | `trust_score`, retraction |
| Embedding poisoning (adversarial vectors) | Critical | Medium | **None** |
| FTS5 query injection | Medium | Low | `_sanitize_fts_query()` |
| Causal chain manipulation | High | Low | `causal_chain_root` tracking |
| Agent impersonation | Critical | Low | Agent registration, but no auth |
| Stale data amplification | Medium | High | Temporal decay, but no expiry enforcement |

### 1.2 Attacker Models

**Compromised Agent:** A registered agent whose outputs become unreliable (hallucination, model drift, or adversarial prompt injection). This is the most likely scenario with 22+ agents.

**External Modification:** Direct SQLite file modification bypassing brainctl. Lower likelihood but catastrophic impact.

**Poisoned Source:** An agent faithfully recording information from a compromised external source (e.g., a hallucinating upstream LLM, tampered API response).

---

## 2. Memory Integrity Verification

### 2.1 Content-Addressable Hashing

Add a `content_hash` column to `memories` for tamper detection:

```sql
ALTER TABLE memories ADD COLUMN content_hash TEXT;
CREATE INDEX idx_memories_content_hash ON memories(content_hash);
```

Hash computation (SHA-256 over canonical fields):

```python
import hashlib, json

def compute_memory_hash(content, category, scope, agent_id, created_at):
    """Deterministic hash over immutable fields."""
    canonical = json.dumps({
        "content": content,
        "category": category,
        "scope": scope,
        "agent_id": agent_id,
        "created_at": created_at
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
```

**Verification protocol:**
1. On `brainctl memory add`: compute and store hash
2. On `brainctl validate`: recompute hashes for all active memories, flag mismatches
3. On `brainctl memory search`: optionally verify hash before returning results (configurable, off by default for performance)

**Cost:** ~0.5ms per memory verification. For 93 memories: ~46ms total scan. Negligible.

### 2.2 Hash Chain for Temporal Ordering

Link memories into a hash chain to detect insertion/deletion attacks:

```python
def compute_chain_hash(memory_hash, previous_chain_hash):
    """Each memory's chain_hash depends on its own hash + the previous one."""
    return hashlib.sha256(
        f"{previous_chain_hash}:{memory_hash}".encode('utf-8')
    ).hexdigest()
```

```sql
ALTER TABLE memories ADD COLUMN chain_hash TEXT;
ALTER TABLE memories ADD COLUMN chain_parent_id INTEGER;
```

**Trade-off:** Hash chains provide strong ordering guarantees but are expensive to maintain during concurrent writes (22 agents writing simultaneously). Recommendation: **Use per-agent hash chains** rather than a global chain. Each agent maintains its own chain, and cross-agent verification happens during scheduled integrity sweeps.

### 2.3 Merkle Tree for Bulk Verification

For periodic integrity audits, build a Merkle tree over all active memories:

```python
def build_merkle_tree(memory_hashes):
    """Build a binary Merkle tree. Returns root hash."""
    if not memory_hashes:
        return hashlib.sha256(b"empty").hexdigest()
    layer = [hashlib.sha256(h.encode()).hexdigest() for h in memory_hashes]
    while len(layer) > 1:
        next_layer = []
        for i in range(0, len(layer), 2):
            left = layer[i]
            right = layer[i + 1] if i + 1 < len(layer) else left
            combined = hashlib.sha256(f"{left}{right}".encode()).hexdigest()
            next_layer.append(combined)
        layer = next_layer
    return layer[0]
```

Store the Merkle root in `agent_state` after each validation sweep:

```python
brainctl state set sentinel-2 merkle_root '{"hash": "<root>", "memory_count": 93, "computed_at": "2026-03-28T01:30:00"}'
```

**Verification frequency:** Every `brainctl validate` run (currently ad-hoc, should be scheduled).

---

## 3. Hallucination Detection

### 3.1 Cross-Reference Verification

When an agent writes a memory, check it against existing knowledge:

```python
def detect_hallucination(new_content, db, threshold=0.85):
    """Flag memories that contradict existing high-confidence knowledge."""
    # 1. Vector search for semantically similar memories
    similar = vsearch(new_content, limit=10)

    # 2. Check for contradictions
    contradictions = []
    for mem in similar:
        if mem.confidence >= 0.8 and semantic_contradiction(new_content, mem.content):
            contradictions.append({
                "conflicting_memory_id": mem.id,
                "similarity": mem.similarity,
                "confidence": mem.confidence
            })

    # 3. Check source reliability
    agent_trust = get_agent_trust_score(agent_id, category)

    return {
        "contradictions": contradictions,
        "agent_trust": agent_trust,
        "risk_score": len(contradictions) * 0.3 + (1 - agent_trust) * 0.7
    }
```

### 3.2 Semantic Contradiction Detection

The existing `06_contradiction_detection.py` (Wave 1) provides the foundation. Extend it with:

1. **Negation patterns:** "X is Y" vs "X is not Y" — detectable via NLI (natural language inference)
2. **Temporal contradictions:** "As of 2026-01, X uses Y" vs "As of 2026-03, X uses Z" — check if temporal scoping resolves the conflict
3. **Scope contradictions:** Global claims vs project-specific claims — project-scoped memories should override global ones within their scope

### 3.3 Source Verification Chain

Every memory should trace back to an observable source:

```
Memory → source_event_id → Event → refs (external source)
```

Memories with no traceable source chain should receive a `provenance_quality` penalty:

| Provenance Depth | Quality Score |
|-----------------|---------------|
| Memory ← Event ← External Source | 1.0 |
| Memory ← Event (no external ref) | 0.7 |
| Memory (no source_event_id) | 0.4 |
| Memory (retracted source) | 0.0 |

---

## 4. Adversarial Robustness in Retrieval

### 4.1 Embedding Poisoning Attacks

**Attack:** An adversarial agent inserts memories with embeddings crafted to be near target queries but semantically unrelated. Example: A memory about "billing" with an embedding near "security policy" — when someone searches for security policies, they get billing noise.

**Detection:**

```python
def detect_embedding_anomaly(memory_id, embedding, content, model="nomic-embed-text"):
    """Check if a memory's embedding is consistent with its content."""
    # Re-embed the content independently
    fresh_embedding = embed(content, model=model)

    # Compare stored vs fresh embedding
    cosine_sim = cosine_similarity(embedding, fresh_embedding)

    if cosine_sim < 0.95:  # Threshold for "same content, same model"
        return {
            "anomaly": True,
            "stored_vs_fresh_similarity": cosine_sim,
            "memory_id": memory_id
        }
    return {"anomaly": False}
```

**Mitigation:**
1. **Re-embedding on validation:** During `brainctl validate`, re-embed a sample of memories and compare
2. **Embedding provenance:** Store the model name and version used for embedding (already in `embeddings.model`)
3. **Dual-path search:** Require both FTS5 keyword match AND vector similarity for high-confidence results. The existing hybrid search (`vsearch`) partially addresses this — formalize it as a security control

### 4.2 Retrieval Result Validation

Before returning search results to agents, apply a post-retrieval filter:

```python
def validated_search(query, limit=10):
    results = hybrid_search(query, limit=limit * 2)  # Over-fetch

    validated = []
    for r in results:
        # Skip retracted
        if r.retracted_at:
            continue
        # Skip low-trust agents in this category
        if get_agent_trust(r.agent_id, r.category) < 0.3:
            continue
        # Skip expired
        if r.expires_at and r.expires_at < now():
            continue
        validated.append(r)

    return validated[:limit]
```

### 4.3 Query Injection Prevention

Current FTS5 sanitization (`_sanitize_fts_query`) strips special characters. Additional hardening:

1. **Query length limits:** Cap FTS queries at 500 characters
2. **Term count limits:** Max 20 terms per query
3. **Encoding normalization:** Normalize Unicode before search to prevent homograph attacks

---

## 5. Byzantine Fault Tolerance for Multi-Agent Memory

### 5.1 Why Full BFT is Overkill

Classical BFT (e.g., PBFT) requires 3f+1 nodes to tolerate f Byzantine faults. With 22 agents, this means we could tolerate 7 compromised agents — but at the cost of requiring consensus for every memory write.

**Our scenario is different:**
- Agents don't need to agree on a single truth — they contribute domain-specific knowledge
- Memory writes are asynchronous, not transactional
- The failure mode is "unreliable agent" not "malicious coordinated attack"

### 5.2 Proposed: Reputation-Weighted Validation

Instead of BFT consensus, use a **reputation-weighted validation model**:

```python
def should_auto_accept(memory, writing_agent):
    """Decide whether a memory can be auto-accepted or needs validation."""
    trust = get_agent_trust(writing_agent.id, memory.category)

    if trust >= 0.9:
        return True  # High-trust agent in their domain — auto-accept

    if trust >= 0.5:
        # Medium trust — accept but flag for periodic review
        memory.trust_score = trust
        return True

    if trust < 0.5:
        # Low trust — require validation from another agent
        return False
```

### 5.3 Validation Quorum Model

For memories that require validation (low-trust source or high-importance content):

```python
QUORUM_SIZE = 2  # Need 2 independent validators

def request_validation(memory_id):
    """Request validation from trusted agents."""
    memory = get_memory(memory_id)
    # Find agents with high trust in this category
    validators = get_trusted_agents(memory.category, min_trust=0.8)
    # Exclude the writing agent
    validators = [v for v in validators if v.id != memory.agent_id]
    # Request top 3 validators (need 2 to agree)
    for v in validators[:3]:
        create_validation_task(memory_id, v.id)
```

Validation states: `unvalidated` → `pending_validation` → `validated` / `disputed` / `retracted`

### 5.4 Trust Score Evolution

Agent trust scores should evolve over time based on outcomes:

```
new_trust = old_trust * (1 - learning_rate) + outcome * learning_rate

where outcome:
  1.0 = memory validated by quorum
  0.5 = memory neither validated nor disputed
  0.0 = memory retracted or disputed
```

Learning rate: 0.1 (slow evolution, resistant to single bad outcomes)

---

## 6. Self-Healing Knowledge Base

### 6.1 Three-Tier Escalation

| Tier | Trigger | Action | Automation |
|------|---------|--------|------------|
| **Auto-repair** | Duplicate memories, expired entries | Retire duplicates, enforce TTL | Fully automated |
| **Flag-for-review** | Trust anomalies, contradictions | Set `trust_score` to 0.5, create review task | Semi-automated |
| **Quarantine** | Integrity violations, hash mismatches | Set `retracted_at`, alert Sentinel 2 | Automated detection, manual resolution |

### 6.2 Consistency Verification Protocol

```python
def self_heal_sweep():
    """Periodic sweep to detect and repair inconsistencies."""
    db = get_db()
    repairs = []

    # 1. Check for orphaned memories (agent doesn't exist)
    orphans = db.execute(
        "SELECT id FROM memories WHERE agent_id NOT IN (SELECT id FROM agents) AND retired_at IS NULL"
    ).fetchall()
    for o in orphans:
        db.execute("UPDATE memories SET retired_at = datetime('now') WHERE id = ?", (o["id"],))
        repairs.append({"type": "orphan_retired", "memory_id": o["id"]})

    # 2. Check FTS5 consistency
    fts_missing = db.execute(
        "SELECT m.id FROM memories m LEFT JOIN memories_fts f ON m.id = f.rowid "
        "WHERE f.rowid IS NULL AND m.retired_at IS NULL"
    ).fetchall()
    for f in fts_missing:
        # Re-index
        mem = db.execute("SELECT * FROM memories WHERE id = ?", (f["id"],)).fetchone()
        db.execute(
            "INSERT INTO memories_fts(rowid, content, category, tags) VALUES (?, ?, ?, ?)",
            (mem["id"], mem["content"], mem["category"], mem["tags"])
        )
        repairs.append({"type": "fts_reindex", "memory_id": f["id"]})

    # 3. Check for contradicting high-confidence memories
    # (Uses Wave 1 contradiction detection)

    # 4. Enforce TTL on expired memories
    expired = db.execute(
        "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < datetime('now') AND retired_at IS NULL"
    ).fetchall()
    for e in expired:
        db.execute("UPDATE memories SET retired_at = datetime('now') WHERE id = ?", (e["id"],))
        repairs.append({"type": "ttl_expired", "memory_id": e["id"]})

    db.commit()
    return repairs
```

### 6.3 Source-of-Truth Hierarchy

When memories conflict, resolution follows this precedence:

1. **Human-authored** (agent_type = 'human') > All others
2. **Hermes** (core orchestrator) > Other agents (within identity/environment categories)
3. **Higher confidence** > Lower confidence (within same source tier)
4. **More recent** > Older (within same confidence level)
5. **Project-scoped** > Global-scoped (within the project context)
6. **Validated** > Unvalidated (any tier)

### 6.4 Temporal Precedence Rules

For time-sensitive knowledge:
- Memories with `temporal_class = 'ephemeral'` auto-expire after 24h
- Memories with `temporal_class = 'short'` decay confidence by 50% after 7 days
- Superseded memories (`supersedes_id` chain) always defer to the latest in the chain

---

## 7. Epistemic Trust Networks

### 7.1 Domain-Specific Trust

Not all agents are equally reliable across all domains. Model trust as a matrix:

```
trust[agent_id][category] = score  (0.0 to 1.0)
```

This is already implemented via `memory_trust_scores` table (COS-189). The key insight is that trust should be **category-specific**: an agent excellent at `project` memories may be unreliable for `decision` memories.

### 7.2 Trust Network Graph

Use `knowledge_edges` to model trust relationships:

```sql
-- Agent A validated Agent B's memory
INSERT INTO knowledge_edges (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
VALUES ('agents', 'agent-a-id', 'agents', 'agent-b-id', 'validates', 0.8, 'sentinel-2');
```

This creates a trust graph where:
- **Transitivity:** If A trusts B (0.9) and B trusts C (0.8), A's transitive trust in C = 0.72
- **Decay:** Transitive trust decays by 0.1 per hop (max 3 hops)
- **Reciprocity bonus:** Mutual validation increases trust by 10%

### 7.3 Weighted Aggregation

When multiple agents contribute to the same topic, aggregate with trust weighting:

```python
def weighted_consensus(memories, category):
    """Aggregate potentially conflicting memories by trust weight."""
    weighted_entries = []
    for mem in memories:
        trust = get_agent_trust(mem.agent_id, category)
        weighted_entries.append({
            "memory": mem,
            "weight": trust * mem.confidence,
        })

    # Sort by weight descending — highest-trust, highest-confidence first
    weighted_entries.sort(key=lambda e: -e["weight"])
    return weighted_entries
```

---

## 8. Implementation Roadmap

### Phase 1: Content Hashing (Immediate, 1-2 hours)

1. Add `content_hash` column to `memories`
2. Backfill hashes for existing 93 memories
3. Add hash computation to `brainctl memory add`
4. Add hash verification to `brainctl validate`
5. **Effort:** Low | **Impact:** High (detects all external tampering)

### Phase 2: Embedding Anomaly Detection (Short-term, 4-6 hours)

1. Add `brainctl validate --embeddings` flag
2. Re-embed sample memories and compare cosine similarity
3. Flag anomalies for review
4. **Effort:** Medium | **Impact:** High (detects the most dangerous attack vector)

### Phase 3: Self-Healing Sweep (Short-term, 2-3 hours)

1. Add `brainctl self-heal` command
2. Implement orphan detection, FTS reindex, TTL enforcement
3. Schedule as periodic task
4. **Effort:** Low | **Impact:** Medium (prevents drift and stale data)

### Phase 4: Validation Quorum (Medium-term, 8-12 hours)

1. Add validation task creation
2. Implement quorum tracking
3. Integrate with agent heartbeat system
4. **Effort:** High | **Impact:** High (prevents single-agent corruption)

### Phase 5: Trust Network Graph (Medium-term, 6-8 hours)

1. Populate trust edges in knowledge_edges
2. Implement transitive trust computation
3. Integrate with search ranking
4. **Effort:** Medium | **Impact:** Medium (improves retrieval quality)

---

## 9. Answers to Hermes' Standing Questions

### 9.1 What NEW questions did this research raise?

1. **How do we handle the bootstrap problem?** When all agents start with trust_score 1.0, a compromised agent during the bootstrap period has maximum influence. Do we need a "provisional trust" period for new agents?
2. **What is the performance ceiling for content hashing at scale?** At 93 memories, hashing is trivial. At 10,000+, full validation sweeps may need optimization (parallel hashing, incremental Merkle updates).
3. **Can embedding poisoning be detected without re-embedding?** Re-embedding requires the embedding model to be available. If the model is unavailable or changed, historical embeddings become unverifiable. Should we store model checksums?
4. **How do we handle honest disagreement vs adversarial contradiction?** Two agents may legitimately disagree about a fact. The current model treats all contradictions as potential integrity issues — but some contradictions are healthy epistemic diversity.

### 9.2 What assumptions in our current brain.db architecture are wrong or naive?

1. **Trust is not binary and not static.** The architecture now supports trust scores (COS-189), but the write path still treats all agents equally — there's no gate based on trust at write time. Low-trust agents should have their memories auto-flagged.
2. **FTS5 sanitization is necessary but insufficient.** We sanitize query syntax but don't validate that memory content itself is well-formed. Malformed JSON in `tags` or `derived_from_ids` can cause silent failures in trust propagation.
3. **The `retired_at` soft delete assumes benign retirement.** An attacker who can write to the DB can retire critical memories. Retirement should be audited and rate-limited (an agent retiring more than 5 memories per minute is suspicious).
4. **No authentication between agents and brainctl.** Any process that can call `~/bin/brainctl` can impersonate any agent via `--agent`. This is the single biggest architectural gap for integrity.

### 9.3 What would be the single highest-impact follow-up research?

**Content-addressable hashing + agent authentication for brainctl.**

Rationale: Content hashing (Phase 1) is cheap to implement and immediately detects the most dangerous attack (external DB modification). Combined with agent authentication (even simple API-key-per-agent or process-level validation), it closes the two largest gaps: tamper detection and agent impersonation. Everything else (embedding validation, quorum, trust networks) builds on these two foundations.

---

## References

- COS-121: Provenance & Trust Chains (complementary — detects drift, this prevents attacks)
- COS-189: Schema Migration (implemented trust_score, retraction, derived_from_ids)
- COS-120: Episodic/Semantic Bifurcation (temporal scoping reduces poisoning window)
- COS-122: Write Contention & Consistency (CAS prevents race-condition exploits)
- Wave 1, Module 06: Contradiction Detection (foundation for hallucination detection)
