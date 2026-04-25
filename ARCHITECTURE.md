# brainctl — Architecture

## Overview

brainctl is a persistent memory system for AI agents backed by a single SQLite
database. No server process, no external dependencies beyond Python and SQLite.
Multiple agents (or a single agent across sessions) share one `brain.db` file
for episodic, semantic, and procedural memory plus events, entities,
decisions, and a knowledge graph.

## Project Structure

```
src/agentmemory/
  __init__.py          Brain class export
  brain.py             Python API (Brain class)
  _impl.py             Full CLI implementation
  db.py                Shared DB utilities
  cli.py               Entry point
  mcp_server.py        MCP server entry
  hippocampus.py       Consolidation engine (brainctl-consolidate entry point)
  procedural.py        Canonical procedural memory service + heuristics
  retrieval/           Query planner, candidate generation, evidence fusion, answerability
  commands/            25 command modules
    agent.py           Agent registration and state
    memory.py          Memory CRUD and search
    event.py           Event logging and queries
    entity.py          Entity management
    search.py          Cross-table search
    graph.py           Knowledge graph queries
    trigger.py         Prospective memory triggers
    trust.py           Trust scoring
    ...                (and more)

bin/
  brainctl             Thin CLI wrapper
  brainctl-mcp         MCP server launcher
  brainctl-bench       Retrieval-quality benchmark runner
  embed-populate       Embedding pipeline (optional)
  consolidation-cycle.sh   Nightly consolidation cycle (shell wrapper)
  hippocampus-cycle.sh     Hippocampus-only cycle (shell wrapper)

ui/
  server.py            Web dashboard (optional)
  static/              HTML/JS/CSS

db/
  init_schema.sql      Full schema definition
  migrations/          Incremental migrations
```

## Database Schema

All state lives in a single `brain.db` file (SQLite, WAL mode).

### Core Tables

| Table | Purpose |
|-------|---------|
| `memories` | Durable facts, preferences, lessons, conventions |
| `procedures` | Canonical procedural memories linked 1:1 to bridge rows in `memories` |
| `procedure_steps` | Ordered step projection for procedures |
| `procedure_sources` | Provenance links from procedures back to memories/events/decisions/entities |
| `procedure_runs` | Execution/application feedback history for procedures |
| `procedure_candidates` | Repeat-pattern staging area before promotion to canonical procedures |
| `events` | Timestamped event log (append-oriented) |
| `entities` | Named entities (people, projects, tools, concepts) |
| `knowledge_edges` | Typed, weighted edges between any two records |
| `decisions` | Decisions with rationale |
| `memory_triggers` | Prospective memory (fire-when conditions) |
| `agents` | Registered agent identities |
| `agent_state` | Per-agent key/value store |
| `access_log` | Audit trail of all operations |
| `epochs` | Temporal segmentation for recency-weighted recall |
| `context` | Chunked knowledge (docs, conversations, code) |
| `tasks` | Shared task state |
| `embeddings` | Vector embedding storage |

See `db/init_schema.sql` for full column definitions and migrations.

`memories.memory_type` is now a three-way core layer selector:
- `episodic` — specific events, traces, and observations
- `semantic` — distilled facts, preferences, and conventions
- `procedural` — reusable workflows, runbooks, troubleshooting sequences, rollback plans

The canonical structured procedure lives in `procedures`; the linked
`memories` row keeps a human-readable synopsis so legacy memory search and
older interfaces continue to see something useful.

### Vector Tables (optional, requires sqlite-vec)

| Table | Purpose |
|-------|---------|
| `vec_memories` | Memory embeddings (768-dim) |
| `vec_entities` | Entity embeddings |
| `vec_events` | Event embeddings |
| `vec_context` | Context chunk embeddings |

These tables are created only when sqlite-vec is installed. Everything else
works without them — you just lose vector search.

## Search Architecture

### FTS5 (always available)

Full-text search via SQLite FTS5 with porter + unicode61 tokenizers. Covers
memories, events, entities, and context. Accessed via `brainctl search` and
`Brain.search()`. Queries are sanitized to strip FTS5 operator characters
(`. & | * " ' \` ( ) - @ ^ ? ! , ; :`) and then OR-expanded token-by-token so
natural-language queries ("what does Alice prefer?") match memories that
contain *any* meaningful term, not only memories that contain every word.
Stopwords are dropped before OR expansion.

### Retrieval Executive + Hybrid Search

`cmd_search` now acts as a compatibility shell around a retrieval executive:

1. `retrieval.query_planner` inspects the query and emits a structured plan
   (`normalized_intent`, `answer_type`, target entities, temporal anchors,
   preferred memory layers, candidate tables, abstain policy).
2. `cmd_search` still performs the existing FTS5/sqlite-vec retrieval paths
   for memories, events, and context.
3. `retrieval.candidate_generation` adds a first-class procedural candidate
   path using `procedures_fts` plus structured fallback search.
4. `retrieval.evidence_graph` expands top procedures over
   `procedure_sources` and `knowledge_edges` to gather supporting episodes,
   decisions, events, tools, and rollback relations.
5. `retrieval.late_reranker` deterministically fuses direct lexical match,
   procedural structure match, validation recency, execution history, and
   evidence support.
6. `retrieval.answerability` decides whether to abstain instead of returning
   ungrounded nearest-neighbor junk.

The effective plan and answerability diagnostics surface in `_debug` /
`metacognition` so benchmark misses remain explainable.

The old hybrid core is preserved: memories/events/context still merge FTS5
and sqlite-vec via Reciprocal Rank Fusion when vector search is available.

