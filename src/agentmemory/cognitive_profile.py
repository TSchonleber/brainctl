"""
cognitive_profile.py — per-agent cognitive profile registry + loader.

Profiles retune the existing brainctl machinery (W(m) gate, AGM conflict
resolution, Bayesian recall priors, retrieval rerank, affect thresholds)
rather than forking it. Each profile is a flat dict of tunables that the
existing functions read at call time. The default profile, `neurotypical`,
preserves brainctl's pre-052 behavior exactly — every numeric default in
the dict matches the hardcoded constants the codebase used before.

The `autistic` profile is grounded in published cognitive theories of
autistic perception and cognition. See docs/AUTISTIC_BRAIN.md for the
full design doc with citations. Headline mappings:

  - HIPPEA (Van de Cruys et al. 2014, "Precise minds in uncertain
    worlds"): high *and inflexible* precision of prediction errors →
    novelty / surprise gets weighted higher in W(m), and the skip
    threshold is lowered so detail is preferentially retained.

  - Hypo-priors (Pellicano & Burr 2012, "When the world becomes 'too
    real'"): weaker top-down priors → Bayesian alpha/beta defaults
    move from uniform (1, 1) to Jeffreys (0.5, 0.5), letting evidence
    dominate posteriors more directly.

  - Weak central coherence (Frith 1989; Happé & Frith 2006): local
    detail preference over global gestalt → contradictions are
    preserved as variants on entities.compiled_truth_variants instead
    of being smoothed into a single compiled_truth, and the AGM
    too-close threshold is raised so both sides survive longer.

  - Monotropism (Murray, Lesser, Lawson 2005, "Attention, monotropism
    and the diagnostic criteria for autism"): attention as a limited
    resource preferentially allocated to a small number of interests
    at high intensity → entities tagged `special_interest=1` get a
    retention multiplier and a `--focus` retrieval boost.

  - Enhanced perceptual functioning (Mottron et al. 2006): superior
    local sensory processing → affect_log gains a per-channel
    sensory_dimensions dict and a sensory_overload threshold.

None of this changes default behavior. Agents without a cognitive_profile
column value, or with the value `'neurotypical'`, use the original
constants. The autistic profile is opt-in per agent via
`brainctl profile set --agent <id> autistic`.
"""

from __future__ import annotations

import sqlite3
from typing import Any


# Built-in profile registry. Keep these flat — the consumers read individual
# keys, never the whole dict shape, so adding a key here is non-breaking.
PROFILES: dict[str, dict[str, Any]] = {
    "neurotypical": {
        # Identity (used for diagnostics + UI)
        "name": "neurotypical",
        "description": (
            "Default brainctl behavior. Balanced novelty/utility trade-off, "
            "uniform Bayesian priors, contradiction-collapsing entity synthesis."
        ),
        # ---- W(m) write gate weights ----
        # Must sum to 1.0. Match the pre-052 hardcoded constants in
        # lib/write_decision.py:gate_write() (line 156).
        "wm_novelty_weight": 0.45,
        "wm_utility_weight": 0.25,
        "wm_importance_weight": 0.20,
        "wm_scope_weight": 0.10,
        # D-MEM RPE routing thresholds.
        "wm_skip_threshold": 0.30,
        "wm_construct_threshold": 0.70,
        # ---- AGM conflict resolution ----
        # Pre-052 default in belief_revision.resolve_conflict() was 0.05.
        "agm_threshold": 0.05,
        "agm_preserve_both_on_tie": False,
        # ---- Credibility scoring (compute_credibility) ----
        # 365-day linear decay; recall log boost divisor 10.0.
        "credibility_recency_half_life_days": 365.0,
        "credibility_recall_log_divisor": 10.0,
        # ---- Bayesian recall priors ----
        # Uniform prior — matches the alpha=1.0, beta=1.0 fallbacks in
        # compute_credibility() (lines 43-44).
        "bayesian_alpha_prior": 1.0,
        "bayesian_beta_prior": 1.0,
        # ---- Entity synthesis ----
        # When True, a contradiction between observations creates a new
        # row in compiled_truth_variants rather than rewriting compiled_truth.
        "preserve_contradictions": False,
        # ---- Monotropism / focus ----
        # 1.0 = no boost. Used by retrieval rerank when a special_interest
        # entity matches the query.
        "monotropic_focus_boost": 1.0,
        # Multiplier on retention / retire-resistance for special-interest
        # entities and memories scoped to them.
        "interest_retention_multiplier": 1.0,
        # ---- Sensory affect ----
        # None = sensory_load column ignored. Number = threshold above
        # which `sensory_overload` events should be auto-emitted.
        "sensory_overload_threshold": None,
    },
    "autistic": {
        "name": "autistic",
        "description": (
            "HIPPEA-grounded retuning: high precision of prediction errors, "
            "weak (Jeffreys) priors, contradiction-preserving entity synthesis, "
            "monotropic focus boost, sensory overload thresholding. See "
            "docs/AUTISTIC_BRAIN.md for citations."
        ),
        # HIPPEA: prediction errors weighted higher; "expected usefulness"
        # smoothing is reduced. Weights still sum to 1.0.
        "wm_novelty_weight": 0.60,
        "wm_utility_weight": 0.15,
        "wm_importance_weight": 0.15,
        "wm_scope_weight": 0.10,
        # Lower skip threshold → more detail retained verbatim. Lower
        # construct threshold → more memories get full embedding+FTS rather
        # than the construct-only path.
        "wm_skip_threshold": 0.20,
        "wm_construct_threshold": 0.60,
        # WCC: contradictions are tolerated. Unless the score gap is large,
        # both sides survive (preserved as variants) instead of one being
        # retracted.
        "agm_threshold": 0.15,
        "agm_preserve_both_on_tie": True,
        # Weaker recency smoothing (5-year window instead of 1-year), and
        # stronger reinforcement from recall (smaller divisor → larger
        # log-boost). Reflects the "high-fidelity literal recall" pattern.
        "credibility_recency_half_life_days": 1825.0,
        "credibility_recall_log_divisor": 4.0,
        # Hypo-priors (Pellicano & Burr 2012): Jeffreys prior (0.5, 0.5)
        # is less informative than uniform — the posterior tracks observed
        # evidence more directly.
        "bayesian_alpha_prior": 0.5,
        "bayesian_beta_prior": 0.5,
        # WCC: keep contradicting variants on the entity rather than
        # smoothing them into a single compiled_truth.
        "preserve_contradictions": True,
        # Monotropism: in-domain results get amplified strongly when a
        # special-interest entity is in the query or `--focus` is set.
        "monotropic_focus_boost": 2.5,
        # Special-interest entities (and memories anchored to them) decay
        # 3× more slowly and resist retire pressure.
        "interest_retention_multiplier": 3.0,
        # Sensory overload threshold (composite sensory_load on affect_log).
        # When exceeded, the affect writer should emit a `sensory_overload`
        # event so the consolidation cycle can react.
        "sensory_overload_threshold": 0.85,
    },
}


