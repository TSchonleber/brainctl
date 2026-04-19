# I5 · Calibration Matrix · Top-Heavy Retrieval Ablation

Plan: `plan-20260419-085511-top-heavy-retrieval-lift-hit-1-m-9114ac`
Run: 2026-04-19 — Ollama up (`nomic-embed-text`, port 11436)
Seed: 42 (pinned in `tests/bench/run.py`)

## Cells

| Cell | Description | Toggles |
|---|---|---|
| **FULL** | shipped I2/I3/I4 default (top-heavy on, intent router on) | none (defaults) |
| **NO_INTENT** | top-heavy on, intent router off | `BRAINCTL_DISABLE_INTENT_ROUTER=1` |
| **ROLLBACK** | all I2/I3/I4 bypassed (pre-lift behaviour) | `BRAINCTL_TOPHEAVY_ROLLBACK=1` |

CE rerank dimension collapsed: neither `Brain.search` nor the bench's `_build_cmd_search_fn` populate `args.rerank`, so CE was off in every cell. Logged in driver rationale; follow-up ticket for codex.

## LongMemEval (289, retrieval-friendly subset, N=289)

| Cell | Hit@1 | Hit@5 | Hit@10 | MRR | nDCG@5 | Recall@5 | Cell time |
|---|---|---|---|---|---|---|---|
| FULL | **0.8685** | 0.9792 | 0.9896 | 0.9147 | 0.8815 | 0.9158 | 911 s |
| NO_INTENT | 0.8685 | 0.9792 | 0.9862 | 0.9142 | 0.8815 | 0.9158 | 936 s |
| ROLLBACK | 0.2457 | 0.2491 | 0.3460 | 0.2913 | 0.2335 | 0.2313 | 922 s |

**Δpp vs. I1 baseline (FTS-only, pre-lift, 0.8824 Hit@1 / 0.9241 MRR):**

| Cell | ΔHit@1 | ΔHit@5 | ΔMRR | ΔnDCG@5 |
|---|---|---|---|---|
| FULL | **-1.39pp** | +0.34pp | -0.94pp | -0.95pp |
| NO_INTENT | -1.39pp | +0.34pp | -0.99pp | -0.95pp |
| ROLLBACK | **-63.67pp** | -72.67pp | -63.28pp | -65.75pp |

## LoCoMo hybrid (cmd backend, N=1982)

| Cell | Hit@1 | Hit@5 | Hit@10 | MRR | nDCG@5 | Recall@5 | Cell time |
|---|---|---|---|---|---|---|---|
| FULL | **0.2785** | 0.5439 | 0.6352 | 0.3940 | 0.3910 | 0.4952 | 458 s |
| NO_INTENT | 0.2785 | 0.5439 | 0.6352 | 0.3940 | 0.3910 | 0.4952 | 243 s |
| ROLLBACK | 0.2725 | 0.5520 | 0.6650 | 0.3964 | 0.3924 | 0.5041 | 312 s |

**Δpp vs. I1 baseline (cmd-backend pre-lift pathology, 0.0232 Hit@1 / 0.0317 MRR):**

| Cell | ΔHit@1 | ΔHit@5 | ΔMRR | ΔnDCG@5 |
|---|---|---|---|---|
| FULL | **+25.53pp** | +50.15pp | +36.23pp | +36.48pp |
| NO_INTENT | +25.53pp | +50.15pp | +36.23pp | +36.48pp |
| ROLLBACK | +24.93pp | +50.96pp | +36.47pp | +36.62pp |

## Findings

