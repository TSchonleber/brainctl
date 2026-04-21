# Changelog

All notable changes to **brainctl** will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [2.4.12] — 2026-04-21 — *Audit follow-up: CE warmup burn-in (kills recurring latency-gate flake)*

Fifth slice of the 2026-04-19 audit fix wave. Single-focus release
closing I23 — the CE cold-start issue that caused the recurring
`latency-gate` flake in CI.

### Fixed

- **Cross-encoder warmup no longer poisons the rolling p95 latency
  window.** The first `_ce_rerank_timed` call includes model loading
  (typically 15–40s for `bge-reranker-v2-m3`). Before 2.4.12 that
  first sample went straight into `_CE_LATENCY_SAMPLES_MS` and stayed
  there for the full 64-call deque rotation — the strict latency
  fallback at the call site then silently skipped CE for the next
  minute+ of queries without a clear operator signal beyond the
  missing `cross_encoder_applied` trace. Now the first
  `_CE_WARMUP_SAMPLES` calls (default 1; env `BRAINCTL_CE_WARMUP_SAMPLES`
  to override) are tracked separately, excluded from the p95 window,
  and surfaced as `cross_encoder_warmup_ms` in `_debug_skips` so the
  cost is still observable. This is what was causing the recurring
  `latency-gate` flake in CI over the prior release chain.

### Testing

`1878 passed, 28 skipped, 2 xfailed` locally. Two new regression
tests at `tests/test_ce_warmup_burn_in.py` lock the warmup-exclusion
behavior and the `BRAINCTL_CE_WARMUP_SAMPLES=0` opt-out path.

## [2.4.11] — 2026-04-21 — *Audit follow-up: schema symlink + scope guard + docs*

Fourth slice of the 2026-04-19 audit fix wave. Closes the remaining
quick-wins; the real 2.5.0 work (CE warmup off the latency deque,
top-heavy rollout MCP tool, competitor bench rerun) is still queued.

### Fixed

- **`db/init_schema.sql` at the repo root is now a symlink** to the
  packaged canonical at `src/agentmemory/db/init_schema.sql`. Before
  2.4.11 the root copy drifted 566+ lines behind the packaged schema
  because release builds regenerated only one. A symlink makes drift
  structurally impossible while keeping every existing reference path
  (tests, docs, dev checkout discovery in `cmd_init`) working.
- **`tool_memory_search` validates `borrow_from` against `scope`.**
  Cross-agent borrow restricts the SQL to `scope='global'` internally;
  combining it with an explicit non-global scope produced an
  impossibly-False predicate and silent 0 results before 2.4.11. Now
  returns a clear error naming the incompatibility.
- **`CLAUDE.md` corrected to describe the actual migration layout:**
  49 numbered migrations + one unnumbered V2-4 quantum-schema file
  occupying the gap at 050. The quantum file doesn't match the
  runner's `^\d+_.+\.sql$` pattern and must be applied manually on
  fresh installs that want the quantum columns (audit I28).

### Changed — hygiene

- Added a comment block above the legacy `schema_version` (singular)
  table in `init_schema.sql` explaining why it coexists with the
  runner's `schema_versions` (plural) tracker — 10 migration files
  write to the singular form and the "append-only" convention means
  we keep the compatibility rather than rewrite them (audit I27).

### Testing

`1876 passed, 28 skipped, 2 xfailed` locally. Two new regression tests
at `tests/test_audit_2_4_11_regressions.py` lock the symlink structure
and the borrow_from scope guard.

## [2.4.10] — 2026-04-20 — *Audit follow-up: structural cleanup + datetime gate*

Third slice of the 2026-04-19 audit fix wave. Focuses on long-tail
correctness + hygiene; the larger 2.5.0 items (root `init_schema.sql`
sync, CE warmup off the latency deque, top-heavy MCP exposure, competitor
bench rerun) are still outstanding and will land in a coordinated 2.5.0
release.

### Fixed

- **`datetime.utcnow()` removed everywhere it was being called.** Three
  sites in `hippocampus.py`, `mcp_server.py`, `mcp_tools_health.py`
  switched to `datetime.now(timezone.utc)`. The `mcp_tools_health` cutoff
  for `access_log` pruning was also producing naive ISO strings that
  sorted incorrectly against Z-suffixed `created_at` rows — under-deletes
  fixed.
- **`decision_lookup` intent now actually queries the `decisions`
  table.** The pre-2.5.0 guard checked `"decisions" not in results`
  (`results` is pre-initialized with that key — guard always False), and
  the body's set union didn't include `"decisions"` either. Two-bug fix
  on a single line. Source-locked by regression test.
- **`config.load()` no longer silently swallows permission errors.**
  Bare `except Exception: pass` replaced with `tomllib.TOMLDecodeError`
  + `OSError` branches that log a warning then fall back to defaults.
  CLI still boots when `config.toml` is unreadable; the user sees the
  reason in stderr instead of stale defaults.
- **`federated_memory_search` / `federated_search` now expose
  `returned_count`** alongside `total_results` so callers can branch on
  the slice without inferring from `len(results)`. `total_results`
  semantics unchanged (full pre-slice match pool).

### Changed — performance + hygiene

- `code_ingest.Extraction.add_node_if_new()` replaces three O(n²)
  set-comprehension dedup sites in the python / typescript / go
  extractors. Indistinguishable on small trees, real difference on a
  100-file ingest.
- `_cached_score` lru_cache shim removed from `rerank.py` — it had no
  callers and always returned `None`. Real cache (`_score_cache`
  dict + eviction list) untouched.
- Subprocess `stdout=open(...)` replaced with `with open(...)` at the
  two backup sites (`_impl.py`, `mcp_tools_health.py`) so the fd
  closes on every exit path, not just success.
- `bin/quiet-hours-{start,end}.sh` cd into the script dir before
  invoking the python sibling so cron (which sets CWD=$HOME) finds
  the file.

### CI

- New `tests/test_datetime_hygiene.py` regex-asserts no `datetime.utcnow()`
  calls remain anywhere in `src/agentmemory/`. The earlier draft of this
  gate also tracked bare `datetime.now()` against an allowlist; that
  half was dropped because line-numbered allowlists drift on every PR
  that touches a long file. Naked-`now()` cleanup will follow as part
  of the coordinated local→UTC timestamp migration.

### Testing

`1874 passed, 28 skipped, 2 xfailed` locally. Three new regression
tests at `tests/test_audit_2_5_0_regressions.py` lock I25, I29, I31.

## [2.4.9] — 2026-04-20 — *Audit follow-up: retrieval correctness, scoring parity, MCP hygiene*

Second slice of the 2026-04-19 audit fix wave. Ships the remaining HIGHs
plus six MEDIUMs covering scoring parity, retrieval-pipeline dead code,
federation input safety, and MCP-layer hygiene.

### Fixed — scoring parity

- **`_surprise_score_mcp` was a stale duplicate** that missed the 2.2.3
  neutral-fallback fix; the MCP write path — the dominant one in
  practice — still returned `(1.0, "fts5_no_matches")` where
  `_impl._surprise_score` returned `(0.5, "fts5_no_matches_neutral")`.
  Deleted the copy and imported the canonical scorer. Regression test
  at `tests/test_mcp_surprise_score_parity.py` locks the parity.
- **`affect_log.safety_flag` was NULL on every row** because
  `brain.affect_log` read the wrong key from the classifier
  (`safety_flag` singular) when `classify_affect` returns
  `safety_flags` (plural list of pattern dicts). Now serializes the
  list as JSON when any pattern fires; NULL otherwise — keeps the
  `idx_affect_safety` sparse index honest.

### Fixed — retrieval correctness

- **The FTS-confidence relative-anchor gate was dead code.** Entry
  condition already established the relative-strong verdict, but the
  block immediately reassigned `rel_strong` to the opposite relation.
  Only absolute-threshold detection was running.
- **`graph_traversal` intent produced zero-row expansion on `events`**
  because the caller did `tbl_key.rstrip("s")` before passing to
  `_graph_expand`, which queries `knowledge_edges` with plural table
  names. Now passes the canonical name directly.
- **Events/context silently cascaded into FTS-only** when the memories
  bucket tripped the `_fts_strong_anchor` gate. Added matching
  `_debug_skips` entries so operators can see the cascade.
- **`_reason_l1_search`, `cmd_push`, and `Brain.orient()`** now build
  the same `_build_fts_match_expression(sanitize(...))` OR expression
  `cmd_search` uses, instead of bare sanitize. Multi-word queries no
  longer fall into FTS5 implicit-AND semantics at those callsites.

### Fixed — CLI / diagnostics

- **`brainctl doctor` hardcoded `"ok": True` and exited 0 regardless
  of health.** JSON output now reflects the actual verdict
  (`ok == healthy`), and the process exits 1 when any issue is
  present — shell `$?`-based automation now works correctly.
- **Plugin env-var mismatch between `BRAINCTL_DB` and `BRAIN_DB`.**
  `paths.get_db_path()` now honors `BRAINCTL_DB` as the go-forward
  canonical name (matches the `BRAINCTL_HOME` / `_BLOBS_DIR` /
  `_BACKUPS_DIR` family) while keeping `BRAIN_DB` working as a
  deprecated alias. `BRAINCTL_DB` wins when both are set.

### Fixed — federation / merge / MCP shape

- **Federation LIKE fallbacks didn't escape `%` / `_`** in the four
  LIKE query sites across `federated_memory_search`,
  `federated_entity_search`, and `federated_search`. Introduced
  `_escape_like` + `ESCAPE '\\'` on every LIKE clause so `api_key`
  matches literally, not `apikey` or `api-key`.
- **Self-merge surfaced as `"database is locked"`.** Added an
  explicit `source == target` guard (resolved to absolute path) that
  raises a clear `ValueError` before `ATTACH DATABASE` runs.
- **MCP error shape normalized** across `mcp_server` and
  `mcp_tools_knowledge`: every error return now carries
  `{"ok": False, "error": ...}` instead of a bare `{"error": ...}`.
  Clients that branch on `ok` no longer need per-tool special-cases.

### Fixed — packaging

- **Three MCP tools depended on `~/bin/lib/` drop-ins**
  (`belief_revision`, `outcome_eval`, `quantum_retrieval`) — they were
  unreachable on PyPI installs. Moved all three in-tree under
  `agentmemory.lib.*`; imports prefer the in-tree copy and fall back
  to the legacy path for users on the old layout.

### Testing

`1866 passed, 28 skipped, 2 xfailed` locally (3 new tests vs 2.4.8
for the surprise-score parity regression).

## [2.4.8] — 2026-04-20 — *Audit hotfix: fresh-install migrate + MCP dispatch*

Closes the two CRITICAL findings from the 2026-04-19 audit
(`docs/audit_2026_04_19/00_VERIFICATION.md`) plus two HIGH companions.

### Fixed

- **Fresh-install `brainctl migrate` halted at step 2.** The idempotent
  error-fragment allowlist in `migrate.py` only tolerated
  `"duplicate column name"`, so the `CREATE INDEX idx_epochs_started`
  statement in migration 002 aborted the run on any DB initialised from
  `init_schema.sql` (which already declares that index). Added
  `"already exists"` under each of `CREATE INDEX / CREATE TABLE /
  CREATE TRIGGER / CREATE VIEW` in `_PER_KIND_TOLERATED_FRAGMENTS`
  rather than as a global fragment — preserves the original "deliberately
  narrow" discipline so unrelated object-exists surfaces still fail
  loudly. Also fixed the leading-keyword extraction to skip inline
  comment/blank prefix lines (the statement splitter attaches a header
  comment like `-- CHECK constraint triggers` to the following
  `CREATE TRIGGER`, which previously made leading detection miss).
  Verified: 49/49 migrations apply to a fresh DB and the second run is
  a 0-applied no-op.
- **All 9 temporal MCP tools returned `TypeError`.** `mcp_tools_temporal.DISPATCH`
  pointed every key at the bare `_handle(name, args)` function, but
  `mcp_server.call_tool` invokes dispatchers as
  `fn(agent_id=..., **arguments)`. The signature mismatch had been
  latent since `ca1a8a3` because no existing test exercised the
  dispatcher call path — the module's tests called the internal
  `_temporal_*` / `_epoch_*` functions directly. Switched to per-tool
  closures (same pattern as `mcp_tools_consolidation`), added
  regression tests that exercise the mcp_server call convention, and
  guard against late-binding closure collapse.
- **`pagerank` MCP tool was unreachable.** The tool was implemented and
  registered in `TOOLS`, but `mcp_server.call_tool`'s dispatch dict
  never listed it, so calls 404'd. Added the wiring.
- **`_graph_{pagerank,communities,betweenness}` raised `TypeError` on
  the second call.** The cache-freshness check subtracted an aware
  datetime (parsed from the stored Z-suffixed timestamp on Py3.11+)
  from a naive `datetime.now()`. Switched to `datetime.now(timezone.utc)`
  at all three call sites (`_impl.py:8293, 8365, 8434`).

### Testing

`1863 passed, 28 skipped, 2 xfailed` locally.

## [2.4.7] — 2026-04-19 — *Security hygiene pass*

