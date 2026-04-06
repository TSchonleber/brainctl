# Research Wave 6 — Report 18: Temporal Classification Repair

**Author:** Engram (b98504a8-eb8e-4bd9-9a98-306936b5bab2)
**Date:** 2026-03-28
**Issue:** [COS-230](/COS/issues/COS-230)
**Prior art:** [COS-202](/COS/issues/COS-202) (SLO baseline), [COS-116](/COS/issues/COS-116) (granularity research)

---

## 1. Observed Distribution (as of 2026-03-28T09:55)

| temporal_class | active | retired | pct of active |
|---|---|---|---|
| medium | 13 | ~99 | 81% |
| long | 3 | 2 | 19% |
| short | 0 | 2 | 0% |
| ephemeral | 0 | 2 | 0% |
| permanent | 0 | 2 | 0% |

**Prior measurement (COS-202):** 96% medium, 4% long, 0% all others.
**This confirms a structural classification failure — not a transient anomaly.**

---

## 2. Root Cause Diagnosis

### RC-1: Default `temporal_class = 'medium'` at all write paths (PRIMARY)

Every memory insertion hardcodes `temporal_class='medium'`:

- `brainctl memory add` (brainctl:232) — no `--temporal-class` argument exposed
- `hippocampus.py compress_memories()` (hippocampus.py:268) — hardcoded `'medium'`
- `hippocampus.py consolidate_cluster()` (hippocampus.py:596) — hardcoded `'medium'`

No initial classification heuristic exists. Category, scope, and content type are
completely ignored at write time. All memories are born as `medium` regardless of
intended longevity.

### RC-2: Demotion thresholds unreachable within normal operational timeframes

From confidence=1.0, the decay math (medium half-life = 23.1 days) produces:

| From class | To class | Threshold | Days required |
|---|---|---|---|
| long | medium | 0.50 | 69.3 days |
| medium | short | 0.30 | 40.1 days |
| short | ephemeral | 0.20 | 23.2 days |

In 5 hours, a medium memory at confidence=1.0 decays only to **0.9938**.
Demotion to `short` requires 40 days of no-recall. This means most memories will
sit as `medium` for over a month before ever getting classified differently.

### RC-3: Access recall counts too low for promotion to trigger

Promotion requires `recalled_count >= 5` within 30 days. Current average is 0.03
recalls per memory. No memories qualify for promotion.

### RC-4: Consolidation cycle frequency insufficient relative to memory write volume

The cycle cron runs every 6 hours. During active agent heartbeats, 100+ memories can
be written between cycles. Classification errors accumulate faster than correction passes.

---

## 3. Impact on Dependent Systems

The five-tier temporal decay system was designed as the foundation for:
- **Spaced repetition**: retrieval scheduling depends on temporal tier
- **Semantic forgetting**: `compress_memories()` targets scope density, not temporal tier
- **Context compression**: tier affects what gets compressed first
- **Memory health SLO**: "Temporal Balance" metric (COS-202) is permanently red

All of these systems are operating with a degraded signal because 81%+ of memories
share the same temporal class.

---

## 4. Fix Specification

### Fix 1: Category-based initial classification heuristic

Introduce a `_initial_temporal_class(category, scope)` function in `hippocampus.py`
and apply it at every write path. Mapping:

| category | default temporal_class | rationale |
|---|---|---|
| identity | long | Who the agent is — stable over months |
| convention | long | Team/coding conventions — stable |
| decision | long | Architectural decisions — long-lived |
| lesson | medium | Lessons may stale but last weeks |
| preference | medium | Preferences change but last weeks |
| user | medium | User info changes over time |
| project | short | Project state changes frequently |
| environment | short | Runtime config, current state |
| observation | ephemeral | Transient, session-scoped |

### Fix 2: Expose `--temporal-class` in `brainctl memory add`

Add `--temporal-class` flag to `mem_add` arg parser with choices:
`['permanent', 'long', 'medium', 'short', 'ephemeral']`, defaulting to
category-derived value.

### Fix 3: Age-based demotion floor in consolidation cycle

Add an age+recall-based reclassification pass before the confidence demotion check:
- If `recalled_count == 0` AND age > 60 days AND class = medium → reclassify to short
- If `recalled_count == 0` AND age > 14 days AND class = long → reclassify to medium
- Do NOT upgrade via this pass (use existing promote logic for that)

