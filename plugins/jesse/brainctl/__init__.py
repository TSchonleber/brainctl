"""
brainctl plugin for Jesse — persistent memory for algo trading strategies.

Two public surfaces:

    1. BrainctlStrategyMixin  — drop-in mixin for Jesse `Strategy` subclasses
       that automatically journals position open / position close / session
       lifecycle events to a brainctl brain. Recommended API.

    2. StrategyBrain          — lower-level helper class for strategies that
       prefer explicit calls. Same semantic operations, manual triggering.

Both share the same underlying `agentmemory.Brain` instance and write to the
same SQLite brain.db.

## Quick start

    from jesse.strategies import Strategy
    from brainctl_jesse import BrainctlStrategyMixin

    class MyStrategy(BrainctlStrategyMixin, Strategy):
        brainctl_config = {
            "agent_id": "my-strategy",
            "project": "btc-scalper",
        }

        # ... your normal Jesse strategy code ...

That's it. Every position open is journaled as a decision event. Every close
is a result event with P&L and a win/loss observation on the pair entity.
Every bot shutdown persists a handoff packet.

See `examples/sample_strategy.py` for a complete working example.
"""

from .mixin import BrainctlStrategyMixin
from .strategy_brain import StrategyBrain

__all__ = ["BrainctlStrategyMixin", "StrategyBrain"]
__version__ = "0.1.0"
