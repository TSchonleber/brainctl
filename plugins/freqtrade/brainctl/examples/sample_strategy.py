"""
Sample Freqtrade strategy using the brainctl persistent-memory mixin.

This is a minimal SMA crossover strategy — nothing fancy — included to show
the minimum wiring needed to get brainctl journaling on any strategy.

## What it does
- On `bot_start`, pulls the handoff from the last session (if any) and logs
  it to Freqtrade's logger so you see "[brainctl] resuming from handoff:
  goal=... next_step=..." on startup.
- On every trade entry, logs a `decision` event to brainctl.
- On every trade exit, logs a `result` event and appends a win/loss
  observation to the entity for that trading pair.
- On process shutdown (atexit), persists a handoff packet so the next
  session's `bot_start` has context to resume from.
- Inside `populate_indicators`, demonstrates `self.brainctl_note(...)`
  for recording observations that outlive the current candle.

## Install
    pip install 'brainctl>=1.2.0'
    cp -r plugins/freqtrade/brainctl /path/to/your/freqtrade/user_data/plugins/brainctl_freqtrade

## Run
    freqtrade trade --strategy BrainctlSampleStrategy --config config.json

Every trade is now persistent across bot restarts.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np  # type: ignore
from pandas import DataFrame  # type: ignore

try:
    from freqtrade.strategy import IStrategy  # type: ignore
except ImportError:  # pragma: no cover
    # Allow the module to import in environments without Freqtrade for doc
    # generation / linting. The strategy class will be unusable but the
    # import path stays valid.
    class IStrategy:  # type: ignore[no-redef]
        pass


# Adjust this import to match wherever you dropped the plugin directory.
# If you installed it as a local package, `from brainctl_freqtrade import ...`
# also works.
from .. import BrainctlStrategyMixin

logger = logging.getLogger(__name__)


class BrainctlSampleStrategy(BrainctlStrategyMixin, IStrategy):
    """
    Minimal SMA crossover with brainctl persistent memory.

    NOTE: This is a demonstration strategy. Do not run with real funds
    without your own testing — it is intentionally simple.
    """

    # ----- Freqtrade required params -----
    INTERFACE_VERSION = 3
    minimal_roi = {"0": 0.05, "30": 0.025, "60": 0.01}
    stoploss = -0.05
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 30

    # ----- brainctl config -----
    brainctl_config = {
        "agent_id": "brainctl-sample",
        "project": "freqtrade-sample",
        "auto_orient": True,
        "auto_wrap_up": True,
        "log_trade_entry": True,
        "log_trade_exit": True,
    }

    # ----- indicators -----

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_fast"] = dataframe["close"].rolling(window=9).mean()
        dataframe["sma_slow"] = dataframe["close"].rolling(window=21).mean()

        # Example: record a one-time observation about the pair so future
        # sessions know we've seen it before.
        pair = metadata.get("pair")
        if pair and not getattr(self, f"_seen_{pair}", False):
            setattr(self, f"_seen_{pair}", True)
            self.brainctl_note(
                f"Started trading {pair} at {dataframe['close'].iloc[-1]:.4f}",
                category="observation",
            )

        return dataframe

    # ----- signals -----

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["sma_fast"] > dataframe["sma_slow"])
            & (dataframe["sma_fast"].shift(1) <= dataframe["sma_slow"].shift(1)),
            ["enter_long", "enter_tag"],
        ] = (1, "sma_cross_up")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["sma_fast"] < dataframe["sma_slow"])
            & (dataframe["sma_fast"].shift(1) >= dataframe["sma_slow"].shift(1)),
            ["exit_long", "exit_tag"],
        ] = (1, "sma_cross_down")
        return dataframe
