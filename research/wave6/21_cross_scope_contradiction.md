# Cross-Scope Contradiction Detection — Implementing the COS-179 Consolidation Pass

**Research Wave:** 6
**Issue:** COS-233
**Author:** Sentinel 2 (Memory Integrity Monitor)
**Date:** 2026-03-28
**Builds On:** COS-179 (cross-agent belief reconciliation), wave1/06_contradiction_detection.py
**Cross-pollinate:** Hippocampus (consolidation cycle owner), Cortex (belief reconciliation analysis)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

The Wave 1 `06_contradiction_detection.py` catches **within-scope** conflicts only: memories with the same `(agent_id, category, scope)` triple that assert opposing facts. COS-179 identified a more dangerous class of divergence: **cross-scope conflicts**, where two memories from different scopes refer to the same real-world entity but make incompatible claims. Neither scope constraint catches this — the existing system is architecturally blind to it.

This report specifies and implements the cross-scope contradiction detection pass as a new function in `06_contradiction_detection.py`, with an integration hook in `05_consolidation_cycle.py`.

**Central design:** Entity extraction → scope bridging → negation matching. The pass is additive, optional, and safe to run without modifying any memories — it only emits `contradiction_detected` events and flags pairs for review.

---

## 1. Why Cross-Scope Conflicts Are Dangerous

### 1.1 The Problem with Scope-Gated Detection

The existing detection logic:
```sql
WHERE a.category = b.category
  AND a.scope = b.scope
  AND a.agent_id = b.agent_id
```

This requires **same scope AND same agent**. Cross-scope conflicts are missed entirely. The real-world cases that matter:

| Scenario | Scope A | Scope B | What current detector misses |
|----------|---------|---------|------------------------------|
| Hermes writes "brain.db has 22 agents" (global) vs. Engram writes "22 agents, none in agentmemory project yet" (project:agentmemory) | global | project:agentmemory | Incompatible agent count claims |
| Sentinel 2 writes "coherence check live" (agent scope) vs. Cortex writes "no automated coherence checking exists" | agent:sentinel-2 | project:agentmemory | Directly contradictory system state |
| Agent A writes "COS-83 is done" (project:costclock) vs. Agent B writes "COS-83 is in_progress" (project:agentmemory) | project:costclock | project:agentmemory | Task status conflict |

### 1.2 Scope Is Not Entity Scope

The `scope` column governs **temporal decay** and **access visibility** — it does not mean the memory is *only about* that scope. A memory in `scope=project:costclock-ai` can make claims about the global auth system, Hermes's identity, or another project's status. Scope-gated detection misses 100% of these cross-cutting claims.

---

## 2. Design of the Cross-Scope Pass

### 2.1 Algorithm Overview

```
For each pair of memories (A, B) where A.scope ≠ B.scope:
  1. Extract key noun phrases / entities from A and B
  2. Check for entity overlap (same entity mentioned in both)
  3. If overlap found, apply negation-pattern matching to that shared context
  4. If negation pattern matches, emit conflict with type='cross_scope_conflict'
```

### 2.2 Entity Extraction (Lightweight NLP)

No LLM or heavy NLP required. Entity extraction is pattern-based:

```python
import re

# Patterns that identify entity mentions in memory content
ENTITY_PATTERNS = [
    # Named agents: "hermes", "sentinel-2", "hippocampus" etc.
    r'\b(hermes|sentinel-2|hippocampus|engram|prune|recall|cortex|weaver|scribe|'
    r'legion|axiom|kernel|cipher|armor|probe|nexus|codex|tempo|lattice|aegis|'
    r'nara|openclaw|paperclip-\w+)\b',

    # Issue identifiers: COS-83, PAP-224, etc.
    r'\b([A-Z]{2,5}-\d+)\b',

    # System components: brain.db, brainctl, hippocampus.py, etc.
    r'\b(brain\.db|brainctl|hippocampus\.py|coherence-check|vec_memories|'
    r'knowledge_edges|memories_fts|memory_trust_scores)\b',

    # Key system concepts as noun phrases
    r'\b(auth system|checkout|trust score|consolidation cycle|decay|'
    r'contradiction detection|embedding|FTS5|semantic search)\b',

    # Numeric claims about agents/memories
    r'\b(\d+)\s+(agents?|memories|events?|issues?)\b',
]

def extract_entities(content: str) -> set[str]:
    """Extract normalized entity tokens from memory content."""
    content_lower = content.lower()
    entities = set()
    for pattern in ENTITY_PATTERNS:
        for match in re.finditer(pattern, content_lower, re.IGNORECASE):
            entities.add(match.group(0).lower().strip())
    return entities
```

### 2.3 Scope Relatedness Filter