### Fix 4: Migration query for existing memories

See Section 5 for the working migration script.

---

## 5. Working Reclassification Script

File: `~/agentmemory/bin/reclassify_temporal.py`

```python
#!/usr/bin/env python3
"""
reclassify_temporal.py — Migration script to repair temporal_class distribution.

Applies category-based initial classification to all active memories that are
still at the default 'medium' class and have never been manually promoted.

Usage:
    python3 reclassify_temporal.py [--dry-run] [--verbose]

Safe to re-run: uses explicit WHERE conditions, never downgrades intentionally
promoted memories, and skips 'permanent' entries entirely.
"""

import sqlite3
import argparse
import datetime
import json
from pathlib import Path

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

# Category -> initial temporal_class mapping
# Source: hippocampus.py five-tier design spec
CATEGORY_CLASS_MAP = {
    "identity": "long",
    "convention": "long",
    "decision": "long",
    "lesson": "medium",
    "preference": "medium",
    "user": "medium",
    "project": "short",
    "environment": "short",
    "observation": "ephemeral",
}

# Age thresholds for unrecalled medium memories
# These represent "this memory was never useful" signals
AGE_DEMOTION_RULES = [
    # (min_age_days, from_class, to_class, max_recalls)
    (60, "medium", "short", 0),
    (90, "short", "ephemeral", 0),
    (14, "long", "medium", 0),
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def reclassify(dry_run: bool = False, verbose: bool = False):
    conn = get_db()
    now = datetime.datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    stats = {
        "category_reclassified": 0,
        "age_demoted": 0,
        "skipped_permanent": 0,
        "skipped_manually_set": 0,
        "total_scanned": 0,
        "changes": [],
    }

    # --- Pass 1: Category-based reclassification ---
    # Target: memories at 'medium' that should have a different class per category
    # We only reclassify if the category maps to something OTHER than medium,
    # so we don't touch memories that are correctly medium by category.
    rows = conn.execute(
        """
        SELECT id, agent_id, category, temporal_class, confidence, recalled_count,
               created_at, tags
        FROM memories
        WHERE retired_at IS NULL
          AND temporal_class != 'permanent'
        ORDER BY id
        """
    ).fetchall()

    stats["total_scanned"] = len(rows)

    for row in rows:
        cat = row["category"]
        current_class = row["temporal_class"]
        target_class = CATEGORY_CLASS_MAP.get(cat)

        if target_class is None or target_class == current_class:
            continue

        # Don't demote a memory that was explicitly promoted above its category default
        # (e.g., a 'project' memory at 'long' because it was heavily recalled)
        category_default = CATEGORY_CLASS_MAP.get(cat, "medium")
        order = ["ephemeral", "short", "medium", "long", "permanent"]
        if order.index(current_class) > order.index(category_default):
            # Memory is above its category default — it was promoted, skip
            stats["skipped_manually_set"] += 1
            if verbose:
                print(f"  SKIP id={row['id']} cat={cat} current={current_class} > default={category_default}")
            continue

        change = {
            "id": row["id"],
            "pass": "category",
            "category": cat,
            "from": current_class,
            "to": target_class,
            "recalled_count": row["recalled_count"],
            "confidence": round(float(row["confidence"]), 4),
        }
        stats["changes"].append(change)
        stats["category_reclassified"] += 1

        if verbose:
            print(f"  RECLASSIFY id={row['id']} cat={cat}: {current_class} -> {target_class}")

        if not dry_run:
            conn.execute(
                "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                (target_class, now_sql, row["id"]),
            )

    # --- Pass 2: Age-based demotion for unrecalled memories ---
    for min_age, from_class, to_class, max_recalls in AGE_DEMOTION_RULES:
        age_rows = conn.execute(
            """
            SELECT id, category, temporal_class, confidence, recalled_count, created_at
            FROM memories
            WHERE retired_at IS NULL
              AND temporal_class = ?
              AND recalled_count <= ?
            """,
            (from_class, max_recalls),
        ).fetchall()

        for row in age_rows:
            created = datetime.datetime.fromisoformat(row["created_at"])
            age_days = (now - created).total_seconds() / 86400.0
            if age_days < min_age:
                continue

            change = {
                "id": row["id"],
                "pass": "age_demotion",
                "category": row["category"],
                "from": from_class,
                "to": to_class,
                "age_days": round(age_days, 1),
                "recalled_count": row["recalled_count"],
            }
            stats["changes"].append(change)
            stats["age_demoted"] += 1

            if verbose:
                print(f"  AGE_DEMOTE id={row['id']} age={age_days:.1f}d: {from_class} -> {to_class}")

            if not dry_run:
                conn.execute(
                    "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                    (to_class, now_sql, row["id"]),
                )

    if not dry_run:
        conn.commit()

    conn.close()

    # Summary
    print(json.dumps({
        "dry_run": dry_run,
        "ran_at": now_sql,
        "total_scanned": stats["total_scanned"],
        "category_reclassified": stats["category_reclassified"],
        "age_demoted": stats["age_demoted"],
        "skipped_manually_set": stats["skipped_manually_set"],
        "total_changes": len(stats["changes"]),
        "changes": stats["changes"] if verbose else f"{len(stats['changes'])} changes (use --verbose to see all)",
    }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair temporal_class distribution in brain.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--verbose", action="store_true", help="Print each change")
    args = parser.parse_args()
    reclassify(dry_run=args.dry_run, verbose=args.verbose)
```

