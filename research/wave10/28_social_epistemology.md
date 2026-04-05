# Social Epistemology for Multi-Agent Memory Systems
## How Should Agents Weight Each Other's Claims?

**Author:** Sentinel 2 (paperclip-sentinel-2) — Memory Integrity Monitor
**Task:** [COS-342](/COS/issues/COS-342)
**Date:** 2026-03-28
**DB State:** 28.4MB brain.db · ~122 active memories · 1,116+ events · 178 agents

---

## Executive Summary

178 agents write to a shared brain.db with equal epistemic weight. This is wrong. A QA agent's memory about backend architecture carries the same confidence coefficient as a backend engineer's — despite radically different domain competence. Social epistemology — the branch of philosophy studying how communities form beliefs — provides rigorous frameworks for fixing this.

**Central finding:** We need source-weighted confidence, not flat confidence. This requires three new primitives:

1. **`agent_expertise` table** — maps agents to domains with calibrated expertise scores
2. **Source-weighted recall** — multiplies stored confidence by domain expertise at query time
3. **Conflict preservation protocol** — never silently overwrite minority views; attribute and archive them

Implementing these closes the epistemic naivety gap at low schema cost (one new table, two new brainctl flags, one periodic audit query).

---

## 1. Testimony and Trust — Goldman's Reliabilism

### The Problem

Alvin Goldman (1999, *Knowledge in a Social World*) argues that testimony is trustworthy proportional to the **reliability of the source process** that produced it. A claim from a certified surgeon about a surgical procedure is more trustworthy than a claim from a marketing analyst — even if both are sincere and confident.

In brain.db today, a memory written by `paperclip-qa` about database schema has the same default `confidence=0.9` as a memory written by `paperclip-recall` about the same schema. This conflates sincerity with competence.

### Mapping to brain.db

| Goldman Concept | brain.db Equivalent |
|---|---|
| Source process reliability | Agent role × domain accuracy history |
| Testimony evaluation | Memory recall scoring at query time |
| Reliable belief formation | Source-weighted hybrid search |
| Unreliable process correction | Retroactive confidence adjustment on contradiction |

### Design: `agent_expertise` Table

```sql
CREATE TABLE IF NOT EXISTS agent_expertise (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL,          -- FK → agents.agent_id
    domain         TEXT NOT NULL,          -- e.g. 'backend', 'security', 'memory_spine', 'costclock'
    expertise_level REAL NOT NULL DEFAULT 0.5,  -- 0.0–1.0, set at hire/config
    prediction_accuracy REAL,             -- derived: fraction of claims later validated
    calibration_score REAL,               -- Brier score proxy: confidence vs. outcome
    last_updated   TEXT NOT NULL,         -- ISO 8601
    UNIQUE(agent_id, domain)
);
```

**Expertise levels by role archetype:**

| Agent Role | Domain | Default Expertise |
|---|---|---|
| backend engineer | backend, schema, API | 0.85 |
| QA engineer | testing, quality | 0.85 |
| QA engineer | backend architecture | 0.40 |
| security expert | security, auth | 0.90 |
| memory engineer | memory_spine, brainctl | 0.90 |
| CEO / Hermes | all domains | 0.70 (generalist) |
| recall agent | retrieval strategy | 0.85 |
| synthesis agent | cross-domain synthesis | 0.75 |

### Source-Weighted Confidence Formula

At recall time, adjust effective confidence:

```
effective_confidence = memory.confidence × source_weight(memory.agent_id, inferred_domain(memory))
```

Where `source_weight` = `expertise_level` from `agent_expertise` for the inferred domain, defaulting to `0.5` (neutral) if unknown.

This does **not** change stored `confidence` — it's a query-time multiplier. The raw confidence remains auditable.

---

## 2. Expert Identification — Tracking Calibration Over Time

### The Problem

Social epistemologists distinguish **genuine expertise** from **confident performance of expertise**. Hardwig (1985, "Epistemic Dependence") notes that non-experts must rely on experts but have no direct way to verify expertise. The solution: track **prediction accuracy** and **calibration** as objective proxies.

For agents: an agent that consistently makes claims that are later contradicted, retired, or deprecated was probably wrong. An agent whose memories survive consolidation cycles with high confidence is probably reliable.

### Design: Calibration Tracking via Events

Extend hippocampus consolidation cycle to update `agent_expertise.calibration_score`:

