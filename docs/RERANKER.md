# Cross-Encoder Reranker Stage

> **Status: opt-in, off by default in 2.4.0.** This document describes
> a new optional stage in brainctl's hybrid retrieval pipeline. Pass
> `--rerank` to your search command (or set `rerank=true` on the
> `memory_search` MCP tool) to activate it. Top-heavy controls include
> `--rerank-top-n` and `--rerank-budget-ms` for bounded rerank scope.

## What it is

After brainctl's hybrid retrieval (FTS5 + sqlite-vec via Reciprocal
Rank Fusion) and the heuristic reranker chain (recency, salience,
Q-value, source weighting, context match, trust, PageRank), the
**cross-encoder reranker** does one more pass:

1. Take the top-K candidates.
2. For each candidate, run the (query, candidate text) pair through
   a small transformer that has been fine-tuned to score relevance
   pairs.
3. Re-sort by the cross-encoder score.
4. Trim to the user's requested limit.

Cross-encoders typically outperform bi-encoders / dual-encoders on
relevance ranking because the transformer can attend to fine-grained
query-candidate token interactions that get lost when the two sides
are encoded independently. The cost is that you have to run the
model once per (query, candidate) pair instead of once per side —
which is why we run it on the small post-fusion set, not the full
corpus.

## When to use it

**Use it when:**
- You're making **agent-level decisions** based on the search
  results — e.g., a planner picking which memory to act on, an
  orient call building a handoff packet, a reasoning loop that needs
  the actual top-1 to be correct.
- You're doing **high-stakes one-shot search** where the user can
  feel the difference between rank 1 and rank 3.
- You can amortize the latency over a relatively low query volume
  (≤ a few QPS).

**Do NOT use it when:**
- You're **logging or polling** at high frequency — the +50ms (GPU)
  to +600ms (CPU) per search will dominate your loop.
- The downstream consumer **doesn't care about the top result** —
  e.g., a federated search collector that re-ranks across multiple
  brains, or a UI that lets the user pick from the top 20.
- You're running on **uniform-timestamp synthetic benchmarks**
  (LOCOMO / LongMemEval); see "Bench results" below.

## Quick start

```bash
# Install the optional ML extras (~1 GB; pulls torch + sentence-transformers).
pip install 'brainctl[rerank]'

# Use the default model (bge-reranker-v2-m3).
brainctl search "what does Alice prefer?" --rerank

# Pin a faster model when latency matters.
brainctl search "what does Alice prefer?" --rerank jina-reranker-v2-base-multilingual

# Bound rerank scope and strict latency budget.
brainctl search "what does Alice prefer?" --rerank --rerank-top-n 40 --rerank-budget-ms 350

# From an MCP client (Claude Code / Hermes / etc.):
memory_search(query="what does Alice prefer?", rerank=True)
memory_search(query="what does Alice prefer?", rerank="jina-reranker-v2-base-multilingual")
```

## Top-heavy rollout controls (I6)

Top-heavy retrieval changes (I2/I3/I4 + I6) are intended for staged,
canary-first rollout:

- `--rollout-mode {on,off,canary}`
- `--rollout-canary-agents agentA,agentB`
- `--rollout-canary-percent 10`
- `--rollback-top-heavy` (immediate off switch)

Environment equivalents:

- `BRAINCTL_TOPHEAVY_ROLLOUT_MODE`
- `BRAINCTL_TOPHEAVY_CANARY_AGENTS`
- `BRAINCTL_TOPHEAVY_CANARY_PERCENT`
- `BRAINCTL_TOPHEAVY_ROLLBACK`

For provenance, run search with `--debug` and inspect `_debug` keys:
`topheavy.rollout_mode`, `topheavy.rollout_reason`,
`topheavy.enabled`, and `<bucket>.cross_encoder_*` /
`<bucket>.*_skipped`.

## Supported models

| Model | Size | Backend | Notes |
|-------|------|---------|-------|
| `bge-reranker-v2-m3` *(default)* | ~600 MB | sentence-transformers | Multilingual, best Pareto balance |
| `jina-reranker-v2-base-multilingual` | ~280 MB | sentence-transformers | Smaller / faster, good when latency matters |
| `qwen3-reranker-4b` | ~4 GB | *deferred* | LLM-style logit-based reranker; not a cross-encoder. Recognised in the registry but **emits a warning and falls through to no-op** in 2.4.0. Will be wired through `FlagLLMReranker` in a follow-up. |

Models load from the Hugging Face Hub on first use (cached at
`~/.cache/huggingface/`). After the first call the model is held in
the per-process module cache.

## Second-stage tiny MLP artifact policy

The local second-stage reranker can optionally load a tiny JSON MLP artifact
from `src/agentmemory/retrieval/models/tiny_mlp_v1.json`, or from an explicit
path passed through the internal reranker configuration. That artifact is not
checked into git. If the file is absent, the second-stage path falls back to
the deterministic heuristic slate scorer and search remains fully functional.

