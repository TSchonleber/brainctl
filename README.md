# brainctl

**Forgetful agents, fixed by a SQLite file.**

One `brain.db` gives your agent durable memory across sessions — episodic evidence, semantic facts, procedural runbooks, decisions made, entities tracked, and state handed off. No server. No API keys. No LLM calls required.

```python
from agentmemory import Brain

brain = Brain(agent_id="my-agent")
ctx = brain.orient(project="api-v2")           # handoff + events + triggers + memories + procedures
brain.remember("rate-limit: 100/15s", category="integration")
brain.remember_procedure(
    goal="Deploy to staging safely",
    steps=["Run tests", "brainctl migrate", "Deploy", "Check health"],
    title="Staging deploy runbook",
)
brain.decide("use Retry-After for backoff", "server controls timing", project="api-v2")
brain.wrap_up("auth module complete", project="api-v2")  # session end: logs + handoff for next run
```

## Install

```bash
pip install brainctl
```

Requires Python 3.11+. SQLite is built-in. No other mandatory dependencies.

```bash
pip install brainctl[mcp]     # MCP server — 201 tools for Claude Desktop, Cursor, VS Code
pip install brainctl[vec]     # vector similarity search (sqlite-vec + Ollama)
pip install brainctl[signing] # Ed25519-signed memory exports + optional Solana on-chain pinning
pip install brainctl[all]     # everything
```

## 5-line example

```python
from agentmemory import Brain

brain = Brain(agent_id="research-bot")
brain.remember("OpenAI rate-limits at 500k TPM on tier 3", category="integration")
results = brain.search("rate limit")          # FTS5 full-text, stemming, ranked
brain.entity("OpenAI", "service", observations=["500k TPM tier 3", "REST API"])
brain.relate("OpenAI", "provides", "GPT-4o")
```

## Feature checklist

**Memory types**
- `convention`, `decision`, `environment`, `identity`, `integration`, `lesson`, `preference`, `project`, `user`
- Core memory layers: episodic, semantic, and procedural
- Category controls natural half-life: identity decays over ~1 year; integration details over ~1 month
- Hard cap: 10,000 memories per agent. Emergency compression retires lowest-confidence entries.

**Retrieval modes**
- FTS5 full-text search with stemming (default, zero dependencies)
- Vector similarity via sqlite-vec + Ollama nomic-embed-text (`brainctl[vec]`)
- Hybrid: Reciprocal Rank Fusion over FTS5 + vector results
- Retrieval executive above memories/events/context/decisions/procedures: query planning, candidate fusion, procedural evidence expansion, deterministic late reranking, grounded abstention
- Context profiles: named search presets scoped to task type (`--profile ops`, `--profile research`, etc.)
- `--benchmark` preset: flattens recency/salience for synthetic evaluation runs

**Reranker chain**
- Intent classifier (regex, 10 labels → 6 profiles) routes queries at `cmd_search`
- Post-FTS reranking by recency, salience, Q-value utility, and Bayesian recall confidence
- Cold-start: auto-detects available reranker backends (cross-encoder > sentence-transformers > fallback)
- Cross-encoder controls: `--rerank-top-n` and `--rerank-budget-ms` tune candidate window + strict latency budget
- Top-heavy staged rollout controls (I6): `--rollout-mode`, `--rollout-canary-agents`, `--rollout-canary-percent`, `--rollback-top-heavy`
- Env mirrors for rollout controls: `BRAINCTL_TOPHEAVY_ROLLOUT_MODE`, `BRAINCTL_TOPHEAVY_CANARY_AGENTS`, `BRAINCTL_TOPHEAVY_CANARY_PERCENT`, `BRAINCTL_TOPHEAVY_ROLLBACK`
- Retrieval regression-gated in CI: >2% drop on P@1/P@5/MRR/nDCG@5 fails the build. Search-quality output also reports the fixture-specific P@5 ceiling and ratio-to-ceiling so sparse graded queries do not make raw P@5 look worse than it is.

**Knowledge graph**
- Typed entity nodes: `agent`, `concept`, `document`, `event`, `location`, `organization`, `person`, `project`, `service`, `tool`
- Auto entity linking: memories mentioning a known entity create the edge automatically
- Compiled truth synthesis per entity (`brainctl entity compile <name>`)
- 3-level enrichment tier; canonical alias dedup (`brainctl entity alias add`)
- Spreading-activation recall across the graph (`brain.think(query)`)

**Belief revision (AGM)**
- Belief set per agent with confidence weights
- Conflict detection and resolution via `brainctl belief conflicts` and `brainctl belief merge`
- Collapse mechanics: decoherent beliefs quarantined, recovery candidates surfaced
- PII recency gate (Proactive Interference Index) on supersedes operations

