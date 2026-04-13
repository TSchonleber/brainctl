"""
BrainctlStrategyMixin — a drop-in mixin for Freqtrade IStrategy subclasses
that automatically journals trades, decisions, and session lifecycle events
into a brainctl long-term memory store.

Use it like:

    from freqtrade.strategy import IStrategy
    from brainctl_freqtrade import BrainctlStrategyMixin

    class MyStrategy(BrainctlStrategyMixin, IStrategy):
        brainctl_config = {
            "agent_id": "my-strategy",
            "project": "btc-scalper",
            "auto_wrap_up": True,
        }
        # ... normal Freqtrade strategy definition ...

What gets logged automatically (when using the default hooks):

    bot_start              -> session_start event + orient() snapshot pulled
    confirm_trade_entry    -> decision event with pair/rate/amount/tag
    confirm_trade_exit     -> result event with P&L + pair entity update
    process shutdown       -> wrap_up() handoff packet (via atexit)

All hooks call `super()` first, so you can compose the mixin with other
mixins or override individual hooks in your strategy without losing the
brainctl side effects.

Graceful degradation: if brainctl isn't installed or the DB is unreachable,
every hook logs a warning once and becomes a no-op. Your strategy keeps
trading — it just loses its long-term memory for that call.
"""

from __future__ import annotations

import atexit
import logging
from datetime import datetime
from typing import Any, ClassVar, Dict, Optional

from .strategy_brain import StrategyBrain

logger = logging.getLogger(__name__)


