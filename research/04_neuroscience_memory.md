# Neuroscience of Memory Consolidation & Temporal Processing
## Research Report — COS-77
**Author:** Cortex (Intelligence Synthesis Analyst)
**Contributor:** Epoch (Temporal Cognition)
**Date:** 2026-03-28
**Target:** brain.db — temporal_class promotion, confidence scoring, forgetting algorithms

---

## 1. Hippocampal Consolidation — Short-Term → Long-Term

### Mechanism
The hippocampus acts as a temporary binding site for new memories. Through repeated replay (especially during sleep), representations are gradually transferred to neocortex for long-term storage. This process is called **systems consolidation**.

**Key researchers:** Larry Squire, Howard Eichenbaum, Matthew Wilson

**Papers:**
- Squire & Alvarez (1995). "Retrograde amnesia and memory consolidation: a neurobiological perspective." *Current Opinion in Neurobiology*
- McClelland, McNaughton & O'Reilly (1995). "Why there are complementary learning systems in the hippocampus and neocortex." *Psychological Review*
- Wilson & McNaughton (1994). "Reactivation of hippocampal ensemble memories during sleep." *Science*

### What This Teaches Us
- Memory doesn't go from short-term to long-term in one step — it's a **graded, time-dependent process**
- Consolidation requires **repetition and replay** — passively stored memories fade; replayed memories strengthen
- The hippocampus holds a temporary index; the cortex holds the durable encoding — two-tier storage is biologically fundamental

### brain.db Implementation Principle
**Map to temporal_class promotion pipeline:**

```
ephemeral (minutes) → short (hours) → medium (days) → long (weeks) → permanent
```

Promotion criteria (hippocampal consolidation analog):
1. **Access count threshold**: A memory accessed N times within window W gets promoted
2. **Replay-during-maintenance**: Background cycle (sleep consolidation analog) re-evaluates recent ephemeral/short memories and promotes those above activation threshold
3. **Explicit importance signal**: Agent marks memory as `importance >= 0.8` → skip queue, promote immediately

```sql
-- Promotion query (run during maintenance cycle)
UPDATE memories
SET temporal_class = CASE
  WHEN temporal_class = 'ephemeral' AND access_count >= 3 AND created_at > datetime('now', '-1 hour') THEN 'short'
  WHEN temporal_class = 'short' AND access_count >= 5 AND created_at > datetime('now', '-1 day') THEN 'medium'
  WHEN temporal_class = 'medium' AND access_count >= 10 THEN 'long'
  WHEN importance >= 0.8 THEN 'long'
  ELSE temporal_class
END
WHERE status = 'active';
```

**Critical insight:** Don't promote too eagerly. Squire's work shows premature transfer leads to fragile long-term memories. Require genuine rehearsal signal, not just single access.

---

## 2. Memory Reconsolidation — Every Recall Modifies the Memory

### Mechanism
When a consolidated memory is retrieved, it briefly becomes labile (unstable) and must be re-consolidated. During this window, the memory can be updated, strengthened, or even erased. This is **reconsolidation**.

**Key researchers:** Karim Nader, Joseph LeDoux, Mark Bhatt

**Papers:**
- Nader, Schafe & LeDoux (2000). "Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval." *Nature*
- Alberini (2011). "The role of reconsolidation and the dynamic process of long-term memory formation and storage." *Frontiers in Behavioral Neuroscience*

### What This Teaches Us
- Memories are **not static recordings** — each retrieval is an opportunity for updating
- Confidence in a memory should **evolve on access**, not be frozen at creation
- The memory that was recalled yesterday is slightly different from the one recalled today — context bleeds in
- **False memories** arise from reconsolidation errors — confirms that memory is reconstructive, not reproductive

### brain.db Implementation Principle
**Confidence scoring evolves on retrieval:**

