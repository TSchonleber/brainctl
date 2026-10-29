# LOCOMO retrieval-only benchmark

LOCOMO ([snap-research/locomo](https://github.com/snap-research/locomo), ACL 2024)
is a long-horizon conversational memory benchmark: 10 multi-session conversations,
5,882 turns total, 1,986 QA pairs across 5 categories. Each QA carries gold
`evidence` turn IDs of the form `D{session}:{turn}`.

This harness runs the **retrieval stage only** — no LLM answer generation, no
GPT-judge. That means no API budget needed and the numbers isolate brainctl's
retrieval quality from any downstream generator.

## Run

```bash
python3 -m tests.bench.locomo.runner --convo 0       # smoke (1 convo)
python3 -m tests.bench.locomo.runner                 # all 10
python3 -m tests.bench.locomo.runner --json out.json # machine-readable
```

## Results (Brain.search / FTS5-only, k_max=20)

```
sample_id     turns    qa   hit@1   hit@5  hit@10  hit@20    r@5   r@10    mrr
conv-26         419   197  0.3401  0.5635  0.6294  0.7107 0.5190 0.5863 0.4351
conv-30         369   105  0.3810  0.6476  0.7429  0.7905 0.6067 0.7003 0.4991
conv-41         663   193  0.3938  0.5959  0.6788  0.7358 0.5516 0.6224 0.4896
conv-42         629   260  0.3308  0.5538  0.6269  0.7231 0.5012 0.5746 0.4278
conv-43         680   242  0.3388  0.6074  0.6736  0.7603 0.5519 0.6123 0.4654
conv-44         675   158  0.2722  0.5380  0.6203  0.7342 0.5036 0.5777 0.3800
conv-47         689   190  0.3211  0.5316  0.6632  0.7263 0.4969 0.6132 0.4224
conv-48         681   239  0.3891  0.5983  0.6946  0.7531 0.5385 0.6238 0.4816
conv-49         509   196  0.3163  0.5867  0.6939  0.7245 0.5150 0.6140 0.4322
conv-50         568   202  0.3218  0.5149  0.5941  0.7129 0.4736 0.5590 0.4203
OVERALL        5882  1982  0.3406  0.5716  0.6584  0.7351 0.5225 0.6039 0.4447

By category (overall, weighted)
cat             count   hit@1   hit@5  hit@10  hit@20    r@5   r@10    mrr
multi-hop         282  0.1667  0.4291  0.5426  0.6596 0.2039 0.2910 0.2821
temporal          321  0.4050  0.6480  0.7228  0.7757 0.6150 0.6862 0.5103
open-domain        92  0.1739  0.3152  0.3696  0.4348 0.2207 0.2739 0.2323
single-hop        841  0.3734  0.6017  0.6885  0.7598 0.5878 0.6728 0.4791
adversarial       446  0.3767  0.6031  0.6884  0.7691 0.5964 0.6805 0.4794
```

Wall time: 267 s for the full 10-conversation run on local sqlite.

## What this measures and what it doesn't

- **Measures.** Whether brainctl's retrieval returns the gold evidence turn for
  a question inside the top-K — `Hit@K`, `Recall@K`, `MRR`. This is the part
  brainctl owns; the downstream generator is replaceable.
- **Does not measure.** End-to-end QA correctness. LOCOMO's published numbers
  use a GPT-judge against a generator's answer; adding that back in is a
  matter of plugging a generator in and re-running — the retrieval harness
  is already separate so the rerun is cheap.

## Method

For each conversation:

1. Spin up a fresh temp `brain.db`.
2. Ingest every turn as one memory: `"[{speaker} @ {date}] {text} [key={dia_id}]"`,
   category `observation`. The `[key=...]` tag lets results be resolved back to
   the LOCOMO `dia_id` (`D{session}:{turn}`) after FTS5 roundtrip.
3. For every QA, call `Brain.search(question, limit=20)`, parse keys from
   results, score against the `evidence` list.
4. Aggregate per-conversation, per-category, and overall (weighted by QA count).

## Backends

The runner can swap retrieval pipelines via `--backend`:

| backend | wraps | what it tests |
|---|---|---|
| `brain` (default) | `Brain.search` | FTS5-only OR-expanded keyword path |
| `cmd` | `_impl.cmd_search` | full production path: FTS5 (+ vector RRF if vec ext loaded), intent routing, adaptive salience, recency, Q-value, RRF blend |

```bash
python3 -m tests.bench.locomo.runner --backend brain   # FTS5-only baseline
python3 -m tests.bench.locomo.runner --backend cmd     # production pipeline
```

### Finding: cmd_search underperforms Brain.search on LOCOMO

On conv-26 (197 QA), the production `cmd_search` path scored
**Hit@5 0.03 vs FTS5-only 0.56**. Disabling recency reranking
(`no_recency=True`) recovered some ground (Hit@5 ~0.03 → 0.03; marginal)
but the gap remained. The same OperationalErrors appear on a handful of
queries (3/197) regardless of args.

Why this matters: brainctl's production reranking — recency decay,
adaptive salience, Q-value exploitation — is tuned for live-use scenarios
where recent memories are usually more relevant. LOCOMO inverts that
assumption: gold evidence is uniformly distributed across all sessions
(often in the *first* session), and every memory is encoded at the same
synthetic timestamp, so salience/recency signals carry no information
and the rerankers actively scramble the FTS ranking.

The honest read: **on this benchmark, the simpler FTS5 path beats the
hybrid path** because LOCOMO's cold-start synthetic structure defeats the
priors the hybrid path is built around. Production traffic looks nothing
like LOCOMO, so this isn't a generic regression, but it does suggest a
"benchmark mode" preset (`brainctl search --benchmark`) that flattens
recency/salience would be worth adding.

## Caveats / next steps

- No vector-only or vector+FTS RRF run yet — sqlite-vec extension needs
  to be loaded and the bench DB embedded with nomic-embed before that's
  meaningful. Still free (Ollama local), worth doing next.
- Category label map follows the snap-research/locomo eval code (`1 multi-hop,
  2 temporal, 3 open-domain, 4 single-hop, 5 adversarial`). Releases before
  2.9.1 swapped 1, 3 and 4.
- End-to-end QA accuracy (the metric the published Mem0/Zep/LangMem
  numbers use) requires plugging a generator + judge LLM into the QA loop.
  Skipped here for cost; the retrieval ceiling we measure is an upper
  bound on what an end-to-end run could achieve.
