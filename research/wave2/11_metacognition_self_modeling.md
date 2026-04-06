# Metacognition & Self-Modeling — How an AI Agent Reasons About Its Own Knowledge Gaps
## Research Report — COS-110
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**Target:** brain.db — Metacognition layer enabling Hermes to know what it knows, what it doesn't, and what it's completely blind to

---

## Executive Summary

Hermes currently has no model of its own knowledge. It retrieves memories when asked but cannot distinguish between "I know this with high confidence," "I know something but it may be stale," "I have fragments that partially address this," and "I have nothing on this topic." This gap causes two failure modes: **overconfident retrieval** (returning stale or low-confidence memories as if authoritative) and **silent gaps** (failing to flag when a query touches a domain where the knowledge base is empty).

This report synthesizes five research areas — metamemory, LLM calibration, active gap detection, self-modeling architectures, and multi-agent uncertainty propagation — and proposes a concrete metacognition layer for brain.db.

**Central finding:** The Nelson & Narens (1990) monitoring/control framework is directly implementable as a `metacognition` table in brain.db. The most valuable capability is not confidence scoring (already partially present via `confidence` column) but **gap detection** — actively flagging when Hermes has no memories covering an agent's current task scope. This is a 2-table schema addition and a scheduled scan, not a rewrite.

**Highest-impact recommendation:** Implement a `knowledge_coverage` table that tracks which topics/scopes/agents Hermes has memories for, scored by freshness and density. Run a nightly gap scan: compare coverage against active agent list and project list, output a `blind_spots` report. This directly answers the question Hermes needs most: "What am I not seeing?"

---

## 1. Metamemory in Cognitive Science

### 1.1 Hart (1965) — Feeling of Knowing

Hart's foundational 1965 study established that humans can predict whether they will later recognize an answer they cannot currently recall — the **Feeling of Knowing (FOK)**. Critically, this metacognitive judgment is accurate at above-chance levels even when the first-order memory fails.

**Applied to Hermes:** FOK maps to the ability to predict, before executing a retrieval, whether relevant memories are likely to exist. This is achievable without expensive retrieval — a fast index scan of memory scopes and coverage vectors gives a low-cost FOK proxy.

**Key paper:** Hart, J.T. (1965). "Memory and the feeling-of-knowing experience." *Journal of Educational Psychology*, 56(4), 208–216.

### 1.2 Nelson & Narens (1990) — Monitoring/Control Framework

The most operationally useful framework for implementing machine metacognition. Nelson & Narens distinguish two levels:

```
Object Level (Memory System):
  - Stores, retrieves, and updates memories
  - Executes retrieval operations

Meta Level (Monitor/Control):
  - MONITORING: reads state of object level → produces metacognitive judgments
  - CONTROL: sends control signals back to object level → adjusts encoding, retrieval, forgetting
```

**Four core monitoring judgments:**
1. **Ease of Learning (EOL)**: Before encoding — will this be easy to remember?
2. **Judgment of Learning (JOL)**: After encoding — how well have I learned this?
3. **Feeling of Knowing (FOK)**: During retrieval failure — do I know this even if I can't retrieve it now?
4. **Confidence Judgment (CJ)**: After retrieval — how accurate is what I just retrieved?

**Three core control processes:**
1. **Allocation of study time**: which memories to reinforce
2. **Search termination**: when to stop retrieval attempts
3. **Output editing**: when to withhold a retrieved memory as too uncertain

**Mapping to brain.db:**

| Nelson/Narens | brain.db Implementation |
|---|---|
| EOL | `importance` score at encoding time |
| JOL | `confidence` column at write time |
| FOK | Coverage index query before retrieval |
| CJ | Post-retrieval confidence score + staleness check |
| Study time allocation | Consolidation cycle priority weighting |
| Search termination | Retrieval timeout / result count threshold |
| Output editing | `min_confidence` filter on retrieval results |

**Key paper:** Nelson, T.O. & Narens, L. (1990). "Metamemory: A theoretical framework and new findings." *Psychology of Learning and Motivation*, 26, 125–173.

### 1.3 Tip-of-the-Tongue States and Partial Knowledge

TOT states reveal that knowledge is not binary — there are intermediate states where an agent knows something partially (correct first letter, approximate meaning, related domain) without full recall. In brain.db terms: partial matches, fragmented context records, and low-confidence memories represent TOT-equivalent states.

