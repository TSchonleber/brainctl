# Evaluation Integrity Notes

This note documents the safety boundary for the legacy comparison harness in
`benchmarks/`.

## Frozen Historical References

`benchmarks/legacy_refs.py` is a plotting/reference input only. It contains
frozen old BrainCTL and MemPalace scores recovered from the historical chart
pack and checked-in docs. It must not be imported by `agentmemory`, `cmd_search`,
`Brain.search`, candidate generation, reranking, or answer selection.

The harness treats those values as historical bars in regenerated comparison
charts. New BrainCTL scores are measured from the checked-out code and written
to ignored result bundles under `benchmarks/results/`.

## Leakage Boundary

The benchmark runners may read dataset questions, gold labels, and historical
reference values only inside evaluation code. Runtime retrieval code must not
receive:

- benchmark query IDs;
- gold session IDs or answer IDs;
- fixture keys;
- historical reference scores;
- exact query-string branches.

Generic metadata that exists at retrieval time, such as source document IDs,
session IDs, timestamps, entity names, and local row IDs, may be used by the
retrieval stack because those fields are part of the indexed corpus rather than
hidden answer labels.

## Baseline Provenance

The old BrainCTL and MemPalace bars are historical references, not reruns. When
the original legacy result bundle is not recoverable locally, the values are
anchored to the checked-in comparison docs and the provided chart images. The
bundle README must distinguish:

- `historical_reference`: frozen old BrainCTL or MemPalace values;
- `measured_current`: scores produced by the current checkout;
- `blocked`: benchmark families where the original data or loader is missing;
- `partial`: benchmark families where only the historical slice or subset is
  available.

If a recoverable old result bundle or commit pin is later found, update the
metadata in the generated bundle and keep the frozen reference value in
`legacy_refs.py` auditable with a source note.

## Reproducibility Metadata

Every generated comparison bundle should include:

- current git commit and branch;
- Python version and platform;
- benchmark runner command;
- dataset path or source;
- benchmark mode (`brain`, `cmd`, `cmd_session`, or `cmd_turn`);
- status (`full_same_machine`, `partial`, or `blocked`);
- generated chart/table file paths.

Generated outputs stay out of the PR diff. They are local artifacts used to
reproduce the comparison pack, not source files.
