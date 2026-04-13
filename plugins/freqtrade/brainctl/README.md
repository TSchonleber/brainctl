# brainctl for Freqtrade

Persistent memory for your [Freqtrade](https://github.com/freqtrade/freqtrade) strategies, powered by [brainctl](https://pypi.org/project/brainctl/).

SQLite-backed long-term memory with FTS5 search, optional vector recall, a knowledge graph, and session handoff packets. One file, zero servers, zero API keys. MIT licensed.

> Every Freqtrade user keeps a "what I tried" note somewhere — scratched in a Notion page, buried in a git branch, or lost entirely. This plugin turns that note into structured, queryable, cross-session state that your strategy can read from and write to at runtime.

## What you get

**Automatic journaling** of the trading lifecycle:

| Event | brainctl write |
|---|---|
| `bot_start` | Pulls `orient()` snapshot → logs pending handoff to Freqtrade's logger, records `session_start` event |
| `confirm_trade_entry` | `decision` event with pair, side, rate, amount, entry tag |
| `confirm_trade_exit` | `result` event with P&L + pair entity update with win/loss observation |
| process shutdown | `wrap_up()` creates a handoff packet for the next `bot_start` |

**Helpers available inside your strategy code:**

- `self.brainctl_note(content, category=...)` — store a durable observation
- `self.brainctl_recall(query, limit=...)` — FTS5 search over long-term memory
- `self.brainctl_decide(title, rationale)` — record a strategy-level decision
- `self.brainctl_warn(summary)` — log a warning event (API errors, unusual markets)

And you can always reach the full `Brain` via the underlying helper:

```python
b = self._brainctl_get()
b.brain.search("btc volatility")       # full brainctl Brain instance
```

## Install

```bash
pip install 'brainctl>=1.2.0'
```

Then drop this plugin into your Freqtrade project:

```bash
# Option A — copy into user_data (Freqtrade's native plugin location)
cp -r plugins/freqtrade/brainctl \
      /path/to/your/freqtrade/user_data/plugins/brainctl_freqtrade

# Option B — symlink for local development
ln -s $(pwd)/plugins/freqtrade/brainctl \
      /path/to/your/freqtrade/user_data/plugins/brainctl_freqtrade
```

Or install it as a local package via `pip install -e plugins/freqtrade/brainctl` if you prefer.

## Usage

Add `BrainctlStrategyMixin` to your strategy's class declaration. Order matters — put the mixin **before** `IStrategy`:

```python
from freqtrade.strategy import IStrategy
from brainctl_freqtrade import BrainctlStrategyMixin

class MyStrategy(BrainctlStrategyMixin, IStrategy):
    brainctl_config = {
        "agent_id": "my-strategy",
        "project": "btc-scalper",
    }

    # ... your normal Freqtrade strategy code ...
```

That's it. Every trade is now journaled. Every restart calls `orient()` and surfaces the previous session's handoff in the bot log. Every shutdown writes a `wrap_up()` packet.

See [`examples/sample_strategy.py`](./examples/sample_strategy.py) for a complete working example.

## Config

All fields on `brainctl_config` are optional.

| Key | Default | Description |
|---|---|---|
| `agent_id` | `freqtrade:<ClassName>` | brainctl agent identifier for scoping writes. Use per-strategy IDs if you run multiple strategies on one bot. |
| `project` | *(none)* | Project scope for events, decisions, and handoffs. |
| `db_path` | `~/agentmemory/db/brain.db` | Override the SQLite brain file. Env fallback: `BRAIN_DB`. |
| `auto_orient` | `true` | Call `orient()` on `bot_start` and surface the handoff. |
| `auto_wrap_up` | `true` | Register an `atexit` hook to call `wrap_up()` on process shutdown. |
| `log_trade_entry` | `true` | Journal `confirm_trade_entry` as a `decision` event. |
| `log_trade_exit` | `true` | Journal `confirm_trade_exit` as a `result` event + pair entity update. |

## Why this exists

Freqtrade is a stateless strategy runner. Every time you restart the bot, your strategy starts from scratch. Every time you tweak params, the "why" of that tweak lives in your head or in git commit messages, not in a form the strategy can query. Every postmortem of a losing trade exists only in Discord screenshots.

brainctl fixes all three by making your strategy's experience durable:

- **Cross-session state** — `orient()` returns the handoff from your last run so the bot knows what was in flight when it restarted. No more "wait, was that a stop-loss exit or a manual close?"
- **Structured decision journal** — every trade is a timestamped event with the rate, pair, and tag. Query across weeks: `self.brainctl_recall("BTC/USDT loss")`.
- **Entity graph per pair** — every pair accumulates win/loss observations. Build a strategy that avoids pairs with too many consecutive losses: `self.brainctl_recall(pair).count("loss")`.
- **Postmortems as data** — instead of notes in Notion, call `self.brainctl_note("Volatility spiked during Powell speech — skipped entries")` in your strategy and that lesson survives every restart.

## Graceful degradation

If brainctl isn't installed or the SQLite file is unreachable, every mixin hook logs a warning once and becomes a no-op. **Your strategy keeps trading.** It just loses its long-term memory for that call. Fix the config, restart the bot, no trades lost.

## Storage footprint

- Plugin code: ~15 KB Python
- `brainctl` package (PyPI): ~2 MB
- `brain.db` SQLite file: starts at ~100 KB, grows ~1 KB per event/memory
- RSS overhead at runtime: ~2 MB (in-process, no subprocess, no sidecar)

There is no subprocess, no background daemon, no network call. The entire memory layer runs in the same Python process as your Freqtrade strategy.

## Compatibility

- Freqtrade ≥ 2024.4 (Interface v3)
- brainctl ≥ 1.2.0
- Python ≥ 3.11

Designed to compose cleanly with other Freqtrade mixins — every hook calls `super()` first before adding brainctl side effects, so you can stack this with custom base strategies or community mixins.

## License

MIT. Part of the [brainctl](https://github.com/TSchonleber/brainctl) project. Contributions welcome.
