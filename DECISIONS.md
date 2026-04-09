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
We are treating brainctl as a practical structured memory system with continuity primitives, not as a full cognitive architecture experiment.

In scope:
- structured memory storage
- handoff packets and continuity primitives
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

## 2026-04-06

### Keep Hermes built-in memory small; use brainctl for continuity
Reason:
- Hermes long-term memory is intentionally tiny and curated
- active working state should not be shoved into Hermes memory
- continuity belongs in brainctl handoff packets, then durable facts get promoted later

### Do not replace handoffs with dreaming/consolidation
Reason:
- dreaming helps dedupe, compress, and promote durable facts
- it does not reliably preserve exact active working context
- handoff packets remain the primary continuity mechanism across resets

### Current branch state is good enough to start Hermes-side integration next
Implemented on feat/phase1-cleanup:
- handoff_packets schema
- CLI add/list/latest/consume/pin/expire
- MCP handoff_add/latest/consume/pin/expire
- tests passing for CLI handoff flows

### Hermes-side reset/resume contract stays private by default
Reason:
- lifecycle hooks and restore policy are Hermes-specific
- upstream brainctl should stay generic and reusable
- Albert-specific continuity behavior should not muddy the public project

Private note drafted in Hermes workspace covers:
- exact payload Hermes writes before reset
- when the automatic handoff is created before the 4 AM refresh
- how Hermes fetches latest relevant pending handoff on resume
- when Hermes consumes the handoff after successful restore

### Next pickup point
Implement Hermes integration against the existing generic brainctl handoff primitives:
- payload builder
- pre-reset handoff creation path
- explicit or conservative resume path
- consume-on-success behavior