**Signed exports**
- `brainctl export --sign` produces a portable Ed25519-signed JSON bundle
- `brainctl verify <bundle.json>` checks the signature offline — no brainctl required for verification
- Optional: `--pin-onchain` writes the SHA-256 hash as a Solana memo transaction (~$0.001 per pin)
- Managed wallet: `brainctl wallet new` creates a local keypair at `~/.brainctl/wallet.json` for users without an existing Solana setup
- Memories never leave the machine; only the hash goes on-chain (opt-in)

**Plugins (16 first-party)**

Agent frameworks:

| Plugin | Target |
|--------|--------|
| `plugins/claude-code/` | Claude Code |
| `plugins/codex/` | OpenAI Codex CLI |
| `plugins/cursor/` | Cursor |
| `plugins/gemini-cli/` | Gemini CLI |
| `plugins/eliza/` | Eliza (TypeScript) |
| `plugins/hermes/` | Hermes Agent |
| `plugins/openclaw/` | OpenClaw |
| `plugins/rig/` | Rig |
| `plugins/virtuals-game/` | Virtuals Game |
| `plugins/zerebro/` | Zerebro |

Trading bots:

| Plugin | Target |
|--------|--------|
| `plugins/freqtrade/` | Freqtrade |
| `plugins/jesse/` | Jesse |
| `plugins/hummingbird/` | Hummingbird |
| `plugins/nautilustrader/` | NautilusTrader |
| `plugins/octobot/` | OctoBot |
| `plugins/coinbase-agentkit/` | Coinbase AgentKit |

## MCP server (209 tools)

```json
{
  "mcpServers": {
    "brainctl": {
      "command": "brainctl-mcp"
    }
  }
}
```

Add to `~/.claude/claude_desktop_config.json`, `~/.cursor/mcp.json`, or equivalent. Full tool list and a decision tree: [MCP_SERVER.md](MCP_SERVER.md).

## CLI reference

```bash
brainctl memory add "content" -c convention   # store a memory
brainctl memory add "rollback steps..." -c convention --type procedural
brainctl search "query"                       # FTS5 search
brainctl procedure add --goal "Deploy to staging safely" --step "Run tests" --step "brainctl migrate"
brainctl procedure search "how do I deploy to staging?"
brainctl procedure feedback 12 --success --validated --outcome "deploy completed cleanly"
brainctl vsearch "semantic query"             # vector search (requires [vec])
brainctl entity create "Alice" -t person      # create entity
brainctl entity relate Alice works_at Acme    # link entities
brainctl event add "deployed v3" -t result    # log an event
brainctl decide "title" -r "rationale"        # record a decision
brainctl export --sign -o bundle.json         # signed export
brainctl verify bundle.json                   # verify a bundle
brainctl wallet new                           # create managed signing wallet
brainctl stats                                # DB overview
brainctl doctor                               # health check
brainctl lint                                 # quality issues
brainctl gaps scan                            # coverage + orphan + broken-edge scans
brainctl consolidate cycle                    # full consolidation pass
```

## Python API

| Method | What it does |
|--------|--------------|
| `orient(project)` | One-call session start: handoff + events + triggers + memories |
| `wrap_up(summary)` | One-call session end: logs event + creates handoff |
| `remember(content, category)` | Store a durable fact through the W(m) write gate |
| `remember(content, category, memory_type="procedural")` | Store free text and compile it into a structured procedure when appropriate |
| `remember_procedure(goal, steps, ...)` | Create a canonical procedural memory with structured fields |
| `search(query)` | FTS5 full-text search with stemming |
| `search_procedures(query)` | Search structured procedures with deterministic procedural scoring |
| `vsearch(query)` | Vector similarity search (optional) |
| `think(query)` | Spreading-activation recall across the knowledge graph |
| `forget(memory_id)` | Soft-delete a memory |
| `entity(name, type)` | Create or retrieve an entity |
| `relate(from, rel, to)` | Link two entities |
| `log(summary, type)` | Log a timestamped event |
| `decide(title, rationale)` | Record a decision with reasoning |
| `trigger(condition, keywords, action)` | Set a prospective reminder |
| `check_triggers(query)` | Match triggers against text |
| `handoff(goal, state, loops, next)` | Save session state explicitly |
| `resume()` | Fetch and consume latest handoff |
| `doctor()` | Diagnostic health check |
| `consolidate()` | Promote high-importance memories |
| `procedure_feedback(procedure_id, ...)` | Record execution outcome, validation, and utility for a procedure |
| `backfill_procedures()` | Synthesize candidate/canonical procedures from existing memories, events, and decisions |
| `tier_stats()` | Write-tier distribution |
| `stats()` | Database overview |
| `affect(text)` | Classify emotional state |
| `affect_log(text)` | Classify and store emotional state |
| `close()` | Close the shared SQLite connection |

