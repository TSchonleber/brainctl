# Implementation Backlog
**Maintained by:** Blueprint (ff1cd3c2) — Implementation Architect
**Last updated:** 2026-04-02 (cycle 5)
**Source:** Scan of COG + QCR research, waves 1-12, QCR-W2 empirical results, health dashboard analysis

---

## Status Key
- `in_review` — spec filed, awaiting Chief assignment
- `done` — implemented
- `partial` — Phase 1 done, Phase 2 pending

---

## Active Implementation Specs (in_review — awaiting Chief assignment)

| Spec | BRN Issue | Priority | Source Research | Description |
|------|-----------|----------|-----------------|-------------|
| BP01 | [COS-401](/COS/issues/COS-401) | high | [COS-398](/COS/issues/COS-398) | Apply Unified Quantum Schema Migration to brain.db |
| BP02 | [COS-404](/COS/issues/COS-404) | high | [COS-383](/COS/issues/COS-383), [COS-392](/COS/issues/COS-392) | Integrate Phase-Aware Quantum Amplitude Scorer into brainctl search |
| BP03 | [COS-402](/COS/issues/COS-402) | high | [COS-368](/COS/issues/COS-368) | Write Decision Model — W(m) worthiness gate for brainctl push |
| BP04 | [COS-403](/COS/issues/COS-403) | medium | [COS-364](/COS/issues/COS-364) | Prospective Memory — memory_triggers table + brainctl trigger commands |
| BP05 | [COS-406](/COS/issues/COS-406) | medium | [COS-363](/COS/issues/COS-363) | AGM Belief Revision — brainctl resolve-conflict command |
| BP06 | [COS-405](/COS/issues/COS-405) | medium | [COS-365](/COS/issues/COS-365) | Outcome-Linked Memory Evaluation — access_log annotation + Brier score |
| BP07 | [COS-410](/COS/issues/COS-410) | high | [COS-367](/COS/issues/COS-367) | Proactive Interference Index — PII formula + recency gate at memory write |
| BP-OSSPkg | [COS-409](/COS/issues/COS-409) | high | internal | Open-Source Packaging — AgentMemory as clean installable memory layer |
| BP08 | [COS-411](/COS/issues/COS-411) | medium | [COS-394](/COS/issues/COS-394) | Belief Collapse Mechanics — `belief_collapse_events` table + 4 trigger hooks + `collapse_mechanics.py` |
| BP09 | [COS-412](/COS/issues/COS-412) | medium | [COS-395](/COS/issues/COS-395) | PCA Dimensionality Reduction — reproject embeddings 768d→159d at ingest, Mahalanobis amplitude scorer |
| BP10 | [COS-413](/COS/issues/COS-413) | high | [COS-202](/COS/issues/COS-202), [COS-323](/COS/issues/COS-323) | Fix Health Dashboard Coverage Metric — exclude policy-noise events from denominator, update SLO thresholds |

**Dependency ordering:** BP01 must be done before BP02 and BP08. BP09 and BP10 are independent (can start now).

---

## Research Completed — Implementation Pending (no spec yet)

These research items are done but have not been translated to BRN specs yet.

| Research Issue | Title | Notes |
|----------------|-------|-------|
| [COS-396](/COS/issues/COS-396) | Empirical Decoherence Validation | Power-law decay confirmed. Decoherence_rate hook into consolidation — **depends on BP01** (quantum schema) before this can be filed. Defer until BP01 done. |
| [COS-397](/COS/issues/COS-397) | Quantum Walk on Knowledge Graph | Deferred — sparse graph (4,718 edges) limits speedup. Re-evaluate when edge count > 20K. |

### No Spec — Research Concludes No Implementation Warranted

| Research Issue | Title | Decision |
|----------------|-------|----------|
| [COS-393](/COS/issues/COS-393) | Bell Test — Empirical detection of entangled agent beliefs | **No implementation.** Bell test found CHSH S ≤ 2.0 for all agent pairs — classical-only correlations. Quantum entanglement infrastructure not warranted. hermes↔openclaw (S=1.9995) flagged for re-test at higher agent density. |

### Items Previously Listed as Pending — Now Confirmed DONE

Cycle 2 verified these are fully implemented in code — no spec needed:

| Research Issue | Verified Implemented |
|----------------|---------------------|
| [COS-352](/COS/issues/COS-352) | `brainctl search --mmr --explore`, Gini inversion fixed in salience_routing.py:187, recall_gini in health output |
| [COS-362](/COS/issues/COS-362) | `brainctl budget`, `brainctl search --budget`, `--min-salience` — all live |
| [COS-354](/COS/issues/COS-354) | Bayesian Phase 2 fully wired: recall→α++ (brainctl:2733), contradiction→β++ (hippocampus:2080), decay→β+= rate*days (hippocampus:981) |
| [COS-357](/COS/issues/COS-357) | `brainctl expertise build/show/list/update`, source-weighting at memory write time (brainctl:251), `_get_source_weight()` in search scoring |
| [COS-359](/COS/issues/COS-359) | `brainctl infer-pretask`, `brainctl infer-gapfill`, `brainctl search --epistemic` — all implemented |

---

## Already Implemented (reference)

Key implementations completed before Blueprint was hired:

| Issue | What was built |
|-------|----------------|
| COS-350, COS-351, COS-353, COS-355, COS-356, COS-358 | Wave 10 Phase 1 implementations (active inference, attention economics, Bayesian Brain, social epistemology) |
| COS-299, COS-300-305, COS-314-322 | Wave 7-9 implementations (trust scores, dream pass, neuromodulation, world model, theory of mind) |
| COS-392 | Phase inference: all 150 memories have confidence_phase populated |

---

## Notes
- Chief (Terrence) reviewing on return. All BRN issues created with `in_review` status, unassigned.
- BP01 is prerequisite for BP02 and BP08. BP09 is independent — can be picked up immediately by Synapse/Tensor.
- W(m) write gate (BP03) is highest ROI: 82% noise reduction with no high-value recall loss.
- COS-393 (Bell Test): no quantum entanglement detected in brain.db under current scheme. No infrastructure needed.
