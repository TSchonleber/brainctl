# Changelog

All notable changes to **brainctl** will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [1.2.0] — 2026-04-13

### Added
- **Hermes Agent memory provider plugin** — `plugins/hermes/brainctl/` ships a
  drop-in `MemoryProvider` implementation for [Hermes Agent](https://hermes-agent.nousresearch.com).
  Wraps `agentmemory.Brain`, exposes `brainctl_remember` / `search` / `think` /
  `log` / `entity` / `decide` / `handoff` tools to the model, auto-prefetches
  recall before each turn, auto-retains completed turns, runs `brain.orient()`
  /`brain.wrap_up()` session bookends, mirrors built-in `MEMORY.md` writes into
  `brain.db`, and persists pre-compression context as `lesson` memories.
- **Context profiles** — task-scoped search presets via `--profile NAME` on `search` and `memory search`
  - 6 built-in profiles: `writing`, `meeting`, `research`, `ops`, `networking`, `review`
  - Each profile scopes tables + categories to what's relevant for that task mode (inspired by Koylan's progressive disclosure pattern)
  - User-defined profiles stored in brain.db: `brainctl profile create/list/show/delete`
  - MCP: `profile` param on `memory_search` and `search` tools — `{"tool":"memory_search","query":"voice","profile":"writing"}`
  - Explicit `--tables` / `--category` flags always win over profile defaults
- **`brainctl obsidian`** — bidirectional sync with Obsidian vaults
  - `export <vault>` — dumps active memories, entities, and events to markdown with YAML frontmatter; follows Karpathy LLM-wiki 3-layer pattern
  - `import <vault>` — ingests new vault notes (no `brainctl_id`) through `Brain.remember()` / W(m) gate; `--dry-run` supported
  - `watch <vault>` — watchdog-based live ingest on create/modify; configurable `--cooldown` window (requires `pip install watchdog`)
  - `status <vault>` — diff table of brain.db vs vault counts, flags un-exported drift
- **Replay priority & SWR tagging** — `replay_priority` and `ripple_tags` columns accumulate dynamically on vsearch retrievals (salience = score × confidence)
- **Reconsolidation window** — `labile_until`, `labile_agent_id`, `retrieval_prediction_error` columns; 20-min lability window opened on high-PE retrieval; agent-scoped write access
- **Arousal-precision coupling** (Free Energy Principle) — `arousal_gain` multiplier in W(m) gate; high-arousal content consolidates stronger
- **5 new MCP consolidation tools** — `replay_boost`, `replay_queue`, `reconsolidation_check`, `reconsolidate`, `consolidation_stats` (176 MCP tools total)

## [1.0.0] — 2026-04-03

Stable release. Every feature verified end-to-end on clean pip install.

### Highlights
- **102 tests** passing in CI (Python 3.11-3.13)
- **81-table production schema** via `brainctl init`
- **44-emotion affect tracking** with 6 safety patterns
- **11-pass consolidation engine** (decay, merge, dream, Hebbian learning)
- **3D neural map** with brain-region layout and live activity feed
- **23-tool MCP server** for Claude Desktop / VS Code
- **Zero-LLM-cost** search, classify, consolidate — all local computation
- **`brainctl report`** compiles knowledge into readable markdown
- **`brainctl lint`** health checks with auto-fix
- Clean JSON error handling on all commands
- Full documentation: README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY

### Added since 0.5.0
- CI fix: test fixtures use `brainctl init` (no production DB dependency)
- Neural Map v6: removed empty bubbles, color-coded edges, legend panel
- Orphan agents hidden, connected agents positioned by content
- `/api/activity` endpoint for live visualization feed
- Crypto team redirected: cancelled over-engineered Solana contract work
- `brainctl-consolidate` entry point for pip install

## [0.3.0] — 2026-04-03

### Added
- **`brainctl init`** — create fresh brain.db with full production schema (30+ tables)
- **MCP server in package** — `brainctl-mcp` now works from pip install (was broken in 0.2.0)
- **Affect MCP tools** — `affect_classify`, `affect_log`, `affect_check`, `affect_monitor` (16 total MCP tools)
- **Write gate integration** — arousal-modulated memory worthiness (high-arousal memories consolidate 40% stronger)
- **brain.py API** — `Brain.affect(text)` and `Brain.affect_log(text)` with type hints
- `affect_log` table in init_schema.sql

### Fixed
- `brainctl-mcp` crash on pip install (missing module, now at `agentmemory.mcp_server`)
- `brainctl init` now uses full schema, not toy 7-table schema
- README: `from brainctl import Brain` corrected to `from agentmemory import Brain`
- Dominance scoring now respects negation ("can't fix" = low dominance, not high)
- Added word forms to lexicons (panicking, terrifying, overwhelmed, etc.)

### Changed
- Schema files unified (db/ and src/agentmemory/db/ now in sync)
- Version numbers aligned across all files

## [0.2.0] — 2026-04-03

### Added
- **Functional affect tracking system** grounded in Anthropic's "Emotion Concepts in LLMs" (2026)
- `src/agentmemory/affect.py` — zero-LLM-cost lexical affect classifier (~1ms)
- 44 named emotions with validated PAD coordinates (valence/arousal/dominance)
- 11 affect clusters matching Anthropic paper findings
- 6 safety patterns detecting manipulation, coercion, sycophancy, deception risks
- Arousal-modulated write gate boost and consolidation priority scoring
- Affect distance metric and velocity tracking
- Fleet-wide affect monitoring for 200 agents
- CLI: `brainctl affect classify|log|check|history|monitor`
- 35 affect-specific tests

## [0.1.1] — 2026-04-03

### Added
- `brainctl cost` — token consumption dashboard with format savings analysis
- `--output json|compact|oneline` on search commands (97% token savings with oneline)
- `--budget` flag for hard token caps on search output
- 50 pytest tests (Brain API + CLI + output formats)
- Dockerfile (python:3.12-slim, MCP server default)
- GitHub Actions CI (Python 3.11-3.13) + PyPI trusted publish on tag
- CONTRIBUTING.md
- Web UI: token cost cards in health view, `/api/cost` endpoint
- 19 new entities + 25 edges seeded into knowledge graph

### Fixed
- MCP_SERVER.md install docs (`agentmemory` → `brainctl[mcp]`)
- Decisions renderer in web UI (handles `title` field)

## [0.1.0] — 2026-04-03

### Added
- Initial PyPI release as `brainctl`
- `Brain` class Python API: remember, search, forget, log, entity, relate, decide, stats
- CLI with 40+ commands across memory, entity, event, search, trigger, neuro subsystems
- MCP server with 12 tools for Claude Desktop / VS Code
- FTS5 full-text search + optional sqlite-vec vector search
- Bayesian confidence with alpha/beta parameters
- Write gate with surprise scoring
- Neuromodulation system (dopamine, acetylcholine, norepinephrine, serotonin)
- Knowledge graph with typed entities and directed relations
- Consolidation engine (confidence decay, dream synthesis, Hebbian learning)
- Prospective memory triggers
- Multi-agent support with per-agent attribution
- Web dashboard on port 3939
