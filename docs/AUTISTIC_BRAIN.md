# Autistic-brain features (cognitive profiles, 2.5.2+)

> **Status:** introduced in migration 052 (May 2026). Default behavior is
> unchanged — every existing agent gets `cognitive_profile = 'neurotypical'`
> on backfill, and every gate falls back to its pre-052 hardcoded constants
> when no profile is supplied.

This is a design statement: **autistic cognition is different, not worse.**
The autistic profile is not a degradation of the neurotypical default — it
is a coherent retuning of brainctl's existing gates against published
cognitive-science theories of autistic perception and memory.

## TL;DR

```bash
# Inspect the profiles
brainctl cognition list
brainctl cognition show autistic

# Opt an agent in
brainctl cognition set autistic --agent my-agent
brainctl cognition status --agent my-agent

# Tag a special interest (monotropism)
brainctl interest add "Tree-sitter grammars" --strength 0.9
brainctl interest list

# Search with a monotropic focus boost (only effective under autistic profile)
brainctl memory search "incremental parsing" --focus "Tree-sitter grammars"
```

The autistic profile retunes — **but does not fork** — the W(m) write gate,
the AGM conflict-resolution threshold, the Bayesian recall priors, the
entity-synthesis behavior, and a retrieval rerank stage. Each retuning is
opt-in per agent; multi-agent deployments can mix profiles freely.

## Research grounding

Each tunable in `src/agentmemory/cognitive_profile.py` traces back to one
of five well-supported cognitive-science theories:

1. **HIPPEA — High Inflexible Precision of Prediction Errors in Autism.**
   *Van de Cruys, Evers, Van der Hallen, Van Eylen, Boets, de-Wit, Wagemans
   (2014). "Precise minds in uncertain worlds." Psychological Review,
   121(4), 649–675.*

   Autistic perception over-weights prediction errors relative to top-down
   priors. Mapped onto the W(m) gate, this means novelty (semantic
   surprise) deserves more weight in the score, and "expected utility"
   smoothing deserves less. Implementation: `wm_novelty_weight: 0.45 →
   0.60`, `wm_utility_weight: 0.25 → 0.15`, `wm_skip_threshold: 0.30 →
   0.20`.

2. **Hypo-priors / Bayesian under-fitting.**
   *Pellicano & Burr (2012). "When the world becomes 'too real':
   a Bayesian explanation of autistic perception." Trends in Cognitive
   Sciences, 16(10), 504–510.*

   Top-down priors are weaker; the posterior tracks observed evidence
   more directly. In our beta-binomial recall scoring, this maps to the
   Jeffreys prior (α = β = 0.5) — strictly less informative than the
   uniform prior (α = β = 1.0). Recall reinforces credibility more
   strongly per observation: `credibility_recall_log_divisor: 10.0 → 4.0`.

3. **Weak central coherence (WCC).**
   *Frith (1989). "Autism: Explaining the Enigma."*
   *Happé & Frith (2006). "The weak coherence account: detail-focused
   cognitive style in autism spectrum disorders." Journal of Autism and
   Developmental Disorders, 36(1), 5–25.*

   Local detail is preferentially preserved over global gestalt.
   Operationalized as: contradicting observations on an entity are
   stored as `compiled_truth_variants` rather than smoothed into a
   single `compiled_truth`, and the AGM "too close to call" threshold
   widens (`agm_threshold: 0.05 → 0.15`) so both sides survive longer.

4. **Monotropism.**
   *Murray, Lesser & Lawson (2005). "Attention, monotropism and the
   diagnostic criteria for autism." Autism, 9(2), 139–156.*

   Attention is a finite resource preferentially allocated to a small
   number of interests at high intensity. First-class implementation:
   the `entities.special_interest` flag, the `interest_strength` REAL,
   the `monotropic_focus_boost` (autistic = 2.5×), and a `--focus`
   retrieval flag. Tagged interests also receive a 3× retention
   multiplier in retire-pressure calculations.

5. **Enhanced perceptual functioning (EPF).**
   *Mottron, Dawson, Soulières, Hubert & Burack (2006). "Enhanced
   perceptual functioning in autism: an update, and eight principles
   of autistic perception." Journal of Autism and Developmental
   Disorders, 36(1), 27–43.*

   Superior local sensory processing. Affect_log gains a
   `sensory_dimensions` JSON column and a composite `sensory_load`
   REAL; the autistic profile enables a `sensory_overload_threshold`
   (default 0.85) above which a `sensory_overload` event is emitted.

