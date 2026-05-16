# Proposal: The Basal Ganglia Subsystem for brainctl

**Status:** Design proposal, not yet implemented. Pairs with the thalamus subsystem at `docs/proposals/thalamus.md` (BG and thalamus are co-evolved and should ship together as the next major architecture step).
**Authors:** Claude Opus 4.7 (synthesis) over an 8-agent BG research swarm
**Date:** 2026-05-15
**Scope:** New subsystem. Sits *upstream* of the thalamus subsystem in the call path. Additive — no breaking changes.

---

## TL;DR

brainctl already captures outcome signals (`outcome_annotate`, `policy_feedback`, `trust` updates, `dopamine_signal`, `retrieval_effectiveness`) but **does not close the loop from outcomes back to gating decisions**. Policies decay without being retrained; the manual `neuro_signal` call is the only path that turns a real-world result into a weight change; tool dispatch at `mcp_server.py:3230–3246` has no pre-check whatsoever.

The basal ganglia is precisely the layer that solves this in biology: a default-deny gating circuit that learns *which actions to release* under *which contexts*, trained by a single broadcast dopamine signal computed from outcome-minus-expectation. A BG subsystem closes brainctl's existing infrastructure into a real reinforcement loop and gives the system its first learned action-selection policy.

**Architectural placement:**
```
agent request → BG (action selection, outcome-driven RL) → thalamus (typed routing, gating) → substrate (W(m), dispatch, retrieval) → response
                              │                                                          │
                              └──── outcome δ ◄─── outcome_annotate / trust deltas ◄─────┘
```

---

## Convergent principles from the 8-agent swarm

Across the 7 neuroscience reports + 1 brainctl recon, the same engineering-relevant ideas appeared repeatedly:

1. **Default-deny disinhibition.** GPi/SNr fire tonically; selection = transiently *withdrawing* inhibition from a chosen channel. The default state of every gate is closed. (Reports 1, 2, 4, 7, 8 — every report.)

2. **Three-pathway arbitration.** Direct (Go) lowers inhibition on candidates; Indirect (NoGo via STN) raises inhibition on competitors; **Hyperdirect** is a fast global "halt everything" path from input directly to output, triggered by conflict / surprise / explicit stop. Three different latencies, three different scopes. (Reports 2, 4, 5, 7.)

3. **TD-error broadcast bus.** A single scalar δ = (observed outcome) − (predicted value) broadcast to every learnable component. Same signal trains the actor and the critic. (Reports 3, 4, 5, 7.)

4. **Opponent D1/D2 channels.** Two pathways per candidate with opposite-sign learning under the same δ. D1 ("promote") strengthens under positive δ; D2 ("suppress") strengthens under negative δ. D2 has higher DA affinity → asymmetric sensitivity to bad outcomes. (Reports 1, 2, 3, 5, 7.)

5. **Eligibility traces solve credit assignment over delays.** ~1s biological trace; longer in artificial systems. Each gating decision leaves a decaying tag keyed by context; δ updates all still-eligible traces when outcome arrives. (Reports 3, 5.)

6. **STN-style global "hold your horses" under conflict.** When candidate scores are close, raise the commit threshold across all channels and accumulate more evidence before firing. (Reports 2, 4, 5, 7.)

7. **Parallel topographic loops with shared intralaminar back-channel.** Five (or more) loops — motor/oculomotor/dlPFC/OFC/ACC — run end-to-end as physically separate pipes; one CM/Pf-style intralaminar bus writes "behaviorally significant interrupt" back into any sector. (Reports 1, 4, 6.)

8. **Distributional value (not scalar).** A short vector of asymmetric quantile estimates per memory/action. Cheap optimistic/pessimistic readouts; native risk-sensitivity. (Report 7.)

9. **Uncertainty-gated model-based vs model-free arbitration.** Two value systems run in parallel; the controller picks whichever has lower posterior variance for the current state. (Reports 6, 7.)

