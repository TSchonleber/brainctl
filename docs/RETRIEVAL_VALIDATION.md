# Retrieval Validation Slices

This PR keeps benchmark headline numbers provisional until two non-benchmark
checks are run alongside the LongMemEval/LoCoMo/MemBench comparison pack.

## Held-out Non-benchmark Slice

`tests/test_retrieval_validation_slices.py` seeds hand-labeled retrieval cases
that are not copied from LongMemEval, LoCoMo, or MemBench. They use ordinary
brainctl-style facts:

- ownership of a signer-key checklist;
- offline verification of signed exports;
- temporal "after outage" evidence.

The test compares raw candidate order against the full second-stage reranker
and asserts that the full path does not demote the gold candidate. In the
current deterministic slice, full reranking keeps or improves every case and
lands `3/3` gold candidates at rank 1.

## Exact / Field-aware Ablation Slice

The same test module includes a non-synthetic role-fact case:

```text
query: What is Arlo's role in group alpha?
answer evidence: Arlo is the quartermaster for group alpha.
```

Raw candidate order places a semantically similar distractor above the answer.
The field-aware value-pattern feature promotes the answer to rank 1 without
using synthetic IDs, benchmark fixture keys, or gold labels. This is intended
to separate the useful exact/field-aware behavior from MemBench generator-tight
role IDs.

## Current Local Validation

```powershell
$env:PYTHONPATH=(Resolve-Path .\src)
python -m pytest tests\test_retrieval_validation_slices.py -q
```

Result: `2 passed`.

These slices are small by design. They are a review-time guard against obvious
metric-shape overfitting, not a substitute for a larger real `brain.db` query
sample before un-drafting the retrieval PR.