## Profile parameter map

| Tunable                                | `neurotypical` | `autistic` | Theory          |
|----------------------------------------|---------------:|-----------:|-----------------|
| `wm_novelty_weight`                    |          0.45  |      0.60  | HIPPEA          |
| `wm_utility_weight`                    |          0.25  |      0.15  | HIPPEA          |
| `wm_importance_weight`                 |          0.20  |      0.15  | HIPPEA          |
| `wm_scope_weight`                      |          0.10  |      0.10  | —               |
| `wm_skip_threshold`                    |          0.30  |      0.20  | HIPPEA          |
| `wm_construct_threshold`               |          0.70  |      0.60  | HIPPEA          |
| `agm_threshold`                        |          0.05  |      0.15  | WCC             |
| `agm_preserve_both_on_tie`             |        `False` |     `True` | WCC             |
| `bayesian_alpha_prior`                 |          1.00  |      0.50  | Hypo-priors     |
| `bayesian_beta_prior`                  |          1.00  |      0.50  | Hypo-priors     |
| `credibility_recency_half_life_days`   |        365.00  |   1825.00  | WCC + literal recall |
| `credibility_recall_log_divisor`       |         10.00  |      4.00  | Hypo-priors     |
| `preserve_contradictions`              |        `False` |     `True` | WCC             |
| `monotropic_focus_boost`               |          1.00  |      2.50  | Monotropism     |
| `interest_retention_multiplier`        |          1.00  |      3.00  | Monotropism     |
| `sensory_overload_threshold`           |        `None`  |      0.85  | EPF             |