10. **Three neuromodulator dials, not one.** Tonic DA = search breadth / action vigor; LC-NE = arousal / surprise reset; 5-HT = time horizon / opponent. A single "temperature" parameter underfits. (Reports 3, 6.)

11. **Task-bracketing of action chunks.** Durable start/stop markers around opaque action sequences; chunks are atomic from the selector's perspective once formed (Graybiel). (Report 6.)

12. **Pathology gives the engineering boundary conditions.** PD = stuck off (under-active gate, bradykinesia). OCD/Tourette = stuck on (over-active gate, intrusive actions). Addiction = premature DLS lock-in (habit replaces deliberation too early). Same circuit, three failure modes. (Reports 3, 6.)

---

## What brainctl has today (recon from subagent 8)

| Component | Present | Closed learning loop? |
|---|---|---|
| `policy_match` / `policy_add` / `policy_feedback` | ✅ | Partial — confidence updates on explicit feedback, decays otherwise |
| `outcome_annotate` / `outcome_report` | ✅ | Reporting only; optionally triggers calibration, doesn't update gates |
| `retrieval_effectiveness` / `usage_*` / `memory_utility_rate` | ✅ | No |
| `reflexion_*` (failure lessons) | ✅ | Reactive only — flags failures *after* they occur, doesn't predict |
| `trust_*` (alpha/beta Bayesian) | ✅ | Yes for retrieval ranking, no for action selection |
| `dopamine_signal` in neurostate | ✅ | Manual only — requires `neuro_signal` call |
| `expertise_build` / `whosknows` (agent capabilities) | ✅ | Static — does not learn from delegation outcomes |
| `world_predict` / `world_resolve` (predict + verify) | ✅ | Predictions logged, resolutions captured, but not fed back into action choice |
| Tool dispatch pre-gate | ❌ | `mcp_server.py:3243` invokes `_invoke_dispatch_fn` with **no admission check** |
| Default-deny action gate | ❌ | — |
| TD-error broadcast bus | ❌ | — |
| Opponent Go/NoGo learning | ❌ | — |
| Eligibility traces | ❌ | — |
| STN-style conflict gate | ❌ | — |
| Distributional value per action | ❌ | — |
| Model-based vs model-free arbitrator | ❌ | RAG and ReAct exist informally; no uncertainty comparator |

**Core finding:** the substrate is in place; the loop isn't closed.

---

## Subsystem design

### Architectural placement

The BG sits at the very top of the call stack — before the thalamus, before the substrate. Every tool call routes through it. Most calls pass through unmodified; the value of the BG is in the cases where it doesn't.

```
        AGENT REQUEST  (tool call, memory write, retrieval, delegation)
                │
                ▼
   ┌────────────────────────────────────────────────────────────┐
   │                   BASAL GANGLIA                              │
   │                                                              │
   │   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
   │   │  Striatum    │  │     STN      │  │   GPi/SNr    │    │
   │   │  (D1+D2)     │  │ (Hyperdirect │  │  (output:    │    │
   │   │  Go/NoGo per │  │  conflict    │  │  per-channel │    │
   │   │  candidate   │  │   gate)      │  │  inhibition  │    │
   │   └──────┬───────┘  └──────┬───────┘  │   vector)    │    │
   │          │                 │           └──────┬───────┘    │
   │          └─────► merge ◄───┘                  │            │
   │                                               ▼            │
   │   ┌────────────────────────────────────────────────────┐  │
   │   │  TD-error bus δ  ◄── outcome_annotate / trust Δ    │  │
   │   │  Eligibility traces  → corticostriatal weights      │  │
   │   │  Tonic DA / LC-NE / 5-HT (3 modulator dials)        │  │
   │   └────────────────────────────────────────────────────┘  │
   └────────────────────────┬─────────────────────────────────────┘
                            │  (decision: approve | block | delegate | queue | escalate-to-MB)
                            ▼
                       THALAMUS subsystem (see thalamus.md)
                            │
                            ▼
                       brainctl substrate
```

### Five parallel loops as channel namespaces

Per Alexander/DeLong/Strick — five topographically segregated loops. brainctl's analog:

