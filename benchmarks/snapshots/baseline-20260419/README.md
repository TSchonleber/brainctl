# I1 — Frozen retrieval baseline (2026-04-19)

Plan: `plan-20260419-085511-top-heavy-retrieval-lift-hit-1-m-9114ac`
Item: **I1** — PH1 baseline pack (pre-change reference for top-heavy retrieval lift)
Owner: `claude-code` (co-executing under codex's plan)
Captured: 2026-04-19T13:17Z

This snapshot is the frozen reference numbers that I2–I4 retrieval
changes (intent router, adaptive fetch/gating, last-mile rerank) must
beat without regressing coverage or latency. Per-query trace JSONL
files are included so I5's calibration matrix can compute per-slice
deltas and so any future agent can recompute any metric without
re-running the benchmarks.

## Environment

- **Git commit:** `2fb2a1c75a8dd9405d1caccd014370637ab27bd7` (worktree
  `agent-a833c7a2`, branched from `main`)
- **Python:** 3.14.3 (Homebrew, Apple Silicon arm64)
- **Package versions:** see `versions.txt` (output of `pip freeze`).
  Note: the globally-installed `brainctl` editable points at
  `/tmp/brainctl-work` — these runs bypassed that install and used the
  worktree's `src/` tree directly via `PYTHONPATH=src:.` so the
  numbers reflect exactly the source at the git commit above.
- **Hardware:** see `sysctl-hw.txt`. Apple M4 Max, 14 CPUs, 36 GB RAM,
  macOS 26.4 Build 25E246 (Darwin 25.4.0).
- **Seed:** 42 (set at `tests/bench/run.py:24`, reused by the
  `_stratified_subset` LongMemEval bucketer).
- **Embedding model:** `nomic-embed-text` via Ollama. **Ollama was
  down at run time** (`curl http://localhost:11434/api/tags` → exit 7
  "connection refused"; see `ollama-probe.txt`). Brain.search's
  fallback path kept FTS5-only scoring for the `brain` backend; the
  `cmd` backend (full hybrid `cmd_search`) still ran, but its RRF
  vector leg was a no-op. This is the same environment state the
  committed `tests/bench/baselines/*.json` were frozen in — all four
  headline metric sets on this snapshot reproduce the committed
  baselines to 4 decimal places.
- **Reranker:** `src/agentmemory/rerank.py` cross-encoder is **off**
  (not wired into Brain.search or cmd_search on main). I2–I4 will
  change that; this baseline captures the pre-change state.

## What ran

| Bench                       | Backend | N     | Wall (s) | Headline                         |
|----------------------------|---------|-------|----------|-----------------------------------|
| LongMemEval (full, judge+)  | brain   | 500   | 47.98    | Hit@1=0.874, MRR=0.9171, nDCG@5=0.8881, Hit@5=0.970 |
| LongMemEval (retrieval-friendly) | brain | 289 | 28.40 | Hit@1=0.8824, MRR=0.9241, nDCG@5=0.8910, Hit@5=0.9758 |
| LoCoMo (brain / turn)       | brain   | 1982  | 267.89   | Hit@1=0.3406, MRR=0.4447, nDCG@5=0.4365, Hit@5=0.5716 |
| LoCoMo (cmd / hybrid)       | cmd     | 1982  | 133.36   | Hit@1=0.0232, MRR=0.0317, nDCG@5=0.0262, Hit@5=0.0424 |
| search-quality (smoke)      | cmd     | 20    | <1       | P@1=0.60, MRR=0.625, nDCG@5=0.5579 |

**LongMemEval (500)** is the headline baseline for this plan —
`--include-judge-only` so the run covers all six `question_type` axes
(retrieval-friendly + LLM-judge-only), retrieval-only scoring
(gold `answer_session_ids`). This is what the task spec called out as
"longmemeval(289), full set, all axes including JUDGE_ONLY" — the 289
is the *retrieval-friendly subset size*; the full set at the dataset
level is 500, and both are captured here for completeness.

**LoCoMo "turn" vs "hybrid"** map to the two existing retrieval
pipelines the harness exposes today:
 - *turn* = `--backend brain` → `Brain.search` (FTS5 + embedding-RRF,
   embedding leg no-op here because Ollama was down).
 - *hybrid* = `--backend cmd` → the full `cmd_search` pipeline
   (`agentmemory._impl.cmd_search`) with recency disabled (LOCOMO
   synthetic timestamps make recency destructive — the legacy runner
   already does this).
The plan's "session" mode is not yet implemented in the harness; see
"known issues" below.

## Latency (end-to-end per-query, incl. FTS5+RRF+any reranker)

From the per-query `timings_ms.query_ms` field in the traces:

```
longmemeval-500   n=500    p50=18.3ms   p95=54.0ms    p99=88.2ms    max=107.2ms
longmemeval-289   n=289    p50=19.6ms   p95=65.0ms    p99=90.6ms    max=108.8ms
locomo-turn       n=1982   p50=124.4ms  p95=244.3ms   p99=305.4ms   max=391.3ms
locomo-hybrid     n=1982   p50=62.5ms   p95=109.9ms   p99=146.5ms   max=205.6ms
```

The plan's latency guardrail is **p95 ≤ +15% vs this baseline**. For
an ergonomic reference: I2–I4 must keep `locomo-turn.p95_ms` below
~281 ms and `longmemeval-500.p95_ms` below ~62 ms.

Note: the single-op micro-latency numbers in
`tests/bench/baselines/latency.json` (from 2026-04-18) are on a
different scale — those are Brain.remember and single Brain.search
calls measured in isolation. The p95s above include the full
per-question eval path (one search per question).

## Artifacts

- `longmemeval.json` — trimmed aggregate (overall + per-axis), 500 entries.
- `longmemeval.traces.jsonl` — 500 per-query trace records.
- `longmemeval-289.json` / `longmemeval-289.traces.jsonl` — same at
  retrieval-friendly scope (matches committed baseline exactly).
- `locomo-turn.json` / `locomo-turn.traces.jsonl` — brain backend, 1982 qs.
- `locomo-hybrid.json` / `locomo-hybrid.traces.jsonl` — cmd backend, 1982 qs.
- `search-quality.json` — 20-query smoke regression fixture.
- `latency.json` — p50/p95/p99 per bench computed from the JSONL.
- `manifest.json` — SHA256 of every artifact so tampering is detectable.
- `versions.txt` — `pip freeze` at run time.
- `sysctl-hw.txt` — host hardware for comparisons on other machines.
- `ollama-probe.txt` — record of Ollama reachability at run time.
- `*.stdout.json` / `*.stderr.log` — raw harness output (kept for
  reproducibility; not in the manifest).

## Trace record schema

Each line in `*.traces.jsonl` is one JSON object with these fields:

```json
{
  "qid": "conv-26:q0",
  "query": "When did Caroline go to the LGBTQ support group?",
  "retrieved_ids": ["D1:3", "D10:5", "..."],
  "scores": [0.0, 0.0, "..."],
  "gold_ids": ["D1:3"],
  "hit_at_k": {"1": 1, "5": 1, "10": 1, "20": 1},
  "recall_at_k": {"1": 1.0, "5": 1.0, "10": 1.0, "20": 1.0},
  "ndcg_at_k": {"1": 1.0, "5": 1.0, "10": 1.0, "20": 1.0},
  "mrr_contribution": 1.0,
  "category": "single-hop",
  "timings_ms": {"query_ms": 28.114, "ingest_ms": 42.1, "total_ms": 70.2}
}
```

`scores` will be all-zero for the `brain` backend because
`Brain.search` results don't expose a `final_score` field; `cmd`
backend populates `final_score` via the `cmd_search` RRF+rerank
pipeline. I3 rerank changes should populate this from the cross-encoder.

## Committed baseline comparison

```
                                 committed         new (this snapshot)
LoCoMo brain / turn     Hit@1 =  0.3406            0.3406   Δ=+0.0000
LoCoMo cmd / hybrid     Hit@1 =  0.0232            0.0232   Δ=+0.0000
LongMemEval (289)       Hit@1 =  0.8824            0.8824   Δ=+0.0000
search-quality          P@1   =  0.60              0.60     Δ=+0.0000
```

All four reproduce to 4 decimal places. **The committed
`tests/bench/baselines/*.json` files are left untouched** — numbers
match within 0.0% so no update is warranted per the task rule
(`>0.5pp drift`). The new 500-entry LongMemEval file
(`longmemeval.json`) is a *superset* of the committed one and does
not replace it.

## Known issues / partial-run notes

1. **LoCoMo "session" mode is not in the harness yet.** The plan text
   mentions "turn/session/hybrid" modes; only `turn` (via
   `--backend brain`, per-dia_id key) and a rough *hybrid* (via
   `--backend cmd`, full `cmd_search` pipeline) exist today. A proper
   session-mode runner (one memory per session, aggregate turn-level
   text) is codex's territory under I3 (intent router) — designing it
   here would conflict with that workstream. Documented so the I5
   calibration matrix accounts for it.
2. **LoCoMo cmd_search shows very low Hit@1 (0.023).** Matches the
   committed `locomo_pre_fix_2026_04_18.json` exactly — this is a
   known pre-existing condition in the cmd_search hybrid path when
   exercised against a per-question fresh-DB ingest. `per_convo`
   carries no `search_errors`; the issue is retrieval-shape, not a
   crash. Codex is aware (I2 adaptive controls + I3 intent router
   target this).
3. **Ollama down at run time.** Vector leg was a no-op, so numbers
   reflect FTS5-only scoring. This is the same condition the
   currently-committed baselines were captured in; they reproduce
   exactly. If Ollama is brought up for future re-runs, the deltas
   will not be comparable to this snapshot — re-capture the baseline.
4. **LongMemEval cmd backend not implemented.** `longmemeval_eval.py`
   raises `ValueError` for `backend != 'brain'`; only the brain
   backend was run. Task acknowledged as a future extension.
5. **Per-query score field is zero for brain backend.** Brain.search
   doesn't expose a score in the returned dict, so the `scores` array
   in brain traces is all-zero. Rank-order information is preserved
   via `retrieved_ids`, which is what I5's per-slice deltas need.

## How to reproduce

```bash
cd /path/to/agentmemory/worktree/agent-a833c7a2
export PYTHONPATH=src:.
export BRAINCTL_BENCH_NO_DOWNLOAD=1

python3 -m tests.bench.run --bench longmemeval --include-judge-only \
    --traces benchmarks/snapshots/baseline-20260419/longmemeval.traces.jsonl
python3 -m tests.bench.run --bench longmemeval \
    --traces benchmarks/snapshots/baseline-20260419/longmemeval-289.traces.jsonl
python3 -m tests.bench.run --bench locomo --backend brain \
    --traces benchmarks/snapshots/baseline-20260419/locomo-turn.traces.jsonl
python3 -m tests.bench.run --bench locomo --backend cmd \
    --traces benchmarks/snapshots/baseline-20260419/locomo-hybrid.traces.jsonl
python3 -m tests.bench.run --bench search-quality
```

Verify the snapshot hasn't drifted:

```bash
python3 -c "
import json, hashlib, pathlib
m = json.load(open('benchmarks/snapshots/baseline-20260419/manifest.json'))
ok = True
for name, want in m['sha256'].items():
    if want is None: continue
    got = hashlib.sha256(pathlib.Path('benchmarks/snapshots/baseline-20260419',name).read_bytes()).hexdigest()
    if got != want:
        print('MISMATCH', name, got, '!=', want); ok = False
print('OK' if ok else 'TAMPERED')
"
```

## Plan handoff

I5's ablation matrix uses the `*.traces.jsonl` to compute per-slice
deltas for every candidate toggle combination. No further baseline
work is planned in this worktree — I8 (CI guardrails) is the next
claude-code item on this plan, and will wire the `.json` files above
into `tests/bench/baselines/` gating + a new `latency.json` p95 gate.