These are not cargo-culted. The `0.45 → 0.60` shift in W(m) novelty
weight, for example, reflects roughly +33 % weight on prediction errors,
which is in the same range as the precision-weighting shifts reported
in the predictive-coding literature for autistic perceptual judgments
(Lawson, Rees & Friston 2014, "An aberrant precision account of
autism," Frontiers in Human Neuroscience, 8: 302).

## Schema additions (migration 052)

```sql
-- agents
ALTER TABLE agents ADD COLUMN cognitive_profile TEXT NOT NULL DEFAULT 'neurotypical';

-- entities (WCC + monotropism)
ALTER TABLE entities ADD COLUMN compiled_truth_variants TEXT;       -- JSON array
ALTER TABLE entities ADD COLUMN contradiction_count INTEGER DEFAULT 0;
ALTER TABLE entities ADD COLUMN special_interest INTEGER DEFAULT 0;
ALTER TABLE entities ADD COLUMN interest_strength REAL DEFAULT 0.0;

-- affect_log (EPF)
ALTER TABLE affect_log ADD COLUMN sensory_load REAL;
ALTER TABLE affect_log ADD COLUMN sensory_dimensions TEXT;          -- JSON
```

All columns are nullable or default to a value that preserves pre-052
behavior. Re-running `brainctl migrate` on a fresh DB applies them
idempotently; on an existing DB, the runner tolerates duplicate-column
errors so re-application is a no-op.

## CLI surface

```
brainctl cognition list                          # all built-in profiles
brainctl cognition show <profile>                # full tunables dict
brainctl cognition set <profile> --agent <id>    # opt an agent in
brainctl cognition status [--agent <id>]         # current profile

brainctl interest add <entity> [--strength 0–1]  # tag special interest
brainctl interest remove <entity>                # untag
brainctl interest list [--scope <s>] [--limit N] # listing

brainctl memory search <q> --focus <name|scope>  # monotropic boost
```

`--json` is supported on every subcommand for scripted use.

## MCP surface

Six tools, in the new `mcp_tools_cognitive.py` extension module:

  - `cognition_list`
  - `cognition_show`
  - `cognition_set`
  - `interest_add`
  - `interest_list`
  - `affect_log_sensory` — writes an affect row carrying per-channel
    `sensory_dimensions` and emits a `sensory_overload` event when
    `sensory_load > sensory_overload_threshold`.

Both surfaces share the same Python implementations; the CLI is not
"more privileged" than the MCP path. An autistic-profile agent that
uses `cognition_set` via MCP and one that uses `brainctl cognition set`
end up in identical states.

## What this changes at runtime

The autistic profile retunes the following code paths. Each is gated on
the agent_id at the call site, so neurotypical agents in the same DB
are unaffected.

| Code path                                                | Effect under `autistic`                                                         |
|----------------------------------------------------------|---------------------------------------------------------------------------------|
| `lib/write_decision.py:gate_write`                       | Higher novelty weight; lower skip threshold; more verbatim detail retained.      |
| `lib/belief_revision.py:compute_credibility`             | Jeffreys priors; longer recency half-life; stronger recall reinforcement.        |
| `lib/belief_revision.py:resolve_conflict`                | Wider too-close threshold; more conflicts escalate rather than auto-collapse.    |
| `_impl.py:cmd_memory_search` (with `--focus`)            | Results matching the focus entity/scope multiplied by `monotropic_focus_boost`.  |
| `mcp_tools_cognitive.py:affect_log_sensory`              | Emits `sensory_overload` events when `sensory_load > 0.85`.                      |

## Things this deliberately does **not** do

- **No "diagnosis."** Setting `cognitive_profile = 'autistic'` on an
  agent is a configuration choice for that agent's memory system, not
  a clinical claim about its operator.
- **No social-inference suppression.** The `theory_of_mind` migrations
  (043) are unchanged. Earlier drafts proposed disabling implicit
  mental-state synthesis on `person` entities; that conflated a
  cognitive style with a social-skills stereotype, and was removed.
- **No "less belief revision."** AGM still resolves conflicts; it
  just escalates more aggressively (preserves both sides) when the
  scores are close. Catastrophic belief retention is still bounded.
- **No fork.** No autistic-only tables, no autistic-only code paths
  beyond the parameter switch. If you want to tweak the constants for
  your own agent, edit `cognitive_profile.PROFILES` — adding a third
  profile is a 30-line patch.

## Testing

Unit tests live in `tests/test_cognitive_profile.py`. They cover:

- Profile resolution under missing column, missing agent, missing
  profile name (all → neurotypical fallback).
- W(m) gate weight changes between profiles using a synthetic candidate
  with controlled novelty.
- AGM threshold widening between profiles.
- `--focus` boost triggering only when `monotropic_focus_boost > 1.0`.

Run with `python3 -m pytest tests/test_cognitive_profile.py -v`.

## Future work

- A `monotropic-focus` rerank profile in `lib/quantum_retrieval.py` so
  in-domain interference effects compound on top of the simple
  multiplicative boost.
- Per-channel sensory decay (currently `sensory_load` is logged but
  not folded back into the consolidation cycle's replay priorities).
- A second non-default profile (e.g. `adhd`) sharing the
  hypo-priors machinery but tuning monotropism the other direction
  (high attentional breadth, low depth).

## Citations (full)

- Frith, U. (1989). *Autism: Explaining the Enigma.* Blackwell.
- Happé, F., & Frith, U. (2006). The weak coherence account: detail-focused
  cognitive style in autism spectrum disorders. *Journal of Autism and
  Developmental Disorders, 36*(1), 5–25.
- Lawson, R. P., Rees, G., & Friston, K. J. (2014). An aberrant precision
  account of autism. *Frontiers in Human Neuroscience, 8*, 302.
- Mottron, L., Dawson, M., Soulières, I., Hubert, B., & Burack, J. (2006).
  Enhanced perceptual functioning in autism: an update, and eight principles
  of autistic perception. *Journal of Autism and Developmental Disorders,
  36*(1), 27–43.
- Murray, D., Lesser, M., & Lawson, W. (2005). Attention, monotropism and
  the diagnostic criteria for autism. *Autism, 9*(2), 139–156.
- Pellicano, E., & Burr, D. (2012). When the world becomes 'too real': a
  Bayesian explanation of autistic perception. *Trends in Cognitive
  Sciences, 16*(10), 504–510.
- Van de Cruys, S., Evers, K., Van der Hallen, R., Van Eylen, L., Boets, B.,
  de-Wit, L., & Wagemans, J. (2014). Precise minds in uncertain worlds:
  predictive coding in autism. *Psychological Review, 121*(4), 649–675.