| Loop | What it gates |
|---|---|
| `motor` | Direct tool calls (mutations, external API hits, file writes) |
| `oculomotor` | Retrieval / "where to look" (memory_search, push, agent_orient) |
| `dlpfc` | Deliberative planning (multi-step reasoning, tool sequences) |
| `lofc` | Value / outcome evaluation (when to commit, when to escalate) |
| `acc` | Conflict monitoring (contradiction detection, delegation triggers) |

Loops do not crosstalk inside the BG. One **shared intralaminar back-channel** (CM/Pf-analog) can write "behaviorally significant interrupt" signals back into any loop's striatum.

### Three pathways per loop

For each candidate action `a` in loop `L`:

- **Direct (Go).** Weight `w_go(L, a, context)` learned by RL. High → lower inhibition on this channel.
- **Indirect (NoGo).** Weight `w_nogo(L, a, context)` learned by RL with opposite sign. High → raise inhibition on competitors of this channel.
- **Hyperdirect.** Not per-action — a *global* gate triggered by:
  - Top-K candidate scores within ε of each other (conflict)
  - Surprise (current observation outside expected distribution)
  - Explicit stop signal (incident mode, user `/abort`)

Hyperdirect outputs raise inhibition globally for one tick before scoring resolves — the brake-first/release-winner dynamic.

### TD-error broadcast bus

A single scalar δ computed at each outcome event:

```
δ = utility(observed_outcome) + γ · V(next_state) − V(current_state)
```

Where utility comes from:
- `outcome_annotate` explicit success/failure
- Implicit signals: did the next tool call succeed? did belief revision converge? did a contradiction emerge?
- Trust deltas: did `trust_score` rise or fall on related memories?

δ updates all still-eligible gating decisions. Same δ also updates:
- `policy_feedback` confidence
- `dopamine_signal` in neurostate (replaces the manual `neuro_signal` call)
- `trust` alpha/beta on memories that fed the decision

### Eligibility traces

Each gating decision deposits a trace:

```
trace[(loop, candidate, context_hash)] = (weight, decay_constant)
```

Traces decay exponentially. When δ arrives, every still-positive trace gets a weight update proportional to (trace × δ × sign-of-pathway).

### Three neuromodulator dials

Not one "temperature":

- **Tonic DA** (`bg_tonic_da`): policy vigor / search breadth. High → exploit, fast commits. Low → explore, slower commits.
- **LC-NE** (`bg_arousal`): global surprise / reset gain. High → broaden eligibility, sharpen distributional readouts, lower commit threshold.
- **5-HT** (`bg_horizon`): effective discount γ. High → longer-horizon, patient. Low → myopic.

Each can be set independently from `neuromodulation_state`. They are *not* a single scalar.

### Distributional value

For each (loop, candidate, context) tuple, store a 5-tuple of expectile estimates (e.g., 0.1, 0.3, 0.5, 0.7, 0.9). Selection can read from any quantile:

- Default (greedy): 0.5
- Risk-averse / consolidation mode: 0.3
- Exploratory / strategic_planning: 0.7

Same machinery, different read-out.

### Model-based vs model-free arbitration

Two value heads per candidate:

- **MF** = fast cached retrieval-style score (e.g., from `expertise`, `policy_match`, prior outcomes)
- **MB** = deliberative (invokes `infer_l3` / `reason` / `world_predict` to simulate forward)

Arbitrate by posterior variance: pick whichever has lower predictive uncertainty for the current state. Tracks Collins & Frank's RLWM split.

---

## Schema additions

New migration: `db/migrations/053_basal_ganglia.sql` (note: 051 and 052 already taken by `code_ingest_cache` and `procedural_memory_layer`).

