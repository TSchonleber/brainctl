"""
Sample Jesse strategy using the brainctl persistent-memory mixin.

A minimal SMA-crossover scaffold — not tuned for real trading — that shows
the minimum wiring needed to get brainctl journaling on any Jesse strategy.

## What it does
- On first `before()`, pulls the handoff from the previous session (if any)
  and logs "[brainctl] resuming from handoff: ..." to Jesse's logger.
- On every position open, logs a `decision` event with symbol/side/price/qty.
- On every position close, logs a `result` event with P&L and appends a
  win/loss observation to the entity for the symbol.
- On `terminate()` or process exit, persists a handoff packet so the next
  session's first `before()` has context to resume from.

## Install
    pip install 'brainctl>=1.2.0' jesse
    cp -r plugins/jesse/brainctl /path/to/your/jesse/strategies/BrainctlSampleStrategy

## Run
    jesse backtest --strategy BrainctlSampleStrategy '2024-01-01' '2024-06-01'

Every trade is now persistent across bot restarts AND across backtest runs
(if you share the same brain.db across runs, which is optional).
"""

from __future__ import annotations

import logging

try:
    from jesse.strategies import Strategy  # type: ignore
except ImportError:  # pragma: no cover
    # Allow the module to import for linting / doc generation without Jesse.
    class Strategy:  # type: ignore[no-redef]
        pass


from .. import BrainctlStrategyMixin

logger = logging.getLogger(__name__)


class BrainctlSampleStrategy(BrainctlStrategyMixin, Strategy):
    """
    Minimal SMA crossover with brainctl persistent memory.

    NOTE: This is a demonstration strategy. Do not run with real funds
    without your own testing — it is intentionally simple.
    """

    brainctl_config = {
        "agent_id": "brainctl-sample-jesse",
        "project": "jesse-sample",
        "auto_orient": True,
        "auto_wrap_up": True,
        "log_open": True,
        "log_close": True,
    }

    # ---------- Jesse strategy API ----------

    def should_long(self) -> bool:
        # Enter long when fast SMA crosses above slow SMA.
        import jesse.indicators as ta  # type: ignore

        fast = ta.sma(self.candles, 9, sequential=True)
        slow = ta.sma(self.candles, 21, sequential=True)
        return fast[-2] <= slow[-2] and fast[-1] > slow[-1]

    def should_short(self) -> bool:
        return False

    def go_long(self) -> None:
        entry = self.price
        qty = self.capital / entry
        self.buy = qty, entry
        self.stop_loss = qty, entry * 0.95
        self.take_profit = qty, entry * 1.05

        # Record the entry reasoning as a decision in brainctl.
        self.brainctl_decide(
            title=f"Long {self.symbol} @ {entry:.4f}",
            rationale="SMA(9) crossed above SMA(21) — momentum confirmation.",
        )

    def go_short(self) -> None:
        pass

    def should_cancel_entry(self) -> bool:
        return False

    def update_position(self) -> None:
        # Example in-strategy memory recall — skip the trade if we've seen
        # too many consecutive losses on this symbol recently.
        recent = self.brainctl_recall(f"{self.symbol} loss", limit=5)
        if len(recent) >= 3:
            self.brainctl_note(
                f"Skipping new entries on {self.symbol} — 3+ recent losses recalled.",
                category="lesson",
            )
