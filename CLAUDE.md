# Claude Code — brainctl / agentmemory

## What This Is
Unified agent memory system. SQLite-backed (brain.db) with FTS5, vector embeddings (sqlite-vec + Ollama nomic-embed-text), knowledge graph, affect tracking, belief collapse mechanics, and AGM conflict resolution.

Published as `brainctl` on PyPI (v2.2.1+, current 2.4.10).

## Key Paths
- **DB:** `db/brain.db` (WAL mode, foreign keys ON, 59 user-facing tables, 49 numbered migrations + one unnumbered V2-4 quantum-schema file). The numbered sequence has an intentional gap at 050 — the V2-4 quantum schema (`db/migrations/quantum_schema_migration_sqlite.sql`) occupies that slot without a number because it was applied ad-hoc during the V2-4 rollout and pre-dates the idempotent runner fix in 2.4.8. The runner only picks up files matching `^\d+_.+\.sql$` so the quantum file is a no-op for `brainctl migrate` on fresh installs — apply manually if you need the quantum columns on a new DB. (Audit I28 — 2026-04-19.)
- **CLI:** `bin/brainctl` — main CLI entry
- **MCP server:** canonical entry is `agentmemory.mcp_server:run` (201 tools across `mcp_server.py` + 29 `mcp_tools_*.py` modules). Installed as the `brainctl-mcp` console script via pip. The legacy standalone `bin/brainctl-mcp` only registers a subset and is being phased out.
- **Bench:** `bin/brainctl-bench` — retrieval eval harness (P@k / MRR / nDCG@k regression gate, fixtures under `tests/bench/`)
- **Source:** `src/agentmemory/` — Python package
- **Config:** `config/` — quiet hours, consolidation schedules
- **Agents:** `agents/` — per-agent config (pipeline, engram, etc.)

## Build & Test
```bash
pip install -e .                                      # dev install
brainctl stats                                        # verify DB
brainctl search "test"                                # test search
python3 -m agentmemory.mcp_server --list-tools        # full 199-tool MCP surface
python3 -m tests.bench.run                            # retrieval quality benchmark
python3 -m tests.bench.run --check                    # fail on >2% regression vs baseline
```

## Architecture
- Tables: memories, events, entities, decisions, context, knowledge_edges, affect_log, access_log, agent_state, agent_beliefs
- FTS5 indexes on memories, events, entities
- Vector embeddings via sqlite-vec extension
- Hybrid retrieval: FTS5 + vector via Reciprocal Rank Fusion, routed through a regex intent classifier (`bin/intent_classifier.py`) that normalises 10 intent labels onto 6 rerank profiles inside `cmd_search`
- Retrieval regression-gated by `tests/bench/` (P@1 / P@5 / Recall@5 / MRR / nDCG@5; >2% drop fails CI)
- W(m) worthiness gate on memory writes (surprise scoring + semantic dedup)
- PII recency gate (Proactive Interference Index) on supersedes
- Bayesian alpha/beta tracking on memory recall
- Entities carry a rewriteable `compiled_truth` synthesis, a 3-level `enrichment_tier`, and a first-class `aliases` JSON list (migrations 033–035)
- Knowledge-gap scanner (`brainctl gaps scan`) also detects orphan memories, broken knowledge_edges, and unreferenced entities (migration 036)

## Conventions
- Agent IDs: use descriptive names like `my-agent`, `research-bot`, `code-reviewer`
- Memory categories: convention, decision, environment, identity, integration, lesson, preference, project, user
- Event types: artifact, decision, error, handoff, memory_promoted, memory_retired, observation, result, session_start, session_end, stale_context, task_update, warning
- Entity types: agent, concept, document, event, location, organization, other, person, project, service, tool

## Don't Touch
- Migration files in `db/migrations/` — append-only
- The W(m) gate logic without understanding surprise scoring
- Quiet hours scripts — they're cron-scheduled

## Signed exports (2.3.0+)

`brainctl export --sign` and `brainctl verify` produce / check
portable, signed JSON bundles of memories using the user's Solana
keypair. Local-first by design — memories never leave the machine;
only the SHA-256 hash is ever pinned on-chain (opt-in via
`--pin-onchain`, ~$0.001 per pin). Implementation lives in
`src/agentmemory/signing.py` (offline + on-chain plumbing) and
`src/agentmemory/commands/sign.py` (CLI handlers, parser
registration). Optional dep:

    pip install 'brainctl[signing]'   # pulls solders>=0.21