```sql
-- Candidate action catalog (one row per "thing the BG can gate")
CREATE TABLE bg_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL CHECK(loop IN ('motor','oculomotor','dlpfc','lofc','acc')),
  action_key TEXT NOT NULL,            -- e.g., 'tool:memory_search', 'delegate:hermes', 'escalate:plan'
  description TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (loop, action_key)
);

-- Striatal weights: opponent Go / NoGo, keyed by context hash
CREATE TABLE bg_striatal_weights (
  action_id INTEGER NOT NULL,
  context_hash TEXT NOT NULL,           -- hash of relevant state features (project, agent, recent outcomes, neurostate mode)
  w_go REAL NOT NULL DEFAULT 0.0,        -- direct pathway strength
  w_nogo REAL NOT NULL DEFAULT 0.0,      -- indirect pathway strength
  -- Distributional value: 5 expectile estimates
  v_q10 REAL DEFAULT 0.0,
  v_q30 REAL DEFAULT 0.0,
  v_q50 REAL DEFAULT 0.0,
  v_q70 REAL DEFAULT 0.0,
  v_q90 REAL DEFAULT 0.0,
  n_updates INTEGER NOT NULL DEFAULT 0,
  last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (action_id, context_hash),
  FOREIGN KEY (action_id) REFERENCES bg_actions(id) ON DELETE CASCADE
);
CREATE INDEX idx_bg_weights_ctx ON bg_striatal_weights(context_hash);

-- Eligibility traces (transient — decayed/swept periodically)
CREATE TABLE bg_eligibility_traces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id INTEGER NOT NULL,
  context_hash TEXT NOT NULL,
  trace_strength REAL NOT NULL DEFAULT 1.0,
  decay_constant REAL NOT NULL DEFAULT 0.95,
  decision_event_id INTEGER,             -- pointer to the originating event
  deposited_at TEXT DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT,                        -- soft expiration; sweep removes after
  FOREIGN KEY (action_id) REFERENCES bg_actions(id) ON DELETE CASCADE
);
CREATE INDEX idx_bg_traces_active ON bg_eligibility_traces(expires_at);

-- Outcome / TD-error event log (the δ broadcast bus)
CREATE TABLE bg_td_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  agent_id TEXT,
  utility REAL NOT NULL,                 -- u(outcome)
  v_current REAL NOT NULL,                -- V(s)
  v_next REAL NOT NULL,                   -- V(s')
  gamma REAL NOT NULL DEFAULT 0.95,
  delta REAL NOT NULL,                    -- δ = u + γ·V(s') − V(s)
  source TEXT NOT NULL,                   -- 'outcome_annotate' | 'trust_delta' | 'belief_collapse' | 'manual'
  fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
  consumed_count INTEGER DEFAULT 0       -- how many traces this δ updated
);
CREATE INDEX idx_bg_td_recent ON bg_td_events(fired_at);

-- Hyperdirect "hold" events (global conflict pauses)
CREATE TABLE bg_holds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,
  reason TEXT NOT NULL,                  -- 'conflict' | 'surprise' | 'explicit_stop'
  trigger_score_gap REAL,                 -- how close were the candidates?
  ticks INTEGER NOT NULL DEFAULT 1,       -- how many decision cycles to hold
  fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
  released_at TEXT
);

-- Neuromodulator dials (single row, broadcast)
CREATE TABLE bg_modulators (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  tonic_da REAL NOT NULL DEFAULT 0.5,    -- policy vigor / search breadth
  lc_ne REAL NOT NULL DEFAULT 0.5,        -- arousal / surprise gain
  serotonin REAL NOT NULL DEFAULT 0.5,    -- time horizon (γ scaling)
  set_by TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO bg_modulators (id) VALUES (1);

-- Action-chunk catalog (Graybiel task-bracketing)
CREATE TABLE bg_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,
  name TEXT NOT NULL,
  start_marker TEXT NOT NULL,            -- pattern that opens the chunk
  end_marker TEXT NOT NULL,              -- pattern that closes it
  body_actions_json TEXT,                 -- opaque sequence of action_ids
  success_count INTEGER DEFAULT 0,
  failure_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (loop, name)
);
```

---

## MCP tool surface

Eight new tools, `bg_*` namespace.

