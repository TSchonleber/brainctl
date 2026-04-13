"""Shared write-gate logic used by Brain.remember() and the MCP write path.

The write gate implements brainctl's "cognitive memory, not a plain vector DB"
contract. Every write must pass:

  1. Reconsolidation lability check — if another agent opened the 20-min
     lability window on a target memory, reject the write.
  2. Surprise scoring — how novel is this content vs. existing memories?
  3. Arousal-precision coupling — amplify writes under high-arousal state.
  4. Valence-gated encoding — suppress writes during strong negative affect.
  5. Pre-worthiness fast-reject — surprise * confidence * trust * arousal * valence.
  6. W(m) semantic gate — deeper novelty check via lib/write_decision.py
     against vec_memories (requires embedding blob + sqlite-vec).

``evaluate_write()`` returns a :class:`GateDecision` the caller uses to
proceed, reject, or downgrade the write tier.
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Source trust weights mirror mcp_server._SOURCE_TRUST_WEIGHTS so Brain
# and MCP reach the same worthiness score for equivalent inputs.
SOURCE_TRUST_WEIGHTS: dict[str, float] = {
    "human_verified": 1.0,
    "mcp_tool": 0.85,
    "llm_inference": 0.7,
    "external_doc": 0.5,
    "python_api": 0.85,  # Brain.remember() from direct Python use
}

PRE_WORTHINESS_FLOOR = 0.3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Lability (reconsolidation window) — moved here from mcp_tools_consolidation
# so it gates writes, not just recall boosting.
# ---------------------------------------------------------------------------


def is_labile(row, agent_id: str) -> tuple[bool, str]:
    """Return (is_labile, reason) for a memory row.

    A memory is "labile" when a retrieval recently opened a ~20-minute
    reconsolidation window (Nader 2000, Ecker 2015). Only the agent that
    opened the window may rewrite it — cross-agent writes during an open
    window are rejected to prevent race conditions.
    """
    if isinstance(row, sqlite3.Row):
        labile_until = row["labile_until"]
        labile_agent = row["labile_agent_id"]
    else:
        labile_until = row.get("labile_until")
        labile_agent = row.get("labile_agent_id")

    if not labile_until:
        return False, "no lability window open"

    try:
        exp = datetime.fromisoformat(str(labile_until).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if _utc_now() > exp:
            return False, "lability window expired"
    except Exception:
        return False, "invalid labile_until timestamp"

    if labile_agent and labile_agent != agent_id:
        return False, f"lability opened by different agent ({labile_agent})"

    return True, "lability window active"


def check_lability_block(db: sqlite3.Connection, memory_id: int, writing_agent_id: str) -> str | None:
    """Write-side reconsolidation guard.

    If ``memory_id`` is in an open lability window owned by a *different*
    agent, return a rejection reason. Otherwise return ``None``.

    During the 20-min window only the agent that opened it may overwrite.
    The task-side guard (:func:`is_labile`) is used for recall boosting.
    """
    try:
        row = db.execute(
            "SELECT labile_until, labile_agent_id FROM memories "
            "WHERE id = ? AND retired_at IS NULL",
            (memory_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # lability columns may be absent on very old DBs — fail open.
        return None
    if not row:
        return None

    labile_until = row["labile_until"] if isinstance(row, sqlite3.Row) else row[0]
    labile_agent = row["labile_agent_id"] if isinstance(row, sqlite3.Row) else row[1]
    if not labile_until:
        return None
    try:
        exp = datetime.fromisoformat(str(labile_until).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if _utc_now() > exp:
            return None  # window has closed, write is fine
    except Exception:
        return None
    if labile_agent and labile_agent != writing_agent_id:
        return (
            f"Memory {memory_id} is in an open reconsolidation window owned by "
            f"agent '{labile_agent}'. Only that agent may overwrite until the "
            f"window closes at {labile_until}."
        )
    return None


# ---------------------------------------------------------------------------
# write_decision.py loader (unchanged public behavior)
# ---------------------------------------------------------------------------


def load_write_decision_module():
    """Load the write_decision module from the built-in lib directory.

    Returns the module or None if loading fails.
    """
    user_path = Path.home() / "agentmemory" / "bin" / "lib" / "write_decision.py"
    builtin_path = Path(__file__).parent / "lib" / "write_decision.py"

    for path in (user_path, builtin_path):
        if path.exists():
            try:
                spec = importlib.util.spec_from_file_location("write_decision", str(path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception as exc:
                logger.debug("Failed to load write_decision from %s: %s", path, exc)
                continue

    return None


def run_write_gate(blob, confidence, category, scope, get_vec_db_fn, force=False):
    """Legacy low-level W(m) gate call — kept for backward compatibility.

    New code should call :func:`evaluate_write`, which also handles surprise,
    lability, arousal/valence modulation, and pre-worthiness fast-reject.
    """
    if force or not blob:
        return (None, "", {})

    wd = load_write_decision_module()
    if not wd:
        logger.debug("write_decision module not available — gate skipped")
        return (None, "", {})

    vdb = get_vec_db_fn()
    if not vdb:
        return (None, "", {})

    try:
        return wd.gate_write(
            candidate_blob=blob,
            confidence=confidence,
            temporal_class=None,
            category=category,
            scope=scope,
            db_vec=vdb,
            force=False,
        )
    except Exception as exc:
        logger.debug("Write gate execution failed: %s", exc)
        return (None, "", {})
    finally:
        vdb.close()


# ---------------------------------------------------------------------------
# Surprise scoring — Python-API variant. Uses the same FTS/word-overlap
# fallback as mcp_server._surprise_score_mcp so the two paths agree when
# embeddings are unavailable (i.e. during tests that don't run Ollama).
# ---------------------------------------------------------------------------


def _word_overlap_surprise(db: sqlite3.Connection, content: str) -> tuple[float, str]:
    try:
        words = set(content.lower().split())
        if not words:
            return 1.0, "empty"
        query_words = list(words)[:20]
        rows = db.execute(
            "SELECT content FROM memories WHERE retired_at IS NULL AND content LIKE ? LIMIT 5",
            (f"%{' '.join(query_words[:5])}%",),
        ).fetchall()
        if not rows:
            return 1.0, "fts5_no_matches"
        max_overlap = 0.0
        for row in rows:
            existing = row["content"] if isinstance(row, sqlite3.Row) else row[0]
            existing_words = set((existing or "").lower().split())
            if not existing_words:
                continue
            intersection = words & existing_words
            union = words | existing_words
            overlap = len(intersection) / len(union) if union else 0.0
            if overlap > max_overlap:
                max_overlap = overlap
        if max_overlap > 0.9:
            surprise = 0.1 + (1.0 - max_overlap) * 2.0
        elif max_overlap < 0.1:
            surprise = 0.9 + (0.1 - max_overlap)
        else:
            surprise = 1.0 - max_overlap
        return round(max(0.0, min(1.0, surprise)), 4), "fts5"
    except Exception as exc:
        logger.debug("word-overlap surprise failed: %s", exc)
        return 0.7, "fts5_error"


def _compute_valence_scale(db: sqlite3.Connection, agent_id: str) -> float:
    """Mirror mcp_server valence scaling: pull the agent's latest affect_log
    valence and convert to a worthiness multiplier.
    """
    try:
        row = db.execute(
            "SELECT valence FROM affect_log WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if not row:
            return 1.0
        val = row["valence"] if isinstance(row, sqlite3.Row) else row[0]
        v = float(val or 0.0)
        if v < -0.5:
            return 0.7
        if v > 0.5:
            return 1.15
        return 1.0
    except sqlite3.OperationalError:
        return 1.0
    except Exception:
        return 1.0


def _compute_arousal_gain(content: str) -> float:
    try:
        from agentmemory.affect import arousal_write_boost, classify_affect

        affect = classify_affect(content)
        return float(arousal_write_boost(affect.get("arousal", 0.0)))
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# High-level gate decision
# ---------------------------------------------------------------------------


@dataclass
class GateDecision:
    """Outcome of :func:`evaluate_write`.

    ``accepted`` is False when the write must be dropped. ``reason`` is a
    short human-readable string suitable for logging or raising. ``components``
    carries the score breakdown for diagnostics.
    """

    accepted: bool
    reason: str = ""
    score: float | None = None
    surprise: float | None = None
    surprise_method: str = ""
    pre_worthiness: float | None = None
    valence_scale: float = 1.0
    arousal_gain: float = 1.0
    write_tier: str = "full"
    components: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "score": self.score,
            "surprise": self.surprise,
            "surprise_method": self.surprise_method,
            "pre_worthiness": self.pre_worthiness,
            "valence_scale": self.valence_scale,
            "arousal_gain": self.arousal_gain,
            "write_tier": self.write_tier,
            "components": self.components,
        }


def evaluate_write(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    content: str,
    category: str,
    scope: str = "global",
    confidence: float = 1.0,
    source: str = "python_api",
    force: bool = False,
    supersedes_id: int | None = None,
    embed_fn: Callable[[str], bytes | None] | None = None,
    get_vec_db_fn: Callable[[], sqlite3.Connection | None] | None = None,
) -> GateDecision:
    """Run the full write gate and return a :class:`GateDecision`.

    The gate mirrors ``mcp_server.tool_memory_add``'s write path: lability →
    surprise → arousal/valence modulation → pre-worthiness floor → W(m)
    semantic gate. The two callers may diverge in embedding availability
    (MCP always tries Ollama; Brain can skip it), so the word-overlap
    surprise path is the common fallback used by both.

    Callers are responsible for emitting rejection events and performing
    the actual INSERT. This function never writes to ``memories`` itself.
    """
    if force:
        return GateDecision(
            accepted=True,
            reason="",
            score=None,
            write_tier="full",
            components={"forced": True},
        )

    # 1. Lability — reject cross-agent writes into an open reconsolidation window.
    if supersedes_id is not None:
        block = check_lability_block(db, supersedes_id, agent_id)
        if block:
            return GateDecision(accepted=False, reason=block, write_tier="skip")

    source_trust = SOURCE_TRUST_WEIGHTS.get(source, 0.7)

    # 2. Surprise — try embedding-based first (if caller provided one),
    # then fall back to word-overlap so the path works offline.
    blob: bytes | None = None
    if embed_fn is not None:
        try:
            blob = embed_fn(content)
        except Exception as exc:
            logger.debug("embed_fn raised: %s", exc)
            blob = None

    surprise, surprise_method = _word_overlap_surprise(db, content)

    arousal_gain = _compute_arousal_gain(content)
    valence_scale = _compute_valence_scale(db, agent_id)

    importance_estimate = confidence
    pre_redundancy = 0.5 if (surprise is not None and surprise < 0.2) else 0.0
    pre_worthiness = (
        (surprise or 0.7)
        * importance_estimate
        * source_trust
        * (1.0 - pre_redundancy)
        * arousal_gain
        * valence_scale
    )

    if pre_worthiness < PRE_WORTHINESS_FLOOR:
        return GateDecision(
            accepted=False,
            reason=(
                f"Low surprise/worthiness ({pre_worthiness:.3f}): content is too "
                f"similar to existing memories (surprise={surprise})"
            ),
            score=None,
            surprise=surprise,
            surprise_method=surprise_method,
            pre_worthiness=round(pre_worthiness, 4),
            valence_scale=valence_scale,
            arousal_gain=arousal_gain,
            write_tier="skip",
        )

    # 3. Deeper W(m) semantic gate — requires both an embedding and a vec_db.
    score: float | None = None
    reason = ""
    components: dict = {}
    if blob and get_vec_db_fn is not None:
        wd = load_write_decision_module()
        if wd is not None:
            vdb = None
            try:
                vdb = get_vec_db_fn()
                if vdb is not None:
                    score, reason, components = wd.gate_write(
                        candidate_blob=blob,
                        confidence=confidence,
                        temporal_class=None,
                        category=category,
                        scope=scope,
                        db_vec=vdb,
                        force=False,
                        arousal_gain=arousal_gain,
                        db_stats=db,
                        agent_id=agent_id,
                    )
            except Exception as exc:
                logger.debug("W(m) gate failed (non-fatal): %s", exc)
            finally:
                if vdb is not None:
                    try:
                        vdb.close()
                    except Exception:
                        pass

    if reason:
        return GateDecision(
            accepted=False,
            reason=f"W(m) gate rejected: {reason}",
            score=score,
            surprise=surprise,
            surprise_method=surprise_method,
            pre_worthiness=round(pre_worthiness, 4),
            valence_scale=valence_scale,
            arousal_gain=arousal_gain,
            write_tier="skip",
            components=components,
        )

    # 4. D-MEM RPE three-tier routing (mirrors mcp_server).
    if score is not None and score < 0.7:
        write_tier = "construct"
    else:
        write_tier = "full"

    return GateDecision(
        accepted=True,
        reason="",
        score=score,
        surprise=surprise,
        surprise_method=surprise_method,
        pre_worthiness=round(pre_worthiness, 4),
        valence_scale=valence_scale,
        arousal_gain=arousal_gain,
        write_tier=write_tier,
        components=components,
    )
