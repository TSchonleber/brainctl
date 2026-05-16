# Proposal: The Thalamus Subsystem for brainctl

**Status:** Design proposal, not yet implemented
**Authors:** Claude Opus 4.7 (synthesis) over an 8-agent research swarm
**Date:** 2026-05-15
**Scope:** New subsystem; additive — no breaking changes to existing APIs.

---

## TL;DR

brainctl already has most of a brain: hippocampus (consolidation), neuromodulation, replay queue, write gate, workspace broadcasts, theory of mind, belief system, allostatic scheduling. **What it is missing is a thalamus** — the single bidirectional gating layer that sits between sources and consumers, types every edge, suppresses competing channels under top-down control, and globally switches operating mode.

Today the 200+ MCP tools are an undifferentiated bus: any caller can hit any tool, search returns what it returns, neurostate is *read* but not *wired into* retrieval or admission decisions. A thalamus subsystem turns the bus into a *switchboard*.

This proposal specifies the design — schema, MCP tools, hookpoints — derived from the neuroscience of the thalamus.

---

## Convergent principles from neuroscience

Across 7 independent research subagents (anatomy, gating modes, TRN, thalamocortical loops, pulvinar, consciousness/arousal, computational models), the same engineering-relevant ideas surfaced repeatedly. The proposal is built on the principles that appeared in ≥ 3 reports:

1. **Driver vs modulator edge typing.** Every input to a thalamic relay cell is one of two types: a *driver* (sparse, content-bearing, dominant) or a *modulator* (abundant, gain/context-setting, adjustable). A relay refuses to compute without a driver. (Sherman & Guillery — the load-bearing wiring principle.)

2. **First-order vs higher-order transport.** First-order = ingress from a non-cortical source-of-truth (sensors). Higher-order = cortex-to-cortex hop *through* the thalamus, carrying a copy of another module's layer-5 output (prediction/efference). Same node interface, different upstream contract. Direct cortico-cortical is paralleled (and sometimes replaced) by a trans-thalamic route.