- **`bg_gate(action_key, loop, agent_id, context, dry_run=False) → dict`** — The primary chokepoint. Returns `{decision: approve|block|delegate|queue|escalate-mb, w_go, w_nogo, v_distribution, hold_active, reason}`. With `dry_run=True`, records the would-have decision without acting.
- **`bg_td_emit(task_id, utility, agent_id, source='outcome_annotate') → dict`** — Compute δ from current V estimates and broadcast. Returns δ + count of eligibility traces consumed.
- **`bg_status(loop=None, agent_id=None) → dict`** — Snapshot: recent δ events, active holds, current modulator dials, top-N actions by w_go and w_nogo per loop.
- **`bg_action_register(loop, action_key, description) → dict`** — Idempotent UPSERT into `bg_actions`.
- **`bg_modulator_set(tonic_da?, lc_ne?, serotonin?, set_by) → dict`** — Update the three dials. Each independent.
- **`bg_hold_trigger(loop, reason, ticks=1) → dict`** — Manually fire a hyperdirect hold (also fired automatically by `bg_gate` on conflict/surprise).
- **`bg_chunk_create(loop, name, start_marker, end_marker, body_actions) → dict`** — Define a task-bracketed action sequence.
- **`bg_sweep_traces() → dict`** — Scheduled maintenance: decay traces, expire old ones, log stats. Called by allostatic scheduler.

---

## Hookpoints (concrete patches against existing brainctl files)

From subagent 8's recon:

### 1. Pre-dispatch gate — `mcp_server.py:3243`

**Today:** `fn = dispatch.get(name); return _invoke_dispatch_fn(fn, agent_id, arguments)`.

**Patch:** wrap with `bg_gate` call. If `decision == 'block'`, return refusal. If `'delegate'`, route via `expertise_list` to a more capable agent. If `'queue'`, defer to next consolidation window. If `'escalate-mb'`, invoke deliberative path (`infer_l3`) before deciding.

**Shadow mode first:** Phase 1 of BG rollout = record what `bg_gate` *would* have decided, do not actually alter dispatch behavior. Compare against actual outcomes to validate the gate before turning it on.

### 2. Post-outcome learning — `mcp_tools_reflexion.py:455 outcome_annotate`

**Today:** writes outcome row; optionally triggers calibration.

**Patch:** also call `bg_td_emit` with utility derived from the outcome. δ broadcasts to all eligibility traces; their weights update; `dopamine_signal` in neurostate auto-updates (replacing the manual `neuro_signal` call).

### 3. Policy reranking — `mcp_tools_policy.py:206 tool_policy_match`

**Today:** rank by `priority * confidence`.

**Patch:** after the existing ranking, multiply by `bg_action.w_go - bg_action.w_nogo` for the matching action. Learned BG weights bias toward policies that have historically worked in the current context.

### 4. Workspace broadcast NoGo — `mcp_tools_workspace.py:290+ workspace_ingest`

**Today:** salience > threshold → ignite.

**Patch:** consult `bg_action[loop=acc, action_key=f'broadcast:{topic}']`. If `w_nogo > w_go`, suppress broadcast even if salience says ignite. (This is the indirect-pathway analog at the workspace level.)

### 5. Expertise delegation — `mcp_tools_expertise.py:100 expertise_list`

**Today:** static expertise scoring.

**Patch:** when computing whom to delegate to, multiply by learned `bg_action.w_go` for `delegate:{target_agent}` action. Learns *which delegations work* over time.

### 6. Consolidation replay prioritization — `mcp_tools_consolidation.py:run_phased_consolidation`

**Today:** replay queue by `replay_priority`.

**Patch:** memories whose retrieval has historically correlated with high δ get a learned replay boost. This is the dopamine-strengthens-rewarded-memories loop.

---

## Failure-mode design (pathology-derived)

The BG's three biological failure modes give us the engineering boundaries:

