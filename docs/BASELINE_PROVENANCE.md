# Legacy Baseline Provenance

The old BrainCTL and MemPalace values in `benchmarks/legacy_refs.py` are
historical references. They are not recomputed by this PR and are not consulted
by the runtime retrieval stack.

## Current Frozen Reference Source

The preferred source is a recoverable local legacy comparison bundle at:

```text
benchmarks/results/full_compare_20260418_033425/summary.json
```

If that bundle is present, the harness reads it first. If it is unavailable,
the fallback values are anchored to the checked-in comparison documentation and
the provided historical chart images:

- `README.md`
- `docs/COMPARISON.md`
- `membench_comparison.jpg`
- `longmemeval_comparison.jpg`
- `locomo_comparison.jpg`
- `coverage_status.jpg`
- `aggregate_primary_metrics.jpg`

## Commit / Config Status

The exact old-BrainCTL source commit that produced
`full_compare_20260418_033425` has not been recovered in this split PR. The
fallback reference block records the capture timestamp
`2026-04-18T04:48:04.326900+00:00`, dataset paths, benchmark modes, status, and
metric values where available.

Until a recoverable commit pin is found, generated comparison bundles must label
these values as `historical_reference`, not `measured_current`.

## Historical Benchmark Modes

- LongMemEval: `brain` and `cmd`, session-level retrieval, `top_k=10`.
- LoCoMo: `cmd_session`, session-level retrieval.
- MemBench FirstAgent: `cmd_turn`, turn-level retrieval.
- ConvoMem: coverage/status only unless the original slice is recovered.

## Runtime Boundary

The historical references are only used by:

- chart/table assembly in `benchmarks/compare_memory_engines.py`;
- fallback loading in `benchmarks/legacy_refs.py`;
- generated bundle README/table metadata.

They must not be imported by `src/agentmemory/**`, `bin/intent_classifier.py`,
or any runtime search/reranking module.