Not all cross-scope pairs are worth comparing. Two memories from completely unrelated scopes (e.g., costclock-ai invoice data vs. agentmemory identity) rarely conflict. Apply a scope relatedness filter first:

```python
def scopes_are_related(scope_a: str, scope_b: str) -> bool:
    """
    Two scopes are related if they share a project prefix, or one is global.
    Avoids O(N²) comparisons on completely unrelated memories.
    """
    if scope_a == 'global' or scope_b == 'global':
        return True  # global scope claims can conflict with anything

    # Both are project scopes — check if same project family
    project_a = scope_a.split(':')[1] if ':' in scope_a else scope_a
    project_b = scope_b.split(':')[1] if ':' in scope_b else scope_b

    # Same top-level project (e.g., project:agentmemory vs project:agentmemory:wave5)
    return (
        project_a == project_b or
        project_a.startswith(project_b) or
        project_b.startswith(project_a)
    )
```

### 2.4 Cross-Scope Negation Matching

Once entity overlap is confirmed, apply the same negation patterns as the within-scope pass, but with an additional **entity-anchored** check: only flag if the negation occurs in a sentence that shares the overlapping entity.

```python
def find_entity_anchored_negation(
    content_a: str,
    content_b: str,
    shared_entities: set[str],
    negation_patterns: list[tuple[str, str]],
) -> list[str]:
    """
    Returns list of matching pattern descriptions if negation found near shared entity.
    Splits content into sentences, filters to entity-bearing sentences, then checks negation.
    """
    # Split into sentences (simple approach — periods, semicolons)
    sentences_a = re.split(r'[.;]\s+', content_a.lower())
    sentences_b = re.split(r'[.;]\s+', content_b.lower())

    # Filter to sentences containing at least one shared entity
    relevant_a = [s for s in sentences_a if any(e in s for e in shared_entities)]
    relevant_b = [s for s in sentences_b if any(e in s for e in shared_entities)]

    if not relevant_a or not relevant_b:
        return []  # entity not mentioned in a comparable sentence

    conflicts_found = []
    for sa in relevant_a:
        for sb in relevant_b:
            for pos_pattern, neg_pattern in negation_patterns:
                a_pos = bool(re.search(pos_pattern, sa))
                b_neg = bool(re.search(neg_pattern, sb))
                b_pos = bool(re.search(pos_pattern, sb))
                a_neg = bool(re.search(neg_pattern, sa))

                if (a_pos and b_neg) or (b_pos and a_neg):
                    conflicts_found.append(f"{pos_pattern} / {neg_pattern}")

    return conflicts_found
```

---

## 3. Implementation — `find_cross_scope_contradictions()`

The complete function to add to `06_contradiction_detection.py`:

```python
def find_cross_scope_contradictions(
    conn: sqlite3.Connection,
    limit: int = 50,
    min_confidence: float = 0.3,
) -> list[dict]:
    """
    Detect contradictions between memories in DIFFERENT scopes.

    Strategy:
    1. Load all active memories grouped by scope
    2. For each cross-scope pair where scopes are related,
       check for entity overlap + negation conflict
    3. Return conflict dicts compatible with find_contradictions() output

    This is more expensive than within-scope detection (O(N²) pairs)
    but is bounded by the scope relatedness filter, which eliminates
    the vast majority of cross-scope pairs before entity extraction.
    """
    conn.row_factory = sqlite3.Row
    conflicts = []

    # Load active memories with sufficient confidence
    rows = conn.execute("""
        SELECT id, agent_id, category, scope, content, confidence, trust_score
        FROM memories
        WHERE retired_at IS NULL
          AND confidence >= ?
        ORDER BY scope, created_at DESC
    """, (min_confidence,)).fetchall()

    memories = [dict(r) for r in rows]

    # Pre-compute entity sets
    entity_cache = {m['id']: extract_entities(m['content']) for m in memories}

    # Cross-scope pairs only
    seen = set()
    for i, m_a in enumerate(memories):
        for j, m_b in enumerate(memories):
            if j <= i:
                continue
            if m_a['scope'] == m_b['scope']:
                continue  # same-scope handled by find_contradictions()
            if not scopes_are_related(m_a['scope'], m_b['scope']):
                continue  # unrelated scopes — skip

            pair_key = (min(m_a['id'], m_b['id']), max(m_a['id'], m_b['id']))
            if pair_key in seen:
                continue
            seen.add(pair_key)

            # Check for entity overlap
            shared = entity_cache[m_a['id']] & entity_cache[m_b['id']]
            if not shared:
                continue  # no shared entities — no meaningful comparison

            # Check for negation conflict anchored to shared entities
            matched_patterns = find_entity_anchored_negation(
                m_a['content'], m_b['content'], shared, NEGATION_PATTERNS
            )
            if not matched_patterns:
                continue

            conflicts.append({
                "type": "cross_scope_conflict",
                "memory_id_a": m_a['id'],
                "memory_id_b": m_b['id'],
                "content_a": m_a['content'][:120],
                "content_b": m_b['content'][:120],
                "confidence_delta": abs(m_a['confidence'] - m_b['confidence']),
                "trust_delta": abs((m_a.get('trust_score') or 1.0) - (m_b.get('trust_score') or 1.0)),
                "pattern": matched_patterns[0],
                "shared_entities": list(shared)[:5],
                "agent_id_a": m_a['agent_id'],
                "agent_id_b": m_b['agent_id'],
                "scope_a": m_a['scope'],
                "scope_b": m_b['scope'],
            })

            if len(conflicts) >= limit:
                break
        if len(conflicts) >= limit:
            break

    return conflicts
```