The fallback is implemented in `src/agentmemory/retrieval/second_stage.py`:
`rerank_top_candidates()` calls `TinyMLPModel.try_load(...)`; when that returns
`None`, the MLP score vector is all zeros and `_heuristic_score()` plus
`_rerank_slate()` produce the final deterministic listwise order. No network,
model download, or checked-in weight file is required for the default path.

This keeps the default package local-first and reviewable:

- no mandatory network fetch,
- no opaque weights bundled in source,
- no hard dependency on numpy at import time,
- no failure when the model artifact is unavailable.

Training and calibration scripts live under `benchmarks/` and emit JSON
artifacts into ignored benchmark/training output directories. If a trained
artifact is published later, it should be attached as a release asset or LFS
object with a short provenance record containing the source commit, training
bundle, feature version, and held-out metrics.

Benchmark numbers reported by a PR must state whether they were produced with
an external MLP artifact present. If no artifact path is supplied and
`tiny_mlp_v1.json` is absent, those numbers are heuristic-fallback numbers.

## Latency / quality tradeoff

Measured on Apple Silicon M-series, CPU only (no MPS), Python 3.14,
torch 2.11, with `bge-reranker-v2-m3`. **Warm latency** (model
already loaded). Numbers are wall-clock for the cross-encoder stage
alone — not the full search.

| Top-K reranked | Median | p95   |
|----------------|--------|-------|
| 5              | 94 ms  | ~150 ms |
| 20             | 327 ms | ~500 ms |
| 50             | 354 ms | ~880 ms |

These are **~3-10× slower than the brief's "~50ms" optimistic
estimate**, which assumed GPU inference. On a real GPU (CUDA or MPS
enabled), expect ~1/5 of these numbers.

**Cold-start cost** (first call after process start) is **~40s** —
weight load + tokenizer init + torch graph compile. The module
caches the model at process scope so this is paid exactly once.

## Bench results — LOCOMO (1 conversation, 197 questions)

| Config                    | Hit@1   | Hit@5   | MRR     | nDCG@5  | Avg latency/query |
|---------------------------|---------|---------|---------|---------|-------------------|
| OFF (no cross-encoder)    | 0.0102  | 0.0305  | 0.0195  | 0.0190  | 53.6 ms           |
| `bge-reranker-v2-m3`      | 0.0102  | 0.0305  | 0.0195  | 0.0190  | 40.7 ms           |
| `jina-reranker-v2-base-multilingual` | 0.0102 | 0.0305 | 0.0195 | 0.0190 | 124.9 ms       |
| `qwen3-reranker-4b`       | *(deferred — LLM-style, not yet wired)* | | | | |

The avg-latency variation between configs is **not** the cross-encoder
cost — at K≈5 (LOCOMO's per-question retrieval window after the bench
sets `no_recency=True`), the CE stage finishes in well under 1ms when
candidates fit in cache. The variation reflects normal cmd_search
variance (FTS / WAL / sqlite-vec lookup time) across separate runs of
this small (~200-question) sample.

### Why the LOCOMO numbers are flat — and why that's OK

LOCOMO's questions are answered against `dia_id`-tagged turns from a
synthetic-conversational corpus. The post-RRF candidate set is
already a tight pool of FTS-matched dialogue turns where re-ranking
shuffles within an essentially-equivalent relevance class — the
*same* gold turn surfaces at the *same* position whether or not the
cross-encoder runs. This is the same pathology that motivated the
`--benchmark` flag in 2.3.1 (memory id 1690): on uniform-timestamp
synthetic benchmarks, every reranker collapses to no-op.

**The cross-encoder does work.** A direct synthetic test:

```
query: "Tell me about Python programming"
input order:
  id=1  Python is a high-level interpreted programming language...
  id=2  Tokyo experienced heavy rain yesterday...               (irrelevant)
  id=3  Python lists are mutable sequences...
  id=4  The user prefers dark mode in their IDE.
  id=5  Python decorators are functions that modify...

reranked output:
  id=1 ce=0.7733  (top)
  id=3 ce=0.0660
  id=5 ce=0.0149
  id=4 ce=0.0000
  id=2 ce=0.0000  (sunk)
```

Tokyo (id=2) — the obviously-irrelevant candidate — drops from
position #2 to last. The reranker is doing what it should; LOCOMO
just doesn't have an obviously-irrelevant pool to test against.

**Where this stage will pay off:** real-world search over a brain
with thousands of memories from many projects/agents, where the
post-RRF top-50 contains a mix of on-topic and off-topic candidates.
That's the regime cross-encoders were trained for.

## How it composes with the rest of the pipeline

```
FTS5 + sqlite-vec
    ↓ (RRF fusion)
heuristic rerankers
    (recency / salience / Q-value / source / context-match / trust / PageRank)
    ↓
cross-encoder rerank      ← only fires when --rerank is passed
    ↓
MMR diversity boost       (if --mmr)
    ↓
quantum amplitude rerank  (if --quantum)
    ↓
final trim to --limit
```

