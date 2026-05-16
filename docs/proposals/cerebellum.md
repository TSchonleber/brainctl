# Proposal: The Cerebellum Subsystem for brainctl

**Status:** Design proposal, not yet implemented. Third brain-inspired subsystem after thalamus and basal ganglia (both shipped 2026-05-15 evening).
**Authors:** Claude Opus 4.7 (synthesis) over an 8-agent cerebellum research swarm
**Date:** 2026-05-15
**Scope:** New subsystem. Sits alongside thalamus and BG; emits driver-class predictions and prediction-error signals into the existing buses.

---

## TL;DR

brainctl has a thalamus (typed routing + gating), a basal ganglia (action selection + outcome-driven Go/NoGo learning + hyperdirect holds), and a TD-error broadcast bus. **What it lacks is a forward-model layer** — a fast, cheap, universal predictor that emits "here is what I expect to happen next" *before* the action commits, and "here is how wrong the last prediction was" *as a continuous teaching signal independent of explicit reward*.

The cerebellum subsystem closes that gap. It mirrors the same shape as the previous two: schema + MCP tools + shadow hookpoints, additive, biologically grounded. It pairs with both existing subsystems — its prediction-error signal supplements BG's δ on the TD-error bus, its precision estimates modulate thalamic sector gain, and its high-PE events drive workspace ignition without going through the slow reward pathway.

**Key novelty over BG:** the cerebellum learns from *observation* (state prediction errors from any cortical partner) rather than from *outcome* (reward signal). The two error channels are complementary: BG learns "was that good?" cerebellum learns "did I predict correctly?" — and biology gives them distinct substrates because they need different decay times, different broadcast scopes, and different plasticity rules.

---

## Convergent principles from the 8-agent swarm

Across reports the same engineering-relevant ideas surfaced repeatedly:

1. **Forward model from efference copy.** Every action emitted produces a parallel predicted-consequence trace. The divergence between prediction and observation is the error signal that retunes the model. (Kawato MPFIM/MOSAIC; Wolpert/Miall/Kawato 1998.)

2. **Sparse expansion before any learned layer.** A fixed, non-learned random expansion of context features (Marr-Albus granule cells) lets a simple downstream linear readout discriminate situations that would otherwise collide. Pattern separation as a free preprocessing layer.

3. **Climbing-fiber-gated supervised plasticity.** A graded error signal (not binary) gates LTD at parallel-fiber → Purkinje synapses within a narrow time window — biology's eligibility trace. The error carries magnitude, not just sign.

4. **Closed loops per cortical partner.** Each cortical/cognitive area gets its own cerebellar partner zone via the corticopontocerebellar route. The same microcircuit is reused for motor, language, ToM, working memory. The cerebellum is *many copies of the same job*, one per partner.

5. **Driver-class output to thalamus, unlike BG's gate-class.** Cerebellar DCN → thalamus is large, perisomatic, AMPA-dominated content delivery. BG's GPi → thalamus is GABAergic disinhibition. Cerebellum says *what*; BG says *whether*. They converge on shared thalamic targets per Bostan & Strick 2018.

6. **Universal Cerebellar Transform.** Same operation across domains. The role is "oscillation dampener" / "predictor" — anything that needs prediction + error-driven adjustment. Damage produces dysmetria of *thought* (CCAS) exactly as it produces dysmetria of movement.

7. **Two-channel error signaling.** Vector-valued state-PE (cerebellum, local, supervised) is distinct from scalar reward-PE (BG, global, reinforcement). Conflating them collapses the very distinction that lets biological systems learn fast in many domains at once.

8. **Sub-second precise timing.** Eyeblink-conditioning analog: given a cue, emit a precisely-timed gating pulse to a downstream consumer *before* the predicted event arrives, with the delay tuned by error.

9. **Boundary-marker channel separate from content.** Climbing-fiber complex spikes act as discrete graded segmentation events that tag sequence boundaries and unexpected outcomes. Used both to teach the timer and to chunk sequences for the partner sequencer.