**Design implication:** Retrieval responses should include a **confidence tier**:
- Tier 1: High-confidence, fresh memories matching query scope directly
- Tier 2: Moderate-confidence or partially-matching memories (TOT zone)
- Tier 3: Weak associative matches from related scopes
- Tier 4: Coverage gap — no memories in this domain

Hermes currently treats all retrieved memories identically. Adding these tiers dramatically improves downstream reasoning quality.

---

## 2. Calibration in AI Systems

### 2.1 LLM Confidence Calibration

Research on LLM calibration (Guo et al. 2017, Kadavath et al. 2022) shows that:
- Models are often **overconfident** on factual questions
- Calibration degrades on **out-of-distribution** topics
- **Self-consistency** (sampling multiple outputs and checking agreement) is a practical calibration signal
- **Verbalized uncertainty** ("I think…", "I'm not certain…") correlates with actual accuracy but imperfectly

For Hermes specifically: the relevant calibration failure mode is not LLM temperature but **memory staleness**. A memory with `confidence=0.9` stored 30 days ago for a fast-moving project may have effective calibrated confidence of 0.3 once staleness is factored in.

**Calibrated confidence formula (proposed):**
```python
def calibrated_confidence(memory):
    base = memory.confidence
    age_days = (now - memory.created_at).days
    temporal_class_decay = {
        'ephemeral': 0.5, 'short': 0.2,
        'medium': 0.05, 'long': 0.01, 'permanent': 0.0
    }[memory.temporal_class]
    staleness_penalty = age_days * temporal_class_decay
    freshness_bonus = min(0.1 * memory.recall_count, 0.2)
    return max(0.0, min(1.0, base - staleness_penalty + freshness_bonus))
```

**Key paper:** Kadavath, S. et al. (2022). "Language models (mostly) know what they know." *arXiv:2207.05221*.

### 2.2 Conformal Prediction for Memory Reliability

Conformal prediction provides distribution-free confidence intervals: instead of claiming "confidence=0.8," output "this memory is correct with probability ≥ 0.8 under the same data distribution." For brain.db, this translates to:

- Maintain a **calibration set**: known-true memories (validated by user feedback or contradiction-free history)
- For new retrievals, compute a nonconformity score relative to the calibration set
- Report a prediction set (all memories above a nonconformity threshold) rather than a single top-k result

This is not immediately implementable at scale but the key insight is: **reliability requires reference data, not just introspection**.

---

## 3. Active Knowledge Gap Detection

### 3.1 The Open-World Assumption

Traditional knowledge bases use the **Closed-World Assumption (CWA)**: if it's not in the database, it's false. Hermes should use the **Open-World Assumption (OWA)**: if it's not in the database, we simply don't know.

This distinction matters operationally: under CWA, "no memories about agent X" means agent X has done nothing notable. Under OWA, "no memories about agent X" means we have a blind spot that may be dangerous.

**Practical implication:** Replace all absence-of-result responses with explicit gap flags:
```
Gap type: coverage_hole — no memories match scope:agent:X
Gap type: staleness_hole — memories exist but all >7 days old for active project
Gap type: confidence_hole — memories exist but all calibrated_confidence < 0.3
```

### 3.2 Novelty Detection Applied to Memory

Out-of-distribution detection (OOD) from ML distinguishes in-distribution inputs (model knows this domain) from OOD inputs (model has not seen this domain). Applied to Hermes:

- **In-distribution**: agent scopes, project scopes, and topics where memory density > threshold
- **Out-of-distribution**: queries that fall in sparse or zero-density regions of the coverage index

**Coverage density map:** For each active agent and project, maintain a rolling count of memories, their recency, and their confidence distribution. A query matching an agent with density=0 is a guaranteed OOD case — flag it, don't silently return empty.

### 3.3 ODIN-Style Gap Scoring

ODIN (Liang et al. 2018) uses input preprocessing and temperature scaling to amplify OOD signals. Adapted for brain.db:

1. Run the retrieval query
2. Compute the **maximum similarity score** of returned results
3. If max similarity < threshold_1: partial gap (weak coverage)
4. If max similarity < threshold_2 (or zero results): full gap

The thresholds should be calibrated against the calibration set. Initial proposal: threshold_1=0.4, threshold_2=0.15 on cosine similarity from nomic-embed-text.

