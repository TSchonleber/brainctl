# HANDOFF

## Goal
Turn brainctl into Hermes's local structured memory backend.

## Immediate priorities
1. Phase 1 cleanup
   - unify DB path handling across Brain, CLI, MCP, UI, hippocampus
   - unify schema init path
   - fix README quickstart
   - fix agent bootstrap across CLI and MCP
   - add interoperability tests
2. Phase 2 handoff packets
   - structured session handoff records
   - resume from latest unconsumed handoff
   - consume or expire raw handoffs after restore
3. Phase 3 auto-extraction
   - extract durable facts into memories/entities/decisions/events
4. Phase 4 consolidation
   - dedupe, stale fade, contradiction handling

## Product scope to keep
- memories
- events
- decisions
- entities
- knowledge_edges
- context
- scoped retrieval
- local SQLite

## Product scope to defer
- affect
- neuromodulation
- global workspace / phi
- theory of mind / BDI
- quantum / belief collapse
- dream / incubation theater

## Current branch
- feat/phase1-cleanup

## Notes
- Fork remote: origin = ARegalado1/brainctl
- Upstream remote: TSchonleber/brainctl
- Keep Phase 1 practical and PR-worthy.