---

## 4. Conflict Report Format

Cross-scope conflicts require richer output than within-scope conflicts because the context needed to evaluate them spans two scopes. Each conflict record includes:

```python
{
    "type": "cross_scope_conflict",          # distinguishes from within-scope types
    "memory_id_a": int,                       # first memory ID
    "memory_id_b": int,                       # second memory ID
    "content_a": str,                         # first 120 chars of memory A
    "content_b": str,                         # first 120 chars of memory B
    "confidence_delta": float,               # |conf_a - conf_b|
    "trust_delta": float,                    # |trust_a - trust_b| — for resolution tiebreaking
    "pattern": str,                          # negation pattern that matched
    "shared_entities": list[str],            # entities that caused the cross-scope link
    "agent_id_a": str,                       # writing agent for A
    "agent_id_b": str,                       # writing agent for B
    "scope_a": str,                          # scope of memory A
    "scope_b": str,                          # scope of memory B
    "resolution": str,                       # "flag_for_review" | "auto_resolvable" | "temporal_sequence"
    "recommended_action": str,               # human-readable recommendation
}
```

### 4.1 Resolution Classification

Before returning conflicts, classify each one by likely resolution path:

```python
def classify_resolution(conflict: dict, conn: sqlite3.Connection) -> dict:
    """
    Add resolution and recommended_action fields to conflict dict.
    """
    id_a, id_b = conflict['memory_id_a'], conflict['memory_id_b']

    # Check temporal ordering — if one clearly precedes the other and
    # the change is a status progression, it may be a temporal sequence, not contradiction
    row = conn.execute("""
        SELECT a.created_at as ca, b.created_at as cb
        FROM memories a, memories b
        WHERE a.id = ? AND b.id = ?
    """, (id_a, id_b)).fetchone()

    if row and row[0] and row[1]:
        # If the newer memory supersedes the older by explicit chain, not a contradiction
        supersedes = conn.execute("""
            SELECT 1 FROM memories
            WHERE id IN (?, ?) AND supersedes_id IN (?, ?)
        """, (id_a, id_b, id_a, id_b)).fetchone()
        if supersedes:
            conflict['resolution'] = 'temporal_sequence'
            conflict['recommended_action'] = 'Already linked via supersedes chain — not a real conflict.'
            return conflict

    # Check if trust_delta is decisive (> 0.15)
    if conflict.get('trust_delta', 0) > 0.15:
        conflict['resolution'] = 'auto_resolvable'
        higher_trust_id = id_a if (conn.execute(
            "SELECT trust_score FROM memories WHERE id = ?", (id_a,)
        ).fetchone() or [0])[0] >= (conn.execute(
            "SELECT trust_score FROM memories WHERE id = ?", (id_b,)
        ).fetchone() or [0])[0] else id_b
        conflict['recommended_action'] = (
            f'Memory {higher_trust_id} has higher trust — consider retiring the other.'
        )
        return conflict

    # Default: flag for human review
    conflict['resolution'] = 'flag_for_review'
    conflict['recommended_action'] = (
        f'Cross-scope conflict between {conflict["scope_a"]} and {conflict["scope_b"]}. '
        f'Shared entities: {", ".join(conflict["shared_entities"][:3])}. '
        f'Manual review required — both agents should update their scope model.'
    )
    return conflict
```

---

## 5. Integration with `05_consolidation_cycle.py`

Add as an optional Step 7b in the consolidation pipeline:

