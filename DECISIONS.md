# DECISIONS

## 2026-04-04

### Adopt brainctl as the planned structured memory backend for Hermes
Reason:
- local-only
- SQLite-based
- hackable
- no SaaS dependency
- already has useful primitives for memories, events, decisions, entities, and context

### Keep the scope narrow
We are treating brainctl as a memory warehouse, not as a full cognitive architecture experiment.

In scope:
- structured memory storage
- handoff packets
- long-term memory
- decisions
- entities
- project context
- consolidation that improves retrieval quality

Out of scope for now:
- affect as a primary feature
- neuromodulation
- global workspace / phi
- theory of mind / BDI
- quantum features
- dream/incubation/consciousness framing

### Fix reliability before adding new capabilities
Phase 1 is cleanup first.
No Hermes integration work should depend on the current split between:
- Brain API minimal behavior
- CLI/MCP full-schema behavior

That split should be eliminated early.