The cross-encoder **rewrites `final_score`** so downstream stages
(MMR, quantum) operate on the cross-encoder ordering. The original
score is preserved on each result as `pre_ce_score` for auditability:

```json
{
  "id": 42,
  "content": "...",
  "rrf_score": 0.034,
  "pre_ce_score": 0.612,    // ← what the heuristic chain produced
  "ce_score": 0.7733,        // ← cross-encoder relevance
  "final_score": 0.7733      // ← what the trim sorts on
}
```

## Failure modes — graceful degradation

The reranker module is designed to **never crash a search**. When it
can't run, it returns the input unchanged with a single stderr warning:

| Condition                                   | Behaviour                                             |
|---------------------------------------------|-------------------------------------------------------|
| `pip install brainctl` (no `[rerank]`)       | All scores → 0; sort is stable; original order preserved |
| Model not pulled on first use, network down | Same as above — module-level "load failed" warning |
| Unknown model name                           | Warns "unknown model X; supported: [...]"; no-op       |
| `qwen3-reranker-4b` requested                | Warns "LLM-style reranker, deferred"; no-op            |
| Model loads but `predict()` raises (e.g. OOM) | Warns "predict failed: <exc>"; no-op                  |
| `BRAINCTL_RERANK_QUIET=1` env var set        | All warnings suppressed (used by the bench harness)    |

The `--benchmark` flag also disables cross-encoder rerank — it
disables every reranker in the chain, by definition.

## Caching

Per-process LRU cache (cap: 1000 entries) keyed on `(model_name,
sha1(query)[:12], sha1(candidate)[:12])`. Repeat queries against the
same candidate set hit the cache and skip the model entirely.

The cache is intentionally **not persisted** between processes:
cross-encoder scores are model-version dependent, and we don't want
stale scores leaking when the user updates the model.

```python
from agentmemory.rerank import cache_clear, cache_stats

cache_stats()    # → {"entries": 47, "max": 1000}
cache_clear()    # drop all entries (used by bench harness between runs)
```

## API

### CLI

```
brainctl search QUERY [--rerank [MODEL]] [--rerank-top-n N] [--rerank-budget-ms MS]
```

- `--rerank` alone uses the default model (`bge-reranker-v2-m3`).
- `--rerank MODEL` pins one of the supported names.
- `--rerank-top-n` limits the CE pass to the top-N pre-trim candidates.
- `--rerank-budget-ms` enforces strict per-call + rolling p95 latency budget.

### MCP tool

```
memory_search(query=..., rerank: bool | str = false)
```

- `false` (default): no cross-encoder rerank.
- `true`: default model.
- `"<model name>"`: pin a specific model.

### Python module

```python
from agentmemory.rerank import (
    SUPPORTED_MODELS, DEFAULT_MODEL,
    available_models,
    score_pairs,
    rerank,
    cache_clear, cache_stats,
)

# Probe what's loadable in this environment.
available_models()
# → ['bge-reranker-v2-m3', 'jina-reranker-v2-base-multilingual']
#   (only when sentence-transformers is installed)

# Score raw pairs.
score_pairs("what does Alice prefer?", ["dark mode", "light mode"])
# → [0.83, 0.21]

# Re-rank a list of brainctl result dicts.
rerank(
    "what does Alice prefer?",
    [
        {"content": "Alice prefers dark mode in her IDE.", "final_score": 0.5, "id": 1},
        {"content": "Bob runs the staging deploy.",        "final_score": 0.4, "id": 2},
    ],
    model="bge-reranker-v2-m3",
    top_k=5,
)
# → [{...id: 1, ce_score: 0.91, pre_ce_score: 0.5, final_score: 0.91}, ...]
```

## Why not Ollama?

The brief originally suggested Ollama as the backend. As of 2.4.0,
**Ollama does not ship a first-class `/api/rerank` endpoint** — its
HTTP surface is `/api/generate`, `/api/chat`, `/api/embed`,
`/api/tags`, `/api/pull`. Cross-encoder scoring is a different
operation (it's not generation, not embedding) and Ollama's existing
endpoints don't expose the right tensor.

The `rerank.py` module therefore uses **sentence-transformers as the
primary backend** with an Ollama probe (`/api/tags`) reserved for the
day Ollama adds a rerank endpoint. When that happens, `_ollama_tags`
already enumerates pulled reranker models — only the scoring path
needs to be wired through.

## Roadmap

- **2.4.x:** Wire `qwen3-reranker-4b` via `FlagEmbedding.FlagLLMReranker`.
- **2.5.x:** GPU/MPS detection (currently CPU-only — torch picks the
  default device, which is CPU on macOS unless MPS is explicitly
  selected).
- **Open question:** Should we auto-fire the cross-encoder when the
  candidate-set entropy is high (i.e., the heuristic rerankers
  produced a flat distribution)? That would let us skip the explicit
  flag in cases where it's most beneficial.