```python
# In run_consolidation_cycle(), after step 7 (contradiction detection):

# 7b. Cross-scope contradiction detection (optional pass)
if run_cross_scope:  # new parameter: run_cross_scope=False by default
    cross_scope_conflicts = find_cross_scope_contradictions(conn, limit=25)
    for cs_conflict in cross_scope_conflicts:
        classify_resolution(cs_conflict, conn)
        if cs_conflict.get('resolution') != 'temporal_sequence':
            flag_contradiction(
                conn,
                cs_conflict['memory_id_a'],
                cs_conflict['memory_id_b'],
                cs_conflict['type'],
            )
    report['cross_scope_contradictions'] = len(cross_scope_conflicts)
    if not dry_run:
        conn.commit()
```

**Why `run_cross_scope=False` by default:** The cross-scope pass is O(N²) across related memory pairs. At current store size (41 memories) this is trivially fast (~500 pairs max). At 10× scale (400 memories) it may require batching or index-assisted pre-filtering. Starting with opt-in prevents regression in the default consolidation cycle.

### 5.1 Enabling the Pass

```bash
# Via Python
report = run_consolidation_cycle(run_cross_scope=True)

# Or via a dedicated scan command (recommended for Sentinel 2)
python ~/agentmemory/research/06_contradiction_detection.py --cross-scope --limit 50
```

---

## 6. Test Cases

### 6.1 Known Cross-Scope Conflict (Current brain.db)

The following pair should be detected by the cross-scope pass:

- Memory in `scope=global`: "22 active agents in brain.db" (hermes)
- Memory in `scope=project:agentmemory`: Any memory asserting a different agent count

This would fire on the numeric entity pattern `\b(\d+)\s+agents?\b`.

### 6.2 Expected Non-Conflicts

The pass should NOT flag:
- Two memories about completely different projects with no shared entities
- Memories where the difference is temporal (older/newer with explicit supersedes link)
- Memories that use negation for different subjects (e.g., "invoice is not paid" vs. "auth is not stateless")

### 6.3 Validation Queries

```sql
-- After running the cross-scope pass, verify events were logged
SELECT id, summary, metadata, created_at
FROM events
WHERE event_type = 'contradiction_detected'
  AND JSON_EXTRACT(metadata, '$.conflict_type') = 'cross_scope_conflict'
ORDER BY created_at DESC
LIMIT 10;

-- Verify knowledge_edges were written
SELECT source_id, target_id, relation_type, weight
FROM knowledge_edges
WHERE relation_type = 'contradicts'
ORDER BY created_at DESC
LIMIT 10;
```

---

## 7. Performance Bounds

| Store Size | Cross-Scope Pairs | Related Pairs (est.) | Entity-Overlap Pairs | Typical Runtime |
|------------|------------------|---------------------|---------------------|----------------|
| 41 memories | ~820 pairs | ~120 (scopes_are_related) | ~30 | < 50ms |
| 400 memories | ~80,000 pairs | ~3,000 | ~500 | ~2s |
| 4,000 memories | ~8M pairs | ~100,000 | ~5,000 | ~30s |

At 4,000+ memories, add an index-assisted pre-filter using the `topical_scope` or `semantic_similar` edges in `knowledge_edges` to find likely cross-scope pairs before O(N²) entity comparison.

---

## 8. Relationship to Trust Score (COS-234)

Cross-scope contradiction detection is tightly coupled to trust score calibration ([COS-234](/COS/issues/COS-234)):

1. **Detection triggers trust update:** When a cross-scope conflict is flagged, `trust_score` should be lowered on both memories (per the COS-234 trust event taxonomy: −0.20 per unresolved conflict).

2. **Trust as resolution tiebreaker:** When `trust_delta > 0.15`, the higher-trust memory is the preferred survivor — use `classify_resolution()` to surface this automatically.

3. **Validated memories skip cross-scope comparison:** Memories with `validated_at IS NOT NULL` and `trust_score >= 0.80` can be treated as authoritative within their scope — do not flag them as candidates for cross-scope conflict auto-resolution (they may be correct and the other scope's memory may be stale).

---

## Summary

The cross-scope contradiction pass adds three new capabilities to `06_contradiction_detection.py`:

1. `extract_entities(content)` — lightweight pattern-based entity extraction
2. `scopes_are_related(scope_a, scope_b)` — scope bridging filter to bound O(N²) comparisons
3. `find_cross_scope_contradictions(conn, limit)` — the main cross-scope detection function
4. `classify_resolution(conflict, conn)` — resolution classification (temporal_sequence / auto_resolvable / flag_for_review)

The integration hook in `05_consolidation_cycle.py` adds `run_cross_scope=False` parameter to `run_consolidation_cycle()` and wires the new pass into step 7b.

The pass is conservative by design: it only emits events and knowledge edges, never modifying memories directly. Resolution is left to `auto_resolve_contradictions()` (with trust-score tiebreaker from COS-234) or human review.
