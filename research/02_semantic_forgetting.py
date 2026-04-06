"""
Semantic Forgetting — Temporal Class Demotion Algorithm
=======================================================
Concept: Memories should be promoted or demoted through temporal classes based on
access patterns, age, and assigned importance. A memory that nobody recalls slides
from 'medium' → 'short' → 'ephemeral' before being retired. A frequently-recalled
memory gets promoted toward 'long' or 'permanent'.

Demotion criteria (all must hold for N consecutive decay passes):
  - confidence < class_floor[temporal_class]
  - recalled_count < expected_recalls_by_age(age_days, temporal_class)
  - No recent recall (days_since_recall > demotion_window[temporal_class])

Promotion criteria:
  - recalled_count >= promotion_threshold[temporal_class]
  - confidence >= 0.85
  - days_since_recall < 7
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"

# Ordered classes — index 0 is most volatile
TEMPORAL_ORDER = ["ephemeral", "short", "medium", "long", "permanent"]

# Minimum confidence floor per class; drop below → candidate for demotion
CLASS_FLOOR = {
    "ephemeral": 0.0,   # already bottom
    "short":     0.25,
    "medium":    0.35,
    "long":      0.45,
    "permanent": 0.50,
}

# Days without recall before demotion is triggered
DEMOTION_WINDOW_DAYS = {
    "permanent": 9999,
    "long":      60,
    "medium":    21,
    "short":     7,
    "ephemeral": 2,
}

# Recall count needed to promote to next class
PROMOTION_MIN_RECALLS = {
    "ephemeral": 3,
    "short":     5,
    "medium":    8,
    "long":      15,
    "permanent": 9999,
}


def days_since(ts_str: str) -> float:
    if not ts_str:
        return 9999.0
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def should_demote(row: dict) -> bool:
    tc = row["temporal_class"]
    if tc == "ephemeral":
        return False  # already at bottom
    floor = CLASS_FLOOR.get(tc, 0.5)
    window = DEMOTION_WINDOW_DAYS.get(tc, 21)
    last_ref = row["last_recalled_at"] or row["created_at"]
    return (
        row["confidence"] < floor
        and days_since(last_ref) > window
    )


def should_promote(row: dict) -> bool:
    tc = row["temporal_class"]
    if tc == "permanent":
        return False
    threshold = PROMOTION_MIN_RECALLS.get(tc, 9999)
    last_ref = row["last_recalled_at"] or row["created_at"]
    return (
        row["recalled_count"] >= threshold
        and row["confidence"] >= 0.85
        and days_since(last_ref) < 7.0
    )


def next_class(tc: str, direction: str) -> str:
    idx = TEMPORAL_ORDER.index(tc)
    if direction == "up" and idx < len(TEMPORAL_ORDER) - 1:
        return TEMPORAL_ORDER[idx + 1]
    if direction == "down" and idx > 0:
        return TEMPORAL_ORDER[idx - 1]
    return tc


def run_demotion_pass(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """
    Scan active memories and promote/demote their temporal_class.
    Returns counts of promoted, demoted, unchanged.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT id, temporal_class, confidence, recalled_count, last_recalled_at, created_at
        FROM memories
        WHERE retired_at IS NULL
    """)
    rows = [dict(r) for r in cur.fetchall()]

    promoted = 0
    demoted = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for row in rows:
        new_class = row["temporal_class"]
        if should_promote(row):
            new_class = next_class(row["temporal_class"], "up")
            promoted += 1
        elif should_demote(row):
            new_class = next_class(row["temporal_class"], "down")
            demoted += 1
        else:
            continue

        if not dry_run and new_class != row["temporal_class"]:
            cur.execute(
                "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                (new_class, now_iso, row["id"])
            )

    if not dry_run:
        conn.commit()
    conn.close()

    return {
        "scanned": len(rows),
        "promoted": promoted,
        "demoted": demoted,
        "unchanged": len(rows) - promoted - demoted,
        "dry_run": dry_run,
    }


# SQL version for direct DB execution
DEMOTION_SQL = """
UPDATE memories
SET temporal_class = CASE
    WHEN temporal_class = 'long'   THEN 'medium'
    WHEN temporal_class = 'medium' THEN 'short'
    WHEN temporal_class = 'short'  THEN 'ephemeral'
    ELSE temporal_class
END,
updated_at = datetime('now')
WHERE retired_at IS NULL
  AND temporal_class NOT IN ('permanent', 'ephemeral')
  AND confidence < CASE
    WHEN temporal_class = 'long'   THEN 0.45
    WHEN temporal_class = 'medium' THEN 0.35
    WHEN temporal_class = 'short'  THEN 0.25
    ELSE 0.0
  END
  AND (
    last_recalled_at IS NULL
    OR (julianday('now') - julianday(last_recalled_at)) > CASE
      WHEN temporal_class = 'long'   THEN 60
      WHEN temporal_class = 'medium' THEN 21
      WHEN temporal_class = 'short'  THEN 7
      ELSE 2
    END
  );
"""


if __name__ == "__main__":
    result = run_demotion_pass(dry_run=True)
    print(f"Demotion pass (dry run): {result}")