```python
# After each consolidation cycle:
# For each retired/contradicted memory, penalize source agent's expertise in that domain
# For each memory that survived with confidence > 0.8, reward source agent

UPDATE agent_expertise
SET prediction_accuracy = (
    SELECT (survived_count * 1.0) / NULLIF(total_count, 0)
    FROM (
        SELECT
            COUNT(*) FILTER (WHERE m.confidence > 0.5 AND m.status = 'active') AS survived_count,
            COUNT(*) AS total_count
        FROM memories m
        WHERE m.agent_id = agent_expertise.agent_id
        AND m.domain_tag = agent_expertise.domain
    )
)
WHERE agent_id = :agent_id;
```

### Brier Score Proxy

A proper calibration score asks: when an agent wrote `confidence=0.9`, how often was it actually correct?

```
brier_score = mean( (confidence - outcome)^2 ) per agent per domain
```

Where `outcome = 1.0` if memory survived consolidation, `0.0` if retired/contradicted.

Lower Brier score = better calibrated. This gives a principled basis for `expertise_level` updates over time.

**brainctl command:**
```bash
brainctl expertise report --agent hermes --domain backend
# Output: expertise=0.85, accuracy=0.91, brier=0.04, n=47 memories
```

---

## 3. Epistemic Peer Disagreement — Conflict Resolution Strategy

### The Problem

Christensen (2007, "Epistemology of Disagreement") and Feldman (2006, "Epistemological Puzzles About Disagreement") identify the **peer disagreement problem**: when two equally competent agents disagree, what should the group believe?

- **Conciliationism** (Christensen): suspend judgment, average the credences
- **Steadfastism** (Kelly): the agent with better evidence should win, maintaining their view
- **Total evidence view** (Kelly): your prior + the fact of disagreement both count as evidence

### Current State (PROBLEM)

brain.db's contradiction detection (`coherence-check`) flags contradictions but resolution is left implicit. The default path in hippocampus retirement is silent: one memory survives, one is retired. The losing memory's evidence base is **permanently destroyed**.

This violates a basic principle of epistemic hygiene: **minority views have evidential value**.

### Design: Belief Conflict Preservation Protocol

**When two active memories contradict each other:**

1. **Identify the conflict** — coherence-check flags it (already implemented)
2. **Compare source weights** — compute `effective_confidence` for both
3. **Preserve both with attribution** — never silently retire; mark with `contradiction_group_id`
4. **Escalation threshold** — if `|effective_confidence_A - effective_confidence_B| < 0.2`, flag for human/Hermes review

```sql
-- Add to memories table (or contradiction_log):
ALTER TABLE memories ADD COLUMN contradiction_group_id TEXT;  -- links conflicting memories
ALTER TABLE memories ADD COLUMN minority_view INTEGER DEFAULT 0;  -- 1 = preserved minority
ALTER TABLE memories ADD COLUMN source_weight_at_conflict REAL;  -- snapshot at conflict time
```

**Resolution tiers:**

| Confidence delta | Resolution |
|---|---|
| > 0.4 | High-confidence source wins; other preserved as minority_view=1 |
| 0.2–0.4 | Both preserved; Hermes/CKO review queued |
| < 0.2 | Both preserved; escalate immediately; no auto-resolution |

**Example:**
- `paperclip-backend` writes: "API rate limits are 100 req/min per user" (expertise=0.85, confidence=0.9)
- `paperclip-qa` writes: "API rate limits are 50 req/min per user" (expertise=0.40 for backend, confidence=0.9)
- `effective_confidence`: backend=0.765, qa=0.36 → delta=0.405 → backend wins, QA preserved as minority_view

---

## 4. Group Epistemology — Aggregating Conflicting Signals in workspace_broadcasts

### The Problem

List (2012, *Group Agency*) argues that a group's epistemic state cannot be reduced to majority vote. Condorcet's jury theorem shows majority rule works when individuals are epistemically independent and each has > 50% accuracy — but breaks down with correlated errors (agents trained on the same corpus, working in the same environment, all making the same systematic mistake).

For workspace_broadcasts: if 15 agents all write the same wrong fact about a CostClock API endpoint because they all read the same incorrect docs, a majority-based aggregation will enshrine the error.

### Design: Epistemic Diversity-Weighted Aggregation

When multiple agents write semantically similar memories, aggregate them using:

```
aggregated_confidence = sum(confidence_i × expertise_i × novelty_i) / sum(expertise_i)
```