3. **TRN: a separate inhibitory control plane.** Every thalamocortical and corticothalamic axon drops a collateral through TRN. TRN sees all traffic both directions, projects *only* back into thalamus (never to cortex), and suppresses channels selectively. **Attention is suppression of distractors, not amplification of targets** (Halassa lab — revision of Crick's searchlight). TRN is sectorized by modality.

4. **Two firing modes per channel.** Tonic = faithful linear relay. Burst = sparse high-confidence "something changed" event. Mode is set by *state* (membrane history), not by stimulus identity. **Suppression arms burstability** rather than muting it — a long-quiet channel that fires gets amplified, not ignored.

5. **CT >> TC asymmetry: feedback is ~10× the volume of feedforward, and almost entirely modulatory.** Cortex tells thalamus what to *expect/prioritize* far more than thalamus tells cortex what just *happened*. Predictive coding lens: forward = residuals, backward = expectations.

6. **Salience map as a first-class data structure.** Pulvinar integrates bottom-up drive (novelty/intensity) and top-down weights (task relevance from PFC) into a per-channel priority score that downstream consumers' gain scales with.

7. **Phase/timing alignment between modules.** Pulvinar's primary job is making sender and receiver mutually coherent (alpha/beta phase) so the message lands — not forwarding payloads. Cortico-cortical communication is regulated through *synchrony*, not just routing.

8. **One global state knob with diffuse broadcast.** Intralaminar nuclei fan out to all cortex; one brainstem neuromodulator change shifts every cortical column simultaneously. State = single low-dimensional variable, broadcast, not addressed.

9. **Loops, not nodes, hold state.** Inhibiting MD thalamus collapses PFC delay-period activity; inhibiting PFC collapses MD. Working memory is the *cycling read-write* between cortex and thalamus.

10. **Failure-mode awareness.** Hyperactive gate → autism-like over-filtering, rigidity. Hypoactive gate → schizophrenia-like leakage, broken consolidation. A tuned operating point matters more than "more inhibition is better."

---

## What brainctl has today (recon from subagent 8)

| Subsystem | Present | Wired into runtime gating? |
|---|---|---|
| Neurostate (org-level arousal, focus, mode) | yes | **no** — read but not used to gate retrieval |
| Attention snapshot (focus score from query history) | yes | **no** — computed but doesn't gate current ops |
| W(m) write gate | yes | yes (admission) |
| Replay queue, consolidation | yes | yes (offline) |
| Reconsolidation lability windows (20 min) | yes | yes (fixed threshold 0.35) |
| Workspace broadcasts (ignition) | yes | partially (fixed salience threshold, no org_state coupling) |
| Trust system (alpha/beta Bayesian) | yes | yes (in scoring) |
| Belief / ToM / world model | yes | yes (per tool) |
| Driver/modulator edge typing | **no** | — |
| Centralized inhibitory sidecar (TRN-analog) | **no** | — |
| Burst-mode sparse novelty emission | **no** | — |
| Trans-thalamic inter-agent routing | **no** (agents share via flat memory; no relay typing) | — |
| Salience map as a structure | **no** (salience is a column, not an integrated map) | — |
| Global mode broadcast that *propagates* | **no** (neurostate exists but doesn't fan out) | — |

The gap is consistent: the substrate exists, the *gating layer* that uses it does not.

---

## Subsystem design

### Architectural placement

```
                   ┌─────────────────────────────────────────────┐
                   │             AGENT / "CORTEX"                 │
                   │  (Claude, Hermes, OpenClaw, user, etc.)     │
                   └───────────────────┬─────────────────────────┘
                                       │  CT (modulator: top-down
                                       │  task, attention, intent)
                                       │  ────────────────►
                                       │  TC (driver: retrieved
                                       │  content, burst events)
                                       │  ◄────────────────
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │                      THALAMUS                             │
        │                                                            │
        │   ┌─────────────────┐   ┌────────────────────────────┐  │
        │   │ Relay catalog   │◄──┤  TRN gate (sidecar)         │  │
        │   │ (typed edges)   │   │  - per-channel suppression  │  │
        │   │ FO + HO         │   │  - sectorized               │  │
        │   └────────┬────────┘   │  - top-down + bottom-up     │  │
        │            │             └────────────┬────────────────┘  │
        │            │                          │                    │
        │   ┌────────▼──────────┐    ┌─────────▼──────────────┐    │
        │   │ Salience map      │    │  Mode controller        │    │
        │   │ (integrated       │    │  (one row, broadcast)   │    │
        │   │  per-candidate    │    └─────────────────────────┘    │
        │   │  priority)        │                                    │
        │   └───────────────────┘                                    │
        └──────────────┬─────────────────────────────────────────────┘
                       │     drivers (content)
                       ▼
    ┌──────────────────────────────────────────────────────────────┐
    │ Existing brainctl substrate: memories, events, entities,      │
    │ beliefs, workspace, consolidation, neuromodulation_state      │
    └──────────────────────────────────────────────────────────────┘
```

The thalamus sits between agents and the substrate. It is the **single legitimate chokepoint** for retrieval (TC) and contextual writes (CT). Agents may continue to call substrate tools directly during migration; the thalamus is opt-in initially, then becomes default for `tool_search`, `tool_push`, and `tool_agent_orient`.

### Sectors (modality grouping for TRN-style competition)

TRN is sectorized topographically. brainctl's analog:

| Sector | What it gates | Examples |
|---|---|---|
| `sensory_external` | First-order ingress from outside the system | user prompts, webhook payloads, file ingest, log streams |
| `agent_efferent` | Higher-order: an agent's own output being relayed to peers | Hermes broadcasts, Claude tool calls, OpenClaw routine results |
| `memory_recall` | Retrieved memories being delivered back to a consumer | `memory_search`, `push`, `agent_orient` results |
| `belief` | Belief writes / belief broadcast | `belief_set`, ToM updates, conflict resolutions |
| `consolidation` | Offline-mode traffic | replay queue items, reconsolidation candidates |
| `pii_sensitive` | Memories tagged with secrets/PII; always biased toward suppression unless explicitly attended | wallet addresses, credentials marked sensitive |

Cross-sector competition rule: top-down bias from the active agent's task context selects which sectors are *suppressed*. Unattended sectors lose by relative gain.

### Tonic vs Burst modes per channel

- **Tonic mode (default):** faithful, ranked relay of items through the existing scoring pipeline. What `memory_search` does today.
- **Burst mode:** triggered when a *suppressed* channel observes high prediction-error (high novelty in a quiet sector). Emits a single sparse event with elevated salience, regardless of normal score — a "wake-up call" to cortex. Burst events surface even when the agent isn't currently asking that sector.

Burst-mode emission is the implementation of "**suppression arms burstability**": when a sector has been quiet/de-prioritized for a while (low bottom-up drive, high suppression), the gate flips that channel's `armed_for_burst` flag. The next high-PE write to that sector fires a burst event into the workspace broadcast layer with elevated salience even though normal scoring would have dropped it.

### Global mode (one row, broadcast)

A single row in `thalamic_mode` controls system-wide operating regime, driven by `neuromodulation_state.org_state` but expressed in per-channel-relevant terms:

| Mode | Trigger | Effect |
|---|---|---|
| `wake_focused` | org_state in `focused_work`, `sprint` | tight similarity cutoffs, sector suppression active, burst rare |
| `wake_exploratory` | org_state in `strategic_planning` | broader cutoffs, lower suppression, more cross-sector mixing |
| `drowsy` | org_state in `normal` low-arousal | mid cutoffs, burst eligibility rises (good for novelty detection) |
| `consolidate` | scheduled / `consolidation_run` | tonic suppressed; spindle-analog: deterministic offline replay only |
| `offline` | system idle | gate fully closed for external sectors; consolidation + dreams only |

Mode change is one write; every downstream gating decision reads from this single source. Cortex doesn't need to enumerate channels to reconfigure them.

---

## Schema additions

All additive. New migration: `db/migrations/050_thalamus.sql`.

```sql
-- 050_thalamus.sql

-- Typed relay catalog: every "edge" between a source and a consumer
CREATE TABLE thalamic_relays (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL UNIQUE,
  sector TEXT NOT NULL,
  driver_source TEXT NOT NULL,
  modulator_sources_json TEXT,  -- JSON array of source identifiers
  target TEXT NOT NULL,
  transport TEXT NOT NULL CHECK(transport IN ('first_order', 'higher_order')),
  default_mode TEXT NOT NULL DEFAULT 'tonic' CHECK(default_mode IN ('tonic', 'burst')),
  default_gain REAL NOT NULL DEFAULT 1.0,
  topographic_key TEXT,           -- preserves addressing (project / agent / entity)
  efference_copy_target TEXT,     -- mirror destination (e.g., event_add for audit)
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT
);
CREATE INDEX idx_relays_sector ON thalamic_relays(sector);
CREATE INDEX idx_relays_target ON thalamic_relays(target);

-- TRN sidecar: per-channel suppression with top-down + bottom-up inputs
CREATE TABLE thalamic_gate (
  channel_id TEXT PRIMARY KEY,
  suppression REAL NOT NULL DEFAULT 0.0,      -- 0=open, 1=fully suppressed
  topdown_bias REAL NOT NULL DEFAULT 0.0,     -- from agent task/attention_snapshot
  bottomup_drive REAL NOT NULL DEFAULT 0.0,   -- traffic-driven
  sector TEXT NOT NULL,
  armed_for_burst INTEGER NOT NULL DEFAULT 0,  -- de-inactivation eligibility
  last_burst_at TEXT,
  bias_source TEXT,                            -- agent_id that set the bias
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (channel_id) REFERENCES thalamic_relays(channel_id) ON DELETE CASCADE
);
CREATE INDEX idx_gate_sector ON thalamic_gate(sector);
CREATE INDEX idx_gate_armed ON thalamic_gate(armed_for_burst) WHERE armed_for_burst = 1;

-- Global mode (single row, broadcast)
CREATE TABLE thalamic_mode (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  mode TEXT NOT NULL DEFAULT 'wake_focused'
    CHECK(mode IN ('wake_focused', 'wake_exploratory', 'drowsy', 'consolidate', 'offline')),
  arousal REAL NOT NULL DEFAULT 0.5,
  acetylcholine REAL NOT NULL DEFAULT 0.5,    -- broaden RF, raise spontaneous
  norepinephrine REAL NOT NULL DEFAULT 0.5,   -- narrow RF, lower spontaneous
  retrieval_breadth_multiplier REAL NOT NULL DEFAULT 1.0,
  similarity_threshold_delta REAL NOT NULL DEFAULT 0.0,
  set_by TEXT,
  set_at TEXT DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO thalamic_mode (id, mode) VALUES (1, 'wake_focused');

-- Salience map (transient, rebuilt per retrieval; not for long-term storage)
-- Backed by a memo cache; older entries pruned on each rebuild
CREATE TABLE thalamic_salience (
  candidate_id TEXT NOT NULL,
  candidate_type TEXT NOT NULL,     -- 'memory' | 'event' | 'belief' | 'entity'
  bottomup_score REAL,
  topdown_score REAL,
  precision REAL DEFAULT 1.0,
  integrated REAL,
  sector TEXT,
  computed_for_agent TEXT,
  computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (candidate_id, candidate_type, computed_for_agent)
);
CREATE INDEX idx_salience_recent ON thalamic_salience(computed_at);
CREATE INDEX idx_salience_integrated ON thalamic_salience(integrated DESC);

-- Burst event log (sparse high-salience "wake-up calls")
CREATE TABLE thalamic_bursts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  sector TEXT NOT NULL,
  reason TEXT NOT NULL,             -- 'novelty', 'high_pe', 'distractor_break_through'
  payload_ref TEXT,                  -- e.g., 'memory:1879', 'event:20377'
  salience REAL NOT NULL,
  fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
  consumed_by TEXT,
  consumed_at TEXT
);
CREATE INDEX idx_bursts_unconsumed ON thalamic_bursts(consumed_at) WHERE consumed_at IS NULL;
```

---

## MCP tool surface

Seven new tools, all under `thalamus_*` namespace. Aim: each is a thin, opinionated façade.

### Read / inspection

- **`thalamus_status(agent_id?, project?) → dict`** — Current mode, per-sector suppression map, armed channels, top-N salience entries for this agent's context, recent burst log. The "what state is the gate in" call.
- **`thalamus_salience(candidates: list, agent_id, project?, query?) → list[(id, integrated_score)]`** — Compute integrated salience for a candidate set. Bottom-up = novelty (W(m) surprise) + recency; top-down = `attention_snapshot.focus_terms` match + project scope match. Multiplied by per-channel `precision`.

### Write / control

- **`thalamus_route(action, **kwargs) → dict`** — The new primary chokepoint. Dispatch: `route(action='retrieve', query=…, agent_id=…, project=…)` runs search through the gate. `route(action='admit', content=…, source=…)` runs write through the gate. Existing tools (`memory_search`, `memory_add`) continue to work; this becomes the recommended path.
- **`thalamus_gate_set(channel_id, suppression?, topdown_bias?, bias_source) → dict`** — Write top-down attention bias. Used by an agent to express "I am focused on X; suppress Y." `bias_source` recorded for auditability and for the disagreement log analog.
- **`thalamus_burst(channel_id, payload_ref, reason='novelty', salience=None) → dict`** — Force-fire a burst event. Normally fired automatically by the gate when an armed channel sees high-PE traffic, but exposed for tooling/testing.
- **`thalamus_mode_set(mode, set_by) → dict`** — Global mode switch. Validated against the enum. Logged as a high-importance event so it shows up in `agent_orient`.
- **`thalamus_relay_create(channel_id, sector, driver_source, target, transport, modulator_sources=None, …) → dict`** — Register a new typed relay. Idempotent on `channel_id`.

---

## Hookpoints (concrete patches against existing brainctl files)

These are the integration points where the existing substrate calls into the new layer. All can be implemented as opt-in wrappers; rollback is `DELETE FROM thalamic_mode; ALTER TABLE … DROP COLUMN routed_through_thalamus`.

### 1. Pre-retrieval gate — `mcp_server.py:tool_search` and `mcp_tools_meb.py:tool_push:391`

**Today:** intent classifier routes to FTS5/vector; static k/threshold; `neuromodulation_state` is read but ignored.

**Patch:** wrap the search entry to first call `thalamus_route(action='retrieve', …)`. The route function:
1. Reads `thalamic_mode.retrieval_breadth_multiplier` → scales `top_k`.
2. Reads `thalamic_mode.similarity_threshold_delta` → adjusts cosine cutoff.
3. Reads `thalamic_gate` rows for the active sectors → filters candidate set by `(1 - suppression)`.
4. Computes `thalamic_salience` for the surviving candidates and re-ranks.
5. Returns through the existing return shape (no consumer changes).

**Failure mode:** if `thalamic_mode` row absent → behave as today (mode='wake_focused', delta=0, multiplier=1.0). Graceful degradation.

### 2. Write admission — `_gates.py:33 run_write_gate`

**Today:** W(m) checks novelty + redundancy + PII; returns `(ok, score, reason)`.

**Patch:** after W(m) passes, consult `thalamic_gate` for the sector this write belongs to:
- If sector is **suppressed > 0.7**: downgrade `write_tier` from `'full'` to `'construct'` (lightweight) — write is admitted but not indexed for surface-level recall.
- If sector is **armed_for_burst** and W(m) surprise > 0.6: emit a `thalamic_burst` event in addition to the normal write. This is the implementation of "suppression arms burstability."
- Update `thalamic_gate.bottomup_drive` for this channel (EMA of write rate).

**Why this matters:** prevents the existing problem where high-volume low-priority writes flood retrieval. Suppressed sectors continue to *admit* memories but the memories are quietly tiered down, while genuinely novel writes in the same sector still surface via burst.

### 3. Consolidation mode coupling — `mcp_tools_consolidation.py:380 consolidation_run`

**Today:** static ripple threshold = 3 tags; runs whenever called.

**Patch:** read `thalamic_mode.mode`:
- `consolidate` / `offline`: enable spindle-analog behavior — only replay items with `thalamic_salience.integrated > 0.5`, and only from sectors not suppressed. This is the "spindles gate which traffic gets consolidated" principle.
- `wake_*`: behave as today.

### 4. Workspace broadcast salience gate — `init_schema.sql:1310+` workspace ignition

**Today:** fixed salience threshold for ignition; no org_state coupling.

**Patch:** replace the fixed threshold with `thalamic_salience(candidate)` lookup. Ignition condition becomes:
```
integrated_salience > threshold * (1 + arousal - mean_sector_suppression)
```
High arousal → easier ignition. High global suppression → harder ignition. Matches the "ignition requires non-specific thalamus in tonic mode" principle from Global Workspace theory.

### 5. Reconsolidation lability — `mcp_tools_consolidation.py:24 _LABILITY_THRESHOLD`

**Today:** static threshold 0.35.

**Patch:** make threshold a function of `thalamic_mode`:
- `wake_focused` + high arousal: 0.25 (looser — open lability easily for active work)
- `wake_exploratory`: 0.35 (today's default)
- `consolidate`: 0.50 (tighter — stabilize during offline replay)
- `offline`: ∞ (freeze — no in-place updates during system idle / external dormancy)

This addresses the "anesthesia breaks the loop" insight: during deep offline modes, *block* mutation, only allow read/replay.

### 6. Session orient — `mcp_server.py:tool_agent_orient`

**Today:** `Brain.orient()` returns recent handoff + recent events + push memories + stats.

**Patch:** before returning, run the candidate sets through `thalamus_salience` with the agent's `attention_class` cap as top-K. This applies attention-budget-tier filtering uniformly across all four payload classes (handoff / events / memories / triggers) instead of just sizing the lists independently.

### 7. Inter-agent routing — new convention

**Today:** agents share context by writing to global / project-scoped memories that any peer reads back.

**Patch:** introduce a `transport='higher_order'` relay class for agent-to-agent messages. An agent writes into the relay (driver = agent A's output, target = agent B); the gate decides whether B sees it now, later (queue for next orient), or never (suppressed). This is the trans-thalamic principle: **no direct cortico-cortical**; sibling agents talk through the memory layer, and the memory layer stamps every inter-agent message with its routing decision.

Existing coordination scripts (`~/.claude/coordination/`) continue to work and become *modulator* inputs (carry context/intent) while the `higher_order` relays become the *driver* channel (carry the actual handoff payload).

---

## What this does NOT add (intentional non-features)

- **No new ranking algorithm.** Existing FTS5 + vector + RRF stays; thalamus only re-weights and filters.
- **No new embedding model.** Salience uses cheap features (term overlap, recency, sector match) + the precision scalar.
- **No replacement for `neuromodulation_state`.** It becomes the *upstream input* to `thalamic_mode`, not a competitor.
- **No mandatory migration.** Direct calls to `memory_search` etc. continue to work; `thalamus_route` is the recommended path going forward.
- **No new auth or PII layer.** PII handling stays in W(m); thalamus just adds a `pii_sensitive` sector that defaults to high suppression.

---

## Failure-mode design

From subagent 3 (TRN pathology):

| Pathology | brainctl analog | Mitigation |
|---|---|---|
| Hyperexcitable gate (autism-like rigidity) | Every sector heavily suppressed, retrieval starves | `thalamus_mode.mode` cannot rest in a state where mean suppression > 0.9; bounds check on `gate_set` |
| Hypoactive gate (schizophrenia-like leakage) | No sectors suppressed, every write surfaces, broadcasts spam | Workspace ignition multiplier requires `mean_suppression > 0.1` lower bound |
| Spindle disruption | Consolidation never enters offline mode | Watchdog: if `consolidate` mode hasn't been entered in N hours, allostatic scheduler forces it |
| Single-point-of-failure (pulvinar deactivation collapses cortex) | Thalamus DB rows unreadable → all retrieval broken | All hookpoints fall back to today's behavior if `thalamic_mode` query fails |

---

## Rollout plan

**Phase 1 — Schema + read-only inspection (1 day, no risk):**
- Apply migration 050.
- Implement `thalamus_status`, `thalamus_salience`, `thalamus_relay_create`.
- Seed relay catalog from observed traffic (one-time analysis of `memory_events` to identify natural channels).

**Phase 2 — Gate writes (opt-in shadow):**
- Implement `thalamus_gate_set`, `thalamus_burst`, `thalamus_mode_set`.
- Wire hookpoint #2 (write admission tier downgrade) but record decisions in a shadow column without changing actual `write_tier`. Compare for one week.

**Phase 3 — Gate reads:**
- Wire hookpoint #1 (`tool_search` re-ranking) behind a feature flag per agent.
- Run with Claude Code session only; compare retrieved-set quality vs. baseline.

**Phase 4 — Mode coupling:**
- Wire hookpoints #3, #4, #5, #6.
- Couple `thalamic_mode` writes to `neuromodulation_state` changes (automatic upstream).

**Phase 5 — Inter-agent (higher-order) routing:**
- Roll out `transport='higher_order'` relays for the Hermes ↔ Claude ↔ OpenClaw triangle.
- Migrate one coordination flow at a time.

---

## Why now

brainctl is at ~485 memories, 20k events, 350+ entities, 200+ MCP tools. The dominant scaling problem at this size is no longer *capacity* (sqlite + sqlite-vec handle this fine) — it's **what to surface, when, to whom**. The existing pieces (neurostate, attention_snapshot, workspace) already point at this answer but don't talk to each other. A thalamus subsystem is the smallest coherent layer that connects them.

Without it, every new MCP tool is another way to hit the substrate directly, and the substrate's quality at any given moment is determined by the most recent caller. With it, there is a single defensible answer to "what should this agent see right now" — driven by mode, salience map, and competitive sector gating — that is independent of which tool the agent happened to call.

---

## Sources

Synthesized from 8 research subagents. Primary citations across the swarm:

- Sherman & Guillery, *Distinguishing drivers from modulators* (PNAS 1998); *Transthalamic Pathways for Cortical Function* (J Neurosci 2024)
- Crick, *Function of the thalamic reticular complex: searchlight hypothesis* (PNAS 1984)
- Wimmer / Halassa, *Thalamic control of sensory selection in divided attention* (Nature 2015)
- Saalmann, Pinsk, Wang, Li, Kastner — pulvinar synchronization of cortico-cortical alpha (Science 2012)
- Bastos et al., *Canonical Microcircuits for Predictive Coding* (Neuron 2012)
- Kanai, Komura, Shipp, Friston, *Cerebral hierarchies, predictive processing, precision and the pulvinar* (Phil Trans R Soc B 2015)
- Bolkan et al., *Thalamic projections sustain prefrontal activity during working memory maintenance* (Nat Neurosci 2017)
- Halassa & Sherman, *Thalamocortical Circuit Motifs: A General Framework* (Neuron 2019)
- Schiff et al., central thalamic DBS in MCS (Nature 2007); mesocircuit hypothesis
- Alkire et al., thalamic switch hypothesis of anesthesia (Anesthesiology 2000)
- Goyal/Mittal/Bengio, *Coordination Among Neural Modules Through a Shared Global Workspace* (ICLR 2022)
- Shazeer et al., Sparsely-Gated Mixture-of-Experts (2017)
- O'Reilly & Frank, PBWM (2006)

Per-slice reports with full citations are preserved in the brainctl event log under event 20377 (session start) and in the wrap-up handoff packet for this session.