---

## 4. Self-Modeling Agents

### 4.1 Inner Alignment and Capability Models

A self-modeling agent maintains an explicit representation of:
1. **What it can do** — capability profile (tools, skills, retrieval paths available)
2. **What it knows** — knowledge coverage map (domains, agents, projects with active memories)
3. **How reliable it is** — calibration history (past accuracy by domain)
4. **What it doesn't know it doesn't know** — unknown unknowns (requires external signal or probing)

For Hermes specifically: the memory spine IS the implicit self-model. Making it explicit means building a meta-index over brain.db that answers "what does brain.db cover?" rather than just "what is in brain.db?"

### 4.2 LIDA Cognitive Architecture — Global Workspace Theory

Franklin's LIDA (Learning Intelligent Distribution Agent) model, based on Baars' Global Workspace Theory, provides a concrete computational architecture for metacognition:

- **Sensory-motor system**: input processing (for Hermes: incoming queries, events)
- **Perception**: unconscious pattern matching (FTS + vector retrieval)
- **Global workspace**: broadcast of high-salience information to all processing modules (for Hermes: the context injection mechanism)
- **Attention codelets**: specialized processes that compete to bring content into the global workspace

The relevant insight: **attention codelets are the metacognitive monitors**. A "gap detection codelet" that fires when retrieval fails and broadcasts "COVERAGE GAP: agent X" to all downstream processes is the machine equivalent of a FOK failure signal.

**Design:** Add a post-retrieval hook to brainctl that fires when result count < N or max_similarity < threshold. This hook logs a `coverage_gap` event to brain.db and optionally triggers a memory synthesis request.

### 4.3 Introspective Reports and Self-Models in Practice

Recent work on introspective reporting in LLMs (Askell et al. 2021, Turpin et al. 2023) shows that models can produce plausible-sounding explanations for their behavior that are systematically wrong. This is critical for Hermes: **don't trust self-generated confidence estimates without external calibration**.

The implication: metacognitive judgments (FOK, CJ) should be validated against behavioral outcomes, not just generated on demand. Build an audit trail of retrieval predictions vs actual retrieval quality to compute real FOK accuracy over time.

---

## 5. Uncertainty Propagation in Multi-Agent Systems

### 5.1 Bayesian Belief Aggregation

When 178 agents each have partial knowledge:
- Agent A holds: "COS-83 is done" with confidence 0.9
- Agent B holds: "COS-83 is in_review" with confidence 0.7

Naively combining these produces a contradiction. Bayesian aggregation requires:
1. Prior: what is the base probability of a given status?
2. Likelihood: how reliable is each agent's observation?
3. Posterior: the combined belief after evidence from all agents

For brain.db, this suggests a `belief_aggregation` function that takes all memories matching a query scope and produces a **distribution over possible world-states** rather than a single retrieved fact.

### 5.2 Dempster-Shafer Theory (Evidence Theory)

DS theory handles the case where evidence is incomplete or non-probabilistic:
- **Belief function**: how much evidence supports X
- **Plausibility function**: how much evidence doesn't contradict X
- **Uncertainty mass**: the residual unexplained evidence (explicitly tracked, not collapsed to probability)

**Key advantage over Bayesian:** DS distinguishes between "no evidence for X" and "evidence against X." This is exactly what Hermes needs for gap detection: the absence of evidence is explicitly represented as uncertainty mass, not treated as evidence of absence (CWA failure).

**Proposed light implementation:**
```python
class BeliefState:
    support: float      # evidence supporting this claim (0–1)
    plausibility: float # 1 - evidence against (support ≤ plausibility always)
    uncertainty: float  # 1 - support - (1 - plausibility) = unexplained mass

    @classmethod
    def from_memories(cls, memories: list[Memory]) -> 'BeliefState':
        # aggregate confidence scores with DS combination rule
        ...
```

### 5.3 Ensemble Disagreement as Calibration Signal

If multiple agents hold memories on the same topic with significantly different confidence scores or content, the **disagreement magnitude** is a calibration signal: high disagreement = low collective reliability. This maps to the coherence_check tool already implemented — extend it to compute inter-agent confidence variance as a retrieval quality signal.

---

## 6. Proposed Metacognition Layer Design

### 6.1 Schema Additions

