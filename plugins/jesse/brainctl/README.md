# brainctl for Jesse

Persistent memory for your [Jesse](https://github.com/jesse-ai/jesse) algo-trading strategies, powered by [brainctl](https://pypi.org/project/brainctl/).

SQLite-backed long-term memory with FTS5 search, optional vector recall, a knowledge graph, and session handoff packets. One file, zero servers, zero API keys. MIT licensed.

> Jesse is a stateless strategy runner. Every backtest, every forward test, every live restart starts from scratch. This plugin turns each session's experience — every trade, every decision, every win or loss on a pair — into structured long-term memory that survives restarts and carries across sessions.

## What you get

**Automatic journaling** of the Jesse lifecycle:

| Jesse hook | brainctl write |
|---|---|
| first `before()` | `session_start` event + `orient()` snapshot surfaced to Jesse's logger |
| `on_open_position` | `decision` event with symbol, side, price, qty |
| `on_close_position` | `result` event with P&L + pair entity win/loss observation |
| `terminate()` / process exit | `wrap_up()` handoff packet |

**Helpers available inside your strategy code:**

- `self.brainctl_note(content, category=...)` — store a durable observation
- `self.brainctl_recall(query, limit=...)` — FTS5 search over long-term memory
- `self.brainctl_decide(title, rationale)` — record a strategy-level decision
- `self.brainctl_warn(summary)` — log a warning event

Full `Brain` access via `self._brainctl_get().brain` for anything advanced.

## Install

```bash
pip install 'brainctl>=1.2.0'
```

Then drop this plugin into your Jesse project:

```bash
# Option A — copy into your project
cp -r plugins/jesse/brainctl /path/to/your/jesse/brainctl_jesse

# Option B — symlink for local development
ln -s $(pwd)/plugins/jesse/brainctl /path/to/your/jesse/brainctl_jesse
```

Or install as a local package via `pip install -e plugins/jesse/brainctl`.

## Usage

Add `BrainctlStrategyMixin` to your strategy's class declaration. Order matters — mixin **before** `Strategy`:

```python
from jesse.strategies import Strategy
from brainctl_jesse import BrainctlStrategyMixin

class MyStrategy(BrainctlStrategyMixin, Strategy):
    brainctl_config = {
        "agent_id": "my-strategy",
        "project": "btc-scalper",
    }

    # ... your normal Jesse strategy code ...
```

See [`examples/sample_strategy.py`](./examples/sample_strategy.py) for a complete working example.

## Config

All fields on `brainctl_config` are optional.

| Key | Default | Description |
|---|---|---|
| `agent_id` | `jesse:<ClassName>` | brainctl agent identifier. Use per-strategy IDs if you run multiple strategies. |
| `project` | *(none)* | Project scope for events, decisions, and handoffs. |
| `db_path` | `~/agentmemory/db/brain.db` | Override SQLite brain file. Env fallback: `BRAIN_DB`. |
| `auto_orient` | `true` | Call `orient()` on the first `before()` and surface handoff. |
| `auto_wrap_up` | `true` | Register an `atexit` hook to `wrap_up()` on shutdown. Also fires on Jesse `terminate()`. |
| `log_open` | `true` | Journal `on_open_position` as a `decision` event. |
| `log_close` | `true` | Journal `on_close_position` as a `result` event + pair entity update. |

## Why brainctl for Jesse

Jesse is excellent at what it does — candle-by-candle strategy execution with clean hooks and a great backtester. But:

- **Backtests lose their own history.** You run a backtest, tune a param, run again. The previous run's lessons live only in your terminal scrollback.
- **Strategy tweaks lose their rationale.** You change `rsi_threshold` from 30 to 28. Why? Git commit message, maybe.
- **Pair-specific knowledge is invisible.** BTC/USDT loses 3 times in a row during high volatility. Your strategy can't know that unless you built it in by hand.

brainctl fixes all three. Every backtest writes to the same `brain.db`. Every decision is a queryable event. Every pair accumulates observations. Strategies can recall their own history at runtime:

```python
def update_position(self):
    recent_losses = self.brainctl_recall(f"{self.symbol} loss", limit=5)
    if len(recent_losses) >= 3:
        # Stand down on this pair — too many recent losses in memory.
        return
```

## Graceful degradation

If brainctl isn't installed or the SQLite file is unreachable, every mixin hook logs a warning once and becomes a no-op. **Your strategy keeps trading.** It just loses its long-term memory for that call. Fix the config, restart the bot, no trades lost.

## Storage footprint

- Plugin code: ~18 KB Python
- `brainctl` package: ~2 MB
- `brain.db` SQLite file: starts at ~100 KB, grows ~1 KB per event/memory
- RSS overhead at runtime: **~2 MB** (in-process, no subprocess, no sidecar)

No subprocess, no background daemon, no network call. Same Python runtime as your Jesse process.

## Compatibility

- Jesse ≥ 1.0
- brainctl ≥ 1.2.0
- Python ≥ 3.11

Designed to compose cleanly with other strategy mixins — every hook calls `super()` first before adding brainctl side effects.

## Differences from the Freqtrade plugin

The Jesse plugin mirrors the [Freqtrade plugin](../../freqtrade/brainctl/) in shape and config surface, but the lifecycle hooks differ:

| Role | Freqtrade | Jesse |
|---|---|---|
| Session start | `bot_start()` | first `before()` call |
| Entry | `confirm_trade_entry()` | `on_open_position(order)` |
| Exit | `confirm_trade_exit()` | `on_close_position(order)` |
| Session end | `atexit` only | `terminate()` + `atexit` |
| P&L source | Passed into `confirm_trade_exit` | Derived from `self.trades[-1]` |

When a third trading integration lands the shared `StrategyBrain` helper should be extracted into `agentmemory.integrations.trading`. For now it's duplicated between the two plugins to keep each self-contained.

## License

MIT. Part of the [brainctl](https://github.com/TSchonleber/brainctl) project.