### Vector Search (optional)

Semantic search via sqlite-vec using cosine similarity (KNN). Requires:
- sqlite-vec Python package installed
- Ollama running locally with an embedding model (e.g., `nomic-embed-text`)
- Embeddings populated via `embed-populate`

Accessed via `brainctl vsearch` or transparently inside `cmd_search` when
available. Supports hybrid mode (FTS5 + vector via RRF) or pure vector
mode.

### Graph Traversal

Multi-hop neighbor queries across the knowledge graph via `brainctl graph`.

### Retrieval Regression Gate

`tests/bench/` ships a deterministic search-quality harness: synthetic
memories + procedures + events + entities with graded queries (3=primary,
2=related, 1=tangential) across entity / procedural / decision / temporal /
troubleshooting / negative / ambiguous classes. The runner reports
P@1, P@5, Recall@5, MRR, nDCG@5 plus P@5 ceiling diagnostics
(`p_at_5_ceiling`, `p_at_5_ratio_to_ceiling`) against a committed baseline at
`tests/bench/baselines/search_quality.json`. Any >2% drop on a headline
metric fails the `test_search_quality_bench.py` pytest regression test.
The harness also records failure modes (`retrieval_failure`,
`utilization_failure`, `hallucination`, `correct_abstain`) and captures the
retrieval executive debug payload. Run with `python3 -m tests.bench.run` or
`bin/brainctl-bench`.

## Knowledge Graph

The `entities` table stores typed, named entities. Every entity carries:

- `compiled_truth` — a rewriteable "current best understanding" synthesis of
  the entity, drawn from observations + linked memories + linked events.
  Refreshed by `brainctl entity compile` or by the nightly consolidation
  cycle. Reads can return just the synthesis via `brainctl entity get ID --compiled`.
- `enrichment_tier` — 1 (critical) / 2 (notable) / 3 (minor), computed from
  recall count, knowledge-edge degree, and event-link count. Tier 1 entities
  get full re-synthesis during consolidation; Tier 3 get observation touch-ups.
- `aliases` — a JSON list of canonical-name variants (misspellings, nicknames,
  emails, handles). Used by the merger as a cheap pre-check before spending
  an embedding on semantic dedup. CLI: `brainctl entity alias add|remove|list`.

The `knowledge_edges` table connects any two records across any tables with
typed, weighted edges:

```
source_table, source_id  -->  target_table, target_id
    relation_type (string)
    weight (0.0 - 1.0)
    agent_id (who created the edge)
```

Relation types include `topical_tag`, `topical_project`, `causal_chain_member`,
`causes`, `semantic_similar`, and any custom type agents define.

Edges are created by:
- The hippocampus consolidation cycle (topical edges)
- The events pipeline (causal edges)
- `embed-populate --graph-edges` (semantic similarity edges)
- Any agent manually via `brainctl graph add-edge`

### Self-Healing Gap Scans

`brainctl gaps scan` detects both metacognitive holes (coverage, staleness,
confidence, contradiction) and knowledge-graph rot:

| gap_type | Detects |
|-----------|---------|
| `orphan_memory` | memory with zero edges + zero recalls + older than 30 days |
| `broken_edge` | `knowledge_edges` row pointing at a deleted memory / entity / event |
| `unreferenced_entity` | entity with no edges, no observations, older than 30 days |

Runs as part of the nightly consolidation cycle; results surface in
`knowledge_gaps` and can be listed via `brainctl gaps list`.

## Consolidation Engine (hippocampus)

`src/agentmemory/hippocampus.py` (exposed as `brainctl-consolidate`) runs periodic maintenance on the memory store:

| Pass | What it does |
|------|-------------|
| **Decay** | Reduces access scores over time; memories that are never recalled fade |
| **Compression** | Merges clusters of related low-value memories into summaries |
| **Dream** | Synthesizes new hypotheses from loosely connected memories |
| **Hebbian** | Strengthens edges between frequently co-accessed records |
| **Procedural synthesis** | Promotes repeated successful action patterns into procedure candidates or canonical procedures |

`bin/consolidation-cycle.sh` chains the hippocampus passes with:

- `brainctl gaps scan` (coverage + self-healing scans)
- `brainctl entity tier --refresh` (T1/T2/T3 promotion across all entities)
- `brainctl entity compile --all` (compiled_truth rewrite for every entity)
- `brainctl trust decay`, global workspace salience pass, health SLO snapshot

Run manually with `bash bin/consolidation-cycle.sh` (or `--dry-run`) or
schedule it via cron.

## Concurrency Model

- **WAL mode**: Multiple concurrent readers, single writer at a time
- **Timeout 10s**: Write attempts queue rather than fail immediately
- **No server process**: Zero overhead, no port conflicts, no daemon to manage
- **File locking**: Handled by SQLite's built-in locking (works on all platforms)

## Modular Command Structure

The CLI is organized into 24 command modules under `src/agentmemory/commands/`.
Each module handles one domain (memory, event, entity, graph, trigger, etc.)
and registers its subcommands with the main CLI parser. This keeps the codebase
navigable and makes it easy to add new command groups.

## Requirements

**Required:**
- Python 3.11+
- SQLite 3.35+ (for generated columns; most systems ship this)

**Optional:**
- `sqlite-vec` — enables vector search tables. Everything works without it.
- Ollama with an embedding model — needed only for `embed-populate` and `vsearch`.

## Backup and Maintenance

```bash
brainctl backup              # On-demand database backup
brainctl validate            # Integrity check
brainctl prune-log --days 30 # Trim old access log entries
brainctl health              # SLO dashboard
```
