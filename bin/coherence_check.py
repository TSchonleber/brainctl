#!/Users/r4vager/agentmemory/.venv/bin/python3
"""Contradiction & Coherence Detection System for the shared memory spine.

Detects four classes of incoherence:
  1. Supersede conflicts     - memory supersedes another but the old one is not retired
  2. Cross-agent contradictions - different agents hold opposing assertions on same topic
  3. Stale assumptions       - memories still active that reference state known to be superseded
  4. Decision conflicts      - active decisions whose rationale/title contradict each other
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"
AGENT_ID = "paperclip-sentinel-2"

# ---------------------------------------------------------------------------
# Negation / polarity patterns used for contradiction detection
# ---------------------------------------------------------------------------
NEGATION_PATTERNS = [
    r"\bnot\b",
    r"\bnever\b",
    r"\bno\b",
    r"\bdon'?t\b",
    r"\bdoesn'?t\b",
    r"\bwon'?t\b",
    r"\bcannot\b",
    r"\bcan'?t\b",
    r"\bforbidden\b",
    r"\bprohibited\b",
    r"\bdisabled\b",
    r"\bremoved\b",
    r"\bsuperseded\b",
    r"\bno longer\b",
    r"\bdead\b",
    r"\bdeprecated\b",
    r"\bunused\b",
]

NEGATION_RE = re.compile("|".join(NEGATION_PATTERNS), re.IGNORECASE)


def get_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def has_negation(text: str) -> bool:
    return bool(NEGATION_RE.search(text or ""))


def extract_key_terms(text: str, min_len: int = 4) -> set[str]:
    """Extract significant lowercase tokens for topic overlap comparison."""
    stopwords = {
        "this", "that", "with", "from", "have", "been", "will", "when",
        "then", "they", "their", "there", "about", "which", "what", "into",
        "also", "each", "some", "more", "used", "uses", "uses", "were",
        "all", "its", "and", "for", "the", "are", "via", "but", "now",
        "not", "can", "may", "per", "any", "our", "has", "was", "new",
        "old", "set", "run", "get", "add",
    }
    tokens = re.findall(r"[a-z][a-z0-9_\-]{%d,}" % (min_len - 1), text.lower())
    return {t for t in tokens if t not in stopwords}


def jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Proactive Interference Index (PII) — COS-410
# ---------------------------------------------------------------------------

import math as _math

_PII_TEMPORAL_WEIGHTS: dict[str, float] = {
    "permanent": 1.00,
    "long":      0.80,
    "medium":    0.50,
    "short":     0.30,
    "ephemeral": 0.15,
}

_PII_TIERS = [
    (0.70, "CRYSTALLIZED"),
    (0.40, "ENTRENCHED"),
    (0.20, "ESTABLISHED"),
    (0.00, "OPEN"),
]


def pii_tier(score: float) -> str:
    for threshold, label in _PII_TIERS:
        if score >= threshold:
            return label
    return "OPEN"


def compute_pii(db: sqlite3.Connection, memory_id: int) -> float:
    """Compute Proactive Interference Index for a memory.

    Returns float in [0.0, 1.0].
    Tier: <0.20=OPEN, 0.20-0.40=ESTABLISHED, 0.40-0.70=ENTRENCHED, 0.70-1.00=CRYSTALLIZED.
    """
    row = db.execute(
        "SELECT alpha, beta, recalled_count, temporal_class FROM memories "
        "WHERE id = ? AND retired_at IS NULL",
        (memory_id,)
    ).fetchone()
    if not row:
        return 0.0

    alpha = float(row["alpha"] or 1.0)
    beta  = float(row["beta"]  or 1.0)
    recalled = int(row["recalled_count"] or 0)
    temporal_class = row["temporal_class"] or "medium"

    max_row = db.execute(
        "SELECT MAX(recalled_count) FROM memories WHERE retired_at IS NULL"
    ).fetchone()
    max_recalled = int(max_row[0] or 1)
    if max_recalled < 1:
        max_recalled = 1

    bayesian_strength = alpha / (alpha + beta)
    recall_weight = (
        _math.log(1 + recalled) / _math.log(1 + max_recalled)
        if max_recalled > 0 else 0.0
    )
    temporal_weight = _PII_TEMPORAL_WEIGHTS.get(temporal_class, 0.50)

    pii = bayesian_strength * recall_weight * temporal_weight
    return min(1.0, max(0.0, pii))


def apply_pi_degradation(
    db: sqlite3.Connection,
    conflicting_memory_id: int,
    conflict_strength: float,  # 0.0–1.0
) -> None:
    """Apply confidence penalty and beta bump to a memory under conflict.

    Only applies if PII > 0.40. Skips permanent class without validation_agent_id.
    """
    row = db.execute(
        "SELECT id, confidence, beta, temporal_class, validation_agent_id "
        "FROM memories WHERE id = ? AND retired_at IS NULL",
        (conflicting_memory_id,)
    ).fetchone()
    if not row:
        return

    pii = compute_pii(db, conflicting_memory_id)
    if pii <= 0.40:
        return

    # Permanent-class memories require an explicit validation agent before degradation
    if row["temporal_class"] == "permanent" and not row["validation_agent_id"]:
        return

    penalty = conflict_strength * 0.05 * (pii - 0.40)
    new_confidence = max(0.30, float(row["confidence"]) - penalty)
    new_beta = float(row["beta"] or 1.0) + conflict_strength * 0.5

    db.execute(
        "UPDATE memories SET confidence = ?, beta = ?, updated_at = datetime('now') WHERE id = ?",
        (new_confidence, new_beta, conflicting_memory_id)
    )


# ---------------------------------------------------------------------------
# Check 1 — Supersede conflicts
# ---------------------------------------------------------------------------

def detect_supersede_conflicts(db: sqlite3.Connection) -> list[dict]:
    """Find memories that supersede another memory but the old one is NOT retired."""
    rows = db.execute("""
        SELECT m1.id        AS new_id,
               m1.agent_id  AS new_agent,
               m1.content   AS new_content,
               m1.created_at AS new_date,
               m2.id        AS old_id,
               m2.agent_id  AS old_agent,
               m2.retired_at AS old_retired,
               m2.content   AS old_content
        FROM memories m1
        JOIN memories m2 ON m1.supersedes_id = m2.id
        WHERE m1.retired_at IS NULL
          AND m2.retired_at IS NULL
    """).fetchall()

    conflicts = []
    for r in rows:
        conflicts.append({
            "type": "supersede_conflict",
            "severity": "CRITICAL",
            "description": (
                f"Memory #{r['new_id']} supersedes #{r['old_id']} but #{r['old_id']} "
                f"is NOT retired — both are active simultaneously."
            ),
            "new_id": r["new_id"],
            "old_id": r["old_id"],
            "new_agent": r["new_agent"],
            "old_agent": r["old_agent"],
            "new_content_snippet": (r["new_content"] or "")[:120],
            "old_content_snippet": (r["old_content"] or "")[:120],
            "recommendation": f"Retire memory #{r['old_id']} immediately.",
        })
    return conflicts


# ---------------------------------------------------------------------------
# Check 2 — Cross-agent contradictions
# ---------------------------------------------------------------------------

def detect_cross_agent_contradictions(db: sqlite3.Connection, similarity_threshold: float = 0.15) -> list[dict]:
    """
    Find pairs of active memories from DIFFERENT agents that:
      - Share significant topic overlap (Jaccard >= threshold)
      - Differ in polarity (one has negation language, the other does not)
        OR both make mutually exclusive factual claims.
    """
    rows = db.execute("""
        SELECT m.id, m.agent_id, m.category, m.scope, m.content, m.confidence,
               a.display_name
        FROM memories m
        LEFT JOIN agents a ON m.agent_id = a.id
        WHERE m.retired_at IS NULL
        ORDER BY m.agent_id, m.id
    """).fetchall()

    memories = [dict(r) for r in rows]
    conflicts = []
    seen = set()

    for i, m1 in enumerate(memories):
        for m2 in memories[i + 1:]:
            if m1["agent_id"] == m2["agent_id"]:
                continue

            pair_key = (min(m1["id"], m2["id"]), max(m1["id"], m2["id"]))
            if pair_key in seen:
                continue

            terms1 = extract_key_terms(m1["content"] or "")
            terms2 = extract_key_terms(m2["content"] or "")
            sim = jaccard_similarity(terms1, terms2)

            if sim < similarity_threshold:
                continue

            neg1 = has_negation(m1["content"])
            neg2 = has_negation(m2["content"])

            # Only flag if polarity differs OR both explicitly negate the same shared terms
            shared_terms = terms1 & terms2
            if neg1 == neg2 and not any(
                NEGATION_RE.search(w) for w in shared_terms
            ):
                continue

            seen.add(pair_key)
            conflicts.append({
                "type": "cross_agent_contradiction",
                "severity": "WARNING",
                "description": (
                    f"Agents '{m1['agent_id']}' and '{m2['agent_id']}' hold "
                    f"potentially contradictory memories (topic overlap: {sim:.0%})."
                ),
                "memory_a": {
                    "id": m1["id"],
                    "agent": m1["agent_id"],
                    "scope": m1["scope"],
                    "snippet": (m1["content"] or "")[:120],
                    "has_negation": neg1,
                },
                "memory_b": {
                    "id": m2["id"],
                    "agent": m2["agent_id"],
                    "scope": m2["scope"],
                    "snippet": (m2["content"] or "")[:120],
                    "has_negation": neg2,
                },
                "shared_terms": sorted(shared_terms)[:10],
                "recommendation": "Hermes should review and reconcile — surface to resolution queue.",
            })

    return conflicts


# ---------------------------------------------------------------------------
# Check 3 — Stale assumptions
# ---------------------------------------------------------------------------

# Patterns that suggest a memory is pinned to a specific numeric/path state
STALE_INDICATORS = [
    # stat counts that get outdated quickly
    r"\b(\d+)\s+agents?\b",
    r"\b(\d+)\s+memories\b",
    r"\b(\d+)\s+events\b",
    r"\b(\d+)\s+active\b",
    r"brain\.db has[^.]+\.",
    # old path references
    r"_shared-brain",
    r"~/Documents/Agent Memory/_shared-brain",
    # explicit superseded references
    r"now superseded by",
]

STALE_RE = re.compile("|".join(STALE_INDICATORS), re.IGNORECASE)


def detect_stale_assumptions(db: sqlite3.Connection) -> list[dict]:
    """
    Flag active memories containing language that pins them to a state
    known to be stale (e.g., old DB stats, superseded paths).
    """
    rows = db.execute("""
        SELECT m.id, m.agent_id, m.category, m.scope, m.content, m.created_at,
               a.display_name
        FROM memories m
        LEFT JOIN agents a ON m.agent_id = a.id
        WHERE m.retired_at IS NULL
        ORDER BY m.created_at
    """).fetchall()

    # Get current DB stats for comparison
    stats_row = db.execute("""
        SELECT
          (SELECT COUNT(*) FROM agents) AS agent_count,
          (SELECT COUNT(*) FROM memories WHERE retired_at IS NULL) AS memory_count,
          (SELECT COUNT(*) FROM events) AS event_count
    """).fetchone()
    current_stats = {
        "agents": stats_row["agent_count"],
        "memories": stats_row["memory_count"],
        "events": stats_row["event_count"],
    }

    stale = []
    for r in rows:
        content = r["content"] or ""
        m = STALE_RE.search(content)
        if not m:
            continue

        matched_text = m.group(0)
        # Check if it's a stat count that's outdated
        num_match = re.search(r"(\d+)\s+agents?", content, re.IGNORECASE)
        claimed_agents = int(num_match.group(1)) if num_match else None

        severity = "INFO"
        reason = f"Contains potentially stale reference: '{matched_text[:60]}'"

        if claimed_agents is not None and abs(claimed_agents - current_stats["agents"]) > 5:
            severity = "WARNING"
            reason = (
                f"Claims {claimed_agents} agents but current count is "
                f"{current_stats['agents']} (delta={abs(claimed_agents - current_stats['agents'])})"
            )
        elif "_shared-brain" in content:
            severity = "WARNING"
            reason = "References old _shared-brain path (superseded by ~/agentmemory/)"

        stale.append({
            "type": "stale_assumption",
            "severity": severity,
            "description": reason,
            "memory_id": r["id"],
            "agent": r["agent_id"],
            "scope": r["scope"],
            "snippet": content[:150],
            "current_stats": current_stats,
            "recommendation": (
                "Update or supersede this memory to reflect current system state."
                if severity == "WARNING" else
                "Review — may contain outdated counts/paths."
            ),
        })

    return stale


# ---------------------------------------------------------------------------
# Check 4 — Decision conflicts
# ---------------------------------------------------------------------------

def detect_decision_conflicts(db: sqlite3.Connection, similarity_threshold: float = 0.2) -> list[dict]:
    """
    Find pairs of unreversed decisions that appear to contradict each other
    based on title/rationale overlap and polarity divergence.
    """
    rows = db.execute("""
        SELECT id, agent_id, title, rationale, reversed_at, project, created_at
        FROM decisions
        WHERE reversed_at IS NULL
        ORDER BY created_at
    """).fetchall()

    decisions = [dict(r) for r in rows]
    conflicts = []
    seen = set()

    for i, d1 in enumerate(decisions):
        for d2 in decisions[i + 1:]:
            pair_key = (min(d1["id"], d2["id"]), max(d1["id"], d2["id"]))
            if pair_key in seen:
                continue

            text1 = f"{d1['title']} {d1['rationale'] or ''}"
            text2 = f"{d2['title']} {d2['rationale'] or ''}"
            terms1 = extract_key_terms(text1)
            terms2 = extract_key_terms(text2)
            sim = jaccard_similarity(terms1, terms2)

            if sim < similarity_threshold:
                continue

            neg1 = has_negation(text1)
            neg2 = has_negation(text2)

            if neg1 == neg2:
                continue

            seen.add(pair_key)
            conflicts.append({
                "type": "decision_conflict",
                "severity": "WARNING",
                "description": (
                    f"Decisions #{d1['id']} and #{d2['id']} share topic overlap "
                    f"({sim:.0%}) but differ in polarity — possible contradiction."
                ),
                "decision_a": {
                    "id": d1["id"],
                    "agent": d1["agent_id"],
                    "title": d1["title"],
                    "has_negation": neg1,
                },
                "decision_b": {
                    "id": d2["id"],
                    "agent": d2["agent_id"],
                    "title": d2["title"],
                    "has_negation": neg2,
                },
                "recommendation": "Hermes should review for logical consistency.",
            })

    return conflicts


# ---------------------------------------------------------------------------
# Report generation & persistence
# ---------------------------------------------------------------------------

def build_report(db: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    supersede = detect_supersede_conflicts(db)
    cross_agent = detect_cross_agent_contradictions(db)
    stale = detect_stale_assumptions(db)
    decision = detect_decision_conflicts(db)

    all_findings = supersede + cross_agent + stale + decision

    by_severity: dict[str, int] = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for f in all_findings:
        by_severity[f.get("severity", "INFO")] += 1

    # Normalized scoring with per-class caps so non-critical findings
    # cannot drive the score to 0.0 on their own.
    #   CRITICAL: -0.4 each, capped at 1.0 (3 criticals → score floor 0.0)
    #   WARNING:  -0.03 each, capped at 0.35 (~12 warnings → -0.35 max)
    #   INFO:     -0.005 each, capped at 0.10 (20+ infos → -0.10 max)
    critical_penalty = min(1.0, 0.40 * by_severity["CRITICAL"])
    warning_penalty  = min(0.35, 0.03 * by_severity["WARNING"])
    info_penalty     = min(0.10, 0.005 * by_severity["INFO"])
    coherence_score  = max(0.0, round(1.0 - critical_penalty - warning_penalty - info_penalty, 3))

    return {
        "run_at": now,
        "agent": AGENT_ID,
        "coherence_score": coherence_score,
        "summary": {
            "total_findings": len(all_findings),
            "by_severity": by_severity,
            "by_type": {
                "supersede_conflicts": len(supersede),
                "cross_agent_contradictions": len(cross_agent),
                "stale_assumptions": len(stale),
                "decision_conflicts": len(decision),
            },
        },
        "findings": all_findings,
    }


def persist_report(db: sqlite3.Connection, report: dict) -> None:
    """Write report as an event + update agent_state for dashboard visibility."""
    severity_label = (
        "CRITICAL" if report["summary"]["by_severity"]["CRITICAL"] > 0
        else "WARNING" if report["summary"]["by_severity"]["WARNING"] > 0
        else "OK"
    )
    summary_line = (
        f"Coherence check: score={report['coherence_score']} | "
        f"{report['summary']['total_findings']} findings | status={severity_label}"
    )

    # Ensure agent row exists
    db.execute("""
        INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at)
        VALUES (?, 'Sentinel 2', 'paperclip', 'active', datetime('now'), datetime('now'))
    """, (AGENT_ID,))

    # Log event
    db.execute("""
        INSERT INTO events (agent_id, event_type, summary, detail, project, importance, created_at)
        VALUES (?, 'coherence_check', ?, ?, 'agentmemory', ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))
    """, (
        AGENT_ID,
        summary_line,
        json.dumps(report, indent=2),
        0.9 if severity_label == "CRITICAL" else 0.7 if severity_label == "WARNING" else 0.5,
    ))

    # Write to agent_state for quick dashboard reads
    db.execute("""
        INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at)
        VALUES (?, 'last_coherence_report', ?, datetime('now'))
    """, (AGENT_ID, json.dumps(report)))

    db.execute("""
        INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at)
        VALUES (?, 'coherence_score', ?, datetime('now'))
    """, (AGENT_ID, str(report["coherence_score"])))

    db.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Contradiction & Coherence Detection System"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output report as JSON (default: human-readable summary)"
    )
    parser.add_argument(
        "--no-persist", action="store_true",
        help="Skip writing results to brain.db"
    )
    parser.add_argument(
        "--findings-only", action="store_true",
        help="Only show findings, skip OK items"
    )
    args = parser.parse_args()

    db = get_db()
    report = build_report(db)

    if not args.no_persist:
        persist_report(db, report)
        db.close()

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Human-readable output
    score = report["coherence_score"]
    s = report["summary"]
    sev = s["by_severity"]

    status = "OK" if sev["CRITICAL"] == 0 and sev["WARNING"] == 0 else \
             "CRITICAL" if sev["CRITICAL"] > 0 else "WARNING"

    print(f"\n{'='*60}")
    print(f"  COHERENCE CHECK — {report['run_at']}")
    print(f"{'='*60}")
    print(f"  Status         : {status}")
    print(f"  Score          : {score} / 1.0")
    print(f"  Total Findings : {s['total_findings']}")
    print(f"    CRITICAL     : {sev['CRITICAL']}")
    print(f"    WARNING      : {sev['WARNING']}")
    print(f"    INFO         : {sev['INFO']}")
    print(f"\n  By Type:")
    for k, v in s["by_type"].items():
        print(f"    {k:<35}: {v}")
    print(f"{'='*60}\n")

    if not report["findings"] and not args.findings_only:
        print("  No findings. Memory spine is coherent.\n")
        return

    for i, f in enumerate(report["findings"], 1):
        sev_label = f.get("severity", "INFO")
        print(f"  [{sev_label}] Finding #{i}: {f['type']}")
        print(f"  {f['description']}")
        if "recommendation" in f:
            print(f"  → {f['recommendation']}")
        print()


if __name__ == "__main__":
    main()
