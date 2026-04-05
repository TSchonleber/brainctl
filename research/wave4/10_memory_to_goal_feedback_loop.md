# Memory-to-Goal Feedback Loop — Research Report

**Wave 4 Research** | COS-180
**Author:** Neuron
**Date:** 2026-03-28
**Builds on:** `07_emergence_detection.py` (Wave 1)

---

## Root Question

> Can memory drive proactive goal formation, not just reactive retrieval?

## Answer: Yes, with a 5-stage pipeline

The pattern-to-goal inference layer is feasible using SQL-first extraction on `brain.db` with no LLM dependency in the critical path. LLM enrichment is optional for human-readable proposal titles.

---

## Architecture

```
brain.db memories/events
         │
    ┌────▼────┐
    │ Signal   │  — topic surges, error clusters, confidence decay,
    │ Extract  │    agent drift, recall dead zones
    └────┬────┘
         │ raw signals
    ┌────▼────┐
    │ Cluster  │  — Jaccard token-overlap with type/scope bonuses
    └────┬────┘
         │ signal clusters
    ┌────▼────┐
    │ Propose  │  — rule-based titles + optional LLM enrichment callback
    └────┬────┘
         │ GoalProposals
    ┌────▼────┐
    │ Rank     │  — composite: 35% strength + 25% coverage + 25% urgency + 15% novelty
    └────┬────┘
         │ ranked proposals
    ┌────▼────┐
    │ Dedup    │  — token-overlap against existing tasks in brain.db + Paperclip
    └────┴────┘
         │
    goal proposals ready for human/CEO review
```

## Five Signal Types

| Signal | Source | What It Detects |
|---|---|---|
| `topic_surge` | memories FTS | Abnormal frequency increase (lift ≥ 3x over prior window) |
| `error_cluster` | events causal chains | Repeated error/retry/blocked sequences sharing a causal root |
| `confidence_decay` | memories confidence | Categories/scopes where memory confidence is degrading |
| `drift` | memories category dist | Agent behavioral shift (distribution divergence > 0.4) |
| `recall_dead_zone` | memories recalled_count | Knowledge areas with many entries but zero recall |

## Key Design Decisions

1. **SQL-first, no LLM in critical path.** All signal extraction runs as SQL queries against `brain.db`. This keeps the pipeline deterministic, auditable, and fast. LLM is injected only via optional `llm_enrich` callback for title generation.

2. **Token-overlap clustering (not embeddings).** Jaccard similarity on tokenized descriptions. Pragmatic choice given `sqlite-vec` is not yet installed (per FRONTIER.md constraints). When embeddings become available, swap in cosine-similarity clustering.

3. **Composite ranking with configurable weights.** Default: `strength=0.35, coverage=0.25, urgency=0.25, novelty=0.15`. Error clusters get highest urgency (1.0), dead zones lowest (0.3). Weights are exposed as parameters.

4. **Deduplication against existing goals/tasks.** Checks both `brain.db` tasks table and an optional external title list (for Paperclip issues). Prevents proposing goals that already exist.

5. **Proposals are suggestions, not actions.** The pipeline outputs ranked `GoalProposal` objects. Creating actual tasks/goals requires explicit approval (CEO/manager review). This preserves governance.

## Threshold Detection

- **Topic surge:** lift ≥ 3.0x AND count ≥ 5 mentions in the window
- **Error cluster:** ≥ 3 events sharing a causal chain root in 30 days
- **Confidence decay:** ≥ 3 memories with confidence < 0.3 in a category/scope
- **Agent drift:** category distribution divergence > 0.4 (symmetric absolute diff)
- **Recall dead zone:** ≥ 10 memories with 0 total recalls in a category/scope

All thresholds are configurable via function parameters.

## Integration Path

### Immediate (Wave 4 prototype)
- Run `run_memory_to_goal_pipeline()` as part of the cognitive consolidation cycle
- Output proposals to a designated memory scope or event log
- CEO/manager reviews proposals in next heartbeat

### Future (requires Wave 3+ infrastructure)
- **Pub/sub integration (Wave 4 candidate #1):** Proposals published to a signal channel that Hermes/managers subscribe to
- **Embedding clustering (requires sqlite-vec):** Replace Jaccard with cosine similarity for better semantic grouping
- **Feedback loop closure:** Track which proposals became actual goals vs. dismissed — use this to tune thresholds and weights over time

## Limitations

1. **No embedding support.** Clustering is token-based. Semantic similarity (e.g., "authentication failures" ≈ "login errors") requires embeddings.
2. **No temporal decay on signals.** A surge from 6 days ago and 1 day ago are weighted equally within the window. Future work: apply recency weighting within the window.
3. **Single-pass clustering.** Current approach is greedy single-pass. Could miss cross-type clusters (e.g., a topic surge + error cluster about the same subsystem).
4. **No feedback loop yet.** The system proposes but doesn't learn from which proposals were accepted/rejected.

## Files

- `wave4/10_memory_to_goal_feedback_loop.py` — Full implementation
- `07_emergence_detection.py` — Foundation (Wave 1, signal extraction primitives)

---

*This research is a prototype. Production integration depends on COS-82 (consolidation pipeline) and governance review.*