## Memory lifecycle

- **Write gate** (W(m)): surprise scoring rejects redundant writes. Bypass with `force=True`.
- **Three-tier routing**: high-value memories get full indexing; low-value get lightweight storage.
- **Procedural compilation**: explicit runbooks live in dedicated procedure tables; `memory_type="procedural"` free text is heuristically compiled without deleting the original evidence.
- **Duplicate suppression**: near-duplicates reinforce existing memories instead of creating new rows.
- **Half-life decay**: unused memories fade at a rate set by category. Recalled memories are reinforced.
- **Consolidation**: Hebbian learning, temporal promotion, compression, and procedural candidate synthesis — runs on a cron schedule.

## Retrieval benchmarks

Tested with default settings, no tuning for benchmark data. Two harnesses
ship in the tree:

* `tests/bench/` — single-system retrieval baselines for `Brain.search`
  and `cmd_search`, now covering procedural lookup, rollback/troubleshooting,
  ambiguity, and abstention, gated against regression in CI.
* `tests/bench/competitor_runs/` — same-fixture head-to-head harness
  with adapters for Mem0, Letta, Zep, Cognee, MemPalace, OpenAI Memory.
  Skip-not-fabricate contract: missing SDK / API key raises
  `CompetitorUnavailable` instead of returning a fake 0. Each result
  row carries a `provenance` block recording `retrieval_mode`,
  `vector_enabled`, `embedding_model`, `rerankers_active`, and the
  full `search_args` so the JSON is self-describing.

### brainctl-only baselines (Brain.search, FTS5)

**LongMemEval** (289-question retrieval-friendly subset of `longmemeval_s`):

| metric | overall | single-session-assistant | single-session-user | multi-session |
|--------|---------|--------------------------|---------------------|---------------|
| hit@1  | 0.882 | 1.000 | 0.900 | 0.910 |
| hit@5  | 0.976 | 1.000 | 1.000 | 0.985 |
| MRR    | 0.924 | 1.000 | 0.935 | 0.944 |

**LongMemEval lock snapshot** (old FTS-only baseline vs final locked, n=289):

| metric | old FTS-only | final locked | abs delta | rel delta |
|--------|--------------|--------------|-----------|-----------|
| hit@1 | 0.8824 | 0.8685 | -0.0139 | -1.58% |
| hit@5 | 0.9758 | 0.9792 | +0.0034 | +0.35% |
| hit@10 | 0.9896 | 0.9896 | +0.0000 | +0.00% |
| hit@20 | 1.0000 | 1.0000 | +0.0000 | +0.00% |
| MRR | 0.9241 | 0.9147 | -0.0094 | -1.02% |
| nDCG@5 | 0.8910 | 0.8815 | -0.0095 | -1.07% |
| Recall@5 | 0.9217 | 0.9158 | -0.0059 | -0.64% |

**LOCOMO** (1,982 questions, 5 categories, 10 conversations):

| metric | overall | adversarial | temporal | open-domain | single-hop | multi-hop |
|--------|---------|-------------|----------|-------------|------------|-----------|
| hit@1  | 0.341 | 0.377 | 0.405 | 0.373 | 0.167 | 0.174 |
| hit@5  | 0.572 | 0.603 | 0.648 | 0.602 | 0.429 | 0.315 |
| MRR    | 0.445 | 0.479 | 0.510 | 0.479 | 0.282 | 0.232 |

**LOCOMO latest retrieval operating points** (n=1,982, no-LLM retrieval):

| metric | turn | session | hybrid |
|--------|------|---------|--------|
| hit@1 | 0.3734 | 0.6731 | 0.6983 |
| hit@5 | 0.6120 | 0.9117 | 0.9132 |
| hit@10 | 0.6892 | 0.9606 | 0.9601 |
| MRR | 0.4731 | 0.7749 | 0.7920 |
| single-hop hit@5 | 0.4645 | 0.8688 | 0.8546 |
| multi-hop hit@5 | 0.3696 | 0.6522 | 0.6739 |
| temporal hit@5 | 0.6604 | 0.8972 | 0.8972 |

Interpretation: hybrid leads session on hit@1, hit@5, MRR, and
multi-hop hit@5, ties temporal hit@5, and slightly trails on
single-hop hit@5.

