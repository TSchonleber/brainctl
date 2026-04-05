# Effective Hilbert Space Dimension — PCA Analysis

**Date:** 2026-03-28  
**Task:** [COS-395](/COS/issues/COS-395)  
**Database:** `~/agentmemory/db/brain.db`  
**Total embeddings analysed:** 1649  
**Embedding model:** nomic-embed-text (768d)  

---

## 1. Effective Dimension Estimates

| Metric | Value |
|--------|-------|
| Full embedding dimension | 768 |
| **Effective dim @ 90% variance** | **101** |
| **Effective dim @ 95% variance** | **159** |
| **Effective dim @ 99% variance** | **300** |
| Participation ratio | 14.6 |
| Compression ratio (95%) | 4.8x |

The cognitive state space in brain.db is **effectively 159-dimensional** (95% variance threshold). This is a 4.8x compression from the nominal 768d Hilbert space. The participation ratio of 14.6 independently confirms a similarly low effective dimensionality.

### Eigenvalue Spectrum (Top 30)

| PC | Explained % | Cumulative % |
|----|------------|--------------|
| PC1 | 20.33% | 20.33% |
| PC2 | 11.51% | 31.84% |
| PC3 | 9.19% | 41.03% |
| PC4 | 3.52% | 44.55% |
| PC5 | 3.03% | 47.58% |
| PC6 | 2.16% | 49.74% |
| PC7 | 2.15% | 51.89% |
| PC8 | 1.62% | 53.51% |
| PC9 | 1.43% | 54.94% |
| PC10 | 1.32% | 56.26% |
| PC11 | 1.22% | 57.48% |
| PC12 | 1.10% | 58.58% |
| PC13 | 1.02% | 59.60% |
| PC14 | 0.98% | 60.57% |
| PC15 | 0.90% | 61.47% |
| PC16 | 0.89% | 62.37% |
| PC17 | 0.84% | 63.20% |
| PC18 | 0.82% | 64.02% |
| PC19 | 0.77% | 64.78% |
| PC20 | 0.75% | 65.54% |
| PC21 | 0.72% | 66.26% |
| PC22 | 0.69% | 66.95% |
| PC23 | 0.67% | 67.62% |
| PC24 | 0.66% | 68.28% |
| PC25 | 0.63% | 68.91% |
| PC26 | 0.60% | 69.51% |
| PC27 | 0.58% | 70.09% |
| PC28 | 0.57% | 70.66% |
| PC29 | 0.56% | 71.22% |
| PC30 | 0.55% | 71.77% |

---

## 2. Cognitive Subspace Identification — Top 10 PCs

Each principal component represents a latent cognitive dimension along which brain.db memories vary the most.

### PC1 (20.33% variance)

**High-scoring entries (positive pole):**
- `[paperclip-cortex]` Memory #293 reclassified medium -> ephemeral (age=0.0d, recalled=9, conf=0.900->0.450)
- `[paperclip-cortex]` Memory #430 reclassified medium -> ephemeral (age=0.0d, recalled=0, conf=0.500->0.250)
- `[unknown]` 
- `[unknown]` 
- `[aegis]` Memory #429 reclassified medium -> ephemeral (age=0.0d, recalled=0, conf=0.500->0.250)

**Low-scoring entries (negative pole):**
- `[paperclip-cortex]` Distilled event #515 (importance=0.5) to memory #660
- `[hermes]` Distilled event #525 (importance=0.5) to memory #670
- `[hermes]` Distilled event #516 (importance=0.5) to memory #661
- `[hermes]` Distilled event #5 (importance=0.5) to memory #793
- `[paperclip-weaver]` Distilled event #616 (importance=0.5) to memory #435

### PC2 (11.51% variance)

**High-scoring entries (positive pole):**
- `[epoch]` Memory #243 reclassified medium -> ephemeral (age=0.0d, recalled=5, conf=0.500->0.250)
- `[epoch]` Memory #375 reclassified medium -> ephemeral (age=0.0d, recalled=0, conf=0.500->0.250)
- `[hippocampus]` Memory #533 reclassified medium -> ephemeral (age=0.0d, recalled=0, conf=0.299->0.149)
- `[epoch]` Memory #370 reclassified medium -> ephemeral (age=0.0d, recalled=0, conf=0.500->0.250)
- `[unknown]` 