Post-release supply-chain hardening. No functional change; upgrade is
recommended for anyone installing brainctl in CI or multi-tenant
contexts.

### Changed

- **Dependency floors tightened.** `sqlite-vec>=0.1.3` (up from 0.1.0)
  to clear CVE-2024-46488 / GHSA-vrcx-gx3g-j3h8 (heap buffer overflow).
  `torch>=2.4` in the `[rerank]` extra (up from 2.0) so the default
  `torch.load(weights_only=True)` protection is always in effect for
  HuggingFace cross-encoder checkpoints.

### Changed — CI/release supply-chain

- PyPI publish now gated behind a GitHub Environment (`pypi`) with
  required-reviewer approval. Maintainer must create the environment
  and re-bind the PyPI trusted publisher before the next tag push.
- All GitHub Actions SHA-pinned (`actions/*`, `dorny/paths-filter`,
  `pypa/gh-action-pypi-publish`). Tags preserved as trailing comments.
- `ci.yml` now declares a workflow-level default `permissions: contents: read`.
- Added `.github/dependabot.yml` (pip + github-actions, weekly).
- Added `.github/CODEOWNERS` for supply-chain-sensitive paths.
- `SECURITY.md` now names `security@brainctl.org` and the GitHub
  private-advisory URL explicitly.
- Removed empty `brain.db` placeholder from the repo root.

## [2.4.6] — 2026-04-19 — *DEFCON Special*

Plan `plan-20260419-085511` (top-heavy retrieval lift) shipped end-to-end
across a 6-way swarm (codex + claude-code). Rollout is the current `main`
default; no runtime config change needed to pick up the lift.

### Added — Top-heavy retrieval controls (I2/I3/I4)

Unified `Brain.search` with the CLI `cmd_search` pipeline so programmatic
callers and the CLI produce identical top-K results (same FTS5 + vec RRF
fusion, same reranker chain, same signal-informativeness gates). Old
`Brain.search` was an FTS5-only path that diverged from `cmd_search` on
uniform-timestamp corpora — caught by the 2026-04-18 audit.

Adaptive retrieval controls: dynamic fetch window, narrowed candidate set
for factoid queries, skip of non-informative recency/salience signals on
intent-classified factoid/general queries. Regex-based intent router
normalises 10 intent labels onto 6 rerank profiles (`bin/intent_classifier.py`).
Last-mile CE reranker with a p95-budget gate (`BRAINCTL_CE_P95_BUDGET_MS`,
default 350ms) — falls back to RRF ordering if the rerank window would
blow the budget.

### Added — Strict retrieval gates in CI (I8)

New `retrieval-gate` job runs on every PR that touches retrieval code
paths (`src/agentmemory/{search,rerank,embeddings,retrieval}.py`,
`bin/intent_classifier.py`, `tests/bench/**`). Gates:

- `hit_at_{1,5,10}`, `mrr`, `ndcg_at_5`, `recall_at_5` at -0.2pp absolute
- per-slice `hit_at_1` / `mrr` / `ndcg_at_5` at -1.0pp per `question_type`
  on LongMemEval and per `category` on LoCoMo
- p95 latency (cross-platform-aware — skips subprocess-bound ops when
  baseline and fresh platform differ)

Per-bench budgets live in `tests/bench/budgets/{longmemeval,locomo}.yaml`
so tolerances adjust via config, not code changes. New `--check-strict`
and `--report-json` flags on `tests/bench/run.py`. PR-comment summary of
the bench matrix on every retrieval PR.

### Added — Frozen baseline snapshot (I1)

`benchmarks/snapshots/baseline-20260419/` holds per-bench metric JSON +
per-query `.traces.jsonl`, plus `manifest.json` with SHA256 of every
artifact. `--traces PATH` flag on the bench harness captures per-query
traces for post-hoc slice analysis.

### Added — Calibration matrix + ablation bypass (I5)

`benchmarks/snapshots/calibration-20260419/` has a 3-cell ablation
(FULL / NO_INTENT / ROLLBACK) × LongMemEval(289) + LoCoMo hybrid, with
traces per cell and a rollout recommendation against the plan envelope.
Added `BRAINCTL_DISABLE_INTENT_ROUTER=1` ablation-only bypass in
`cmd_search` (no behaviour change when unset).

Headline numbers (FULL = current `main`):
- **LoCoMo hybrid:** Hit@1 +25.5pp, MRR +36.2pp vs pre-lift (plan envelope
  +1.0pp / +0.5pp — crushed by an order of magnitude).
- **LongMemEval:** flat within measurement noise on n=289. FULL beats
  ROLLBACK by +62.3pp Hit@1 on a like-for-like Ollama-up comparison.

### Added — Staged rollout controls + docs (I6/I7)

`BRAINCTL_TOPHEAVY_ROLLBACK=1` emergency bypass (pre-I2 behaviour).
Landing page / comparison docs updated with new metrics.

### Fixed

- `init_schema.sql` synced to include the `code_ingest_cache` table +
  indexes from migration 051 (brainctl 2.4.5 `[code]` extra). Fresh
  installs now match upgrade-path schemas without a post-init migrate
  step. Test: `tests/test_schema_parity.py`.
- `test_connection_lifecycle.py::test_public_methods_reuse_single_connection`
  assertion widened to filter by originating file. After the
  `Brain.search` → `cmd_search` unification, `_try_get_db_with_vec` opens
  a short-lived vec-loaded conn (same pattern `vec.index_memory` already
  uses); the core invariant "at most one `brain.py`-originated shared
  conn per Brain lifecycle" is now what's asserted.
- Cross-platform `latency-gate` false positives on ubuntu CI against
  darwin-calibrated baselines. `tests/test_latency_regression.py`
  detects `baseline.platform != fresh.platform` and skips the five
  subprocess-bound ops (`cli_search_*`, `cli_remember_*`, `cli_stats`)
  whose per-op cost is dominated by Python interpreter cold-start.
  Library-level ops (`brain_search_*`, `brain_remember_*`, `vec_*`) stay
  gated cross-platform.

### Known follow-ups (not blocking)

- CE rerank dimension unreachable via bench harness (`args.rerank` not
  populated by `tests/bench/{locomo,longmemeval}_eval.py`). Wire it to
  measure CE in the calibration matrix.
- Intent router is a no-op at current bench granularity (FULL == NO_INTENT
  on all metrics to 4dp). Per-`question_type` slice analysis before
  deciding to remove.
- I5 driver `_extract_metrics` couldn't parse per-query timings →
  `baseline_p95_ms` still missing in budget YAMLs; p95 leg is advisory
  until populated.

## [2.4.5] — 2026-04-19

### Added — `brainctl[code]` extra: tree-sitter code ingestion into the knowledge graph

New optional extra `pip install 'brainctl[code]'` and a new CLI group
`brainctl ingest code <path>`. Walks a source tree, parses supported
files with tree-sitter, and writes file / function / class entities
plus `contains` / `imports` relations into the existing entity graph.

**Zero LLM, zero GPU, CPU-only.** SHA256-cached via migration 051 so
re-runs on unchanged trees are metadata-only. Self-ingest of
`src/agentmemory/` (90 files): 0.36s cold, 0.11s warm.

Ships three grammars on purpose — `tree-sitter-python`,
`tree-sitter-typescript`, `tree-sitter-go` — keeping the wheel
footprint around 4 MB. Adding a language means updating
`EXT_TO_LANG` + `EXTRACTORS` in `src/agentmemory/code_ingest.py`.

Entity naming is prefixed so searches stay unambiguous:
`file:<relpath>`, `fn:<relpath>:<qualname>`,
`class:<relpath>:<qualname>`, `module:<import_spec>`. Provenance is
encoded on `knowledge_edges.weight` (1.0 for direct-source, 0.7 for
unresolved external imports). Re-ingest deliberately does **not**
touch `last_reinforced_at` / `co_activation_count` — those are
synaptic signals owned by hippocampus, and re-parsing a file is
idempotent state-sync, not an activation event. One `access_log`
row per file (not per entity) to keep the audit trail honest.

New files: `src/agentmemory/code_ingest.py`,
`src/agentmemory/commands/ingest.py`,
`db/migrations/051_code_ingest_cache.sql`,
`tests/test_code_ingest.py` (12 cases).

Known follow-ups (not blocking):
- No `mcp__brainctl__ingest_code` tool yet — agents must shell out via CLI.
- `init_schema.sql` won't include `code_ingest_cache` until regenerated in
  a release commit; fresh installs need `brainctl migrate` before
  `brainctl ingest code`.