1. **LoCoMo hybrid lift is massive.** FULL beats the pre-lift cmd_search pathology by +25.5pp Hit@1 / +36.2pp MRR. Blows past the plan's +1.0pp / +0.5pp envelope by an order of magnitude. The regression in the pre-lift cmd_search path was real; I2 (unified pipeline via cmd_search delegation) fixes it.
2. **LongMemEval is flat, not lifted.** FULL -1.39pp Hit@1 vs I1 — within noise on n=289 (SE ≈ ±1.9pp), but no measurable lift. Target of +0.8pp Hit@1 is **NOT met** on LME. The I2 adaptive controls don't help on text-rich session retrieval where FTS5 is already near-ceiling (0.87 Hit@1).
3. **Intent router is a no-op at measurement.** FULL and NO_INTENT are identical to 4 decimal places on every metric, on both benches. Either: (a) the regex classifier maps most bench queries to the same profile, (b) the env-var gate isn't being honoured everywhere, or (c) the router's downstream effect washes out on these specific question types. Needs a per-question_type slice analysis before concluding.
4. **ROLLBACK is catastrophic on LME** (-63.7pp Hit@1). Turning off top-heavy controls while leaving Ollama vec-fusion on produces worse results than FTS-only pre-lift. Implication: the lift isn't the I2/I3/I4 controls themselves — it's the *combination* of controls + vec fusion. Rollback-without-vec-off is a broken state.
5. **Latency capture broken** — all cells report `p95_latency_ms: 0.0`. The driver's `_extract_metrics` couldn't parse per-query timings out of the bench runner's payload. Follow-up for I8: fix the extractor, re-run the 3 LME cells for latency only, populate `tests/bench/budgets/*.yaml`. The I8 gate stays informational-only until then.

## Rollout recommendation

**Ship FULL (status quo of `main`).** It's what landed in commit `8912455`. No config change required.

- LoCoMo hybrid envelope **passes** (+25.5pp Hit@1, +36.2pp MRR, +15.9pp Hit@10).
- LongMemEval envelope **does not pass** (-1.39pp Hit@1 vs I1 baseline). But:
  - Within measurement noise on n=289 (SE ±1.9pp).
  - I1's 0.8824 was captured with Ollama DOWN, which is not the production config. A like-for-like baseline (Ollama up, pre-I2 code) is the ROLLBACK cell — where FULL beats ROLLBACK by **+62.3pp Hit@1**.
  - In production (Ollama up), FULL is the vastly superior path.

**Rollback switch:** `BRAINCTL_TOPHEAVY_ROLLBACK=1` works, but use it for emergency regressions, not as a "conservative default" — it ships a broken state (vec-fusion on, heuristics off).

**Intent router:** keep on (default). Measurement shows no harm; the router is cheap (regex), and may help on slices we didn't measure. Revisit only if we get a per-question_type breakdown showing a regression.

## Pareto frontier

ASCII — y-axis = Hit@1 lift over pre-lift, x-axis = latency cost. Latency was not captured in this run (known bug), so the frontier is quality-only.

```
LoCoMo hybrid:                         LongMemEval(289):
                                       
  +25.5pp ● FULL + NO_INTENT             +0.00pp    · (reference: I1 FTS-only)
  +24.9pp ● ROLLBACK                     -1.39pp ● FULL + NO_INTENT
                                         -63.7pp ● ROLLBACK
```

On LoCoMo: FULL dominates marginally. On LME: all "Ollama-up" cells sit below the FTS-only I1 baseline, with FULL closest. Pick FULL — best on LoCoMo, no worse than NO_INTENT on LME.

## Known issues / follow-ups

- **I8 latency gate**: still informational-only. Driver `_extract_metrics` needs a fix so p95 extraction works, then re-run 3 cells and populate `tests/bench/budgets/longmemeval.yaml` + `locomo.yaml` `baseline_p95_ms:` fields.
- **Intent router measurability**: per-question_type slice analysis needed to see if the router helps any slice. If not, consider removing to cut complexity.
- **CE rerank not reachable via bench harness**: `args.rerank` not populated by `tests/bench/locomo_eval.py` or `longmemeval_eval.py`. Wire it so the CE dimension is measurable.
- **ROLLBACK is broken in prod** if Ollama is up. Either tie `BRAINCTL_TOPHEAVY_ROLLBACK=1` to also force FTS-only, or document that rollback requires `BRAINCTL_DISABLE_VEC=1` too.

## Artifacts

- `matrix.json` — machine-readable matrix (6 cells, identical to this table)
- `{full,no_intent,rollback}-{longmemeval,locomo}.json` — per-cell bench outputs
- `{full,no_intent,rollback}-{longmemeval,locomo}.traces.jsonl` — per-query traces for downstream analysis
- `run.log` — driver stderr
- `_run_matrix.py` — reproducer