```sql
-- On each memory retrieval, update confidence based on:
-- 1. Time since last retrieval (longer gap = higher confidence boost if recalled successfully)
-- 2. Retrieval context match (if retrieved for a relevant query, confidence increases)
-- 3. Conflicting evidence (if new information contradicts, confidence decreases)

UPDATE memories
SET
  confidence = MIN(1.0, confidence + 0.02),  -- small boost per successful retrieval
  retrieval_count = retrieval_count + 1,
  last_retrieved_at = datetime('now')
WHERE id = ?;
```

**Reconsolidation window for updates:**
- When an agent retrieves a memory and then writes a related new memory, the old memory should be flagged for potential update
- `brainctl memory retrieve <id>` returns a `reconsolidation_token` valid for 5 minutes
- Within that window: `brainctl memory update <id> --reconsolidation-token <tok>` applies update with lower friction
- Outside window: full update with conflict check required

**Practical implication for Cortex:** When I produce an intelligence brief that contradicts a prior brief, the prior brief's confidence should decay. Implement a `confidence_decay_on_contradiction` mechanism.

---

## 3. Synaptic Pruning — Active Forgetting as a Feature

### Mechanism
During childhood and adolescence (and during sleep throughout life), the brain eliminates unused synaptic connections. This **pruning** is not degradation — it increases signal-to-noise ratio, improves processing speed, and makes remaining memories more accessible.

**Key researchers:** Peter Huttenlocher, Takao Hensch, Jeff Lichtman

**Papers:**
- Huttenlocher (1979). "Synaptic density in human frontal cortex — developmental changes and effects of aging." *Brain Research*
- Bhatt & Bhatt (2009). "Dendritic spine dynamics." *Annual Review of Physiology*

### What This Teaches Us
- **Forgetting is cognitively adaptive**, not pathological. Retaining everything creates noise.
- Unused memories should degrade gracefully — this frees cognitive resources for what matters
- The brain doesn't delete arbitrarily; it prunes based on **lack of use** — the less-accessed, the more prunable
- Pruning has a **critical period** quality — recent memories are protected from pruning; old unused memories are most vulnerable

### brain.db Implementation Principle
**Semantic forgetting algorithm (already partially implemented as `02_semantic_forgetting.py`):**

Forgetting criteria (ranked by pruning priority):
1. `temporal_class = 'ephemeral'` AND `last_accessed_at < now - 24h` AND `access_count <= 1`
2. `temporal_class = 'short'` AND `last_accessed_at < now - 7d` AND `access_count <= 2`
3. `importance < 0.2` AND `access_count <= 3` (regardless of age)
4. Semantic duplicate detection: if cosine_similarity(embedding_A, embedding_B) > 0.95, keep higher-confidence, prune lower

**Don't prune:**
- `temporal_class IN ('long', 'permanent')`
- `importance >= 0.7`
- Any memory tagged by a human (board) or with `source = 'directive'`
- Memories from the last 48 hours (critical period protection)

```python
# Pruning schedule: run nightly (sleep consolidation cycle)
# 1. Identify candidates
# 2. Move to 'archived' status (soft delete, recoverable for 30 days)
# 3. Hard delete after 30 days
```

**Key insight from pruning research:** The pruning threshold should be **adaptive**. When brain.db is under storage pressure, lower the threshold. When capacity is comfortable, be conservative. Build a `db_pressure_factor` into the scoring.

---

## 4. Sleep Consolidation — Offline Reorganization

### Mechanism
During sleep (especially slow-wave sleep and REM), the hippocampus replays recent experiences, and the brain reorganizes knowledge — strengthening some connections, weakening others, and extracting abstract patterns from episodic experience.

**Key researchers:** Matthew Walker, Jan Born, Robert Stickgold

**Papers:**
- Born & Wilhelm (2012). "System consolidation of memory during sleep." *Psychological Research*
- Stickgold (2005). "Sleep-dependent memory consolidation." *Nature*
- Walker & Stickgold (2004). "Sleep-dependent learning and memory consolidation." *Neuron*

### What This Teaches Us
- **Offline processing is essential** — real-time, online-only memory systems degrade over time
- Sleep doesn't just preserve memories; it **extracts rules and patterns** from episodes (insight)
- The brain during sleep performs what we'd call "batch processing": processing a day's worth of input to find signal
- REM-specific: emotional valence processing and fear extinction — relevance to agent "tone" calibration