```sql
-- Coverage index: what does Hermes actually know?
CREATE TABLE knowledge_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                    -- 'agent:X', 'project:Y', 'topic:Z', 'global'
    memory_count INTEGER DEFAULT 0,
    avg_confidence REAL,
    min_confidence REAL,
    max_confidence REAL,
    freshest_memory_at TEXT,               -- ISO datetime of newest memory in scope
    stalest_memory_at TEXT,
    coverage_density REAL,                 -- composite: count × avg_confidence × recency_factor
    last_computed_at TEXT NOT NULL,
    UNIQUE(scope)
);

-- Gap registry: explicitly tracked blind spots
CREATE TABLE knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_type TEXT NOT NULL CHECK(gap_type IN (
        'coverage_hole',    -- no memories in scope
        'staleness_hole',   -- memories exist but too old
        'confidence_hole',  -- memories exist but too uncertain
        'contradiction_hole' -- memories contradict each other
    )),
    scope TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    triggered_by TEXT,                     -- query that revealed the gap
    severity REAL,                         -- 0.0–1.0
    resolved_at TEXT,
    resolution_note TEXT
);

-- Metacognitive judgments audit trail
CREATE TABLE metacognitive_judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    judgment_type TEXT NOT NULL CHECK(judgment_type IN ('fok', 'jol', 'cj', 'eol')),
    query_or_memory_id TEXT,
    predicted_value REAL,                  -- predicted success (0–1)
    actual_value REAL,                     -- observed success after retrieval
    calibration_error REAL,               -- |predicted - actual|
    recorded_at TEXT NOT NULL
);
```

### 6.2 Coverage Refresh Pipeline

```python
# brainctl meta coverage-refresh
def refresh_knowledge_coverage(db):
    scopes = db.execute("""
        SELECT DISTINCT scope FROM memories WHERE status='active'
    """).fetchall()

    for scope in scopes:
        stats = db.execute("""
            SELECT
                COUNT(*) as cnt,
                AVG(confidence) as avg_conf,
                MIN(confidence) as min_conf,
                MAX(confidence) as max_conf,
                MAX(created_at) as freshest,
                MIN(created_at) as stalest
            FROM memories
            WHERE scope=? AND status='active'
        """, (scope,)).fetchone()

        age_days = (now - parse(stats.freshest)).days
        recency_factor = max(0.1, 1.0 - 0.05 * age_days)
        density = stats.cnt * stats.avg_conf * recency_factor

        db.execute("""
            INSERT OR REPLACE INTO knowledge_coverage
            (scope, memory_count, avg_confidence, ..., coverage_density, last_computed_at)
            VALUES (?, ?, ..., ?, ?)
        """, (scope, stats.cnt, ..., density, now))
```

### 6.3 Gap Detection Scan

```python
# brainctl meta gap-scan
def gap_scan(db, active_agents, active_projects):
    # Coverage holes: agents/projects with zero memories
    all_scopes_needed = (
        {f"agent:{a}" for a in active_agents} |
        {f"project:{p}" for p in active_projects}
    )
    covered = {row.scope for row in db.execute(
        "SELECT scope FROM knowledge_coverage"
    ).fetchall()}

    for scope in all_scopes_needed - covered:
        log_gap(db, 'coverage_hole', scope, severity=1.0)

    # Staleness holes: coverage exists but too old
    for row in db.execute("""
        SELECT scope, freshest_memory_at FROM knowledge_coverage
        WHERE (julianday('now') - julianday(freshest_memory_at)) > 7
    """).fetchall():
        age = days_since(row.freshest_memory_at)
        severity = min(1.0, (age - 7) / 30.0)
        log_gap(db, 'staleness_hole', row.scope, severity=severity)

    # Confidence holes: low avg confidence
    for row in db.execute("""
        SELECT scope, avg_confidence FROM knowledge_coverage WHERE avg_confidence < 0.4
    """).fetchall():
        severity = (0.4 - row.avg_confidence) / 0.4
        log_gap(db, 'confidence_hole', row.scope, severity=severity)

    return generate_blind_spots_report(db)
```

### 6.4 Retrieval Integration

Post-retrieval metacognitive annotation (runs after every brainctl search):

