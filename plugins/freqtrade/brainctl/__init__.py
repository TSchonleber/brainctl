"""
brainctl plugin for Freqtrade — persistent memory for trading strategies.

Two public surfaces:

    1. BrainctlStrategyMixin  — drop-in mixin for IStrategy subclasses that
       automatically logs bot_start / trade entry / trade exit / shutdown
       events to a brainctl brain. This is the recommended API.

    2. StrategyBrain           — lower-level helper class for strategies
       that prefer explicit calls. Same operations, manual triggering.

Both use the same underlying `agentmemory.Brain` instance and write to
the same SQLite brain.db. A strategy can safely use either, or both.

## Quick start

    from freqtrade.strategy import IStrategy
    from brainctl_freqtrade import BrainctlStrategyMixin

    class MyStrategy(BrainctlStrategyMixin, IStrategy):
        brainctl_config = {
            "agent_id": "my-strategy",
            "project": "btc-scalper",
        }

        # ... your normal Freqtrade strategy code ...

That's it. Every trade is now journaled. Every restart calls `orient()`
so the bot knows what happened before it crashed. Every shutdown writes
a handoff packet.

See `examples/sample_strategy.py` for a complete working example.
"""

from .mixin import BrainctlStrategyMixin
from .strategy_brain import StrategyBrain

__all__ = ["BrainctlStrategyMixin", "StrategyBrain"]
__version__ = "0.1.0"