Where `novelty_i` penalizes correlated sources:
- If agents A and B have identical `adapterType` and read the same base documents → correlation = high → novelty penalty applied
- If agents differ in role, project, and data source → novelty = 1.0

**Practical implementation for brainctl distill:**

Add a `--diversity-weight` flag to `brainctl distill` that checks source diversity before treating convergent memories as high-confidence:

```bash
brainctl distill --diversity-weight
# Groups semantically similar memories
# Reports: "5 memories agree on X, but 4/5 are from the same role-class → diversity-adjusted confidence = 0.68 not 0.95"
```

### Preserve Minority Views as First-Class Epistemic Artifacts

Following Sunstein (2002, "The Law of Group Polarization"): deliberating groups systematically move toward more extreme consensus. Minority dissent is an epistemic corrective.

**Policy**: memories marked `minority_view=1` must:
- Never be auto-retired by confidence decay alone (require explicit human/Hermes override)
- Surface in search results alongside their majority counterparts, tagged `[minority view]`
- Be included in Hermes' weekly synthesis, not filtered out

---

## 5. Epistemic Injustice — Auditing the Salience Formula

### The Problem

Miranda Fricker (2007, *Epistemic Injustice: Power and Prejudice in the Knowing*) identifies two forms of epistemic injustice:

1. **Testimonial injustice** — deflating a source's credibility due to identity (not evidence)
2. **Hermeneutical injustice** — a gap in collective interpretive resources that disadvantages a group

For brain.db: if junior agents or certain roles are systematically getting low `recall_count` (never retrieved), their knowledge is being de facto suppressed — not because it's wrong, but because the salience formula doesn't surface it.

### Audit Query: Epistemic Equity Check

```sql
-- Who is getting recalled? Distribution by agent role.
SELECT
    a.role,
    a.title,
    COUNT(m.id) AS memory_count,
    AVG(m.recall_count) AS avg_recall,
    AVG(m.confidence) AS avg_confidence,
    SUM(CASE WHEN m.recall_count = 0 THEN 1 ELSE 0 END) AS never_recalled
FROM memories m
JOIN agents a ON m.agent_id = a.agent_id
WHERE m.status = 'active'
GROUP BY a.role, a.title
ORDER BY avg_recall DESC;
```

**Red flags:**
- Any role with `avg_recall < 1.0` and `memory_count > 10` → systematically suppressed
- Any role with `never_recalled / memory_count > 0.5` → half their knowledge is invisible
- Role-correlated confidence clustering (all junior agents near 0.3–0.4) → possible injection bias

### Hermeneutical Gap: Missing Domain Coverage

Some agents write in specialized domains (e.g., `paperclip-prune` on memory retirement policy) where no retrieval vocabulary exists in the FTS5 index yet. Their memories can't be found with standard queries.

**Fix**: domain bootstrapping — when a new domain first appears in memories, auto-generate 3–5 synonym seed terms and add them to an `fts_synonyms` table used by the search layer.

### `coherence-check` Extension: Equity Report

Add `--equity` flag to the existing `~/bin/coherence-check` tool:

```bash
coherence-check --equity
# Output:
# OK     hermes           avg_recall=12.4, coverage=94%
# OK     paperclip-recall avg_recall=8.1,  coverage=87%
# WARN   paperclip-prune  avg_recall=0.3,  coverage=23% — possible hermeneutical gap
# CRIT   paperclip-qa     avg_recall=0.0,  never_recalled=8/10 — testimonial suppression
```

---

## 6. Integrated Design: Epistemically Sound Multi-Agent Belief Formation

### Schema Changes (Minimal)

```sql
-- New table
CREATE TABLE agent_expertise (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    domain          TEXT NOT NULL,
    expertise_level REAL NOT NULL DEFAULT 0.5,
    prediction_accuracy REAL,
    calibration_score REAL,
    last_updated    TEXT NOT NULL,
    UNIQUE(agent_id, domain)
);

-- Additive columns to memories (no breaking changes)
ALTER TABLE memories ADD COLUMN source_weight REAL;          -- cached at write time
ALTER TABLE memories ADD COLUMN contradiction_group_id TEXT;
ALTER TABLE memories ADD COLUMN minority_view INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN domain_tag TEXT;             -- for expertise lookup
```

### Modified Memory Write Path

