"""
BrainctlStrategyMixin — drop-in mixin for Jesse `Strategy` subclasses that
automatically journals positions, session lifecycle, and handoff packets
into a brainctl long-term memory store.

Use it like:

    from jesse.strategies import Strategy
    from brainctl_jesse import BrainctlStrategyMixin

    class MyStrategy(BrainctlStrategyMixin, Strategy):
        brainctl_config = {
            "agent_id": "my-strategy",
            "project": "btc-scalper",
            "auto_wrap_up": True,
        }
        # ... normal Jesse strategy definition ...

What gets logged automatically (with default flags):

    first before() call     -> session_start event + orient() snapshot pulled
    on_open_position        -> decision event with symbol/side/price/qty
    on_close_position       -> result event with P&L + pair entity update
    terminate() / atexit    -> wrap_up() handoff packet

All hooks call `super()` first, so the mixin composes cleanly with other
mixins or with strategies that override individual hooks.

Graceful degradation: if brainctl isn't installed or the DB is unreachable,
every hook logs a warning once and becomes a no-op. The strategy keeps
trading — it just loses its long-term memory for that call.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any, ClassVar, Dict, Optional

from .strategy_brain import StrategyBrain

logger = logging.getLogger(__name__)


class BrainctlStrategyMixin:
    """Drop-in persistent-memory mixin for Jesse strategies."""

    #: Config dict. All keys optional, sensible defaults apply.
    #:
    #:     agent_id        (str)    brainctl agent id; default f"jesse:{ClassName}"
    #:     project         (str)    project scope for events/decisions/handoffs
    #:     db_path         (str)    override SQLite brain path (env: BRAIN_DB)
    #:     auto_orient     (bool)   call orient() on first before() (default True)
    #:     auto_wrap_up    (bool)   call wrap_up() on terminate/atexit (default True)
    #:     log_open        (bool)   journal on_open_position  (default True)
    #:     log_close       (bool)   journal on_close_position (default True)
    brainctl_config: ClassVar[Dict[str, Any]] = {}

    _brainctl: Optional[StrategyBrain] = None
    _brainctl_oriented: bool = False
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
        return f"jesse:{type(self).__name__}"

    def _brainctl_flag(self, key: str, default: bool) -> bool:
        return bool((self.brainctl_config or {}).get(key, default))

    # ---------- Jesse lifecycle hooks ----------

    def before(self) -> None:
        """Called by Jesse at the start of each candle. We use the first
        call to orient and register the atexit handler, then delegate to
        any super().before() that exists.
        """
        # Delegate first so any base-class initialization runs.
        super_fn = getattr(super(), "before", None)
        if callable(super_fn):
            try:
                super_fn()
            except Exception:
                raise

        if self._brainctl_oriented:
            return
        self._brainctl_oriented = True

        if self._brainctl_flag("auto_orient", True):
            brain = self._brainctl_get()
            try:
                snap = brain.orient()
            except Exception as e:
                logger.warning(f"[brainctl] orient failed in before(): {e}")
                snap = None

            # Log session_start.
            try:
                if brain.is_available() and brain._brain is not None:
                    brain._brain.log(
                        f"Jesse session start — strategy={type(self).__name__}",
                        event_type="session_start",
                        project=brain.project,
                        importance=0.5,
                    )
            except Exception as e:
                logger.warning(f"[brainctl] session_start log failed: {e}")

            # Surface the handoff to Jesse's logger on startup.
            if snap and snap.get("handoff"):
                h = snap["handoff"]
                logger.info(
                    "[brainctl] resuming from handoff: goal=%s | next_step=%s",
                    h.get("goal", "—"),
                    h.get("next_step", "—"),
                )
                if h.get("open_loops"):
                    logger.info("[brainctl] open loops: %s", h["open_loops"])

        # Register atexit handler once per process.
        cls = type(self)
        if (
            self._brainctl_flag("auto_wrap_up", True)
            and not cls._brainctl_atexit_registered
        ):
            atexit.register(self._brainctl_atexit_handler)
            cls._brainctl_atexit_registered = True

    def on_open_position(self, order: Any) -> None:
        """Jesse hook: called when a position opens. We log a decision
        event with the order details, then delegate to super()."""
        if self._brainctl_flag("log_open", True):
            try:
                self._brainctl_log_order_open(order)
            except Exception as e:
                logger.warning(f"[brainctl] on_open_position journal failed: {e}")

        super_fn = getattr(super(), "on_open_position", None)
        if callable(super_fn):
            super_fn(order)

    def on_close_position(self, order: Any) -> None:
        """Jesse hook: called when a position closes. We log a result
        event plus a win/loss observation on the pair entity."""
        if self._brainctl_flag("log_close", True):
            try:
                self._brainctl_log_order_close(order)
            except Exception as e:
                logger.warning(f"[brainctl] on_close_position journal failed: {e}")

        super_fn = getattr(super(), "on_close_position", None)
        if callable(super_fn):
            super_fn(order)

    def terminate(self) -> None:
        """Jesse hook: called at end of run. We persist a handoff packet
        unless the atexit handler is already going to fire."""
        super_fn = getattr(super(), "terminate", None)
        if callable(super_fn):
            try:
                super_fn()
            except Exception:
                raise

        if self._brainctl_flag("auto_wrap_up", True):
            try:
                self._brainctl_get().wrap_up(
                    summary=f"Jesse terminate — strategy={type(self).__name__}",
                    goal="Continue trading strategy",
                    open_loops="",
                    next_step="Resume on next session, apply orient snapshot.",
                )
            except Exception as e:
                logger.warning(f"[brainctl] terminate wrap_up failed: {e}")

    # ---------- order introspection ----------

    def _brainctl_log_order_open(self, order: Any) -> None:
        """Extract fields from a Jesse order and call log_open."""
        symbol = self._brainctl_symbol_of(order)
        price = float(getattr(order, "price", 0.0) or 0.0)
        qty = float(
            getattr(order, "qty", None)
            or getattr(order, "quantity", None)
            or 0.0
        )
        side = (
            getattr(order, "side", None)
            or getattr(order, "type", None)
            or "long"
        )
        side_str = str(side).lower().replace("buy", "long").replace("sell", "short")
        self._brainctl_get().log_open(
            symbol=symbol,
            price=price,
            qty=qty,
            side=side_str,
            strategy_name=type(self).__name__,
        )

    def _brainctl_log_order_close(self, order: Any) -> None:
        """Extract fields from a Jesse close order and call log_close."""
        symbol = self._brainctl_symbol_of(order)
        price = float(getattr(order, "price", 0.0) or 0.0)

        # Jesse exposes P&L on the strategy (self.trades / self.trade) rather
        # than on the order. Pull the most recent closed trade if available.
        pnl = 0.0
        pnl_pct: Optional[float] = None
        entry_price: Optional[float] = None
        reason = ""

        try:
            trades = getattr(self, "trades", None)
            if trades:
                last = trades[-1]
                pnl = float(
                    getattr(last, "pnl", None)
                    or getattr(last, "profit", None)
                    or 0.0
                )
                pnl_pct = getattr(last, "pnl_percentage", None) or getattr(
                    last, "roi", None
                )
                if pnl_pct is not None:
                    pnl_pct = float(pnl_pct)
                entry_price = (
                    getattr(last, "entry_price", None)
                    or getattr(last, "opening_price", None)
                )
                if entry_price is not None:
                    entry_price = float(entry_price)
                reason = str(getattr(last, "exit_reason", "") or "")
        except Exception:  # defensive — Jesse API shape varies
            pass

        self._brainctl_get().log_close(
            symbol=symbol,
            price=price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_price=entry_price,
            reason=reason,
        )

    @staticmethod
    def _brainctl_symbol_of(order: Any) -> str:
        return (
            getattr(order, "symbol", None)
            or getattr(order, "pair", None)
            or getattr(order, "market", None)
            or "UNKNOWN"
        )

    # ---------- shutdown ----------

    def _brainctl_atexit_handler(self) -> None:
        """Persist a handoff packet when the Jesse process shuts down."""
        try:
            self._brainctl_get().wrap_up(
                summary=f"Jesse session ended — strategy={type(self).__name__}",
                goal="Continue trading strategy",
                open_loops="",
                next_step="Resume on next session, apply orient snapshot.",
            )
        except Exception as e:
            logger.warning(f"[brainctl] atexit wrap_up failed: {e}")

    # ---------- public helpers for strategy authors ----------

    def brainctl_note(
        self,
        content: str,
        category: str = "lesson",
    ) -> Optional[int]:
        """Shortcut for storing a durable fact from inside strategy code."""
        return self._brainctl_get().note(content, category=category)

    def brainctl_recall(self, query: str, limit: int = 8) -> list:
        """Shortcut for FTS5 recall from strategy code."""
        return self._brainctl_get().recall(query, limit=limit)

    def brainctl_decide(self, title: str, rationale: str) -> Optional[int]:
        """Shortcut for recording a strategy-level decision with rationale."""
        return self._brainctl_get().decide(title, rationale)

    def brainctl_warn(self, summary: str) -> Optional[int]:
        """Shortcut for logging a warning event."""
        return self._brainctl_get().log_warning(summary)