The Brain.search baseline remains weaker on hop-heavy categories
(single-hop/multi-hop hit@1 0.167 / 0.174). Root cause: recency and
salience rerankers bias toward recent memories; LOCOMO uses uniform
synthetic timestamps with gold evidence concentrated in early sessions,
so reranking can fight lexical evidence. A `--benchmark` preset that
flattens recency/salience is available for evaluation runs.

### Top-heavy rollout and provenance (I2/I3/I4/I6)

- Rollout policy is staged and canary-first: start with `--rollout-mode canary`, then move to `on` after guardrails hold.
- Emergency rollback is explicit: `--rollback-top-heavy` or `BRAINCTL_TOPHEAVY_ROLLBACK=1`.
- Cross-encoder top-heaviness knobs are explicit: `--rerank-top-n N`, `--rerank-budget-ms MS`.
- Canary targeting supports both allowlist and percent sampling: `--rollout-canary-agents` / `BRAINCTL_TOPHEAVY_CANARY_AGENTS`, `--rollout-canary-percent` / `BRAINCTL_TOPHEAVY_CANARY_PERCENT`.
- Provenance is emitted in search output via `_debug` (always with `--debug`; opportunistically otherwise). Inspect: `topheavy.rollout_mode`, `topheavy.rollout_reason`, `topheavy.enabled`, `<bucket>.cross_encoder_applied`, `<bucket>.cross_encoder_skipped`, `<bucket>.cross_encoder_latency_ms`, `<bucket>.cross_encoder_p95_ms`, `<bucket>.cross_encoder_top_n`, and gate keys such as `<bucket>.recency_skipped`, `<bucket>.salience_skipped`, `<bucket>.qvalue_skipped`, `<bucket>.trust_skipped`, `<bucket>.fetch_narrowed`.

### Head-to-head vs MemPalace (measured 2026-04-18)

Same machine (Intel Core Ultra 7 258V / 33.9 GB RAM / Windows 10),
same datasets, same scoring. Repro:
`python benchmarks/compare_memory_engines.py --label full_compare`.

| benchmark | scoring | brainctl | mempalace | delta |
|---|---|---|---|---|
| LoCoMo (n=1,986) | session-level avg recall | **0.9217** | 0.6028 | +0.319 |
| LongMemEval (n=470) | R@5 | **0.9702** | 0.9660 | +0.004 |
| LongMemEval (n=470) | R@10 | **0.9894** | 0.9830 | +0.006 |
| MemBench FirstAgent (n=200) | hit@5 | **0.930** | 0.885 | +0.045 |
| ConvoMem | — | blocked | blocked | n/a |

**Honesty caveat (verbatim from the artifact bundle):** the
vector-on/off flag for the `cmd_search` run was not persisted in that
specific bundle. Subsequent runs through `tests/bench/competitor_runs/`
record `retrieval_mode` + `vector_enabled` automatically (commit
40c1ed2), so the gap is closed for any future re-run. We will not
cite the cmd_search numbers above as a clean vector-vs-FTS statement
without rerunning that exact variant with the flag captured.

## Upgrading

```bash
cp $BRAIN_DB $BRAIN_DB.pre-upgrade
brainctl doctor      # diagnose migration state
brainctl migrate     # apply pending migrations
```

For databases predating the migration tracker, see the full recovery workflow in the README's Upgrading section (below the install block in the full docs).

## Multi-agent

```python
researcher = Brain(agent_id="researcher")
writer     = Brain(agent_id="writer")

researcher.remember("API uses OAuth 2.0 PKCE", category="integration")
writer.search("OAuth")   # finds researcher's memory — same brain.db, shared graph
```

Every operation accepts `agent_id` for attribution. Agents share one `brain.db`. The knowledge graph connects insights across agents automatically.

## Documentation

| Doc | What it covers |
|-----|---------------|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 60-second onboarding — install, remember, search, sign |
| [docs/COMPARISON.md](docs/COMPARISON.md) | Feature matrix vs Mem0, Letta, Zep, Cognee, OpenAI Memory |
| [docs/AGENT_ONBOARDING.md](docs/AGENT_ONBOARDING.md) | Step-by-step agent integration guide |
| [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md) | Copy-paste blocks for MCP, CLI, Python agents |
| [docs/SIGNED_EXPORTS.md](docs/SIGNED_EXPORTS.md) | Bundle format, threat model, verify-without-brainctl recipe |
| [MCP_SERVER.md](MCP_SERVER.md) | 201 tools with decision tree |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Technical deep-dive |

## License

MIT