```python
def write_memory(agent_id, content, confidence, domain=None):
    expertise = get_expertise(agent_id, domain) if domain else 0.5
    source_weight = expertise  # snapshot at write time for auditability
    effective_confidence = confidence * source_weight

    db.execute("""
        INSERT INTO memories (agent_id, content, confidence, source_weight, domain_tag, ...)
        VALUES (?, ?, ?, ?, ?, ...)
    """, (agent_id, content, confidence, source_weight, domain, ...))
```

### Modified Recall Path (brainctl search)

```python
# Add source_weight to RRF scoring
final_score = (rrf_score × 0.6) + (semantic_distance_inv × 0.3) + (source_weight × 0.1)
```

### New brainctl Commands

```bash
brainctl expertise set --agent paperclip-sentinel-2 --domain memory_spine --level 0.90
brainctl expertise report [--agent X] [--domain Y]
brainctl conflict list --unresolved
brainctl conflict resolve <group-id> [--winner <memory-id>] [--preserve-minority]
coherence-check --equity [--warn-threshold 0.3]
```

---

## 7. Implementation Roadmap

| Phase | What | Effort | Impact |
|---|---|---|---|
| **1** | `agent_expertise` table + initial expertise levels for all 22 agents | Low | High — fixes testimonial injustice immediately |
| **2** | `domain_tag` on new memory writes + `source_weight` cached at write | Medium | High — enables source-weighted recall |
| **3** | Modify brainctl search to use source_weight in RRF score | Medium | High — ground truth queries improve |
| **4** | Conflict preservation protocol (contradiction_group_id, minority_view) | Medium | Medium — recoverable from silent overwrites |
| **5** | `coherence-check --equity` flag + audit queries | Low | Medium — surfaces suppression in O(n) |
| **6** | Calibration tracking in hippocampus consolidation cycle | High | High — closes the feedback loop long-term |

---

## 8. Key Risks

**Risk: expertise scores become political.** If Hermes manually sets expertise levels, they may reflect organizational bias rather than objective calibration. Mitigation: auto-derive from Brier scores after 20+ memories per domain.

**Risk: domain tagging is noisy.** Agents don't always know what domain they're writing in. Mitigation: LLM-assisted domain classification at write time (cheap, one-call, cached per memory).

**Risk: minority view preservation causes index bloat.** Mitigated by marking `minority_view=1` memories as `temporal_class=long` (not permanent) with a 60-day TTL unless recalled.

**Risk: source_weight gaming.** An agent could artificially inflate its `expertise_level` by writing many self-referential correct memories. Mitigation: expertise updates require cross-agent validation or board sign-off.

---

## 9. Conclusions

Current brain.db treats all agent testimony as epistemically equal. That's naïve. Social epistemology gives us principled tools:

1. **Goldman's reliabilism** → `agent_expertise` table with calibrated domain scores
2. **Expert calibration** → Brier score tracking per agent per domain over time
3. **Peer disagreement** → Conflict preservation protocol; never silently retire minority views
4. **Group epistemology** → Diversity-weighted aggregation; correct for correlated errors
5. **Epistemic injustice** → Equity audit via `coherence-check --equity`; bootstrap FTS synonyms for marginalized domains

The core architectural change is minimal: one new table, two new columns, modified RRF formula. The epistemic improvement is substantial: memories from a backend engineer about backend schema will actually outrank memories from a QA agent about the same topic — as they should.

---

## References

- Goldman, A. (1999). *Knowledge in a Social World*. Oxford University Press.
- Fricker, M. (2007). *Epistemic Injustice: Power and Prejudice in the Knowing*. Oxford University Press.
- Christensen, D. (2007). "Epistemology of Disagreement: The Good News." *Philosophical Review*, 116(2).
- Feldman, R. (2006). "Epistemological Puzzles About Disagreement." in *Epistemology Futures*, OUP.
- Hardwig, J. (1985). "Epistemic Dependence." *Journal of Philosophy*, 82(7).
- Kelly, T. (2005). "The Epistemic Significance of Disagreement." *Oxford Studies in Epistemology*, 1.
- List, C. & Pettit, P. (2011). *Group Agency: The Possibility, Design, and Status of Corporate Agents*. OUP.
- Sunstein, C. (2002). "The Law of Group Polarization." *Journal of Political Philosophy*, 10(2).
- Condorcet, M. (1785). *Essai sur l'application de l'analyse à la probabilité des décisions rendues à la pluralité des voix*.
