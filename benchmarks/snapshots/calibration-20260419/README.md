# I5 Calibration Matrix — 2026-04-19

Plan: `plan-20260419-085511-top-heavy-retrieval-lift-hit-1-m-9114ac` · Item I5 (PH6).

## What's here

- `matrix.md` — human-readable ablation table + rollout recommendation.
- `matrix.json` — machine-readable 6-cell matrix.
- `{full,no_intent,rollback}-{longmemeval,locomo}.{json,traces.jsonl}` — per-cell metrics + per-query traces.
- `_run_matrix.py` — reproducer.
- `run.log` — driver stderr from the successful run.

## Headline

**Ship FULL (current `main`).**

- LoCoMo hybrid: +25.5pp Hit@1 / +36.2pp MRR vs pre-lift (plan envelope +1.0pp / +0.5pp — crushed).
- LongMemEval: flat vs I1 baseline (-1.39pp Hit@1, within noise on n=289), but FULL beats ROLLBACK by +62.3pp on a like-for-like Ollama-up comparison.
- Intent router: no measurable effect on current benches — keep on by default (cheap; may help on slices not measured here).
- `BRAINCTL_TOPHEAVY_ROLLBACK=1` works but is a footgun (vec-fusion stays on, heuristics off → catastrophic LME).

## Environment

- Git commit of this worktree: see PR description.
- Python: 3.14.3 (Homebrew).
- Ollama: running on `127.0.0.1:11436`, `nomic-embed-text` (768-dim).
- Seed: 42 (`tests/bench/run.py` sets `random.seed(42)` on import).
- Per-cell runtime: LongMemEval ~15 min, LoCoMo ~4-8 min.

## Known issues

- `p95_latency_ms` is 0.0 on every cell — driver `_extract_metrics` couldn't parse per-query timings. I8's latency gate stays informational until this is fixed + re-run (cheap follow-up).
- CE rerank was off in every cell because the bench harness doesn't populate `args.rerank`. Wire that up to measure the CE dimension.