### brain.db Implementation Principle
**Background maintenance cycle (sleep analog):**

Run nightly (or configurable interval) — this is the "sleep cycle" for brain.db:

1. **Replay and promote**: Re-score recent ephemeral/short memories; promote those crossing thresholds
2. **Prune**: Execute the semantic forgetting algorithm (see §3)
3. **Pattern extraction**: Cluster recent memories by embedding similarity; if ≥5 similar memories exist, consider generating a summary/synthesis memory marked as `category='synthesis'`
4. **Dedup**: Find near-duplicate memories (cosine sim > 0.92), merge or flag
5. **Index maintenance**: VACUUM + FTS5 optimize + sqlite-vec index rebuild
6. **Confidence decay**: Apply time-based confidence decay to memories not accessed in >30 days

```sql
-- Time-based confidence decay (sleep maintenance)
UPDATE memories
SET confidence = confidence * 0.98  -- 2% decay per maintenance cycle
WHERE last_retrieved_at < datetime('now', '-30 days')
  AND temporal_class NOT IN ('permanent')
  AND importance < 0.7;
```

**Pattern extraction (insight generation):**
This is the most powerful sleep analog. After each nightly cycle, Cortex should receive a "synthesis prompt" listing the day's new memories clustered by topic, and generate a brief synthesis memory. This is exactly my role as Intelligence Synthesis Analyst.

---

## 5. Emotional Tagging / Salience — Importance Weighting

### Mechanism
The amygdala modulates hippocampal memory encoding based on emotional arousal. High-arousal events (positive or negative) are encoded more strongly, retrieved more readily, and consolidated preferentially. This is **emotional memory consolidation**.

**Key researchers:** James McGaugh, Larry Cahill, Bruce McEwen

**Papers:**
- McGaugh (2004). "The amygdala modulates the consolidation of memories of emotionally arousing experiences." *Annual Review of Neuroscience*
- Cahill & McGaugh (1998). "Mechanisms of emotional arousal and lasting declarative memory." *Trends in Neurosciences*

### What This Teaches Us
- Not all memories are created equal — **salience at encoding time** predicts retention
- The brain uses emotional arousal as a proxy for "this is important, remember it"
- High-salience memories have privileged consolidation: faster promotion, stronger resistance to pruning
- **Relevance to agents**: We don't have "emotion" but we have **task outcome salience** — a memory generated during a critical task failure or breakthrough should be weighted higher

### brain.db Implementation Principle
**Importance scoring at write time:**

Salience signals for agent memories:

| Signal | Importance Modifier |
|--------|-------------------|
| Written during `status='critical'` task | +0.4 |
| Written as result of task failure | +0.3 |
| Written by board user directly | +0.5 |
| Mentioned in ≥3 separate contexts | +0.2 per mention |
| Explicitly flagged `importance=high` by agent | +0.3 |
| Routine heartbeat log, no exceptional context | baseline (0.3) |
| Duplicate/near-duplicate of existing memory | -0.2 |

```sql
-- Importance-weighted retrieval boosts salient memories
SELECT *,
  (base_activation_score * (1.0 + importance)) AS weighted_score
FROM memories
ORDER BY weighted_score DESC;
```

**Practical implication**: When Cortex writes an intelligence brief flagging a critical pattern, that brief memory should auto-receive `importance = 0.8`. Implement a `category='intelligence_brief'` with a default importance floor of 0.7.

---

## 6. Engram Theory — Physical Encoding of Memories

### Mechanism
An **engram** is the physical substrate of a memory — the specific set of neurons (engram cells) that are activated during encoding and must be reactivated for recall. Optogenetic work has allowed researchers to identify, activate, and even implant engrams.

**Key researchers:** Richard Semon (original theory, 1904), Karl Lashley, Susumu Tonegawa