VALID_PROFILES = frozenset(PROFILES.keys())


def get_profile(name: str | None) -> dict[str, Any]:
    """Resolve a profile name to its tunables dict. Unknown / None → neurotypical."""
    if not name:
        return PROFILES["neurotypical"]
    return PROFILES.get(name, PROFILES["neurotypical"])


def get_agent_profile_name(
    db: sqlite3.Connection | None,
    agent_id: str | None,
) -> str:
    """Read the cognitive_profile column for an agent. Defaults to neurotypical.

    Tolerates a missing column (pre-052 DB) and missing agent rows. Never
    raises — falls back to 'neurotypical' on any failure path so this
    can be called from hot paths without a try/except wrapping every site.
    """
    if not db or not agent_id:
        return "neurotypical"
    try:
        row = db.execute(
            "SELECT cognitive_profile FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        # Column doesn't exist yet (migration 052 not applied).
        return "neurotypical"
    except Exception:
        return "neurotypical"
    if not row:
        return "neurotypical"
    val = row[0] if isinstance(row, tuple) else row["cognitive_profile"]
    if not val or val not in VALID_PROFILES:
        return "neurotypical"
    return val


def get_agent_profile(
    db: sqlite3.Connection | None,
    agent_id: str | None,
) -> dict[str, Any]:
    """One-shot helper: resolve agent_id → profile tunables dict."""
    return get_profile(get_agent_profile_name(db, agent_id))


def set_agent_profile(
    db: sqlite3.Connection,
    agent_id: str,
    profile_name: str,
) -> None:
    """Set an agent's cognitive_profile. Raises ValueError on unknown profile."""
    if profile_name not in VALID_PROFILES:
        raise ValueError(
            f"Unknown cognitive profile {profile_name!r}; "
            f"valid: {sorted(VALID_PROFILES)}"
        )
    cur = db.execute(
        "UPDATE agents SET cognitive_profile = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (profile_name, agent_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"Agent {agent_id!r} not found")
    db.commit()


def list_profiles() -> list[dict[str, Any]]:
    """Return the registered profiles as a list of {name, description, ...} dicts."""
    return [dict(p) for p in PROFILES.values()]


__all__ = [
    "PROFILES",
    "VALID_PROFILES",
    "get_profile",
    "get_agent_profile",
    "get_agent_profile_name",
    "set_agent_profile",
    "list_profiles",
]