CLI surface:

    brainctl export --sign --keystore <path> [--filter-agent X]
        [--category Y] [--scope Z] [--created-after T]
        [--created-before T] [--ids 1,2,3] [--pin-onchain]
        [--rpc-url <url>] [-o bundle.json] [--json]

    brainctl verify <bundle.json> [--check-onchain]
        [--rpc-url <url>] [--json]

Exit codes: `0` ok, `1` tamper / missing keystore / IO, `2` unsigned
export attempted or `--check-onchain` found no receipt.

No token gating — anyone with brainctl + a Solana keypair can sign
their own memories (preference memory #1691). Threat model + bundle
format spec + a 30-line "verify without brainctl" recipe live in
`docs/SIGNED_EXPORTS.md`.

## Managed wallet (2.3.2+)

For users who don't already have a Solana wallet (most chat-bot
operators and agent builders), `brainctl wallet new` creates a
non-custodial keypair at `~/.brainctl/wallet.json` (chmod 0600). The
key is generated locally and **never** transmitted, copied, or backed
up by brainctl — the user owns the file. `brainctl export --sign`
auto-discovers the managed wallet when `--keystore` isn't passed
(precedence: `--keystore` > `~/.brainctl/wallet.json` >
`$BRAINCTL_SIGNING_KEY_PATH` > error). Implementation in
`src/agentmemory/commands/wallet.py`. Subcommands: `new` (with
`--force`/`--yes`), `address`, `balance`, `show`, `export <path>`,
`import <path>`, `rm`, `onboard`. Override default location via
`$BRAINCTL_WALLET_PATH` or `--path`.

For AI agents: when a user wants to sign a bundle but doesn't have a
wallet, surface this flow. The MCP tools `wallet_show` and
`wallet_create` let you check / create on the user's behalf — but
`wallet_create` will REFUSE to overwrite an existing keystore unless
explicitly passed `force=true` (destructive — pass only after the
user explicitly consents). Or use `brainctl export --sign
--auto-setup-wallet` which creates a wallet inline and signs in one
shot. `--pin-onchain` with 0 SOL skips cleanly with `exit 0` and
`pin_skipped_reason="zero_balance"` — the offline signature is still
valid; pinning is opportunistic.

## Code-aware ingestion (2.4.5+, optional `[code]` extra)

`brainctl ingest code <path>` walks a source tree and writes file /
function / class entities plus `contains` and `imports` relations into
the existing entity graph. CPU-only via tree-sitter — no LLM, no GPU,
no network at ingest time. SHA256-cached in migration 051 so re-runs
on unchanged trees are metadata-only and finish in well under a
second for a ~100-file package.

Ships three grammars on purpose (python, typescript, go) to keep the
wheel footprint around 4 MB. Adding a language means:

 1. Add grammar to `[code]` extra in `pyproject.toml`.
 2. Add suffix(es) to `EXT_TO_LANG` in `src/agentmemory/code_ingest.py`.
 3. Write `extract_<lang>(path, src, relpath)` and register in `EXTRACTORS`.

CLI surface:

    pip install 'brainctl[code]'
    brainctl ingest code <path> [--scope project:<name>]
        [--languages python,typescript,go] [--no-cache]
        [--max-files N] [--verbose] [--json]

Entity naming is prefixed so searches stay unambiguous: `file:<relpath>`,
`fn:<relpath>:<qualname>`, `class:<relpath>:<qualname>`,
`module:<import_spec>`. The fine-grained kind lives in
`properties.kind` alongside `language`, `path`, `line`, `signature`,
`parent`. Provenance is encoded on `knowledge_edges.weight` — 1.0 for
direct-source (`contains`, local resolvable imports), 0.7 for
unresolved external imports. Re-ingest does **not** touch
`last_reinforced_at` / `co_activation_count`: those are synaptic
reinforcement signals owned by hippocampus, and re-parsing a file is
an idempotent state-sync, not an activation event.

Inspired by `github.com/safishamsi/graphify` (the `{nodes, edges}`
extractor protocol + SHA256 skip-when-unchanged pattern).

Known follow-ups (not blocking the extra):

 * No `mcp__brainctl__ingest_code` tool yet — agents that want to
   trigger ingest must shell out via the CLI. MCP wrapper is a
   separate PR.
 * `init_schema.sql` won't include `code_ingest_cache` until it's
   regenerated in a release commit. Until then, fresh installs that
   want code-ingest need `brainctl migrate` applied after
   `brainctl init`.