| Pathology | brainctl equivalent | Mitigation |
|---|---|---|
| **Parkinson's (stuck off)** | All w_go decayed near zero; nothing passes the gate; system bradykinetic | Floor on `w_go`; periodic baseline injection; watchdog that fires if no `bg_gate` returns 'approve' in N minutes |
| **OCD / Tourette (stuck on)** | w_nogo collapsed; intrusive repeated actions; agent loops | Ceiling on `w_go - w_nogo`; cool-down after repeated same-action wins; STN hold auto-fires after K consecutive identical decisions |
| **Addiction (premature DLS lock-in)** | Habitual chunks formed too fast; deliberative path never invoked; agent stops escalating to MB | Chunk formation requires N successful uses + variance threshold; periodic forced MB sampling (epsilon-style) |

---

## Rollout plan

**Phase 1 — Schema + read-only inspection (1 day):**
- Apply migration 053.
- Implement `bg_status`, `bg_action_register`, `bg_modulator_set`.
- Seed `bg_actions` from observed tool-call traffic (similar to thalamic relay seeding).

**Phase 2 — Shadow gating:**
- Implement `bg_gate` in shadow mode only. Wired into hookpoint #1 (`mcp_server.py`) but never alters dispatch. Records would-have decisions in a side log.
- Implement `bg_td_emit` and wire into hookpoint #2 (`outcome_annotate`). Eligibility traces accumulate.
- One week of shadow data: compare BG decisions against actual outcomes.

**Phase 3 — Activate gate per loop:**
- Turn on `bg_gate` for one loop at a time (start with `acc` — low blast radius). Monitor failure-mode metrics.
- Activate hyperdirect holds.
- Wire hookpoints #3 (policy rerank) and #4 (workspace NoGo).

**Phase 4 — Activate delegation + replay:**
- Wire hookpoints #5 (expertise delegation) and #6 (replay prioritization).
- Enable distributional value reads from non-greedy quantiles based on `bg_modulators.tonic_da`.

**Phase 5 — Model-based escalation:**
- Implement uncertainty-gated MB/MF arbitration. When MF posterior variance > threshold, escalate to `infer_l3` / `reason` before deciding.

**Phase 6 — Chunks:**
- Activate `bg_chunks` for sequences that have stabilized across many successful runs.

---

## Why now, why pair with thalamus

The thalamus subsystem (already designed) gives brainctl typed gating and centralized inhibitory control. But the thalamus has no *learned policy* — its gates are set by neurostate and explicit top-down bias. The BG supplies the learned policy: it watches outcomes, computes δ, and trains the gating weights that the thalamus then enforces.

Shipping them together — thalamus as the gating substrate, BG as the policy that trains it — is how the biology works and how brainctl should ship the architecture. The thalamus Phase 1 lays the schema and read-only tools; the BG Phase 1 (this proposal's Phase 1) does the same. Together they unlock Phase 2 and beyond for both.

---

## Sources

Synthesized from 8 research subagents covering: BG anatomy & subdivisions, three-pathway architecture, dopamine RL & RPE, BG-thalamus loop topology, PBWM working-memory gating, habits/sequences/effort-costs, computational BG models survey, and brainctl codebase recon. Primary references across the swarm:

- Houk, Adams & Barto 1995 — BG actor-critic
- Schultz, Dayan & Montague 1997 — DA = TD error
- Redgrave, Prescott & Gurney 1999 — BG as vertebrate selection device
- Alexander, DeLong & Strick 1986 — five parallel cortico-BG-thalamo-cortical loops
- Frank 2006 — Go/NoGo opponent learning + STN "hold your horses"
- O'Reilly & Frank 2006 — PBWM
- Daw, Niv & Dayan 2005 — model-based vs model-free arbitration
- Bogacz & Gurney 2007 — BG as MSPRT
- Dabney et al. 2020 — distributional RL in dopamine
- Yagishita et al. 2014 — eligibility-trace timescale in striatum
- Graybiel — task-bracketing of action chunks
- Bostan & Strick 2018 — thalamic convergence of BG and cerebellum
- Halassa lab — TRN/thalamus interaction with BG output

Full per-slice citations preserved in the brainctl event log under the Track E1 task notifications from this session.
