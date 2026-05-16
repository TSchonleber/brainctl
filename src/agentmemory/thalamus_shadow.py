"""Thalamic gate shadow-consult helper used by the W(m) write gate hookpoint.

Phase 2 of the thalamus subsystem: when the W(m) gate decides on a write,
also consult the thalamic gate to compute what it *would* have done — record
that as an audit row in `thalamic_shadow_decisions`. Production behavior is
unchanged; this only writes one row per gate decision so we can compare
shadow decisions against actual outcomes before flipping to enforcement.

Design:
- Never raises. Any error (missing schema, locked DB, etc.) is swallowed
  and logged at debug. The W(m) gate's contract is unchanged.
- Sector mapping mirrors `mcp_tools_thalamus._sector_for_candidate` but is
  inlined here to avoid pulling in the MCP type machinery on the write hot
  path.
- Decision policy (shadow-only — does NOT alter behavior):
    * suppression > 0.7  → "tier_downgrade" (in real mode would tier 'full'→'construct')
    * armed_for_burst AND surprise_score > 0.6 → "burst_fire"
    * otherwise → "pass"
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from agentmemory.paths import get_db_path

logger = logging.getLogger(__name__)

_SUPPRESSION_DOWNGRADE_THRESHOLD = 0.7
_BURST_SURPRISE_THRESHOLD = 0.6

_PII_TOKENS = ("pii", "secret", "credential", "wallet", "private-key", "token")


def _sector_for_scope_category(scope: str | None, category: str | None) -> str:
    s = (scope or "").lower()
    c = (category or "").lower()
    if any(t in s for t in _PII_TOKENS) or any(t in c for t in _PII_TOKENS):
        return "pii_sensitive"
    if c in {"decision", "lesson"}:
        return "belief"
    if c == "consolidation" or "consolidation" in s:
        return "consolidation"
    if c in {"user", "identity", "environment"}:
        return "sensory_external"
    return "memory_recall"


def consult_for_write(
    *,
    scope: str | None,
    category: str | None,
    surprise_score: float | None = None,
    agent_id: str | None = None,
    channel_id: str | None = None,
    payload_hash: str | None = None,
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Record a shadow-mode decision for a W(m) write event.

    Returns the decision dict on success, or None if the consult was skipped
    (schema missing, DB locked, etc.). Never raises.
    """
    sector = _sector_for_scope_category(scope, category)
    try:
        path = db_path or str(get_db_path())
        conn = sqlite3.connect(path, timeout=2.0)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("thalamus shadow: cannot open db: %s", exc)
        return None
    try:
        try:
            row = conn.execute(
                """
                SELECT AVG(suppression) AS suppression,
                       MAX(armed_for_burst) AS armed,
                       AVG(bottomup_drive) AS bottomup_drive
                FROM thalamic_gate
                WHERE sector = ?
                """,
                (sector,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Schema not applied yet (migration 050 not run). Silent skip.
            return None

        suppression = float(row[0]) if row and row[0] is not None else 0.0
        armed = bool(row[1]) if row and row[1] is not None else False
        bottomup_drive = float(row[2]) if row and row[2] is not None else 0.0
        surprise = float(surprise_score) if surprise_score is not None else 0.0

        if suppression > _SUPPRESSION_DOWNGRADE_THRESHOLD:
            decision = "tier_downgrade"
            reason = f"sector {sector} suppression {suppression:.2f} > {_SUPPRESSION_DOWNGRADE_THRESHOLD}"
        elif armed and surprise > _BURST_SURPRISE_THRESHOLD:
            decision = "burst_fire"
            reason = f"sector {sector} armed; surprise {surprise:.2f} > {_BURST_SURPRISE_THRESHOLD}"
        else:
            decision = "pass"
            reason = None

        try:
            conn.execute(
                """
                INSERT INTO thalamic_shadow_decisions (
                    agent_id, source_call, sector, channel_id, decision,
                    reason, suppression, bottomup_drive, surprise_score,
                    payload_hash
                )
                VALUES (?, 'write_gate', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    sector,
                    channel_id,
                    decision,
                    reason,
                    suppression,
                    bottomup_drive,
                    surprise,
                    payload_hash,
                ),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            # Migration 053 not applied yet. Silent skip.
            logger.debug("thalamus shadow: shadow_decisions table missing: %s", exc)
            return None

        return {
            "sector": sector,
            "decision": decision,
            "reason": reason,
            "suppression": suppression,
            "armed": armed,
            "surprise_score": surprise,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