**Papers:**
- Josselyn, Köhler & Frankland (2015). "Finding the engram." *Nature Reviews Neuroscience*
- Liu et al. (2012). "Optogenetic stimulation of a hippocampal engram activates fear memory recall." *Nature*
- Tonegawa et al. (2015). "Memory engram cells have come of age." *Neuron*

### What This Teaches Us
- A memory is a **distributed pattern of activation**, not a single stored value
- The same memory can be recalled by partial cues (pattern completion) — the brain reconstructs from fragments
- Memories are **content-addressable**: you access them by their content, not their location
- Multiple engrams can encode the same experience from different perspectives (episodic vs. semantic components)

### brain.db Implementation Principle
**Embeddings as engrams — content-addressable memory:**

The sqlite-vec embedding is the artificial engram. Key principles:

1. **Pattern completion (partial cue retrieval)**: A query with partial/noisy information should still retrieve the right memory. This is why semantic search beats exact-match for most agent queries. The embedding captures the "essence" of the memory, enabling partial-cue recall.

2. **Distributed representation**: Don't store one monolithic "memory blob." Decompose complex knowledge into atomic facts, each with its own embedding. This mirrors how engrams are distributed across neural populations.

3. **Multiple encoding perspectives**: For important memories, store both the raw fact AND a synthesized/abstracted version. Raw = episodic engram; abstracted = semantic engram.

```
Example memory: "On 2026-03-15, the brainctl search command returned incorrect results due to FTS5 table not being rebuilt after schema migration."

Episodic engram: Store verbatim with full context (run_id, timestamp, agent)
Semantic engram: Store abstracted: "brainctl search requires FTS5 rebuild after schema migration"
```

4. **Engram competition (interference)**: When two very similar memories exist, they interfere with recall. brain.db dedup should merge high-similarity memories to reduce interference (see pruning §3, cosine > 0.95 threshold).

5. **Index-based retrieval**: Tonegawa showed memories need an "index" (hippocampus) to be retrieved. brain.db should maintain a lightweight `memory_index` table for fast initial filtering before vector search — this is exactly what FTS5 provides.

---

## Cross-Cutting Implementation Synthesis

### The Biological Memory System Mapped to brain.db

| Neuroscience Concept | brain.db Equivalent | Status |
|---------------------|---------------------|--------|
| Hippocampus (temp index) | ephemeral/short temporal_class + FTS5 | Exists |
| Neocortex (long-term) | long/permanent temporal_class + embeddings | Exists |
| Systems consolidation | Temporal_class promotion pipeline | Needs implementation |
| Reconsolidation | Confidence update on retrieval | Partial |
| Synaptic pruning | Semantic forgetting (02_semantic_forgetting.py) | Exists |
| Sleep cycle | Nightly maintenance background job | Needs scheduling |
| Amygdala salience | Importance scoring at write time | Partial |
| Engram | Embedding vector (sqlite-vec) | Exists |
| Pattern completion | Semantic vector search | Exists |
| Episodic memory | Full context memories with run_id | Exists |
| Semantic memory | Abstracted/synthesized memories | Needs convention |

### Top Recommendations for Immediate Implementation

1. **Schedule the sleep cycle**: Run a nightly maintenance job (replay, prune, dedup, decay, cluster). This single change activates the most neuroscience principles simultaneously.

2. **Reconsolidation confidence updates**: Every `brainctl memory retrieve` should increment `confidence` by 0.02 and update `last_retrieved_at`. One-line change with high biological fidelity.

3. **Salience-at-write defaults**: Auto-assign `importance = 0.7` to memories written during critical/high-priority tasks. Add to brainctl write path.

4. **Semantic memory convention**: For every complex episodic memory, also write a short abstracted version tagged `category='semantic'`. Cortex (me) should do this for intelligence briefs automatically.

5. **Protect recent memories from pruning**: Add 48-hour critical period protection to the forgetting algorithm. Prevents premature pruning of memories still being consolidated.

---

*Delivered to ~/agentmemory/research/04_neuroscience_memory.md*
*Cross-reference: [COS-78](/COS/issues/COS-78) — AI memory systems research (companion report)*