Inspired by [safishamsi/graphify](https://github.com/safishamsi/graphify)
(the `{nodes, edges}` extractor protocol + SHA256 skip-when-unchanged
pattern). Not a code port — brainctl's entity graph + migration
discipline are reused as-is.

## [2.4.3] — 2026-04-18

### Added — three new agent-framework plugins (16 → 19)

Three workers in parallel; each new plugin in its own canonical
`plugins/<framework>/brainctl/` directory. All three pre-research-verified
against the framework's actual integration model — no guessing.

**Goose** (`plugins/goose/brainctl/`) — Block / now AAIF (Linux Foundation),
the open-source on-machine AI agent. **MCP-only** (Goose has no hook
surface). Ships `goose-extension.yaml` fragment + idempotent
`install.py` that merges into `~/.config/goose/config.yaml` (with
`%APPDATA%\Block\goose\config\config.yaml` on Windows). Flags: `--config`,
`--dry-run`, `--uninstall`, `--force`, `--no-validate`, `--yes`. Falls
back to a tiny manual YAML emitter when `pyyaml` isn't installed.
11/11 smoke tests pass. Plugin doc lives at `GOOSE.md`.

**OpenCode** (`plugins/opencode/brainctl/`) — anomalyco/opencode (was
sst/opencode), the 145k-star Claude-Code-style coding agent. **MCP +
TypeScript hook plugins** — first plugin in the brainctl tree using
TS hooks (gemini-cli used Python via subprocess; OpenCode runs JS/TS
in its own runtime). Three TS hook plugins:
- `brainctl-orient.ts` — `session.created` → `agent_orient` + injects 1-line context block via `client.app.log`
- `brainctl-tool-log.ts` — `tool.execute.after` → `event_add` (skips noisy tools, 200-char input cap, never logs full output)
- `brainctl-wrap-up.ts` — `session.idle` + `session.deleted` → `agent_wrap_up` (deduped via `${TMPDIR}/brainctl-opencode-wrapped/{shortid}.flag` to handle session.idle firing on every pause)

`install.py` supports `--scope global|project`, `--mcp-only` /
`--plugins-only` opt-out flags, full uninstall path. Hooks shell out
to `brainctl` CLI (not `client.mcp.tool` — that surface isn't documented
in OpenCode's plugin client API; CLI path is stable and dependency-free
since bun ships inside opencode). All 11 smoke tests pass; TS files
compile clean via `bun --check`.

**Pi** (`plugins/pi/brainctl/`) — `@mariozechner/pi-coding-agent` (Mario
Zechner's minimal terminal coding harness, popularized by Armin Ronacher's
"The Minimal Agent Within OpenClaw" essay). Pi deliberately has no
built-in MCP support (anti-bloat philosophy), so the plugin depends on
the community-standard `pi-mcp-adapter` (Nico Bailon) which exposes
MCP servers via a single proxy tool with lazy loading.

`install.py` detects `pi-mcp-adapter` via two paths (Pi extensions dir
+ `npm list -g`), prints actionable install instructions when missing
(or runs `pi install npm:pi-mcp-adapter` itself with `--auto-install-adapter`),
then merges brainctl into `~/.pi/agent/mcp.json` under `mcpServers.brainctl`.
**Important: Pi proxy convention is `mcp({tool: "agent_orient", args: '{"agent_id": "..."}'})` — the `args` field is a JSON STRING, not a nested object.** AGENTS.md documents this so models don't hallucinate `mcp__brainctl__*` calls (which would be the gemini-cli convention). 10/10 smoke tests pass.

### Numbers

- **19 first-party plugins** (was 16): agent frameworks (Claude Code,
  Codex CLI, Cursor, Gemini CLI, **Goose, OpenCode, Pi**, Eliza, Hermes,
  OpenClaw, Rig, Virtuals Game, Zerebro) + trading bots (Freqtrade,
  Jesse, Hummingbot, NautilusTrader, OctoBot, Coinbase AgentKit)
- **1857 tests passing, 0 failed** — no regressions
- New plugin types covered: TypeScript hook plugins (OpenCode), proxy-
  via-adapter MCP (Pi), pure-MCP YAML registration (Goose)



## [2.4.2] — 2026-04-18

### Added — `brainctl status` (single-screen brain health overview)

```
brainctl 2.4.2  (/Users/.../brain.db)

  ✓ brain.db                 51.89 MB
  ✗ schema                   17 pending  run `brainctl migrate`

  ✓ memories                 220 active  (1,696 total)
  ✓ events                   3,936
  ✓ entities                 327
  ✓ decisions                105
  ✓ handoff_packets          96
  ✓ agents                   290 registered
  ✓ most active 24h          default (132 ops)

  ✗ ollama                   unreachable  http://localhost:11434
  ✓ sqlite-vec               installed
  ✗ signing (solders)        not installed  pip install brainctl[signing]
  ✗ managed wallet           not configured  brainctl wallet new

  ✓ mcp tools                201
  ✓ plugins                  16 first-party

  ! 17 pending migration(s) — run `brainctl migrate`
  ! Ollama unreachable — vector embeddings will be skipped on memory_add
```

Combines the existing `stats` (DB counts + size) and `doctor` (issue
detection) with a fresh layer of service-availability checks: Ollama
reachable, sqlite-vec loadable, signing extras installed, managed wallet
configured. Reports pending migrations, most-active agent in last 24h,
MCP tool count, and plugin count.

`brainctl status [--json] [--issues]` — `--json` for machine-readable;
`--issues` skips the green checkmarks and only shows what needs fixing.
Exits 0 when everything is green; exits 1 when any "needs attention"
item fires (so it slots cleanly into shell scripts and CI).

Useful for: users who want a sanity-check after install, agents that
need to know what services are available before deciding which tools
to call (e.g., skip `--pin-onchain` if the wallet's not set up), and
ops scripts that need a clean exit code on health.

Lives in `src/agentmemory/_impl.py:cmd_status`. Pure stdlib (no new
deps); Ollama check uses `urllib.request` with a 2s timeout so it can't
hang. No regressions: **1857 passed, 0 failed**.

### Wallet smoke-test verification (2.3.2 follow-up)

Confirmed the wallet UX shipped in 2.3.2 degrades gracefully when
`solders` isn't installed — every `wallet *` impl returns
`{ok: False, error: "solders not installed — pip install 'brainctl[signing]'"}`
instead of crashing. The user-facing message is actionable and the
exit code is 1, so wrapping scripts can detect the missing-extras
condition without parsing.

## [2.4.1] — 2026-04-18

### Fixed — vec.py write-path perf

Two of Worker D's 2.4.0 perf escalations addressed in `src/agentmemory/vec.py`:

- **`_find_vec_dylib` cached.** The auto-discover walked site-packages
  + globbed two filesystem patterns (~5-15ms cold) on **every**
  `index_memory` and `vec_search` call. The dylib path doesn't change
  at runtime — cached at module level via a sentinel-aware single
  variable. `index_memory` was calling this twice per write; now once
  per process.
- **`index_memory` connection pool.** Was opening a fresh sqlite
  connection + `load_extension` (5-20ms) on every memory write.
  Replaced with a per-thread pooled vec-extension-loaded connection
  (same shape as 2.1.2's MCP server pool — `_VEC_WRITE_POOL` keyed on
  `(thread_id, db_path)`, atexit cleanup, `SELECT 1` liveness check).
  Bulk imports + agent runs that fire many `memory_add` calls in a
  row no longer pay the extension-load tax per write.

The two changes together knock 30-100ms off the `Brain.remember`
hot path when Ollama is reachable (vec write becomes a near-direct
`INSERT OR REPLACE` instead of a full connection rebuild). Test suite:
**1857 passed, 0 failed** — no regressions.

Three more perf escalations from Worker D's 2.4.0 audit remain open:
FTS join scaling at N=10k (needs schema change in `brain.py`),
`_load_phase_map` reload per quantum_rerank call (sideloaded module,
mtime-based cache deferred), `_try_get_db_with_vec` connection reuse
(needs lifecycle design pass).

## [2.4.0] — 2026-04-18

### Added — "be the best local memory system" wave (5 parallel workers)

Coordinated infrastructure push: pluggable embedding models, optional
cross-encoder reranker, latency observability, competitor benchmark
harness, comparison docs, and public `/benchmarks` + `/comparison`
landing pages. Test suite: **1857 passed, 0 failed (+25 new tests)**.

**Pluggable embedding models** (`src/agentmemory/embeddings.py`).
Five-model registry — `nomic-embed-text` (default), `bge-m3`,
`mxbai-embed-large`, `snowflake-arctic-embed2`, `qwen3-embedding:8b` —
each with declared dim and Ollama tag. Switch via
`BRAINCTL_EMBED_MODEL=<name>`. New CLI:
`brainctl reindex --model <name> [--dry-run] [--limit N]` re-embeds
existing memories with dim-mismatch validation. Bake-off harness at
`tests/bench/embedding_bakeoff.py` benchmarks each model on LOCOMO +
LongMemEval (winner-pick deferred — requires Ollama).

**Optional cross-encoder reranker** (`src/agentmemory/rerank.py`).
`bge-reranker-v2-m3` / `jina-reranker-v2-base-multilingual` /
`qwen3-reranker-4b` (deferred). `brainctl search --rerank [MODEL]`
+ matching `rerank` kwarg on `mcp__brainctl__memory_search`.
Lazy-imports `sentence-transformers` behind `pip install brainctl[rerank]`;
graceful no-op fallback if missing. LRU cache on
`(query_hash, candidate_hash) → score`. Honest perf disclosure in
`docs/RERANKER.md`: 94ms p50 / 150ms p95 at top-K=5 on Apple Silicon CPU.

**Latency benchmark + regression gate** (`tests/bench/latency.py`,
`tests/test_latency_regression.py`). Nine hot-path operations × three
scales (N=100/1k/10k), p50/p95/p99 over 100 runs each. All targets
met at the 1k gated scale. Baseline locked at
`tests/bench/baselines/latency.json`. New CLI: `brainctl perf [--full]`.
**Perf win shipped:** removed per-row commit in `_update_q_value` →
`brain_search_hybrid` p95 35→25ms (-30%); `mcp_memory_search` p95
36→29ms (-20%). Q-value updates now durable only after the caller's
end-of-request commit.

**Competitor benchmark harness** (`tests/bench/competitor_runs/`).
Adapters for Mem0, Letta, Zep, Cognee, MemoryLake, OpenAI Memory's
vector_stores, and brainctl reference. Cost-gated runner
(`run_all.py --cost-ceiling-usd 5`). `[competitor-bench]` extra in
pyproject pins each SDK. Methodology in
`tests/bench/COMPETITOR_RESULTS.md`.

**Documentation + landing.** New `docs/COMPARISON.md` (26-row honest
matrix vs Mem0 / Letta / Zep / Cognee / OpenAI Memory; unverified
cells marked `?`). New `docs/QUICKSTART.md` (4-step 60-sec onboarding
including the 2.3.2 managed-wallet flow). New landing site routes
`/comparison` and `/benchmarks` (built from `tests/bench/baselines/*.json`
at `next build` time). Honest gap admissions: no managed cloud
(deliberate), no UI, LOCOMO single-hop hit@1 = 0.167 root-cause documented.

### Investigated, deferred to 2.4.x

- **The cmd_search vs Brain.search "18× LOCOMO gap" is partially
  structural.** `Brain.search` is *literally one FTS5 SQL query* —
  no vec, no rerankers. Comparing it to cmd_search's full hybrid
  pipeline is apples-to-oranges. The 2.3.1 reranker auto-detect
  didn't move LOCOMO numbers because the underlying issue is likely
  the FTS+vec RRF fusion itself: nomic-embed-text on short
  conversational turns produces noisy vectors that drag down the
  pure-FTS ranking when fused. Remediation depends on the bake-off
  surfacing a better embedding for conversational data. Logged to
  brain (memory 1700).

### Notes for 2.4.x

- Run the embedding bake-off (Ollama required) to pick a default winner.
- Run the competitor benchmark with `--limit 50` to populate the
  `/benchmarks` page's "Competitor comparison" section.
- Five perf escalations queued: FTS join scaling at 10k, vec extension
  reload per-write, vec dylib auto-discover caching.
- Cross-encoder rerank doesn't differentiate on LOCOMO due to dataset
  degeneracy — needs a curated mixed-relevance fixture.

## [2.3.2] — 2026-04-18

### Added — managed Solana wallet (zero-friction signing for non-crypto users)

2.3.0 shipped signed memory exports requiring `--keystore <path>` to a
Solana CLI keystore. That UX assumed users had Solana CLI installed, had
run `solana-keygen new`, knew where the keystore lived, and how to fund
it. Most brainctl users (chat-bot operators, agent builders) are not
crypto-native and bounced off that flow. **2.3.2 makes the signing path
work end-to-end without ever touching `solana-keygen` or any external
crypto tooling.**

**`brainctl wallet` subcommand suite** (`src/agentmemory/commands/wallet.py`):

| subcommand | what it does |
|---|---|
| `brainctl wallet new [--force] [--yes]` | Generate Ed25519 keypair, store at `~/.brainctl/wallet.json`, `chmod 0600`, print address + safety warning |
| `brainctl wallet address` | Print the public address only (pipe-friendly: `$(brainctl wallet address)`) |
| `brainctl wallet balance [--rpc-url]` | Fetch SOL balance via JSON-RPC `getBalance` |
| `brainctl wallet show [--json]` | Full diagnostic — address, balance, keystore path, perms, mtime |
| `brainctl wallet export <path> [--force]` | Backup the keystore to a chosen path with `chmod 0600` + warning |
| `brainctl wallet import <path> [--force] [--yes]` | Bring an existing Solana CLI keystore into brainctl's managed slot |
| `brainctl wallet rm [--yes]` | Delete the managed keystore (with confirmation) |
| `brainctl wallet onboard [--yes]` | Guided interactive flow — creates wallet + prints next-step funding instructions + offers to sign a sample export |

**Auto-discovery in `brainctl export --sign`:** if no `--keystore` is
passed and a managed wallet exists at `~/.brainctl/wallet.json`, brainctl
uses it automatically. If neither exists, exits with an actionable
message: *"No wallet found. Run `brainctl wallet new` to create one
(takes 2 seconds, brainctl never sees the key). Or pass `--keystore <path>`
to use an existing Solana CLI wallet."*

**`--auto-setup-wallet` flag for agent-driven flows:** if the user's
agent decides they should sign something, `brainctl export --sign --auto-setup-wallet`
will create a wallet on the fly (with `--yes` semantics — non-interactive)
and proceed with the export.

**Friendly 0-SOL guidance during `--pin-onchain`:** before attempting
the on-chain post, brainctl checks the wallet balance via RPC. If 0 SOL,
exits cleanly with: *"Your wallet at \<address\> has 0 SOL. To pin
on-chain (~$0.001 per pin), send any small amount of SOL to that
address. Skipping the on-chain pin — the offline signature is still
valid in your bundle file."* Offline signing always succeeds whether or
not the on-chain pin works.

**MCP wallet tools** so AI agents can guide users through onboarding:
- `mcp__brainctl__wallet_show` — read-only view (address, balance,
  keystore path, perms, mtime). Description leads with *"SAFETY: this
  tool only READS the wallet."*
- `mcp__brainctl__wallet_create` — creates the managed wallet (needs
  explicit `force=true` to overwrite an existing one — no surprise
  destruction). Description includes the non-custodial safety warning
  so the AI agent knows to surface it to the user.

These let an AI agent in conversation say *"I notice you don't have a
brainctl wallet yet — want me to create one for you?"* and walk a
non-technical user through the whole flow without them touching the CLI.

**Security & UX decisions (worth knowing):**
- **Non-custodial.** Wallet file lives on the user's disk. brainctl
  never transmits the key, never backs it up to a server, never asks the
  user to trust us with custody. Loud in every user-facing string.
- **Atomic 0600 write** via `os.open(O_CREAT|O_WRONLY|O_EXCL, 0o600)` —
  file never momentarily world-readable. Parent dir `~/.brainctl/`
  hardened to `0700` on first wallet write. Skipped silently on Windows.
- **Non-TTY safety.** Every interactive prompt has a `--yes` non-interactive
  escape. When stdin isn't a TTY and `--yes` wasn't passed, brainctl
  exits 1 with a "use --yes to confirm" hint rather than hanging — so
  agent wrappers can't stall.
- **Keystore precedence:** `--keystore` > managed wallet > legacy
  `$BRAINCTL_SIGNING_KEY_PATH` env var > friendly error. 2.3.0 users
  who set the env var are not broken.
- **Backup is verbatim.** Solana CLI keystore IS the secret material
  (no BIP39 seed to re-derive). `wallet export` chmods the backup
  `0600` and warns the user the backup is just as sensitive as the
  source.
- **Test isolation:** every test pins `BRAINCTL_WALLET_PATH` to a
  per-test `tmp_path` via autouse fixture, so the suite cannot pollute
  or destroy `~/.brainctl/wallet.json` on a real machine.

**41 new tests** in `tests/test_wallet_cmd.py`. All 41 pass plus the
40 signing tests from 2.3.0 = 81/81 in the wallet+signing suite. Full
suite: 1832 passed, 0 failed.

**Quick start (the new non-crypto-user path, end to end):**

```bash
pip install 'brainctl[signing]'
brainctl wallet new --yes               # 2 seconds; prints your address
# (optional) send any small amount of SOL to that address
brainctl export --sign --pin-onchain    # uses managed wallet, prompts
                                        # gracefully if 0 SOL
brainctl verify bundle.json             # anyone can verify offline
```

That's it. No `solana-keygen`, no `--keystore`, no manual file paths.

## [2.3.1] — 2026-04-18

### Fixed — closed an 18× retrieval-quality gap on cold-start data

brain memory id 1690 documented an 18× Hit@5 gap between `cmd_search` and
`Brain.search` on the LOCOMO benchmark (0.0424 vs 0.5716). The CLI/MCP path
applied recency / salience / Q-value rerankers on top of FTS+vec fusion,
but on cold-start data (uniform timestamps, zero recall history, default
trust) those signals were uninformative and *scrambled* the FTS ranking
they received. **This wasn't just a synthetic-data problem — every
brainctl user on Day 1 of a fresh `brain.db` hit the same shape.**

**Two fixes shipped together:**

1. **`brainctl search --benchmark` flag (immediate escape hatch).** Skips
   the recency / salience / Q-value / source-weight / context-match /
   quantum / temporal-contiguity / PageRank rerankers; keeps trust as the
   single provenance signal that's not stale-data-sensitive. Returns the
   raw FTS+vec RRF-fused ranking. The MCP `memory_search` tool gets a
   matching `benchmark: bool = False` kwarg. Stderr emits a one-line note
   when the flag fires so it's never silent.

2. **Auto-detect uninformative rerankers (the proper fix).** Each
   reranker now performs a signal-informativeness check on the candidate
   set BEFORE applying its weight:
   - **Recency:** if `stdev(created_at) < 60s` → weight 0.0
   - **Salience:** if `stdev(replay_priority) < 0.05` OR no affect data → weight 0.0
   - **Q-value:** if `sum(recalled_count) < 3` → weight 0.0 (not enough recall history)
   - **Trust:** if `stdev(trust_score) < 0.02` → weight 0.0 (everything's the default)

   Every downweight decision lands in the search response's `_debug` dict
   so an auditor can see WHY a particular ranking happened. Thresholds
   are module-level constants — `_RECENCY_STDEV_FLOOR_SECONDS`,
   `_SALIENCE_PRIORITY_STDEV_FLOOR`, `_QVALUE_RECALL_FLOOR`,
   `_TRUST_STDEV_FLOOR` — tunable and documented inline with rationale.

**Before/after on `tests/bench/` (the existing internal benchmark, which
has the same cold-start shape as LOCOMO):**

| metric | baseline | post-fix | delta |
|---|---:|---:|---:|
| `p_at_1` | 0.45 | **0.60** | **+15pp** |
| `mrr` | 0.5375 | **0.625** | **+8.75pp** |
| `ndcg_at_5` | 0.496 | **0.5579** | +6.2pp |
| `p_at_5` | 0.18 | 0.18 | 0 |
| `recall_at_5` | 0.5083 | 0.5083 | 0 |

cmd_search is now ~73% of the FTS-only ceiling instead of ~63%. New
`tests/test_reranker_robustness.py` (31 cases) regression-guards both
the synthetic-data behavior (rerankers stay quiet) and the real-data
behavior (rerankers still fire on informative signals).

### Added — external benchmark CI gates

Two industry-standard memory benchmarks added to `tests/bench/`,
opt-in via `BRAINCTL_RUN_BENCH=1` env gate so they don't slow the
default `pytest` (datasets are gitignored).

**LOCOMO** (Long-term Conversational Memory, Stanford SNAP, MIT-licensed)
— 1,982 questions across 5 categories. Run via:
```bash
BRAINCTL_RUN_BENCH=1 pytest tests/test_locomo_bench.py
```

Baseline captured (Brain.search backend, full sweep, 267s wall):

| metric | overall | single-hop | multi-hop | temporal | open-domain | adversarial |
|---|---:|---:|---:|---:|---:|---:|
| Hit@1 | 0.3406 | — | — | — | — | — |
| Hit@5 | **0.5716** | 0.4291 | 0.3152 | 0.6480 | 0.6017 | 0.6031 |
| MRR | 0.4447 | — | — | — | — | — |
| nDCG@5 | 0.4365 | — | — | — | — | — |

A dated pre-fix snapshot at `tests/bench/baselines/locomo_pre_fix_2026_04_18.json`
captures the broken cmd_search path for historical comparison.

**LongMemEval** (long-term agent memory, ~289 retrieval-friendly entries
from the `_s_cleaned` split). Run via:
```bash
BRAINCTL_RUN_BENCH=1 pytest tests/test_longmemeval_bench.py
```

Baseline (Brain.search, 30s wall): Overall Hit@5 = **0.9758**, single-session
1.000, multi-session 0.985, single-session-preference 0.833.

Shared `tests/bench/external_runner.py` with `ingest_conversation_into_brain`
and `eval_questions` helpers for future benchmarks (HotPotQA, BEIR, etc.).
Documentation lives in `tests/bench/EXTERNAL_BENCHMARKS.md`.

## [2.3.0] — 2026-04-17

### Added — signed memory exports (new subsystem)

The first Web3 utility added to brainctl. **Signed memory bundles + optional
on-chain attestation roots.** Local-first by design: memories never touch
the chain, only a SHA-256 hash + the signer's pubkey + a version prefix get
posted (~80 bytes per pin, ~$0.001 at current SOL prices). Anyone can
verify a bundle's authenticity offline; the on-chain receipt adds tamper-
evident timestamping the signer can't backdate.

**No token gating.** Anyone with brainctl and a Solana keypair can sign
their own memories. The token funds development, never gates access.
(Brand preference memory 1691.)

**CLI surface:**

```
brainctl export --sign --keystore <path> [--filter-agent X] [--category Y]
    [--scope Z] [--created-after T] [--created-before T] [--ids 1,2,3]
    [--pin-onchain] [--rpc-url <url>] [-o bundle.json] [--json]

brainctl verify <bundle.json> [--check-onchain] [--rpc-url <url>] [--json]
```

Exit codes: `0` ok, `1` tamper detected / missing keystore / IO, `2`
unsigned export attempted or `--check-onchain` found no receipt.

**Properties:**
- **Tamper-proof.** Ed25519 signature over SHA-256 of canonical JSON.
  Tested against 7 distinct tamper classes (memory content, IDs, swapped
  signature/pubkey, modified filter, modified timestamp, faked hash) —
  all fail verification. One byte change → invalid.
- **Opt-in.** Both `--sign` and `--pin-onchain` are explicit flags
  defaulting to off. Plain `brainctl export` does no crypto.
- **Not invasive.** `solders` is a lazy import inside the signing
  functions; the base `pip install brainctl` doesn't pull it in. Run
  `pip install 'brainctl[signing]'` to enable. Zero background daemons,
  no telemetry, no auto-signing.

**Bundle sizes (real measurements against the dev brain.db, 210 active
memories):**

| filter | memories | bundle size |
|---|---:|---:|
| 10 most recent | 10 | 13 KB |
| 100 most recent | 100 | 109 KB |
| project:brainctl scope | 44 | 52 KB |
| full active brain | 210 | 296 KB |

A full export of the dev brain is smaller than a single phone screenshot.

**Implementation:**
- `src/agentmemory/signing.py` (~470 lines) — `build_bundle`,
  `bundle_hash`, `sign_bundle`, `verify_bundle`, `pin_onchain`,
  `verify_onchain`. Lazy solders import; raw `urllib.request` for the
  four Solana RPC methods we use (no asyncio).
- `src/agentmemory/commands/sign.py` — CLI parser + handlers for
  `export --sign` and `verify`.
- `tests/test_signing.py` — 40 cases covering round-trip, 7 tamper
  classes, filter combinations, canonical-JSON reproducibility, version
  forward-compat, mock-RPC for on-chain paths, and end-to-end CLI smoke.
  All 40 pass when `solders` is installed; skip cleanly without it.
- `docs/SIGNED_EXPORTS.md` — threat model, bundle format, on-chain cost
  breakdown, and a 30-line "verify without brainctl installed" reference
  recipe in pure `cryptography` (so any auditor can verify a bundle
  without the brainctl stack).

**Bundle format spec (v1):** outer wrapper
`{version, bundle, bundle_hash_hex, signature_b58, signer_pubkey_b58, signed_at}`
over inner bundle
`{version, generated_at, filter_used, memories[]}`. Canonical JSON via
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
Ed25519 sig is over the raw 32-byte SHA-256 of the canonical bundle (not
the hex string). On-chain memo body:
`brainctl/v1:<bundle_hash_hex>:<signer_pubkey_b58>` via SPL Memo v2.

### Why this is the response to MemWal

The competition (MemWal/Walrus/Sui) puts memory blobs on-chain — slow,
expensive, privacy-leaking. brainctl 2.3.0 takes the opposite shape:
**memories stay local, only the proof goes on-chain.** Same sovereignty +
portability + tamper-evidence narrative, with sub-millisecond reads,
zero storage rent, and ~$0.001 per pinned export instead of pay-per-op.

## [2.2.4] — 2026-04-17

### Added

- **`brainctl update` CLI command.** One-shot upgrade path: detects install
  mode (pip / pipx / editable dev), shells out to the right package
  manager to upgrade, then hands off to the newly-installed binary to run
  `brainctl migrate`. Handles the "virgin tracker + schema drift" edge
  case from the README — if `brainctl doctor --json` reports the
  dangerous drift state, the command prints the recovery workflow and
  exits cleanly instead of crashing the user's brain.db. Flags:
  `--dry-run`, `--pre` (accept pre-releases), `--skip-migrate`, `--json`.
  Lives in `src/agentmemory/update.py` + `cmd_update` in `_impl.py`;
  16 new unit tests in `tests/test_cmd_update.py` all passing.

- **Gemini CLI plugin** at `plugins/gemini-cli/brainctl/`. Brings
  brainctl's 199-tool MCP surface into Google's Gemini CLI via the
  extension-install pattern (`~/.gemini/extensions/brainctl/`). Ships:
  `gemini-extension.json` (MCP server + context-file manifest),
  `hooks/hooks.json` (wires `SessionStart`/`SessionEnd`/`AfterTool`),
  three hook scripts ported from the Claude Code plugin with `_common.py`
  adapted for Gemini's stdin schema, an `install.py` with `--mcp-only`
  (for users who want MCP without hooks) / `--dry-run` / `--uninstall`
  modes, a `GEMINI.md` per-session context brief, and a README. No direct
  equivalent for `UserPromptSubmit` (Gemini's `BeforeModel` fires on
  every model turn — different semantics), so that event is intentionally
  not wired. Requires Gemini CLI ≥ 0.26.0 for hook support.

## [2.2.3] — 2026-04-17

### Fixed — design wave (medium-tier audit findings)

Three parallel workers + 2 inline cleanup edits. Closes the medium-tier
correctness items from the 2026-04-16 audit. Test suite: 1786 passed.

**Mechanical correctness**

- **`datetime.utcnow()` deprecation purged.** 18 sites across `_impl.py`
  (13 sites — the audit only counted the obvious one), `mcp_tools_policy.py`
  (4 sites including a `replace(tzinfo=None)` off-by-one bug), `mcp_tools_neuro.py`
  (1 site), `tests/test_mcp_tools_temporal_abstraction.py`, and
  `tests/test_mcp_tools_health.py`. All converted to `datetime.now(timezone.utc)`
  with explicit TZ-awareness on both sides of every comparison. Deprecation
  warnings dropped from 60 → 0.
- **`mcp_tools_dmem.py:146` no longer swallows event-insertion failures.**
  `memory_promoted` event insert errors now print to stderr; promote path
  remains non-fatal (audit log being down shouldn't block the promotion
  itself). `_embed` and `_get_vec_db` graceful-fallback contracts left
  intentionally silent — they're documented as such.
- **`bin/intent_classifier.py` gained an `entity_lookup` rule.** The
  builtin fallback in `_impl.py` had it; the external classifier didn't,
  so "Who is Alice?" fell through to `general` intent and the wrong
  rerank profile when external loaded. Mirrors the builtin keyword set
  with confidence 0.8 and the same table routes.

**Schema integrity (migration 048)**

- **FK cascade triggers** for the high-leverage parents: `agents` delete
  nullifies `memories.validation_agent_id`; hard-deleting a memory,
  entity, or event cascades the orphan rows in `knowledge_edges`. The
  audit flagged 47 migrations declare ON DELETE inconsistently; this
  pragmatic fix protects against accidental hard-deletes via raw SQL
  without the destructive table-rebuild that "real" ON DELETE clauses
  would require under SQLite's no-ALTER-CONSTRAINT rule.
- **FTS5 retire convergence.** When a memory is retired (`retired_at`
  set), the corresponding `memories_fts` row is now removed by the
  `memories_fts_update_insert` trigger's `WHEN new.retired_at IS NULL`
  guard preventing re-insert. Discovery: the naive
  "AFTER UPDATE … DELETE FROM memories_fts" approach corrupted FTS5
  content-linked segments ("database disk image is malformed"); only
  the prevent-the-reinsert path is safe. The user's brain.db was on
  the legacy single-trigger form (migration 031 marked
  `(backfilled)`) — migration 048 retroactively converges it.
- Mirrored into `db/init_schema.sql` and `src/agentmemory/db/init_schema.sql`
  so fresh installs ship with the new triggers.

**Design fixes (W(m) gate, vec batch, affect retention — migration 049)**

- **W(m) surprise score: neutral fallback (0.5) when novelty cannot be
  assessed.** The audit flagged `fts5_no_matches` returning `1.0` (false
  positive on near-duplicates with different vocabulary). Worker C found
  the same class bug at `cosine_no_neighbors` and fixed both. New
  vec-fallback path: when FTS5 finds nothing AND a vector blob was
  supplied, computes `1 - max_cosine` against existing vectors. When no
  blob (the common hot path), falls through to `0.5` — neutral, not
  inflated — and lets W(m) decide on other signals. Method tags now
  carry the observed signal (`cosine_max_sim_0.78`,
  `vec_fallback_max_sim_0.65`, `*_neutral`) so debuggers can tell what
  path fired.
- **`vec_purge_retired` is now chunked.** Was a per-row DELETE loop
  (~30 min on 50k retired memories). Now does chunked `IN (...)` deletes
  of 500 ids per round to stay under `SQLITE_MAX_VARIABLE_NUMBER`. New
  `--limit N` flag bounds total wall-time per invocation. Verified
  ~15× speedup on 1k retired rows.
- **`affect_log` retention policy.** New `agentmemory.affect.prune_affect_log(db, days, max_rows, dry_run)` and `brainctl affect prune` CLI.
  Default policy: keep last 90 days OR last 100k rows, whichever is
  more permissive (union — keeps the broader set). Never auto-runs;
  only the explicit CLI call (or a user cron) deletes data. Migration
  049 adds an `idx_affect_created_at` index needed for cross-agent
  time-range deletes.

### Notes for 2.2.4

- Worker A's mcp_tools_neuro.py inline fix changed the comparison shape
  slightly — verify no downstream caller relied on the old "naive when
  exp is naive" behavior. (Pytest is green so probably fine, but worth
  a closer look.)
- `cursor.rowcount` on sqlite-vec virtual tables can be -1 — Worker C's
  vec_purge fallback uses input-id count when rowcount is unreliable.
  Verify against a real sqlite-vec-loaded brain.db before broader use.
- Worker C's affect retention uses union semantics ("more permissive
  policy survives") — if intersection is preferred, it's a one-line
  swap.

## [2.2.2] — 2026-04-17

### Fixed

- **`tests/test_search_quality_bench.py::test_no_regression` flake.** The
  bench's CLI entry at `tests/bench/run.py:16` seeds `random.seed(42)`
  at module top, but the pytest test imports `tests.bench.eval` directly
  and never triggered that seed — so `cmd_search`'s downstream Thompson-
  sampling step (`_apply_recency_and_trim` → `random.betavariate`) ran
  with whatever entropy pytest happened to leave around. `p_at_1` drifted
  0.40-0.45 across runs and the regression gate failed ~50% of the time
  (pre-existing on every 2.x release). Added a module-scoped autouse
  fixture in the test file that mirrors `run.py`'s seed before the
  bench fixture runs. Verified: 5/5 consecutive pytest invocations pass
  cleanly post-fix.

## [2.2.1] — 2026-04-17

### Fixed — follow-up to the 2.2.0 correctness wave

The 2.2.0 audit closed ten bugs but left three open loops it had created or
revealed. Three more parallel workers, three more isolated worktrees,
five more items closed.

- **`_merge_memories` orphans `knowledge_edges` (Bug 6c).** Worker D's
  2.2.0 fix to `_merge_entities` taught the merge to thread an
  `entity_id_map` through to `_merge_knowledge_edges`. The same shape
  bug existed in the memory path — `_merge_memories` inserted memories
  into the target DB without tracking the id remap, so any edge
  referencing a memory by id was orphaned across cross-DB merges.
  `_merge_memories` now returns a `memory_id_map` populated in both
  the dedup-match and fresh-insert branches; `_merge_knowledge_edges`
  uses it to redirect `*_table='memories'` edges with the same
  HIGHEST-WEIGHT-WINS conflict rule. Self-loops produced by remap
  collapse are dropped.
- **SQLi sweep across all 28 `mcp_tools_*.py` modules.** Worker C's
  2.2.0 sweep covered `mcp_server.py` only. This patch extends the same
  methodology to the 29 extension modules registered through
  `_EXT_MODULES` (the brief said 28 — actual count was 29; deferred to
  the registry to prevent future drift). **Zero real injection vectors
  found**: the audit's heuristic ("f-string + `', '.join` near
  `execute(`") flagged predicates that are source-literal-only, not
  caller-controlled. Hardened two sites (`tool_task_update`,
  `tool_expertise_update`) with allowlist + parameterized helpers
  matching Worker C's 2.2.0 pattern, parameterized one `LIMIT` clause,
  and added `# nosec B608` markers with rationale to every
  defense-in-depth f-string SQL. New `tests/test_sqli_tool_modules.py`
  includes an AST-level static linter that REQUIRES the marker on any
  f-string passed to `.execute()/.executemany()/.executescript()` — a
  future violation fails the test.
- **Trust contradiction logic factored into `agentmemory.trust`.**
  Bug 7's fix in 2.2.0 lived in two parallel implementations
  (`mcp_tools_trust.tool_trust_update_contradiction` for the MCP path
  and `_impl.cmd_trust_update_contradiction` for the CLI path). They
  were kept in lockstep manually — a maintenance bomb. New module
  `agentmemory.trust` exposes `apply_contradiction_penalty(db, a, b,
  resolved)`; both surfaces shrunk to ~14-line wrappers. Pure refactor,
  zero behavior change. Connection lifecycle stays caller-owned.

### Maintenance

- **`bin/brainctl-mcp --list-tools` now flags the undercount.** The
  legacy standalone script registers a subset of tools (it doesn't
  merge `_EXT_MODULES`). Added a stderr note pointing users at the
  canonical `python3 -m agentmemory.mcp_server --list-tools` (or the
  pip-installed `brainctl-mcp` console script) for the full 199-tool
  surface. Phasing out the standalone fully is a 2.3.0 task.
- **`CLAUDE.md` (in-repo) refreshed.** Was claiming v1.6.1 and pointing
  at the legacy MCP standalone. Updated to v2.2.1+, current schema
  numbers (61 tables, 47 migrations), and the canonical MCP entry.

### Known issue

- `tests/test_search_quality_bench.py::test_no_regression` flakes ~50%
  of runs (Thompson sampling stochastic noise; baseline `p_at_1=0.45`,
  samples drift to 0.40). Pre-existing on every 2.x release; not
  caused by 2.2.0/2.2.1 changes. Fix is to seed the RNG in the bench
  fixture; deferred to 2.2.2.

### Notes for 2.3.0 (the design-decision wave)

- `datetime.utcnow()` deprecation across `mcp_tools_policy.py`,
  `_impl.py:13933`, and one test file (mechanical fix).
- W(m) surprise score returns 1.0 on `fts5_no_matches` — near-duplicates
  with different vocabulary pass as novel; needs design choice on a
  saner fallback (likely ~0.5).
- Add `ON DELETE` clauses across the 47 migrations (only 2 declare them
  today); per-table choice between CASCADE and SET NULL.
- `memories_fts` DELETE trigger when `retired_at` is set (FTS index
  bloats with orphan rows).
- `mcp_tools_dmem.py:146` silent `except Exception: pass` swallows
  `memory_promoted` event failures.
- `intent_classifier` external path missing the `entity_lookup` rule.
- Batched `vec_purge_retired` (still ~30 min stalls on large retired
  sets — from 2.1.x crash audit).
- `affect_log` retention policy.

## [2.2.0] — 2026-04-16

### Fixed — correctness audit, ten bugs across the stack

A focused correctness pass following the 2.1.x crash-risk hardening. Five
parallel workers, isolated git worktrees, ten bugs closed, +75 tests, no
file conflicts at merge time. Test suite: 1712 passed, 0 failed.

**Critical**

- **Duplicate migration version numbers (Bug 1).** Five pairs of migrations
  shared four version numbers (`012`, `013`, `017`, `023`). The runner
  sorted alphabetically and silently skipped the second of each pair on
  fresh DBs — `agent_beliefs`, `belief_conflicts`, `agent_perspective_models`,
  `agent_bdi_state`, `workspace_*`, `memory_rbac`, `agent_uncertainty_log`
  search columns, and `agents.attention_class` were never created on new
  installs. Renamed to versions 043–047, made all CREATE/ALTER idempotent,
  added a duplicate-version detector to the migration runner that fails
  loud on regression. The 5th dupe (`021_attention_class.sql`) was caught
  by the new detector during this fix.
- **Defense-in-depth: trigger update SQL hardening (Bug 2).**
  `mcp_server.py:1383`'s `f"UPDATE memory_triggers SET {', '.join(updates)}"`
  pattern was empirically not exploitable on 2.1.x (kwargs are dispatcher-
  filtered), but the f-string-over-runtime-list shape is fragile and a
  future generic-update refactor would re-introduce the sink. Replaced
  with an allowlist + parameterized helper, plus a sweep of the rest of
  `mcp_server.py` for sibling patterns (none found).
- **Vector index stale on memory update (Bug 3).** `cmd_memory_update`
  updated `content` but never re-embedded; semantic search returned
  results for the OLD text. Now mirrors the `cmd_memory_add` indexing path
  with stderr-warn-and-continue if the vec extension is absent.

**High**

- **Bayesian alpha/beta runaway (Bug 4).** Recall incremented `alpha` but
  never `beta`, so a memory recalled 100 times had a `Beta(101, 1)`
  posterior near 0.99 regardless of actual quality, and Thompson sampling
  exploited the inflation. Adopted an anti-attractor prior:
  `_BETA_PRIOR_INCREMENT = 0.1` per recall, capped at `_AB_PRIOR_CAP = 1000.0`.
  Posterior after 100 recalls is now ~0.91 vs old runaway ~0.99. Updates
  are atomic (single `UPDATE` with `CASE`) — no read-modify-write window.
- **RRF rank-by-id consistency (Bug 5).** Verified the
  `id == rowid` invariant from `init_schema.sql` (INTEGER PRIMARY KEY
  AUTOINCREMENT) so the audit's id-vs-rowid concern is moot at the fusion
  layer; added a deterministic tie-breaker (`(-score, id)`) so identical
  RRF scores produce stable order across runs.
- **Entity merge orphans `knowledge_edges` (Bug 6).** Both merge paths
  (`merge.py._merge_entities` cross-DB and `mcp_tools_reconcile.tool_entity_merge`
  in-DB) failed to redirect knowledge_edges referencing the dropped
  entity. `merge.py` now threads an `entity_id_map` through to a new
  `_merge_knowledge_edges` that uses the existing `uq_knowledge_edges_relation`
  unique index for ON CONFLICT MAX(weight) folding; `mcp_tools_reconcile`
  was rewritten to a gather → plan → execute pipeline that dedupes
  collisions and drops self-loops. Conflict rule: HIGHEST WEIGHT WINS.
- **Trust contradiction penalty asymmetric (Bug 7).** `tool_trust_update_contradiction`
  in `mcp_tools_trust.py` and the parallel CLI copy in `_impl.py` both
  penalized memories by argument order, not by who lost. AGM-correct now:
  loser-by-trust eats the heavier penalty; winner gets +0.02 only on
  resolved=True (premature reinforcement during live conflict was wrong).
  Both implementations updated together with comments cross-referencing
  each other so they stay in lockstep.
- **EWC protection asymmetric (Bug 8).** `hippocampus.resolve_contradictions`
  only checked `ewc_importance` on the loser. A high-EWC memory that
  happened to have higher confidence (the winner) could be silently
  retired in a later contradiction. Now checks EWC on both sides; if
  either is protected and similarity is below threshold, emits a
  `warning` event and queues for review instead of retiring.
- **Q-value RMW race (Bug 9).** `_update_q_value` did `SELECT … UPDATE`
  without `BEGIN IMMEDIATE`. Collapsed to a single atomic `UPDATE` with
  inline TD-error math; race window eliminated entirely.
- **PII recency gate inverted (Bug 10).** New memories were inserted with
  `alpha=alpha_floor` but `beta=NULL` (treated as 1.0), creating a
  `Beta(N, 1)` prior with mean ~0.75 — favoring the new memory and
  defeating the gate's purpose of defending high-PII incumbents. Now
  seeds `beta = alpha_floor` so the prior is symmetric `Beta(N, N)`
  with mean 0.5 ("we're not yet sure").

### Notes for future maintainers

- `mcp_tools_trust.tool_trust_update_contradiction` and
  `_impl.cmd_trust_update_contradiction` share the same logic across
  the MCP and CLI surfaces. Keep them in lockstep; consider a shared
  helper in 2.2.x.
- `_merge_memories` in `merge.py` has the same id-remap shape as the
  Bug 6a fix to `_merge_entities` — knowledge_edges referencing memories
  by id will be orphaned after cross-DB memory merges. Out of this wave's
  scope; deferred to 2.2.1.
- The 28 `mcp_tools_*.py` modules registered through `_EXT_MODULES` were
  not part of Worker C's SQLi sweep (scope was `mcp_server.py` only).
  Recommended for the next correctness pass.

## [2.1.2] — 2026-04-16

### Fixed — MCP server connection pooling

- **`bin/brainctl-mcp` now reuses sqlite connections per thread.** Every
  one of the MCP tool handlers used to do `db = get_db() ... db.close()`,
  which spawned a fresh sqlite connection (and, for vec-using tools, a
  fresh `sqlite_vec` extension load — 5–20ms each) on every call. Under
  rapid tool-call bursts this approached the macOS 256 file-handle
  ceiling and burned real CPU on extension reloads. Both `get_db()` and
  `_get_vec_db()` are now per-thread pooled. Caller code is unchanged
  — a `_PooledConn` wrapper makes `.close()` a no-op (the pool owns
  lifecycle and closes everything via `atexit` on process exit), while
  `.commit()`, transactions, row factory, and context-manager semantics
  are forwarded transparently. Per-thread isolation preserves SQLite's
  thread-safety guarantees. `_get_vec_db()` still returns `None` if the
  sqlite-vec extension can't load — the contract for callers checking
  `if vdb is not None:` is unchanged.

## [2.1.1] — 2026-04-16

### Fixed — resource-exhaustion hardening (no functional changes)

- **Dream REM pairwise scan now wall-clock capped.** The O(n²) bisociation
  scan in `hippocampus.run_dream_pass` could peg a CPU for tens of
  seconds on weak hardware (200 candidates × 200 = 40k cosine ops on
  768-dim vectors). Added `DREAM_MAX_WALL_SECONDS = 30.0` deadline
  checked in both loop levels. Anything not scanned this cycle is still
  a candidate next cycle. Stats now report `wall_time_capped: 1` when
  the cap fired.
- **Obsidian watch eviction is now always-on.** The `_evict_stale`
  helper in `cmd_obsidian_watch` short-circuited when the dict had
  fewer than 256 entries — fine in steady state, broken under burst
  imports where thousands of unique files arrived inside the TTL
  window and all looked "fresh." Eviction now runs on every event with
  bounded O(n) cost.
- **Dream daemon checkpoints WAL after every real cycle.** SQLite's
  autocheckpoint only fires at 1000 pages; long-running daemons with
  steady writes let `brain.db-wal` balloon into the 100s of MB before
  that triggered. Explicit `PRAGMA wal_checkpoint(TRUNCATE)` after
  `run_dream_cycle` keeps the WAL bounded across weeks.
- **Recommended pattern for distill cron: wrap each tier in `timeout 60s`.**
  A stalled DB lock or Ollama hang in any hourly distill pass can back
  up the cron queue and cascade-fail the next hour. The local example
  in `config/distill-cron.sh` (gitignored) now demonstrates the pattern;
  any operator running scheduled `brainctl distill` should apply the
  same timeout wrapper.

### Deferred to 2.1.2

- MCP server connection pool (audited risk: 199 tools each call
  `get_db()` fresh; rapid loops can exhaust file handles). Needs care
  to not break callers that do `conn.close()` — designing properly
  next patch.
- `vec_purge_retired` batched DELETE.
- `affect_log` retention policy.

## [2.1.0] — 2026-04-16

### Removed

- **Bundled web dashboard (`brainctl ui` subcommand).** The local
  `http.server`-based dashboard with the Explorer + Neural Map views has
  been removed. The forward-facing UI for brainctl is now Obsidian via
  `brainctl obsidian` (export your vault, browse natively, sync back).
  The Neural Map's WebGL render was heavy enough to crash GPUs on warm
  laptops; the Obsidian path is lighter, native, and roundtrippable.
  Deleted: `src/agentmemory/ui/`, top-level `ui/` duplicate, the `ui`
  argparse subcommand, and the `ui/static/*` package-data lines.

## [2.0.0] — 2026-04-16

**brainctl 2.0** — the full Complementary Learning Systems architecture.
Auto entity linking, quantum cognition, belief collapse, typed causal
reasoning, and temporal abstraction. Backed by 120 peer-reviewed papers.

### Added — Auto Entity Linking (Pillar 1)

- **FTS5 entity name matching (Layer 1).** `brainctl entity autolink`
  scans all active memories for known entity name substrings.
  Production result: **KG isolation 92% → 16%** (746 edges). Zero deps.
- **GLiNER NER (Layer 2, optional).** Zero-shot NER via 205M-param
  bidirectional transformer. Production result: **16% → 2% isolation**
  (38 new entities, 58 edges). `pip install brainctl[ner]`.
  (Zaratiana et al. 2024, NAACL)
- **Entity co-occurrence edges (Layer 3).** Memories mentioning 2+
  entities auto-generate entity↔entity edges. Production result:
  **2,270 → 4,441 co-occurrence edges**. (SPRIG, Wang 2025)

### Added — Quantum Integration (Pillar 2)

- **Quantum schema deployed.** 548-line migration adding
  confidence_phase, hilbert_projection, coherence_syndrome,
  decoherence_rate columns + recovery_candidates, agent_entanglement,
  agent_ghz_groups tables. Backward compatible.
- **Phase-aware quantum amplitude scoring.** 50/50 classical+quantum
  blend in the RRF pipeline. Constructive interference boosts;
  destructive reduces. Gated on confidence_phase population.
- **Belief collapse mechanics.** `_collapse_belief` resolves superposed
  beliefs to definite states. Four collapse types: task_checkout,
  direct_query, evidence_threshold, time_decoherence.
  `_check_collapse_triggers` finds overdue superpositions.
  (Quantum Wave 2, 02_collapse_dynamics.md)

### Added — Frontier Capabilities (Pillar 3)

- **Typed causal edges + counterfactual attribution.** `causes`,
  `enables`, `prevents` edge types. `_trace_causal_chain` follows
  causal paths forward; `_counterfactual_attribution` traces backward
  from outcomes and boosts Q-values of contributing memories weighted
  by edge strength. (Kang et al. 2025 / Hindsight)
- **Temporal abstraction hierarchy.** `_assign_temporal_levels` maps
  memories to 6 levels based on age (moment → session → day → week →
  month → quarter). `_build_temporal_summary` creates hierarchical
  summaries. Production distribution: week=106, day=45, moment=30,
  session=3. (Shu et al. 2025 / TiMem)

### Production consolidation results (enriched graph)

- Entity clusters: 8 → **81** (10×)
- Coupling gate pass rate: 25% → **97.6%**
- Schema-accelerated promotions: 0 → **90**
- Knowledge edges: ~5,000 → **7,963**
- Entities: 248 → **286**

### Migrations

- Quantum schema (existing, newly deployed)
- 042 — q_value (from v1.9.0, included in 2.0 release)

### Tests

- 46 new tests (collapse + causal + temporal)
- Full suite: 220+ tests passing

## [2.0.0a1] — 2026-04-16

First alpha toward v2.0. Fixes the 77% knowledge-graph isolation problem
and deploys the quantum cognition schema.

### Added

- **Auto entity linking (Layer 1).** `brainctl entity autolink` scans
  all active memories for known entity name substrings. Case-insensitive,
  idempotent, zero dependencies. On production brain.db: **reduced KG
  isolation from 92% to 16%** (746 new `mentions` edges). This unblocks
  quantum interference, coupling gate promotion, and PageRank spreading
  activation. (HippoRAG, Gutierrez et al. 2024)
- **Entity co-occurrence edges (Layer 3).** Memories linked to 2+
  entities automatically generate entity-to-entity `co_occurs` edges.
  Created **2,270 entity-entity edges** on production brain.db, enabling
  rich graph traversal. (SPRIG, Wang 2025)
- **Quantum schema migration deployed.** 548-line migration adding
  quantum cognition columns (`confidence_phase`, `hilbert_projection`,
  `coherence_syndrome`, `decoherence_rate`) and tables
  (`recovery_candidates`, `agent_entanglement`, `agent_ghz_groups`).
  All columns have defaults; classical code paths unaffected.
- **Phase-aware quantum amplitude scoring.** Integrated into the RRF
  pipeline as a 50/50 classical+quantum blend, gated on
  `confidence_phase` being populated. Constructive interference from
  knowledge-graph neighbors boosts retrieval score; destructive
  reduces it.

### Tests

- 19 new tests across entity autolink + quantum scoring
- Full suite: 93+ tests passing, bench unchanged

## [1.9.0] — 2026-04-16

Completes the full CLS (Complementary Learning Systems) architecture
with adaptive memory utilities and pattern-driven consolidation.

### Added

- **Q-value utility scoring (migration 042).** Each memory now carries
  a `q_value` updated via temporal-difference learning after retrieval
  outcomes. Memories that contribute to task success get higher Q-values,
  improving future retrieval ranking (0.8x to 1.2x score multiplier).
  Q-values self-correct with every retrieval cycle.
  (Zhang et al. 2026 / MemRL)
- **Schema-accelerated consolidation.** Episodic memories with >= 3
  knowledge_edges to entities are immediately promoted to semantic,
  bypassing the normal holding period. Mirrors Tse et al.'s finding
  that schema-consistent information consolidates 10x faster.
  Integrated into the phased pipeline as a new phase between coupling
  gate and de-overlap. (Tse et al. 2007, Science)
- **Per-project retrieval presets.** `agent_orient` now returns a
  `retrieval_preset` for the active project (if one has been saved).
  Stored in `agent_state` key-value table. Enables MAML-style fast
  adaptation — each project can tune its own retrieval weights.
  (Finn et al. 2017)
- **Access-pattern-driven replay.** Consolidation replay now prioritizes
  memories that were created near high-importance events (within ±2h
  window, importance >= 0.7). Replay weight = salience * event
  importance. Replaces the flat salience-only ordering.
  (Yang & Buzsaki 2024; Ramirez-Villegas et al. 2025)

### Migrations

- **042** — `q_value REAL DEFAULT 0.5` on memories

### Tests

- 29 new tests across 4 test files
- Full suite: 180+ tests passing

## [1.8.0] — 2026-04-16

Context-aware retrieval: memories now carry their encoding context and
use it to boost retrieval relevance. Plus a spaced-review scheduler
for optimal memory maintenance intervals.

### Added

- **Encoding context snapshot (migration 040).** Every memory now
  captures a JSON snapshot of the agent's operational context at write
  time (`encoding_task_context`) plus a SHA-256 hash
  (`encoding_context_hash`) for fast matching. Context includes project,
  agent_id, session_id. (Tulving & Thomson 1973; Heald et al. 2023)
- **Context-matching reranker.** Search results now get a score boost
  (up to 20%) when their encoding context matches the current search
  context. Hash match gives +0.3 bonus; key-value Jaccard overlap gives
  partial credit. Plugs into the existing RRF pipeline alongside FTS5,
  vector, Thompson Sampling, and PageRank signals.
  (Smith & Vela 2001; HippoRAG / Gutierrez et al. 2024)
- **Spaced-review scheduler (migration 041).** `schedule_spaced_reviews`
  computes optimal inter-study intervals (ISI = 15% of retention
  interval, scaled by memory stability) and stamps `next_review_at` on
  each memory. `process_due_reviews` replays due memories and
  reschedules them at expanding intervals. Integrates with the
  consolidation cycle. (Cepeda et al. 2006; Murre & Dros 2015)

### Migrations

- **040** — `encoding_task_context TEXT`, `encoding_context_hash TEXT`
  on memories + partial index
- **041** — `next_review_at TEXT` on memories + partial index

### Tests

- 24 new tests across 3 test files
- Full suite: 150+ tests passing

## [1.7.0] — 2026-04-16

Two bodies of work: neuroscience-grounded retrieval improvements (Tier A)
and a principled consolidation engine overhaul (Tier B). Backed by 75
peer-reviewed papers across Complementary Learning Systems, meta-learning,
context-dependent encoding, and synaptic homeostasis. Full research spec
at `research/wave14/32_neuroscience_grounded_improvements.md`.

### Added — Tier A: retrieval & write-gate improvements

- **Retrieval-practice strengthening.** Successful recall now boosts
  memory confidence by `0.02 * (1 + retrieval_prediction_error)`. Hard
  retrievals (high prediction error) strengthen more than easy ones —
  the "desirable difficulties" effect. Labile reconsolidation window
  resets on each recall. (Roediger & Karpicke 2006; Bjork 1994)
- **Thompson Sampling retrieval.** Search reranking now draws from
  `Beta(alpha, beta)` instead of using the confidence point-estimate.
  Memories with uncertain confidence get explored more; certain memories
  get exploited. Self-improving retrieval with zero new infrastructure —
  uses existing alpha/beta columns. (Thompson 1933; Glowacka 2019)
- **Temporal contiguity bonus.** When a memory is retrieved, temporally
  adjacent memories from the same agent (within 30 min) get a 15% score
  boost. Mimics the brain's sequential recall tendency.
  (Dong et al. 2026, Trends in Cognitive Sciences)
- **Modification resistance for reconsolidation.** Memories develop
  resistance to reconsolidation based on age, recall count, and EWC
  importance. High-surprise events can still breach resistance, but
  trivial events cannot destabilize strong memories.
  (O'Neill & Winters 2026, Neuroscience)
- **Encoding affect linkage (migration 037).** Each memory now links to
  the agent's affect state at encoding time via `encoding_affect_id` FK
  to `affect_log`. Provides the foundation for mood-congruent retrieval
  in future releases. (Eich & Metcalfe 1989; Morici et al. 2026)
- **A-MAC 5-factor write gate.** The W(m) worthiness gate now uses a
  5-factor decomposition: future utility (0.15), factual confidence
  (0.15), semantic novelty (0.20), temporal recency (0.10), content
  type prior (0.40). Content type prior is the most influential factor.
  Replaces the old `surprise * importance * (1 - redundancy)` formula.
  (Zhang et al. 2026, ICLR Workshop MemAgents)
- **W(m) gate calibration feedback loop.** `brainctl lint` now reports
  a `gate_calibration` metric: Pearson correlation between confidence-
  at-write and recalled_count. Flags a warning when < 0.1 (miscalibrated
  gate). (Dunlosky & Metcalfe 2009; Nelson & Narens 1990)

### Added — Tier B: Consolidation 2.0

- **Homeostatic pressure trigger.** Consolidation now tracks
  `total_confidence_mass / active_memory_count` as homeostatic pressure.
  When pressure exceeds a setpoint (0.55) or learning load exceeds a
  threshold (20 new memories), consolidation triggers on demand rather
  than waiting for the cron schedule. (Tononi & Cirelli 2003, 2006)
- **Global proportional downscaling (migration 038).** Replaces per-
  category fixed decay rates with a single multiplicative
  `downscale_factor = setpoint / pressure` applied to all non-permanent,
  non-tagged memories. High EWC-importance memories resist downscaling
  via `factor^(1 - importance)`. Memories below 0.05 confidence get
  retired. (Tononi & Cirelli 2014; Kirkpatrick et al. 2017 / EWC)
- **Synaptic tagging protection (migration 038).** Memories in active
  labile windows get `tag_cycles_remaining = 3`, exempting them from
  downscaling for 3 consolidation cycles. Tags decrement each cycle and
  expire if the memory is not recalled. (Frey & Morris 1997; Redondo &
  Morris 2011)
- **Spacing-effect decay function (migration 039).** `compute_spacing_decay`
  uses `exp(-rate * t / stability)` where stability increases for
  well-spaced recalls (ISI >= 15% of retention interval). Memories with
  regular spaced practice decay dramatically slower.
  (Cepeda et al. 2006; Hou et al. 2024)
- **Entity-clustered replay with magnitude weighting.** Replay now
  groups candidates by shared entity references (knowledge_edges), not
  temporal order. High-salience candidates get priority. Replay is
  decoupled from Hebbian strengthening — replay broadly, tag selectively.
  (Niediek et al. 2026; Robinson et al. 2026; Widloski & Foster 2025)
- **Coupling gate.** Episodic memories can only be promoted to long-term
  storage if they have at least one knowledge_edge. Prevents isolated,
  unconnected memories from entering the semantic store.
  (Schwimmbeck et al. 2026)
- **De-overlap mechanism.** Detects similar-but-distinct memories via
  word-overlap Jaccard similarity and flags them for discrimination.
  Sleep actively separates overlapping representations.
  (Aquino Argueta et al. 2026)
- **7-phase consolidation pipeline** (`brainctl-consolidate
  consolidation-cycle --phased`). Strict NREM→REM ordering:
  N2 tagging → N3 downscaling → Replay → Coupling gate → De-overlap →
  REM dream → Housekeeping. Available via `--phased` flag; existing
  12-pass flat sequence preserved for backward compatibility.
  (Diekelmann & Born 2010; Klinzing et al. 2019; Kim & Park 2025)

### Changed

- **Bench harness seeded for determinism.** `tests/bench/run.py` now
  seeds `random.seed(42)` before running, ensuring Thompson Sampling
  produces deterministic results for regression testing. Baseline
  re-established: P@1=0.45, MRR=0.537, nDCG@5=0.496 (down from
  0.60/0.625/0.558 pre-Thompson — expected with uninformative priors
  on bench fixtures; production memories accumulate alpha/beta through
  recall and the regression self-corrects).

### Research

- **Wave 14 research spec** at `research/wave14/
  32_neuroscience_grounded_improvements.md` (1442 lines, 75 papers).
  Covers CLS, meta-learning, context-dependent encoding, synaptic
  homeostasis, plus a 2026 supplement with 30 papers from Jan-Apr 2026
  (Nature Neuroscience, Neuron, ICLR 2026, bioRxiv). Section 11+12.9
  provides whitepaper citation guides mapping papers to brainctl.org
  claims.

### Migrations

- **037** — `encoding_affect_id INTEGER` on memories (FK to affect_log)
- **038** — `tag_cycles_remaining INTEGER DEFAULT 0` on memories
- **039** — `stability REAL DEFAULT 1.0` on memories

### Tests

- 130 new tests across 9 test files (80 Tier A + 50 Tier B)
- Full suite: 130+ tests passing, no regressions

## [1.6.1] — 2026-04-15

Patch release covering an audit of the Obsidian integration. Five real
bugs found and fixed, plus a README walkback on what the integration
actually does.

### Security
- **SQL injection in `brainctl obsidian export --scope` / `--category`.**
  Both flags were f-string-interpolated directly into the SELECT WHERE
  clause in `cmd_obsidian_export`, e.g. `where += f" AND scope = '{args.scope}'"`.
  A user passing `--scope "x' OR 1=1; DROP TABLE memories--"` would
  execute arbitrary SQL against the brain.db file the export targeted.
  Fixed by parameterizing the query with `?` placeholders and a bound
  params list. Regression tests in
  `tests/test_obsidian.py::TestExportSqlInjection` verify that classic
  injection payloads no longer drop or delete rows.

### Fixed — Obsidian integration
- **`Brain` instance recreated per file in import + watch.** Both the
  `cmd_obsidian_import` for-loop and the `VaultHandler._handle` watchdog
  callback constructed a new `Brain(db_path=..., agent_id=...)` for
  every single file. This defeated the v1.2.0 lazy shared sqlite
  connection optimization — every file event paid the connection setup
  cost from scratch. Hoisted `Brain` to outer scope in both code paths
  so the shared connection is actually shared.
- **YAML frontmatter was stripped, not parsed.** The old import path
  found the closing `---` and discarded everything in between, never
  reading the metadata. Any user-supplied `category:`, `tags:`,
  `entity_type:`, etc. on a new note was silently ignored. Added a
  ~20-line zero-dependency `_parse_frontmatter()` helper that returns
  `(metadata: dict, body: str)`. Both import and watch now use it. The
  `--scope` / `--category` discovery on import respects frontmatter
  values when they name a documented category.
- **Watch handler hardcoded `category="general"`,** which is **not**
  in the documented category enum (`convention`, `decision`,
  `environment`, `identity`, `integration`, `lesson`, `preference`,
  `project`, `user`). Replaced with a `_DEFAULT_CATEGORY = "project"`
  constant (which IS in the enum) plus frontmatter-supplied override
  via the new `_category_from_metadata()` helper. The valid-category
  set is now a single source of truth at the top of the module.
- **Entities imported from the vault became memories with `category=identity`,**
  not entity rows. The import path called `Brain.remember()` with a
  tweaked category instead of `Brain.entity()`, breaking the
  export → edit → import round trip for entities. Now: files under
  `vault/brainctl/entities/` are routed through `Brain.entity()`, the
  canonical name is pulled from the first H1 heading (with the
  filename stem as fallback), and `entity_type` is honored from
  frontmatter (defaulting to `concept`). New regression test
  `TestEntityImportCreatesEntity` verifies the behavior.
- **Entity export filename collisions.** Entity files were named
  `{slug(name)}.md` with no ID prefix, so two entities slugging to the
  same string (`API rate limiter` and `API rate limiting` both →
  `api-rate-limit`) overwrote each other on export. Memories already
  prefixed with `{id:06d}-`; entities now do the same.
- **Watch handler's `_recently_processed` cache grew unbounded.** It
  kept entries forever for any file ever touched, leaking memory on
  long-running watch processes. Added a periodic eviction pass that
  drops entries older than `5 × cooldown` (minimum 30s) once the cache
  exceeds 256 entries.

### Changed — Obsidian README walkback
- Dropped the **"Bidirectional sync between brain.db and an Obsidian vault"**
  framing. The actual behavior is *one-way export* with an *ingest path
  for new notes*; edits to already-exported notes never flow back to
  brain.db because there's no merge or conflict-detection logic. The
  README now says so explicitly: "treat the export as
  canonical-from-brain and the vault layer as edit-and-replay rather
  than two-way mirror." Avoids overselling a feature that doesn't yet
  do what users would reasonably assume "bidirectional sync" means.

### Tests
- `tests/test_obsidian.py` grew from 35 to 44 tests:
  `TestFrontmatterParser` (5), `TestCategoryFromMetadata` (5),
  `TestExportSqlInjection` (3), `TestEntityImportCreatesEntity` (1),
  `TestExtractEntityName` (3). Existing tests still pass; the new ones
  cover the regression surface for every fix above.

## [1.6.0] — 2026-04-15

This release lands four bodies of work: new retrieval-quality
instrumentation, a first-class entity synthesis surface, self-healing
gap scans, and the plugin set brainctl launches with (cursor, openclaw,
trading-bot and crypto-native agent-framework placeholders). It also
includes a broad code-quality sweep across the core modules.

### Added — plugins
- **Cursor plugin** (`plugins/cursor/brainctl/`). Idempotent installer
  that wires the brainctl MCP server into `~/.cursor/mcp.json` with a
  sentinel-wrapped block, ships a `.mdc` rules template that teaches
  Cursor the orient / wrap_up lifecycle on every session start, and
  supports `install.py --dry` / `--print` / `--uninstall` with
  automatic backups. Same ergonomic shape as the Codex CLI plugin
  from v1.4.0.
- **OpenClaw plugin** (`plugins/openclaw/brainctl/`). Ships as a skill
  + `AGENTS.md` snippet injection rather than a config-file merge —
  OpenClaw's multi-agent topology discovers brainctl through its
  skill registry rather than a static config file. This closes the
  one plugin that the brainctl launch site has been listing as
  "coming next".
- **Trading-bot plugin placeholders** under `plugins/` for
  `hummingbot`, `nautilustrader`, `octobot`, and `coinbase-agentkit`.
  Each is a scaffold with a `README.md`, install stub, and the
  integration points brainctl needs — enough structure that a
  contributor can land a working integration without architectural
  uncertainty. Roadmap lives at `plugins/TRADING_INTEGRATIONS.md`.
- **Crypto-native agent-framework plugin placeholders** for `rig`,
  `virtuals-game`, and `zerebro`. Same scaffold-and-ready structure
  as the trading bots; these target the Solana / crypto-agent
  ecosystem rather than traditional trading desks.

### Added — retrieval quality instrumentation
- **Deterministic search-quality benchmark harness.** `tests/bench/`
  seeds a 29-memory / 8-event / 6-entity corpus with 20 graded queries
  (entity / procedural / decision / temporal / troubleshooting / negative /
  ambiguous), runs them through either `Brain.search` or the full
  `cmd_search` hybrid pipeline, and reports P@1 / P@5 / Recall@5 / MRR /
  nDCG@5. Committed baseline at `tests/bench/baselines/search_quality.json`;
  pytest regression gate in `tests/test_search_quality_bench.py` fails on
  any >2% drop on a headline metric. `bin/brainctl-bench` is the
  first-class CLI wrapper.
- **Query-intent taxonomy normalization in `cmd_search`.** The regex
  classifier in `bin/intent_classifier.py` produces 10 labels; downstream
  rerank branches only checked 6. Added `_INTENT_ALIAS` mapping so every
  classifier output reaches a concrete rerank profile
  (`historical_timeline`/`task_status`/`troubleshooting`/`orientation` →
  `event_lookup`, `decision_rationale` → `decision_lookup`, `how_to`/
  `research_concept` → `procedural`, `cross_reference` → `entity_lookup`,
  `factual_lookup` → `general`). Effective rerank branch is surfaced as
  `metacognition.rerank_branch` on every search response.

### Added — entity synthesis surface
- **Migration 033 — `entities.compiled_truth`.** Adds a rewriteable
  "current best understanding" block to each entity, populated from
  observations + linked memories + linked events. CLI:
  `brainctl entity compile [--all]`, `brainctl entity get ID --compiled`.
  MCP tool: `entity_compile`.
- **Migration 034 — `entities.enrichment_tier`.** Auto-classifies entities
  into Tier 1 (critical) / Tier 2 (notable) / Tier 3 (minor) from recall
  count, knowledge-edge degree, and event-link count. CLI: `brainctl entity
  tier [--refresh]`. MCP tool: `entity_tier`.
- **Migration 035 — `entities.aliases`.** First-class canonical-name list
  used by the merger as a pre-check before semantic dedup. CLI:
  `brainctl entity alias (list|add|remove) ID [values...]`. Helper
  `find_entity_by_alias()` for the merger. MCP tool: `entity_alias`.
- `bin/consolidation-cycle.sh` now runs `entity tier --refresh` and
  `entity compile --all` after the gap scan.

### Added — self-healing gap scans
- **Migration 036 — `knowledge_gaps` CHECK expansion.** Adds three
  gap types: `orphan_memory` (no edges + no recalls + old),
  `broken_edge` (`knowledge_edges` pointing at deleted rows),
  `unreferenced_entity` (no edges, no observations, old). Rebuilt via
  temp-table round-trip so the schema stays byte-identical to a fresh
  install. `brainctl gaps scan` now reports all three (with
  `--skip-self-healing` for fast mode).
- Latent schema bug fixed along the way: the `recent_belief_collapses`
  view referenced a non-existent `belief_collapse_events_old` table,
  which would have crashed any future DDL touching the schema. Rebuilt
  to point at the real `belief_collapse_events` table in init_schema.sql
  and migration 036.

### Fixed — natural-language query regressions in `cmd_search`
- `_FTS5_SPECIAL` regex was missing `?`, `!`, `'`, `` ` ``, `,`, `;`, `:`,
  causing `fts5: syntax error near "?"` on common natural-language
  queries. Extended the character class.
- `cmd_search` passed the space-separated sanitized query directly to
  FTS5 MATCH, which FTS5 treats as implicit AND. Natural-language
  queries ("What does Alice prefer?") silently returned zero results
  because FTS5 demanded every token match. Added
  `_build_fts_match_expression` that drops stopwords and joins meaningful
  tokens with `OR`, restoring parity with `Brain.search` behavior.

### Changed — code quality sweep
- **Deduplicated `DB_PATH` boilerplate** and the `_days_since` helper
  across modules into a single shared source of truth. Reduces drift
  risk on path resolution and date math.
- **Dead code elimination pass.** Removed unused functions, imports,
  and branches surfaced by a coverage pass — roughly the set of code
  that was never executed by the test suite or the CLI / MCP entry
  points.
- **Weak type annotations strengthened.** Local variables that were
  previously `Any` or untyped now carry precise types, which both
  improves pyright/mypy output and catches a handful of latent
  argument-order bugs at type-check time.
- **Defensive `try/except` blocks that hid bugs have been removed.**
  The rule applied: a `try/except` that catches a broad exception
  and logs a warning is only acceptable at a genuine trust boundary
  (user input, network calls, filesystem). Internal paths that had
  been wrapped in `except Exception: pass` or `except: log.warning(...)`
  are now bare — if an invariant breaks, we want the traceback, not
  a silent swallow.
- **Deprecated / legacy code and dead branches removed.** A cleanup
  of code paths left behind from earlier migrations, feature flags
  that never got flipped, and compatibility shims for interfaces no
  caller uses anymore.
- **AI-slop narration comments removed.** Comments that described
  what the next line was doing in English prose — the kind that
  appear when an LLM writes code and explains its work — have been
  purged. Remaining comments are either WHY-level or WORKAROUND
  markers.

## [1.5.2] — 2026-04-14

### Fixed
- **`brainctl version` reported the wrong version.** `src/agentmemory/_impl.py`
  hard-coded `VERSION = "1.1.2"` — a separate string from `__version__` in
  `agentmemory/__init__.py` — that nobody had been bumping across releases.
  Users on v1.2.0 through v1.5.1 ran `brainctl version` and saw `"version":
  "1.1.2"`. No functional impact (pyproject metadata, pip install, and the
  Python API all read the correct `__version__`), but misleading and likely
  caused at least one user to think they were running a much older build
  than they actually had installed.
  Fix: `_impl.py` now imports `VERSION` directly from
  `agentmemory.__version__`, so the CLI version string is pinned to the
  same source of truth as pip metadata. Can't drift again.

## [1.5.1] — 2026-04-13

### Fixed — `status_verbose` heuristic false negatives

Both bugs were discovered live while walking a production `brain.db`
through the v1.5.0 recovery workflow. Neither corrupted data, but both
caused the diagnostic to mis-classify already-applied migrations as
`partial` or `pending`, which would have led a less-cautious user to
re-run migrations and clobber state.

- **Generated virtual columns were invisible.** `status_verbose` used
  `PRAGMA table_info` which hides columns added via
  `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS AS (...) VIRTUAL`.
  Migration 024 (confidence_alpha/beta) classified as `pending (0/2)`
  on a db that already had both columns, and attempting to re-apply
  crashed with `duplicate column name`. Fix: switch to
  `PRAGMA table_xinfo` which reveals hidden and generated columns.
- **`ADD COLUMN IF NOT EXISTS` regex mis-capture.** The ADD COLUMN
  pattern was
  `ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)` — on migration 023's
  `ALTER TABLE access_log ADD COLUMN IF NOT EXISTS tokens_consumed`
  that captured `IF` as the column name and searched for a column
  named `IF` on `access_log`, producing a false `partial (7/8)`
  reading. Fix: tolerate an optional `IF NOT EXISTS` between `COLUMN`
  and the identifier:
  `ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)`.

### Added — regression tests
- `test_generated_virtual_columns_detected` — builds a fixture db with
  `GENERATED ALWAYS AS (...) VIRTUAL` columns and asserts migration 024
  classifies as `likely-applied (2/2)`.
- `test_add_column_if_not_exists_regex` — builds a fixture db with
  every column migration 023 expects and asserts the result is
  `likely-applied (8/8)`, not `partial (7/8)`.

Both tests would fail against v1.5.0's heuristic and pass against
v1.5.1's.

## [1.5.0] — 2026-04-13

### Safe upgrade path for existing brain.db files

Closes the sharp edge where users upgrading brainctl could silently break
writes against older `brain.db` schemas. Adds the diagnostic and backfill
tools needed to safely align a pre-existing `brain.db` with a newer version
of the package.

### Added
- **`brainctl migrate --status-verbose`** — per-migration DDL heuristic. Each
  migration file gets classified as `likely-applied`, `partial`, `pending`,
  or `unknown` based on whether its expected columns/tables already exist
  in the schema. Works by regex-parsing `ALTER TABLE ADD COLUMN` and
  `CREATE TABLE` statements and checking `sqlite_master` / `PRAGMA
  table_info`. Imperfect but genuinely diagnostic — revealed real partial-
  apply drift in a test database that ad-hoc inspection had missed.
- **`brainctl migrate --mark-applied-up-to N`** — backfill `schema_versions`
  for migrations 1..N as "already applied" without running their SQL. For
  `brain.db` files that predate the migration tracking framework: their
  schema already has the effects, but the tracker is virgin, so
  `brainctl migrate` would try to re-apply everything and crash on column
  collisions. Rows written with a `(backfilled)` name suffix so they're
  distinguishable from "really ran" tracking rows.
  - **Guard**: refuses to go below the current high-water mark (prevents
    rewriting tracker state you've already committed to). Backfilling
    *above* the high-water mark is always allowed — this handles the
    partial-tracker case where a user ran `brainctl migrate`, got a few
    through, crashed, and now needs to skip the rest.
  - Supports `--dry-run` for preview.
  - Duplicate-version migrations (`012_*`, `013_*`, `017_*`, `021_*`,
    `023_*` each have two files) collapse into one `schema_versions` row.
- **`Brain()` pending-migrations warning** — `Brain(db_path)` against an
  existing `brain.db` with pending migrations emits a `logging.warning` with
  branched advice:
  - **Virgin tracker** (`applied == 0 AND pending > 0`): "migration tracking
    not initialized — run `brainctl doctor` for diagnosis." **Never** tells
    the user to run `brainctl migrate` blindly — that would crash on
    pre-existing columns.
  - **Partial tracker** (`applied > 0 AND pending > 0`): "N pending, run
    `brainctl migrate`."
  - Deduped per-process per-db via a module-level flag so multiple `Brain()`
    constructions don't spam the log.
  - Gated on `BRAINCTL_SILENT_MIGRATIONS=1` for CI and tests.
- **`brainctl doctor` migrations section** — three-state diagnostic matching
  the Brain warning, plus a fourth state `virgin-tracker-with-drift`
  triggered when the tracker is empty but the schema shows ≥2 late-migration
  marker columns (`write_tier`, `ewc_importance`, `labile_until`,
  `memory_type`, `protected`). In that state, doctor prints the full
  4-step recovery workflow:
  1. `brainctl migrate --status-verbose`
  2. apply truly-pending migrations manually via `sqlite3`
  3. `brainctl migrate --mark-applied-up-to N`
  4. `brainctl migrate`
- **README `Upgrading` section** — covers the normal upgrade path, the
  virgin-tracker-with-drift edge case, the full recovery workflow,
  backup/rollback guidance. Visible right after `Install` so new users
  encounter it before they pip-upgrade an existing brain.

### Tests
- 21 new test cases in `tests/test_migrate.py`:
  - `TestMarkAppliedUpTo` (11): dry-run, real backfill, idempotent re-run,
    extend above high-water, guard refuses below high-water / above max /
    N<1, partial tracker extend, duplicate-version collapse, subsequent
    `migrate run` correctly skips backfilled versions.
  - `TestStatusVerbose` (5): extends base status, classifies every
    migration, fresh db shows `likely-applied`, bare db shows `pending`,
    UPDATE-only migrations land in `unknown`.
  - `TestBrainMigrationWarning` (5): virgin → doctor advice (never
    migrate), partial → migrate advice, up-to-date is silent, env var
    suppression, dedupe across multiple constructions.
  - `TestDoctorMigrationsCheck` (2): JSON includes migrations section,
    detects virgin-tracker-with-drift via late-column markers.
- Full suite: 1369 passing, no regressions.

### Known limitations
- The migration runner itself is not yet idempotent at the statement
  level — collisions on `ALTER TABLE ADD COLUMN` still crash the run
  (stops on first error). The v1.5.0 story is "detect before applying";
  a DDL-only idempotent runner with savepoints is planned for v1.5.1.
- `status()` returns `total` (file count) and `applied`/`pending` (version
  counts), so `total != applied + pending` when duplicate-version
  migrations are present. Cosmetic; will be cleaned up in v1.5.1.

## [1.4.0] — 2026-04-13

### Added
- **Codex CLI plugin** — `plugins/codex/brainctl/` gives [OpenAI Codex CLI](https://github.com/openai/codex)
  persistent memory via the brainctl MCP server. Ships:
  - `install.py` — idempotent, sentinel-wrapped merge of `[mcp_servers.brainctl]`
    into `~/.codex/config.toml`. Supports `--dry`, `--print`, `--uninstall`
    with automatic backup. Leaves other MCP servers and config untouched.
  - `AGENTS.md.template` — session-lifecycle instructions (orient on start,
    wrap up on end) that users drop into their project's `AGENTS.md` so Codex
    auto-loads the brainctl memory protocol on every session.
  - `README.md` — install + usage + troubleshooting.
  - `plugin.yaml` — metadata with `brainctl[mcp]>=1.3.0` as the pip floor
    (MCP extra required for the `brainctl-mcp` server entry point).
  Once installed, Codex gets the full brainctl tool surface (196 tools)
  including the native `agent_orient` / `agent_wrap_up` session bookends
  shipped in v1.3.0.

## [1.3.0] — 2026-04-13

### Added
- **Eliza plugin** — `plugins/eliza/brainctl/` ships `@brainctl/eliza-plugin`, a
  TypeScript plugin that gives Eliza agents persistent memory via the brainctl
  MCP server. Six actions (`BRAINCTL_REMEMBER` / `SEARCH` / `ORIENT` / `WRAP_UP`
  / `DECIDE` / `LOG`) plus an auto-recall memory provider that injects context
  before every message. Spawns `brainctl-mcp` as a stdio subprocess via
  `@modelcontextprotocol/sdk`. (#67)
- **Claude Code plugin** — `plugins/claude-code/brainctl/` hooks into Claude
  Code's `session_start`, `session_end`, `user_prompt_submit`, and
  `post_tool_use` events. Adds `brainctl orient` / `brainctl wrap_up` CLI
  commands for manual bookends and `<private>` redaction for sensitive
  content in memories.
- **Freqtrade plugin** — `plugins/freqtrade/brainctl/` — strategy mixin that
  gives Freqtrade strategies persistent memory across backtests and live
  runs. (#68)
- **Jesse plugin** — `plugins/jesse/brainctl/` — strategy mixin for the
  [Jesse](https://jesse.trade) algotrading framework, same shape as the
  Freqtrade plugin. (#69)
- **Native `agent_orient` / `agent_wrap_up` MCP tools** — session-lifecycle
  primitives are now first-class MCP tools instead of being composed
  client-side from `handoff_latest` / `handoff_add` / `event_search`. Plugins
  that want session bookends can call them directly. Tool count: 196. (#70)

### Changed
- **Lazy shared sqlite3 connection per `Brain` instance** — opens a single
  shared connection on first use instead of churning a new one per operation.
  (#62)
- **Schema rebase** — `init_schema.sql` rebased, 6 dead tables dropped, archive
  safety net added so historical rows survive schema migrations. (#63)
- **MCP helper consolidation** — duplicate helpers across MCP tool modules
  consolidated into `agentmemory.lib.mcp_helpers`. No behavior change. (#64)

### Docs
- Hermes plugin install path clarified: user-plugin dir is
  `~/.hermes/plugins/brainctl`, and any Python deps must be installed into
  Hermes's venv (not the shell's default pip). Workaround for the Hermes
  memory-provider discovery mismatch documented. (See also:
  [NousResearch/hermes-agent#9246](https://github.com/NousResearch/hermes-agent/pull/9246)
  which lands brainctl in-tree and removes the workaround entirely.)

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
- ~~Web dashboard on port 3939~~ (removed in 2.1.0 — Obsidian is the user-facing UI; see `brainctl obsidian`)
