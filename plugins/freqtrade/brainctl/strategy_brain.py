"""
StrategyBrain — lower-level brainctl wrapper for Freqtrade strategies
that prefer explicit calls over the automatic mixin.

All operations degrade gracefully: if brainctl is not installed or the
brain.db is unreachable, every method logs a warning and returns None
instead of raising. The strategy keeps trading — it just loses its
long-term memory for that call.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from agentmemory import Brain  # type: ignore
except ImportError:  # pragma: no cover
    Brain = None  # type: ignore


class StrategyBrain:
    """
    Explicit brainctl wrapper. Instantiate once in `bot_start` and call
    methods from your strategy's lifecycle hooks.

    Example:

        def bot_start(self, **kwargs):
            self.brain = StrategyBrain(
                agent_id="btc-scalper",
                project="btc-scalper",
            )
            ctx = self.brain.orient()
            if ctx and ctx.get("handoff"):
                logger.info(f"Resuming: {ctx['handoff'].get('goal')}")
    """

    def __init__(
        self,
        agent_id: str = "freqtrade",
        project: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self.agent_id = agent_id
        self.project = project
        self.db_path = db_path or os.environ.get("BRAIN_DB")
        self._brain: Optional[Any] = None
        self._available: Optional[bool] = None

    # ---------- lifecycle ----------

    @property
    def brain(self) -> Optional[Any]:
        """Lazy-initialize the underlying brainctl Brain. Returns None if
        brainctl is unavailable — callers should treat that as a no-op."""
        if self._available is False:
            return None
        if self._brain is None:
            if Brain is None:
                logger.warning(
                    "[brainctl] agentmemory is not installed; "
                    "StrategyBrain calls will be no-ops. "
                    "Install with: pip install brainctl"
                )
                self._available = False
                return None
            try:
                self._brain = Brain(db_path=self.db_path, agent_id=self.agent_id)
                self._available = True
            except Exception as e:
                logger.warning(f"[brainctl] failed to open brain: {e}")
                self._available = False
                return None
        return self._brain

    def is_available(self) -> bool:
        return self.brain is not None

    # ---------- session bookends ----------

    def orient(self, query: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Pull the session-start snapshot: pending handoff, recent events,
        active triggers, top memories. Returns None if brainctl is unavailable."""
        b = self.brain
        if b is None:
            return None
        try:
            return b.orient(project=self.project, query=query)
        except Exception as e:
            logger.warning(f"[brainctl] orient failed: {e}")
            return None

    def wrap_up(
        self,
        summary: str,
        goal: Optional[str] = None,
        open_loops: Optional[str] = None,
        next_step: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist a session handoff. Call on shutdown."""
        b = self.brain
        if b is None:
            return None
        try:
            return b.wrap_up(
                summary,
                goal=goal,
                open_loops=open_loops,
                next_step=next_step,
                project=self.project,
            )
        except Exception as e:
            logger.warning(f"[brainctl] wrap_up failed: {e}")
            return None

    # ---------- durable facts ----------

    def note(
        self,
        content: str,
        category: str = "lesson",
        tags: Optional[List[str]] = None,
        confidence: float = 1.0,
    ) -> Optional[int]:
        """Store a durable fact (strategy observation, market insight, etc.)."""
        b = self.brain
        if b is None:
            return None
        try:
            return b.remember(
                content, category=category, tags=tags, confidence=confidence
            )
        except Exception as e:
            logger.warning(f"[brainctl] remember failed: {e}")
            return None

    def recall(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        """FTS5 search over long-term memories. Returns [] on failure."""
        b = self.brain
        if b is None:
            return []
        try:
            result = b.search(query, limit=limit)
            if isinstance(result, dict):
                return result.get("results", [])
            return list(result or [])
        except Exception as e:
            logger.warning(f"[brainctl] search failed: {e}")
            return []

    # ---------- trade lifecycle ----------

    def log_entry(
        self,
        pair: str,
        rate: float,
        amount: float,
        side: str = "long",
        entry_tag: Optional[str] = None,
        extra: Optional[str] = None,
    ) -> Optional[int]:
        """Log a trade entry as a `decision` event."""
        b = self.brain
        if b is None:
            return None
        tag_str = f" tag={entry_tag}" if entry_tag else ""
        extra_str = f" {extra}" if extra else ""
        summary = (
            f"Enter {side} {pair} @ {rate:.6f} amount={amount:.6f}{tag_str}{extra_str}"
        )
        try:
            return b.log(
                summary,
                event_type="decision",
                project=self.project,
                importance=0.6,
            )
        except Exception as e:
            logger.warning(f"[brainctl] log_entry failed: {e}")
            return None

    def log_exit(
        self,
        pair: str,
        rate: float,
        profit_ratio: float,
        exit_reason: str,
        entry_rate: Optional[float] = None,
        duration: Optional[str] = None,
    ) -> Optional[int]:
        """Log a trade exit as a `result` event AND append a win/loss
        observation to the entity for the trading pair."""
        b = self.brain
        if b is None:
            return None
        profit_pct = profit_ratio * 100
        outcome = "win" if profit_ratio > 0 else "loss"
        entry_str = f" from {entry_rate:.6f}" if entry_rate is not None else ""
        dur_str = f" duration={duration}" if duration else ""
        summary = (
            f"Exit {pair} @ {rate:.6f}{entry_str} "
            f"({profit_pct:+.2f}%) reason={exit_reason}{dur_str}"
        )
        try:
            event_id = b.log(
                summary,
                event_type="result",
                project=self.project,
                importance=0.7 if outcome == "loss" else 0.5,
            )
            try:
                b.entity(
                    pair,
                    entity_type="service",
                    observations=[
                        f"{outcome} {profit_pct:+.2f}% ({exit_reason})"
                    ],
                )
            except Exception as e:
                logger.warning(f"[brainctl] entity update failed: {e}")
            return event_id
        except Exception as e:
            logger.warning(f"[brainctl] log_exit failed: {e}")
            return None

    def log_warning(self, summary: str) -> Optional[int]:
        """Log a warning event (stop-loss hit, API error, unusual condition)."""
        b = self.brain
        if b is None:
            return None
        try:
            return b.log(
                summary,
                event_type="warning",
                project=self.project,
                importance=0.6,
            )
        except Exception as e:
            logger.warning(f"[brainctl] log_warning failed: {e}")
            return None

    def decide(
        self,
        title: str,
        rationale: str,
    ) -> Optional[int]:
        """Record a strategy-level decision with its rationale. Use when
        you change a parameter, pivot a strategy, or accept a risk."""
        b = self.brain
        if b is None:
            return None
        try:
            return b.decide(title, rationale, project=self.project)
        except Exception as e:
            logger.warning(f"[brainctl] decide failed: {e}")
            return None