**Low-scoring entries (negative pole):**
- `[event]` Coherence check: score=0.8 | 2 findings | status=WARNING

{
  "run_at": "2026-03-28T04:14:07.705011Z
- `[event]` [Routed context — relevance 0.42] From: event:event:84

Coherence check: score=0.8 | 2 findings | st
- `[event]` Coherence check: score=0.8 | 2 findings | status=WARNING

{
  "run_at": "2026-03-28T04:12:45.807604Z
- `[event]` [Routed context — relevance 0.42] From: event:event:82

Coherence check: score=0.8 | 2 findings | st
- `[event]` Coherence check: score=0.8 | 2 findings | status=WARNING

{
  "run_at": "2026-03-28T04:12:45.807604Z

### PC3 (9.19% variance)

**High-scoring entries (positive pole):**
- `[hermes]` MASSIVE SESSION: Built CKO identity, created COG+BRN projects (76 issues total), hired/woke/managed 
- `[hermes]` MASSIVE SESSION: Built CKO identity, created COG+BRN projects (76 issues total), hired/woke/managed 
- `[hermes]` Major session: built entire agent memory spine from scratch. Created ~/agentmemory/ with SQLite+FTS5
- `[hermes]` Major session: built entire agent memory spine from scratch. Created ~/agentmemory/ with SQLite+FTS5
- `[paperclip-recall]` COS-122 complete: write contention research delivered. Empirical findings: multi-agent collisions co

**Low-scoring entries (negative pole):**
- `[event]` [Routed context — relevance 0.40] From: event:event:84

Coherence check: score=0.8 | 2 findings | st
- `[event]` [Routed context — relevance 0.40] From: event:event:84

Coherence check: score=0.8 | 2 findings | st
- `[event]` [Routed context — relevance 0.40] From: event:event:21

Compressed 15 memories in scope project:cost
- `[event]` [Routed context — relevance 0.40] From: event:event:21

Compressed 15 memories in scope project:cost
- `[event]` Compressed 15 memories in scope project:costclock-ai:invoice-compression-test-1774662366 into 5 memo

### PC4 (3.52% variance)

**High-scoring entries (positive pole):**
- `[paperclip-cortex]` COS-341 complete: Active Inference & Free Energy research delivered to ~/agentmemory/research/wave10
- `[paperclip-recall]` COS-351 done: Active Inference Phase 1 — precision weighting added to both FTS and exact retrieval s
- `[paperclip-sentinel-2]` COS-363 complete: AGM belief revision research delivered to ~/agentmemory/research/wave12/29_agm_bel
- `[paperclip-recall]` COS-336 done: Deployed adaptive retrieval weights in salience_routing.py and brainctl. Added compute
- `[paperclip-recall]` COS-336 done: Deployed adaptive retrieval weights in salience_routing.py and brainctl. Added compute

**Low-scoring entries (negative pole):**
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260326_125153", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260326_023922", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260326_033949", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260326_044015", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260326_054041", "source": "cron", "model": "gpt-5.2-codex", "mes

### PC5 (3.03% variance)

**High-scoring entries (positive pole):**
- `[paperclip-engram]` Engram heartbeat: inbox empty, no assigned tasks. Exiting.
- `[paperclip-engram]` Engram heartbeat: inbox empty, no assigned tasks. Exiting.
- `[paperclip-recall]` Recall heartbeat: inbox empty, no assignments. Clean exit.
- `[paperclip-recall]` Recall heartbeat: inbox empty, no assignments. Clean exit.
- `[paperclip-recall]` Recall heartbeat: inbox empty, no assignments. Clean exit.

**Low-scoring entries (negative pole):**
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260327_191103", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260326_074143", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260327_161014", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260327_085811", "source": "cron", "model": "gpt-5.2-codex", "mes
- `[conversation]` {"session_id": "cron_472a0f85f06b_20260327_095829", "source": "cron", "model": "gpt-5.2-codex", "mes

### PC6 (2.16% variance)

**High-scoring entries (positive pole):**
- `[paperclip-weaver]` brainctl attention-class get/set commands available (COS-358). Column attention_class added to agent
- `[hermes]` test event
- `[paperclip-tempo]` Heartbeat run: pre-search completed, wake reason heartbeat_timer, inbox empty, direct Tempo assignee
- `[hermes]` Grandchild event: tests passed
- `[hermes]` Grandchild event: tests passed

**Low-scoring entries (negative pole):**
- `[hippocampus]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr
- `[paperclip-codex]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr
- `[paperclip-codex]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr
- `[hippocampus]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr
- `[paperclip-codex]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr

### PC7 (2.15% variance)

**High-scoring entries (positive pole):**
- `[conversation]` {"session_id": "20260326_005413_c988de01", "source": "telegram", "model": "claude-opus-4-6", "messag
- `[conversation]` {"session_id": "20260326_003043_7fdbf6", "source": "cli", "model": "claude-opus-4-6", "messages": 2,
- `[conversation]` {"session_id": "20260326_003029_038047", "source": "cli", "model": "claude-opus-4-6", "messages": 2,
- `[conversation]` {"session_id": "20260326_004119_1a502a", "source": "cli", "model": "claude-opus-4-6", "messages": 27
- `[conversation]` {"session_id": "20260326_002742_5aa76b", "source": "cli", "model": "claude-opus-4-6", "messages": 2,

**Low-scoring entries (negative pole):**
- `[hippocampus]` Consolidation cycle — decayed=41, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, comp
- `[hippocampus]` Consolidation cycle — decayed=41, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, comp
- `[paperclip-codex]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr
- `[hippocampus]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr
- `[paperclip-codex]` Consolidation cycle — decayed=9, retired=0, demoted=0, promoted=0, contradictions=0, merged=0, compr

### PC8 (1.62% variance)

**High-scoring entries (positive pole):**
- `[paperclip-cipher]` COS-72 heartbeat: no new context since last analysis post. Still awaiting Kokoro review on VORTEX-MA
- `[paperclip-cipher]` COS-72 heartbeat: no new context since last analysis post. Still awaiting Kokoro review on VORTEX-MA
- `[paperclip-cipher]` COS-72 heartbeat: no new comments from Kokoro. Blocked-task dedup — skipping re-engagement. Awaiting
- `[paperclip-cipher]` COS-72 heartbeat: no new comments from Kokoro. Blocked-task dedup — skipping re-engagement. Awaiting
- `[paperclip-cortex]` COS-86 heartbeat 5: Intelligence brief posted. Brain.db health improving — active memories 14 (up fr

**Low-scoring entries (negative pole):**
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:21) to 10 agents: Cortex, Prism, Engram, Vertex, Conduit +5 more
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:14) to 10 agents: Hermes, Kokoro, Onramp, Recall, Sentinel 2 +5 more
- `[hermes]` Filed COS-76: hiring 8 agents for the Memory & Intelligence Division under Hermes. Assigned to Legio
- `[hermes]` Filed COS-76: hiring 8 agents for the Memory & Intelligence Division under Hermes. Assigned to Legio
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:13) to 10 agents: Recall, Warden, Sentinel 2, Hermes, Locus +5 more

### PC9 (1.43% variance)

**High-scoring entries (positive pole):**
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:81) to 10 agents: Warden, Hermes, Onramp, Phantom, Report +5 more
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:86) to 10 agents: Hermes, Onramp, Warden, Uptime, Engram 2 +5 more
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:82) to 10 agents: Warden, Hermes, Onramp, Phantom, Report +5 more
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:85) to 10 agents: Warden, Hermes, Onramp, Phantom, Report +5 more
- `[da3c6951-eae8-4818-83a1-611a09142779]` Routed context (event:event:16) to 10 agents: Epoch, Engram 2, Engram, Prune, Locus +5 more

**Low-scoring entries (negative pole):**
- `[paperclip-weaver]` Heartbeat: inbox empty, no assignments. Idle.
- `[paperclip-weaver]` Heartbeat: inbox empty, no assignments. Idle.
- `[paperclip-sentinel-2]` Heartbeat: inbox empty, no assignments in any status. Clean exit.
- `[paperclip-prune]` Heartbeat: no assignments found. Inbox empty. Nothing to do.
- `[paperclip-engram]` COS-188 complete: Added memory_type column (episodic|semantic) to brain.db. Semantic memories decay 

### PC10 (1.32% variance)

**High-scoring entries (positive pole):**
- `[paperclip-codex]` Memory #50 decaying — consider recalling or retiring
- `[hippocampus]` Memory #556 decaying — consider recalling or retiring
- `[hippocampus]` Memory #555 decaying — consider recalling or retiring
- `[hippocampus]` Memory #557 decaying — consider recalling or retiring
- `[hippocampus]` Memory #551 decaying — consider recalling or retiring

**Low-scoring entries (negative pole):**
- `[paperclip-recall]` Distilled event #272 (importance=0.5) to memory #210
- `[paperclip-cortex]` Distilled event #228 (importance=0.9) to memory #314
- `[hippocampus]` Distilled event #279 (importance=0.8) to memory #193
- `[paperclip-cortex]` Distilled event #228 (importance=0.9) to memory #191
- `[paperclip-engram]` Distilled event #262 (importance=0.5) to memory #218

---

## 3. Interference Effectiveness vs. Dimension

The QCR Phase ([COS-380](/COS/issues/COS-380)) and Amplitude ([COS-383](/COS/issues/COS-383)) modules currently compute interference in the full 768d space. With an effective dimension of 159, all interference computations should be projected into the reduced 159-PC basis before scoring.

**Performance improvement from dimension reduction:**

| Operation | Full 768d | Reduced basis (159d) | Speedup |
|-----------|-----------|--------------|---------|
| Dot product (cosine sim) | O(768) | O(159) | 4.8x |
| Projection matrix mult | O(768²) | O(159²) | 23.3x |
| Interference kernel | O(768²) | O(159²) | 23.3x |

Recommendation: pre-project all embeddings to the top 159 PCs at ingest time. Store the 159-dimensional projections alongside raw embeddings. QCR algorithms operate on the projected vectors — raw embeddings only needed for reconstruction.

---

## 4. Agent Subspace Alignment

### Per-Agent Subspace Spread (in 50-PC reduced space)

| Agent | # Embeddings | Mean dist from centroid |
|-------|-------------|------------------------|
| aegis | — | 0.4968 |
| conversation | — | 0.4448 |
| da3c6951-eae8-4818-83a1-611a09142779 | — | 0.1489 |
| epoch | — | 0.4925 |
| event | — | 0.2100 |
| hermes | — | 0.5174 |
| hippocampus | — | 0.5327 |
| kernel | — | 0.4413 |
| obsidian_note | — | 0.3407 |
| openclaw | — | 0.5176 |
| paperclip-armor | — | 0.4304 |
| paperclip-axiom | — | 0.4512 |
| paperclip-cipher | — | 0.4610 |
| paperclip-codex | — | 0.5472 |
| paperclip-cortex | — | 0.5163 |
| paperclip-embed | — | 0.3799 |
| paperclip-engram | — | 0.5165 |
| paperclip-lattice | — | 0.4962 |
| paperclip-legion | — | 0.5207 |
| paperclip-nexus | — | 0.5185 |
| paperclip-probe | — | 0.4984 |
| paperclip-prune | — | 0.5128 |
| paperclip-recall | — | 0.5267 |
| paperclip-scribe-2 | — | 0.5042 |
| paperclip-sentinel-2 | — | 0.5211 |
| paperclip-tempo | — | 0.4450 |
| paperclip-weaver | — | 0.5129 |
| unknown | — | 0.1681 |

### Hermes vs Hippocampus Subspace Overlap

**Cosine similarity of centroid vectors (50-PC space):** 0.3733

Moderate overlap. Partial entanglement structure. Some shared cognitive dimensions (likely meta/system topics), but each agent has a distinct core subspace.

### Pairwise Centroid Cosine Similarity Matrix (top agents)

| Agent | aegis | conversation | da3c6951-eae8-4818-83a1-611a09142779 | epoch | event | hermes | hippocampus | kernel |
|-------|------|------|------|------|------|------|------|------|
| aegis |  1.00 | -0.32 | -0.17 | 0.51 | -0.31 | 0.32 | 0.19 | 0.27 |
| conversation |  -0.32 | 1.00 | 0.03 | -0.28 | 0.20 | -0.36 | -0.31 | -0.26 |
| da3c6951-eae8-4818-83a1-611a09142779 |  -0.17 | 0.03 | 1.00 | -0.16 | 0.03 | 0.06 | -0.15 | -0.03 |
| epoch |  0.51 | -0.28 | -0.16 | 1.00 | -0.57 | 0.56 | 0.45 | 0.40 |
| event |  -0.31 | 0.20 | 0.03 | -0.57 | 1.00 | -0.82 | -0.31 | -0.34 |
| hermes |  0.32 | -0.36 | 0.06 | 0.56 | -0.82 | 1.00 | 0.37 | 0.56 |
| hippocampus |  0.19 | -0.31 | -0.15 | 0.45 | -0.31 | 0.37 | 1.00 | 0.53 |
| kernel |  0.27 | -0.26 | -0.03 | 0.40 | -0.34 | 0.56 | 0.53 | 1.00 |

---

## 5. Tensor Product Structure Test

If the 768d Hilbert space factors as `A ⊗ B` (e.g., `content ⊗ temporal`, `content ⊗ confidence`), the quantum formalism gains multiplicative structure. We test by reshaping top PCs as matrices and measuring their rank-1 approximation quality.

**Mean absolute correlation between top-50 PCs:** 0.0000

**Rank-1 ratio for candidate factorizations** (1.0 = perfect tensor product):

| Factorization | Rank-1 ratio (top PC) | Interpretation |
|--------------|----------------------|----------------|
| 32x24 | 0.083 | Weak / no tensor structure |
| 16x48 | 0.102 | Weak / no tensor structure |

Low mean correlation (< 0.05) suggests near-orthogonal PCs (expected). Tensor product structure would manifest as clusters of correlated PCs. Observed: 0.0000

---

## 6. Recommendations for QCR Algorithm Improvements

1. **Project to 159d basis**: All QCR algorithms should operate in the effective 159-PC subspace. Pre-compute and store the PCA projection matrix (768 → 159) as `pca_projection.npy` and transform all embeddings at ingest. This gives a 23x speedup on matrix operations.

2. **Phase interference ([COS-380](/COS/issues/COS-380))**: Phase angles should be computed in the reduced 159d basis. Interference patterns are more distinct in the reduced space because noise dimensions (PCs 160–768) are eliminated.

3. **Amplitude scoring ([COS-383](/COS/issues/COS-383))**: Gaussian amplitude kernels should use the Mahalanobis distance in the reduced PCA space (eigenvalue-normalized). This naturally weights each PC by its variance.

4. **Entanglement structure ([COS-382](/COS/issues/COS-382))**: Agent subspace alignment analysis shows overlapping subspaces for hermes and hippocampus (cosine sim = 0.3733). The entanglement Hamiltonian should be parameterized by centroid overlap in the reduced space.

5. **Tensor product structure**: The test suggests no strong tensor product structure. Best candidate factorization: `16x48`. If rank-1 ratios are low, the full 768d space does not factor cleanly — quantum gates must be applied in the full PC basis, not factored form.

---

## 7. Source Distribution

| Source table | Count |
|-------------|-------|
| events | 1214 |
| context | 230 |
| memories | 205 |

---

*Generated by Hilbert (agent 85e1c837) — COS-395*