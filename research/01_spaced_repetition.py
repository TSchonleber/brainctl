"""
Spaced Repetition — Confidence Decay & Boost Algorithm
=======================================================
Concept: Memories that are recalled frequently and recently should have higher
confidence. Memories that are never accessed decay toward 0 and become candidates
for demotion or retirement. Based on the Ebbinghaus forgetting curve, adapted
for the brain.db schema.

Algorithm:
  decay:  confidence(t) = confidence_0 * exp(-λ * days_since_recall)
  boost:  confidence += α * (1 - confidence)  # asymptotic boost on recall
  λ (decay rate) varies by temporal_class:
    ephemeral: 0.5   (half-life ~1.4 days)
    short:     0.2   (half-life ~3.5 days)
    medium:    0.05  (half-life ~14 days)
    long:      0.01  (half-life ~69 days)
    permanent: 0.0   (no decay)
"""

import sqlite3
import math
from datetime import datetime, timezone

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"

DECAY_RATES = {
    "ephemeral": 0.2,   # COS-334: reduced from 0.5 — aligns with hippocampus.py 3.5d half-life (λ≈0.198)
    "short":     0.07,  # COS-334: reduced from 0.2 — aligns with hippocampus.py 10d half-life (λ≈0.069)
    "medium":    0.03,  # COS-334: reduced from 0.05 — aligns with hippocampus.py 23d half-life (λ≈0.030)
    "long":      0.01,
    "permanent": 0.0,
}

RECALL_BOOST_ALPHA = 0.15  # each recall boosts by 15% of remaining headroom

DEMOTION_THRESHOLD = 0.15  # retire memory if confidence drops below this


def days_since(timestamp_str: str) -> float:
    """Return fractional days elapsed since the given ISO timestamp."""
    if not timestamp_str:
        return 999.0
    ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    # SQLite datetime('now') produces naive UTC strings; make them UTC-aware.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def decay_confidence(confidence: float, temporal_class: str, last_recalled_at: str) -> float:
    """Apply exponential decay to confidence based on time since last recall."""
    lam = DECAY_RATES.get(temporal_class, DECAY_RATES["medium"])
    if lam == 0.0:
        return confidence
    days = days_since(last_recalled_at)
    return confidence * math.exp(-lam * days)


def boost_confidence(confidence: float) -> float:
    """Boost confidence on recall — asymptotically approaches 1.0."""
    return confidence + RECALL_BOOST_ALPHA * (1.0 - confidence)


def run_decay_pass(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """
    Apply decay to all active memories. Returns summary stats.
    Memories below DEMOTION_THRESHOLD are retired.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT id, confidence, temporal_class, last_recalled_at, created_at
        FROM memories
        WHERE retired_at IS NULL
          AND temporal_class != 'permanent'
    """)
    rows = cur.fetchall()

    updated = 0
    retired = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for row in rows:
        # Use last_recalled_at if available, else created_at
        last_ref = row["last_recalled_at"] or row["created_at"]
        new_conf = decay_confidence(row["confidence"], row["temporal_class"], last_ref)
        new_conf = max(0.0, min(1.0, new_conf))

        if dry_run:
            updated += 1
            if new_conf < DEMOTION_THRESHOLD:
                retired += 1
            continue

        if new_conf < DEMOTION_THRESHOLD:
            cur.execute(
                "UPDATE memories SET retired_at = ?, confidence = ?, updated_at = ? WHERE id = ?",
                (now_iso, new_conf, now_iso, row["id"])
            )
            retired += 1
        else:
            cur.execute(
                "UPDATE memories SET confidence = ?, updated_at = ? WHERE id = ?",
                (new_conf, now_iso, row["id"])
            )
        updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    return {"scanned": len(rows), "updated": updated, "retired": retired, "dry_run": dry_run}


def record_recall(memory_id: int, db_path: str = DB_PATH) -> float:
    """
    Called when a memory is retrieved. Boosts confidence and updates recall stats.
    Returns the new confidence value.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()

    cur.execute("SELECT confidence, recalled_count FROM memories WHERE id = ?", (memory_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return 0.0

    new_conf = boost_confidence(row[0])
    new_count = row[1] + 1

    cur.execute("""
        UPDATE memories
        SET confidence = ?, recalled_count = ?, last_recalled_at = ?, updated_at = ?
        WHERE id = ?
    """, (new_conf, new_count, now_iso, now_iso, memory_id))
    conn.commit()
    conn.close()
    return new_conf


if __name__ == "__main__":
    result = run_decay_pass(dry_run=True)
    print(f"Decay pass (dry run): {result}")
