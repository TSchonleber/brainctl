# Changelog

All notable changes to **brainctl** will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] — 2026-04-03

### Added
- **`brainctl init`** — create a fresh brain.db from schema
- **Affect MCP tools** — `affect_classify`, `affect_log`, `affect_check`, `affect_monitor` (16 total MCP tools)
- **Write gate integration** — arousal-modulated memory worthiness (high-arousal memories consolidate 40% stronger)
- **brain.py API** — `Brain.affect(text)` and `Brain.affect_log(text)` with full type hints
- `affect_log` table in init_schema.sql

### Changed
- Schema files unified (db/ and src/agentmemory/db/ now in sync)
- CHANGELOG.md added
- README updated with affect tracking docs, quick start, and init command

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
