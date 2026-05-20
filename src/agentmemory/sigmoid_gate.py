"""Smooth sigmoid read-gate — issue #116 Phase 1-C (observability slice).

The memo (issue #116 §1.4 Stage 2) argues that a hard `--limit N`
truncation of search results is both noise-sensitive (an item whose
relevance score fluctuates slightly around the cutoff flips in and
out unpredictably) and non-learnable (zero gradient at the boundary
gives a feedback loop nothing to update against).

The memo's recommendation is a smooth sigmoid threshold with
learnable slope and midpoint, calibrated from operational data.

This module ships the **observability slice** of that recommendation:

  - Pure-math sigmoid + per-rank weight helpers.
  - Conservative default parameters (shallow slope, mid midpoint).
  - A `weight_for_rank(rank, total)` helper that surfaces a soft
    confidence-in-relevance per result item without changing the
    item's actual rank position.

The intended use at this stage is **additive** — `cmd_search`
attaches a `_sigmoid_rank_weight` field to each returned item so
downstream consumers (BG/cerebellum learning loops, agents, future
sigmoid-aware rerankers) can experiment with using it without any
risk of regressing the existing rank order or the bench harness.

Learning the slope / midpoint from accumulating outcomes is Phase 4
territory and not implemented here. The conservative defaults are
chosen so that the surfaced weights are useful as a relative ordering
signal but do not over-commit to any particular calibration before
the data is in.

See also:
  - research/issue-116-audit-vs-origin-main.md §6.2.4 — "Smooth
    sigmoid read-gate threshold"
  - research/brainctl-brain-architecture-issue-116.md §1.4 Stage 2
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

# Conservative defaults — chosen for "shallow slope, conservative
# midpoint" per memo §1.4 Stage 2 ("Start with a shallow slope and
# a conservative midpoint, then tighten as data accumulates").
#
# With slope=6.0 and midpoint=0.5 over normalized rank ∈ [0, 1]:
#   rank 1 of  5 (normalized 1.0 ) → weight ≈ 0.953
#   rank 3 of  5 (normalized 0.5 ) → weight = 0.5
#   rank 5 of  5 (normalized 0.0 ) → weight ≈ 0.047
#   rank 1 of 10 (normalized 1.0 ) → weight ≈ 0.953
#   rank 5 of 10 (normalized 0.555) → weight ≈ 0.583
#   rank 10 of 10 (normalized 0.0) → weight ≈ 0.047
#
# A shallower slope produces a flatter weight curve that distributes
# uncertainty more evenly across positions; a steeper slope sharpens
# the in/out distinction. The Phase 4 calibration step will fit these
# from accumulated outcomes.
DEFAULT_SLOPE = 6.0
DEFAULT_MIDPOINT = 0.5


@dataclass(frozen=True)
class SigmoidParams:
    """A (slope, midpoint) pair. Frozen so it can be passed around and
    cached without surprise mutations."""
    slope: float = DEFAULT_SLOPE
    midpoint: float = DEFAULT_MIDPOINT

    def __post_init__(self) -> None:
        # Defensive — keeps callers honest about what shapes are
        # meaningful. midpoint must be in (0, 1) so rank-position
        # normalization stays well-defined; slope must be > 0 so the
        # function is monotone increasing in x.
        if not (0.0 < self.midpoint < 1.0):
            raise ValueError(
                f"midpoint must be in (0, 1); got {self.midpoint!r}"
            )
        if self.slope <= 0.0:
            raise ValueError(f"slope must be > 0; got {self.slope!r}")


DEFAULT_PARAMS = SigmoidParams()


def sigmoid(x: float, *, slope: float = DEFAULT_SLOPE,
            midpoint: float = DEFAULT_MIDPOINT) -> float:
    """Smooth threshold function.

    Returns a value in (0, 1) that crosses 0.5 at x = midpoint and
    approaches 1.0 (resp. 0.0) for x well above (resp. below) midpoint.

    The slope controls how sharp the transition is — large slope
    approximates a hard cutoff, small slope produces a gentle ramp.
    """
    # math.exp can overflow for huge negative exponents; guard.
    z = -slope * (x - midpoint)
    if z > 500:
        return 0.0
    if z < -500:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def normalize_rank(rank: int, total: int) -> float:
    """Map a 1-based rank position (1=best) inside a result set of
    `total` items to a normalized score in [0, 1] where 1.0 is the top
    of the list and 0.0 is the bottom.

    Edge cases:
      - total <= 0           → 0.5 (degenerate input; refuse to commit)
      - total == 1, rank == 1 → 1.0 (the only item is the top)
      - rank outside [1, total] is clamped to that range.
    """
    if total <= 0:
        return 0.5
    if total == 1:
        return 1.0
    if rank < 1:
        rank = 1
    elif rank > total:
        rank = total
    # rank=1 → 1.0, rank=total → 0.0, linear in between.
    return (total - rank) / (total - 1)


def weight_for_rank(rank: int, total: int,
                    *, params: SigmoidParams = DEFAULT_PARAMS) -> float:
    """Sigmoid weight for an item at 1-based `rank` in a result set
    of `total` items. Composition of `normalize_rank` + `sigmoid`."""
    return sigmoid(
        normalize_rank(rank, total),
        slope=params.slope,
        midpoint=params.midpoint,
    )


def weights_for_results(total: int,
                        *, params: SigmoidParams = DEFAULT_PARAMS) -> list[float]:
    """Compute the sigmoid weight at every rank position 1..total.

    Returns a list of length `total` indexed by zero-based position
    (i.e. result[0] holds the weight for rank 1). Convenient for
    callers that hand out a ranked list and want to attach weights
    in one pass.
    """
    if total <= 0:
        return []
    return [weight_for_rank(rank, total, params=params)
            for rank in range(1, total + 1)]


def annotate_with_weights(items: Iterable[dict],
                          *,
                          weight_key: str = "_sigmoid_rank_weight",
                          params: SigmoidParams = DEFAULT_PARAMS,
                          ) -> list[dict]:
    """Mutate each dict in `items` by attaching a sigmoid weight at
    `weight_key`, keyed by 1-based rank in iteration order.

    Items that already carry a value at `weight_key` are left
    untouched — the gate does not overwrite an explicit upstream
    decision. Returns the same list (passed through) for chaining.
    """
    items_list = list(items)
    total = len(items_list)
    if total == 0:
        return items_list
    for idx, item in enumerate(items_list, start=1):
        if not isinstance(item, dict):
            continue
        if weight_key in item:
            continue
        item[weight_key] = weight_for_rank(idx, total, params=params)
    return items_list