class BrainctlStrategyMixin:
    """Drop-in persistent-memory mixin for Freqtrade strategies."""

    #: Subclasses set this to configure agent_id / project / db_path / etc.
    #: All keys are optional — sensible defaults apply.
    #:
    #: Recognized keys:
    #:     agent_id        (str)    brainctl agent identifier for scoping writes
    #:     project         (str)    project scope for events/decisions/handoffs
    #:     db_path         (str)    override SQLite brain path (env: BRAIN_DB)
    #:     auto_wrap_up    (bool)   call wrap_up() on process exit (default True)
    #:     auto_orient     (bool)   call orient() on bot_start (default True)
    #:     log_trade_entry (bool)   journal confirm_trade_entry (default True)
    #:     log_trade_exit  (bool)   journal confirm_trade_exit  (default True)
    brainctl_config: ClassVar[Dict[str, Any]] = {}

    _brainctl: Optional[StrategyBrain] = None
    _brainctl_atexit_registered: bool = False

    # ---------- accessors ----------

    def _brainctl_get(self) -> StrategyBrain:
        """Lazy-initialize the StrategyBrain helper."""
        if self._brainctl is None:
            cfg = self.brainctl_config or {}
            self._brainctl = StrategyBrain(
                agent_id=cfg.get("agent_id") or self._brainctl_default_agent_id(),
                project=cfg.get("project"),
                db_path=cfg.get("db_path"),
            )
        return self._brainctl

    def _brainctl_default_agent_id(self) -> str:
        """Use the strategy class name as a default agent_id so multiple
        strategies in the same bot get separate write scopes."""
        return f"freqtrade:{type(self).__name__}"

    def _brainctl_flag(self, key: str, default: bool) -> bool:
        return bool((self.brainctl_config or {}).get(key, default))

    # ---------- Freqtrade lifecycle hooks ----------

    def bot_start(self, **kwargs: Any) -> None:
        super_fn = getattr(super(), "bot_start", None)
        if callable(super_fn):
            try:
                super_fn(**kwargs)
            except TypeError:
                super_fn()  # type: ignore[misc]

        if not self._brainctl_flag("auto_orient", True):
            return

        brain = self._brainctl_get()
        try:
            snap = brain.orient()
        except Exception as e:
            logger.warning(f"[brainctl] orient failed in bot_start: {e}")
            snap = None

        # Register atexit handler once per process, regardless of how many
        # strategies are loaded.
        cls = type(self)
        if (
            self._brainctl_flag("auto_wrap_up", True)
            and not cls._brainctl_atexit_registered
        ):
            atexit.register(self._brainctl_atexit_handler)
            cls._brainctl_atexit_registered = True

        # Log the session_start event.
        try:
            brain._brain and brain._brain.log(  # type: ignore[attr-defined]
                f"Freqtrade bot_start — strategy={type(self).__name__}",
                event_type="session_start",
                project=brain.project,
                importance=0.5,
            )
        except Exception as e:
            logger.warning(f"[brainctl] session_start log failed: {e}")

        # Surface the handoff to Freqtrade's logger so users see it in their
        # bot output on startup.
        if snap and snap.get("handoff"):
            h = snap["handoff"]
            logger.info(
                "[brainctl] resuming from handoff: goal=%s | next_step=%s",
                h.get("goal", "—"),
                h.get("next_step", "—"),
            )
            if h.get("open_loops"):
                logger.info("[brainctl] open loops: %s", h["open_loops"])

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str] = None,
        side: str = "long",
        **kwargs: Any,
    ) -> bool:
        super_fn = getattr(super(), "confirm_trade_entry", None)
        if callable(super_fn):
            approved = super_fn(
                pair=pair,
                order_type=order_type,
                amount=amount,
                rate=rate,
                time_in_force=time_in_force,
                current_time=current_time,
                entry_tag=entry_tag,
                side=side,
                **kwargs,
            )
        else:
            approved = True

        if approved and self._brainctl_flag("log_trade_entry", True):
            try:
                self._brainctl_get().log_entry(
                    pair=pair,
                    rate=rate,
                    amount=amount,
                    side=side,
                    entry_tag=entry_tag,
                    extra=f"order_type={order_type}",
                )
            except Exception as e:
                logger.warning(f"[brainctl] confirm_trade_entry journal failed: {e}")

        return approved

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Any,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs: Any,
    ) -> bool:
        super_fn = getattr(super(), "confirm_trade_exit", None)
        if callable(super_fn):
            approved = super_fn(
                pair=pair,
                trade=trade,
                order_type=order_type,
                amount=amount,
                rate=rate,
                time_in_force=time_in_force,
                exit_reason=exit_reason,
                current_time=current_time,
                **kwargs,
            )
        else:
            approved = True

        if approved and self._brainctl_flag("log_trade_exit", True):
            try:
                entry_rate = getattr(trade, "open_rate", None)
                if entry_rate:
                    profit_ratio = (rate - entry_rate) / entry_rate
                else:
                    profit_ratio = 0.0

                duration = None
                open_date = getattr(trade, "open_date_utc", None) or getattr(
                    trade, "open_date", None
                )
                if open_date is not None:
                    try:
                        duration = str(current_time - open_date)
                    except Exception:
                        duration = None

                self._brainctl_get().log_exit(
                    pair=pair,
                    rate=rate,
                    profit_ratio=profit_ratio,
                    exit_reason=exit_reason,
                    entry_rate=entry_rate,
                    duration=duration,
                )
            except Exception as e:
                logger.warning(f"[brainctl] confirm_trade_exit journal failed: {e}")

        return approved

    # ---------- shutdown ----------

    def _brainctl_atexit_handler(self) -> None:
        """Persist a handoff packet when the Freqtrade process shuts down."""
        try:
            brain = self._brainctl_get()
            brain.wrap_up(
                summary=f"Freqtrade session ended — strategy={type(self).__name__}",
                goal="Continue trading strategy",
                open_loops="",
                next_step="Resume on next bot_start, apply orient snapshot.",
            )
        except Exception as e:
            logger.warning(f"[brainctl] atexit wrap_up failed: {e}")

    # ---------- public helpers for strategy authors ----------

    def brainctl_note(
        self,
        content: str,
        category: str = "lesson",
    ) -> Optional[int]:
        """Shortcut for storing a durable fact from inside strategy code.

        Example:
            if self.dataframe_is_stale(dataframe):
                self.brainctl_note(
                    "Data feed stale > 5min for BTC/USDT — fallback to cached",
                    category="environment",
                )
        """
        return self._brainctl_get().note(content, category=category)

    def brainctl_recall(self, query: str, limit: int = 8) -> list:
        """Shortcut for FTS5 recall from strategy code."""
        return self._brainctl_get().recall(query, limit=limit)

    def brainctl_decide(self, title: str, rationale: str) -> Optional[int]:
        """Shortcut for recording a strategy-level decision with rationale."""
        return self._brainctl_get().decide(title, rationale)

    def brainctl_warn(self, summary: str) -> Optional[int]:
        """Shortcut for logging a warning event (unusual market, API error)."""
        return self._brainctl_get().log_warning(summary)