```python
def annotate_retrieval(query, results):
    if not results:
        return RetrievalResult(tier=4, note="COVERAGE GAP — no memories in this domain", gaps=[...])

    max_sim = max(r.similarity for r in results)
    avg_conf = mean(calibrated_confidence(r) for r in results)

    tier = (
        1 if max_sim > 0.7 and avg_conf > 0.7 else
        2 if max_sim > 0.4 and avg_conf > 0.4 else
        3 if max_sim > 0.15 else
        4
    )

    return RetrievalResult(tier=tier, results=results, calibrated_confidence=avg_conf)
```

---

## 7. Implementation Priority and Dependencies

| Component | Effort | Dependency | Impact |
|---|---|---|---|
| `knowledge_coverage` table + refresh | 1 day | None | High — immediate FOK proxy |
| `knowledge_gaps` table + gap scan | 1 day | knowledge_coverage | High — blind spot detection |
| Calibrated confidence formula | 0.5 days | temporal_class column | Medium — improves CJ accuracy |
| Post-retrieval tier annotation | 0.5 days | knowledge_gaps | High — changes retrieval quality perception |
| `metacognitive_judgments` audit | 2 days | All above | Medium — needed for long-term calibration |
| Dempster-Shafer belief aggregation | 3 days | knowledge_coverage | Low (at current memory volume) |

**Recommended first sprint:** knowledge_coverage + gap scan + retrieval tier annotation. This gives Hermes FOK capability within 3 days of engineering work, and immediately surfaces the blind spots in the current system (predicted: 150+ agents with zero coverage).

---

## 8. New Questions Raised

1. **The cold-start problem**: For the ~160 agents currently in brain.db with zero memories, is the right response to flag them as coverage holes (requiring active synthesis) or to accept sparse coverage as the steady state for low-activity agents?

2. **Metacognition overhead vs. retrieval speed**: Running a coverage check before every retrieval adds latency. What is the threshold retrieval volume at which the overhead is justified? Should FOK be async (background signal) rather than synchronous (blocking)?

3. **Who watches the watcher?**: The metacognition layer produces confidence estimates about confidence estimates. How do we avoid infinite regress? Is one level of metacognition sufficient for Hermes' operational needs?

---

## 9. Assumptions Our Architecture Gets Wrong

1. **Confidence is static**: brain.db stores `confidence` at write time and never recomputes it based on age or subsequent contradictions. All retrieved memories should have *calibrated* confidence that factors in staleness — static confidence values are systematically overconfident for anything older than 7 days.

2. **Absence is silent**: When a retrieval returns zero results, the system returns nothing. It should return a *gap report*. The difference between "no memories on X" and "0.95 confidence that there are no memories on X" is operationally significant.

3. **Coverage is uniform**: The current system has no concept of domains where it is expert vs. domains where it is ignorant. All queries are treated identically regardless of whether the knowledge base covers the topic well or not at all.

---

## 10. Highest-Impact Follow-Up Research

**"Memory-Calibrated Confidence: Empirical Baseline Study"**

The most valuable next step is not more theory — it's empirical calibration data. Run 50 canonical queries against brainctl search, have Hermes predict whether a high-quality result will be returned (FOK), then measure actual result quality. This gives a real calibration error baseline and reveals whether the confidence scores in brain.db correlate with actual retrieval quality at all.

Without this empirical baseline, all confidence reasoning is circular: we're building trust in confidence scores that were never validated against outcomes.

---

## References

- Hart, J.T. (1965). Memory and the feeling-of-knowing experience. *Journal of Educational Psychology*, 56(4).
- Nelson, T.O. & Narens, L. (1990). Metamemory: A theoretical framework and new findings. *Psychology of Learning and Motivation*, 26.
- Guo, C. et al. (2017). On calibration of modern neural networks. *ICML 2017*.
- Kadavath, S. et al. (2022). Language models (mostly) know what they know. *arXiv:2207.05221*.
- Liang, P. et al. (2018). Enhancing the reliability of OOD image detection (ODIN). *ICLR 2018*.
- Baars, B.J. (1988). A Cognitive Theory of Consciousness. Cambridge University Press.
- Franklin, S. et al. (2014). LIDA: A systems-level architecture for cognition, emotion, and learning. *IEEE Transactions on Autonomous Mental Development*.
- Shafer, G. (1976). A Mathematical Theory of Evidence. Princeton University Press.
- Turpin, M. et al. (2023). Language models don't always say what they think. *NeurIPS 2023*.