10. **Responsibility-weighted modular gating (MOSAIC).** Per-module prediction error gates which module learns and which module owns the current context. Modules specialize without explicit assignment.

11. **Failure-mode boundary conditions.**
    - **Dysmetria**: mis-scaled output from a frozen, un-recalibrated predictor.
    - **Intention tremor**: late-firing damping signal → overshoot → correction → oscillation.
    - **Feedback-only fallback**: when prediction degrades, naive systems silently fall back to slow closed-loop control without announcing it.
    - **Critical-period miscalibration**: downstream systems anchor to an immature predictor and inherit its miscalibration.

12. **Always emit confidence.** Cerebellar patients don't *know* their predictions are bad — they just act badly. An engineered analogue must announce "prediction unreliable" instead of silently degrading.

---

## What brainctl has today (recon from subagent 8)

| Prediction machinery | File:line | What it predicts | When it runs | Where error goes |
|---|---|---|---|---|
| `free_energy_check` | `mcp_tools_consolidation.py:24-90` | Knowledge-gap severity ((1-conf)×imp) | Pre-task (manual) | `agent_uncertainty_log` |
| `memory_calibration` | `mcp_tools_consolidation.py:966+` | Confidence/recall match (Brier) | Post-use (offline) | Output only |
| `infer_pretask` / `infer_gapfill` | `mcp_tools_reasoning.py:163+` | Low-confidence memories matching task | Pre-task (manual) | Gap logging; no reward loop |
| `world_predict` / `world_resolve` | `mcp_tools_world.py:270+` | Agent capability, project velocity | Post-task (manual) | Snapshots; no feedback to action selection |
| Reconsolidation lability | `mcp_tools_consolidation.py:24, 714` | Retrieval prediction error (cosine > 0.35) | On retrieval | Opens 20-min window; closed by timeout |
| Write-gate surprise | `lib/write_decision.py:154` | Novelty = 1 - max_similarity | At write time | W(m) tier; no global broadcast |
| BG TD-error bus | `bg_shadow.py:broadcast_td_error` | δ = utility + γV(s') − V(s) | On `outcome_annotate` | `bg_td_events`; consumes eligibility traces |

**Diagnosis:** every existing prediction is *slow* and *selective* — invoked by an explicit tool call. None emit a *fast*, *cheap*, *universal* "the last prediction was wrong by this much" signal that flows into every gating decision. brainctl runs feedback-only fallback by default and doesn't know it.

---

## Subsystem design

### Architectural placement

```
        AGENT REQUEST → tool call
                │
                ▼
   ┌────────────────────────────────────────────────────────────┐
   │  BASAL GANGLIA shadow consult (per-dispatch)                │
   │    • lookup action → striatal weights                        │
   │    • emit Go/NoGo signal                                     │
   │    • deposit eligibility trace                               │
   │    • check holds (hyperdirect)                               │
   └────────────────────────────┬────────────────────────────────┘
                                │
                                ▼
   ┌────────────────────────────────────────────────────────────┐
   │  CEREBELLUM shadow consult (per-dispatch + per-write)        │
   │    • lookup forward model for this (partner, action)         │
   │    • predict expected outcome / latency / next-state         │
   │    • return prediction + confidence                          │
   │    • deposit prediction-trace                                │
   └────────────────────────────┬────────────────────────────────┘
                                │
                                ▼
   ┌────────────────────────────────────────────────────────────┐
   │  THALAMUS shadow consult (per-write)                         │
   │    • sector-based suppression                                │
   │    • burst-arming on suppression × novelty                   │
   └────────────────────────────┬────────────────────────────────┘
                                │
                                ▼
        BRAINCTL SUBSTRATE (memory, events, beliefs, ...)
                                │  outcome / next-state
                                ▼
   ┌────────────────────────────────────────────────────────────┐
   │  CEREBELLUM error emission                                   │
   │    • δ_forward = observed − predicted (per partner)          │
   │    • update forward model weights (Marr-Albus LTD analog)    │
   │    • broadcast δ_forward onto BG TD-error bus               │
   │    • emit boundary-marker if |δ_forward| > burst threshold  │
   └────────────────────────────────────────────────────────────┘
```

The cerebellum consult is **between** BG and thalamus in the dispatch path: it observes the action that BG would approve, predicts what it will yield, and supplies that prediction + confidence both to downstream consumers (as a driver-class signal) and to itself (deposit a trace for later error attribution).

### Five cortical partners (mirroring the BG's 5 loops)

Per Strick: each cortical area gets its own cerebellar partner zone via the corticopontocerebellar loop. brainctl's analog:

| Partner | What it predicts |
|---|---|
| `motor_partner` | Outcomes of state-mutating tool calls (writes, registrations, broadcasts) |
| `oculomotor_partner` | Expected retrieval relevance for memory_search / push / agent_orient |
| `dlpfc_partner` | Plan-step completion timing + result shape for reasoning chains |
| `lofc_partner` | Expected utility / outcome class for value-laden actions |
| `acc_partner` | Conflict probability between competing beliefs or agents |

Same microcircuit, same learning rule, instantiated five times.

### The microcircuit (Marr-Albus simplified)

Each partner module holds:

1. **A fixed sparse expansion of context features.** Hash-based: `context_features = sparse_hash(scope, category, agent_id, project, recent_actions)`. No learned parameters; just a stable expansion that separates contexts.

2. **A learned linear readout per prediction target.** For each (partner, prediction_kind), a weight vector `w` indexed by expansion key. Prediction = `dot(w, context_features)`.

3. **A graded error signal on observation.** When the actual outcome arrives, compute `δ_forward = observed − predicted`. Update `w += lr × eligibility_trace × δ_forward`. LTD-analog: `w` shrinks toward zero for keys that didn't predict well.

4. **An eligibility trace per prediction.** Same shape as BG eligibility traces; decayed each consumption; pruned after TTL.

5. **A confidence estimate per prediction.** Running mean and variance of recent |δ_forward| per (partner, context). Confidence = 1 / (1 + variance). High confidence → trust the prediction; low → flag it.

### Prediction kinds (Phase 1 minimum)

- `success_probability` — probability the action will complete successfully (positive utility outcome).
- `expected_latency_ms` — predicted wall-time for the action to finish.
- `expected_outcome_class` — predicted outcome label (success / failure / partial).

Phase 2+ can add `predicted_next_state` (vector for retrieval-relevance estimation), `predicted_conflict` (probability of contradicting an existing belief), etc.

---

## Schema additions

New migration: `db/migrations/056_cerebellum.sql`. Additive.

```sql
-- Per-partner forward models. One row per (partner, prediction_kind).
-- The "model" is just a weighted linear readout; weights live in
-- cerebellum_weights keyed by hashed context features.
CREATE TABLE IF NOT EXISTS cerebellum_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner TEXT NOT NULL CHECK(partner IN (
      'motor_partner', 'oculomotor_partner', 'dlpfc_partner',
      'lofc_partner', 'acc_partner')),
    prediction_kind TEXT NOT NULL CHECK(prediction_kind IN (
      'success_probability', 'expected_latency_ms',
      'expected_outcome_class')),
    description TEXT,
    n_predictions INTEGER NOT NULL DEFAULT 0,
    mean_abs_error REAL NOT NULL DEFAULT 0.0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE (partner, prediction_kind)
);

-- Weighted readouts per (module, context_hash). The "linear" part of
-- the Marr-Albus architecture; the sparse expansion is computed
-- on the fly and not stored.
CREATE TABLE IF NOT EXISTS cerebellum_weights (
    module_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    n_updates INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    PRIMARY KEY (module_id, context_hash),
    FOREIGN KEY (module_id) REFERENCES cerebellum_modules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cb_weights_module ON cerebellum_weights(module_id);

-- Prediction log (sliding-window audit). Used by error-emission step
-- to compute δ_forward and to feed boundary-marker detection.
CREATE TABLE IF NOT EXISTS cerebellum_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    predicted_value REAL NOT NULL,
    confidence REAL NOT NULL,
    decision_event_id INTEGER,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    observed_value REAL,
    observed_at TEXT,
    delta_forward REAL,
    FOREIGN KEY (module_id) REFERENCES cerebellum_modules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cb_pred_recent ON cerebellum_predictions(fired_at);
CREATE INDEX IF NOT EXISTS idx_cb_pred_pending ON cerebellum_predictions(observed_at)
    WHERE observed_at IS NULL;

-- Eligibility traces — same shape as bg_eligibility_traces but for
-- predictions instead of dispatches. The two trace tables stay
-- separate so the cerebellum can decay/prune on its own clock.
CREATE TABLE IF NOT EXISTS cerebellum_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    prediction_id INTEGER,
    trace_strength REAL NOT NULL DEFAULT 1.0,
    decay_constant REAL NOT NULL DEFAULT 0.95,
    deposited_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    expires_at TEXT,
    FOREIGN KEY (module_id) REFERENCES cerebellum_modules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cb_traces_active ON cerebellum_traces(expires_at);

-- Boundary-marker log: complex-spike-analog discrete events fired when
-- |δ_forward| exceeds a threshold. Drives workspace ignition + serves
-- as segmentation signal for downstream chunkers.
CREATE TABLE IF NOT EXISTS cerebellum_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner TEXT NOT NULL,
    delta_forward REAL NOT NULL,
    context_hash TEXT NOT NULL,
    prediction_id INTEGER,
    salience REAL NOT NULL,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    consumed_by TEXT,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cb_boundaries_recent ON cerebellum_boundaries(fired_at);
```

---

## MCP tool surface (Phase 1)

Phase 1 ships 4 tools mirroring the BG/thalamus Phase-1 shape (read + minimal idempotent setup writes):

- **`cerebellum_status(partner?, top_n=10)`** — snapshot: per-module statistics (n_predictions, mean_abs_error), top weights by |weight|, recent prediction log, pending (un-observed) predictions, recent boundary markers.

- **`cerebellum_module_register(partner, prediction_kind, description?)`** — idempotent UPSERT into cerebellum_modules; auto-creates a new partner+kind combination.

- **`cerebellum_predict(partner, prediction_kind, context, agent_id?)`** — compute a forward prediction for a context. Returns `{predicted_value, confidence, prediction_id}`. Logs to cerebellum_predictions + deposits an eligibility trace. Phase 1 uses linear readout; never blocks the caller.

- **`cerebellum_observe(prediction_id, observed_value)`** — close the loop. Computes δ_forward, updates weights via the three-factor rule, decays the trace, fires a boundary marker if |δ_forward| > 0.5, broadcasts onto bg_td_events as δ_forward supplement.

Phase 2 will add `cerebellum_sweep_traces`, `cerebellum_boundary_consume`, and wire `cerebellum_predict` into the existing shadow consult at `mcp_server.py:3247` (after BG, before thalamus) so every dispatch gets a forward prediction without explicit tool calls.

---

## Hookpoints (Phase 2+ — not Phase 1)

Phase 1 is read + manual setup only. Phase 2 wires shadow consults at four sites:

1. **Pre-dispatch forward prediction.** `mcp_server.py:3247` — after BG shadow consult, before `_invoke_dispatch_fn`. Issues `cerebellum_predict` for each of the 3 prediction kinds; result attached to dispatch event for later closure.

2. **Post-dispatch observation closure.** Hook on `_invoke_dispatch_fn` return — record actual latency + outcome class, call `cerebellum_observe` to compute δ_forward.

3. **W(m) write gate prediction.** `_gates.py:33` — predict whether the write will be recalled (call it once per write, observe later via `memory_utility_rate` updates).

4. **`outcome_annotate` cascade.** When δ is broadcast onto bg_td_events, supplement with δ_forward from any matching pending predictions.

Phase 3 will use cerebellar confidence to modulate thalamic sector precision and to fire boundary markers into workspace_broadcasts.

---

## Three-factor learning rule (specialized for supervised PE)

Cerebellum updates use the same eligibility-trace + outcome pattern as BG, but with a *supervised* error rather than reward TD error:

```
For each active trace (module, context, trace_strength):
  δ_forward = observed − predicted
  weight[module, context] -= lr × trace_strength × δ_forward    (Marr-Albus LTD)
  confidence[module, context] = 1 / (1 + EMA(|δ_forward|))
```

Note the sign: cerebellar LTD *weakens* synapses that over-predicted, so the readout converges on the true target. Compare to BG which has *opponent* D1/D2 dynamics with opposite signs on the same δ. Different rules, different substrates, different jobs.

Default `lr = 0.05` (slower than BG's 0.1 since predictions are higher-fidelity / lower-noise than reward signals).

---

## Failure-mode design (from CCAS pathology)

| Failure | brainctl analog | Mitigation |
|---|---|---|
| **Dysmetria** (mis-scaled) | Weights drift; predictions systematically over/undershoot | `cerebellum_status` surfaces `mean_abs_error`; threshold → watchdog alert |
| **Intention tremor** (late damping → oscillation) | Predictions for damping/termination fire late; system overshoots and corrects | Tight bound on observation latency (must observe within trace TTL or the prediction is dropped); explicit "observation overdue" flag |
| **Feedback-only silent fallback** | Cerebellum unavailable but downstream code keeps querying | `cerebellum_predict` returns `{confidence: 0.0}` instead of erroring; callers must check confidence |
| **Critical-period miscalibration** | Other subsystems anchor to immature predictions | Phase 1 is shadow-only; Phase 3 enforcement requires `n_predictions > 100` per (module, context) before its prediction is read by downstream gating |

---

## Rollout plan

**Phase 1 — Schema + read-only inspection + 4 tools (this proposal):**
- Migration 056.
- `cerebellum_status`, `cerebellum_module_register`, `cerebellum_predict`, `cerebellum_observe`.
- Register the 5 cortical partner modules ×3 prediction kinds = 15 modules.

**Phase 2 — Shadow consults at dispatch + write gate:**
- Wire `cerebellum_predict` into the dispatch hookpoint; observe via tool-call result.
- Wire into W(m) for write-recall prediction.
- Supplement BG δ on TD-error bus with cerebellar δ_forward.

**Phase 3 — Thalamic precision modulation + workspace ignition:**
- `cerebellum_status` precision per (partner, context) read by `thalamus_salience` as a multiplier.
- Boundary markers fired into `workspace_broadcasts` with elevated salience.

**Phase 4 — Enforcement-flip:** prediction confidence gates dispatch; high-PE actions get re-routed; this is where the cerebellum stops being read-only. Requires Phase 2+3 shadow data showing sane behavior.

---

## Sources

Synthesized from 8 research subagents covering: cerebellar anatomy, forward models (Kawato MPFIM/MOSAIC), timing & coordination (Ivry, eyeblink conditioning, Graybiel comparison), cerebello-thalamic-cortical loops (Strick, Bostan & Strick 2018, Sherman driver/gate dichotomy), computational models (Marr 1969, Albus 1971, Ito LTD, Kawato MPFIM, Wolpert MOSAIC, Bouvier SGDEGE, Ha & Schmidhuber world models, Hafner Dreamer, Doya TD(λ) interpretation), cognitive cerebellum (Schmahmann CCAS, universal cerebellar transform, Lesage language, Van Overwalle ToM), failure modes (dysmetria, intention tremor, dysdiadochokinesia, CCAS, autism), and brainctl architecture recon. Full per-slice reports with citations preserved in the brainctl event log under task notifications from this session (2026-05-15 ~21:00 EDT swarm).