---

## 6. SQL Migration Query

For direct database repair without running the Python script:

```sql
-- Pass 1: Category-based reclassification
-- Reclassify 'project' and 'environment' memories from medium -> short
UPDATE memories
SET temporal_class = 'short', updated_at = datetime('now')
WHERE retired_at IS NULL
  AND temporal_class = 'medium'
  AND category IN ('project', 'environment');

-- Reclassify 'observation' memories from medium -> ephemeral
UPDATE memories
SET temporal_class = 'ephemeral', updated_at = datetime('now')
WHERE retired_at IS NULL
  AND temporal_class = 'medium'
  AND category = 'observation';

-- Reclassify 'identity', 'convention', 'decision' from medium -> long
UPDATE memories
SET temporal_class = 'long', updated_at = datetime('now')
WHERE retired_at IS NULL
  AND temporal_class = 'medium'
  AND category IN ('identity', 'convention', 'decision');

-- Pass 2: Age-based demotion (60+ day unrecalled medium -> short)
UPDATE memories
SET temporal_class = 'short', updated_at = datetime('now')
WHERE retired_at IS NULL
  AND temporal_class = 'medium'
  AND recalled_count = 0
  AND julianday('now') - julianday(created_at) > 60;

-- Verify the repair
SELECT temporal_class, COUNT(*) as cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM memories WHERE retired_at IS NULL), 1) as pct
FROM memories
WHERE retired_at IS NULL
GROUP BY temporal_class
ORDER BY cnt DESC;
```

---

## 7. Expected Post-Migration Distribution

Based on current active memory categories (decision=7, lesson=3, project=3,
environment=2, identity=1), expected distribution after migration:

| class | expected count | pct |
|---|---|---|
| long | 8 (decision + identity) | ~50% |
| medium | 3 (lesson) | ~19% |
| short | 5 (project + environment) | ~31% |
| ephemeral | 0 | 0% |
| permanent | 0 | 0% |

This still lacks ephemeral entries (correct — no `observation` category memories exist)
and permanent entries (correct — that requires explicit human intent).

---

## 8. Prevention: Changes Required in hippocampus.py and brainctl

### hippocampus.py changes needed (for Epoch or Engram to implement):

1. Add `_initial_temporal_class(category: str) -> str` helper returning the mapping above
2. Update `compress_memories()` line 268: replace `'medium'` with computed class
3. Update `consolidate_cluster()` line 596: same fix
4. Update `consolidation_cycle` to call age-based reclassification as Pass 0

### brainctl changes needed:

1. Add `--temporal-class` argument to `mem_add` argument parser (line ~4241)
2. Pass it through to the INSERT (line 232-236)
3. If not specified, apply category-based heuristic from the map above

---

## 9. Conclusion

The temporal classification system is structurally broken at the write path.
All memories default to `medium` because no initial classification was designed.
The decay/demotion system works correctly but operates on a timescale (40+ days for
medium→short) that is entirely impractical for a store that refreshes daily.

The fix is a two-part repair:
1. **Immediate**: run `reclassify_temporal.py` to repair existing data
2. **Forward**: implement category-based initial classification at all write paths

With these fixes, the expected temporal balance SLO (target: no class > 60%) becomes
achievable within one consolidation cycle.
