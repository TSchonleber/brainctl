"""
Continuous LLM Consolidation — Reference Prototype
====================================================
Wave 4 Research | COS-183
Author: Tensor
Builds on: 05_consolidation_cycle.py, 08_context_compression.py

Design:
  - Hybrid event-driven + polling service (poll every POLL_INTERVAL_SEC)
  - Write-time dedup: prevent redundant inserts at source
  - Incremental running summary: update per cluster, not full batch rebuild
  - Token-bucket rate limiter: caps LLM calls per hour
  - Cluster cooldown: prevents thrash on frequently-updated clusters

This is a research prototype. No LLM dependency in the critical path —
LLM is injected via summarizer_fn callback (same pattern as 05_consolidation_cycle.py).
"""

import sqlite3
import re
import json
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
CYCLE_AGENT_ID = "paperclip-tensor"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.monotonic()


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ContinuousConsolidatorConfig:
    poll_interval_sec: int = 300        # 5-minute polling cycle
    min_cluster_size: int = 3           # minimum members before consolidating
    ephemeral_max_age_min: float = 30.0 # age watermark for ephemeral class (minutes)
    short_max_age_hr: float = 6.0       # age watermark for short class (hours)
    llm_calls_per_hour: int = 60        # token-bucket cap
    cooldown_sec: int = 600             # cluster cooldown between re-consolidations
    summarize_every_k: int = 3          # incremental re-summary frequency
    full_resummary_threshold: int = 30  # full re-summary after cluster exceeds N
    dedup_jaccard_threshold: float = 0.70  # write-time dedup threshold
    similarity_threshold: float = 0.40  # clustering assignment threshold
    batch_size_per_poll: int = 200      # max memories per poll cycle


# =============================================================================
# Token-bucket rate limiter
# =============================================================================

class RateLimiter:
    """Token bucket rate limiter for LLM calls."""

    def __init__(self, calls_per_hour: int = 60):
        self.capacity = float(calls_per_hour)
        self.tokens = float(calls_per_hour)
        self.refill_rate = calls_per_hour / 3600.0  # tokens per second
        self.last_refill = now_ts()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            elapsed = now_ts() - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now_ts()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def wait_and_acquire(self, timeout_sec: float = 60.0) -> bool:
        deadline = now_ts() + timeout_sec
        while now_ts() < deadline:
            if self.acquire():
                return True
            time.sleep(1.0)
        return False


# =============================================================================
# Jaccard similarity (FTS-based, no embeddings required)
# =============================================================================

def _token_set(text: str) -> set:
    return set(re.findall(r'\b\w{4,}\b', text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = _token_set(a), _token_set(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# =============================================================================
# Write-time dedup hook
# =============================================================================

def is_redundant(
    conn: sqlite3.Connection,
    new_content: str,
    agent_id: str,
    category: str,
    threshold: float = 0.70,
    lookback: int = 50,
) -> tuple[bool, Optional[int]]:
    """
    Check if new_content is redundant with an existing active memory.
    Returns (True, existing_id) if overlap >= threshold.
    Fast path: FTS Jaccard, no embeddings.
    """
    rows = conn.execute("""
        SELECT id, content FROM memories
        WHERE agent_id = ? AND category = ? AND retired_at IS NULL
        ORDER BY created_at DESC LIMIT ?
    """, (agent_id, category, lookback)).fetchall()

    for row_id, content in rows:
        if jaccard(new_content, content) >= threshold:
            return True, row_id
    return False, None


def bump_confidence(
    conn: sqlite3.Connection,
    memory_id: int,
    delta: float = 0.02,
) -> None:
    """Increase confidence of an existing memory instead of inserting duplicate."""
    conn.execute("""
        UPDATE memories
        SET confidence = MIN(1.0, confidence + ?),
            recalled_count = recalled_count + 1,
            last_recalled_at = datetime('now'),
            updated_at = datetime('now')
        WHERE id = ?
    """, (delta, memory_id))


# =============================================================================
# Cluster state tracking
# =============================================================================

@dataclass
class ClusterState:
    """In-memory state for a running cluster."""
    cluster_key: str                  # "{category}::{scope}::{agent_id}"
    member_ids: list[int] = field(default_factory=list)
    running_summary: Optional[str] = None
    summary_member_count: int = 0     # members count when summary was last updated
    last_consolidated_at: float = 0.0 # monotonic timestamp


class ClusterRegistry:
    """Thread-safe cluster state registry."""

    def __init__(self):
        self._state: dict[str, ClusterState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, key: str) -> ClusterState:
        with self._lock:
            if key not in self._state:
                self._state[key] = ClusterState(cluster_key=key)
            return self._state[key]

    def is_on_cooldown(self, key: str, cooldown_sec: int) -> bool:
        with self._lock:
            s = self._state.get(key)
            if not s:
                return False
            return (now_ts() - s.last_consolidated_at) < cooldown_sec

    def mark_consolidated(self, key: str) -> None:
        with self._lock:
            if key in self._state:
                self._state[key].last_consolidated_at = now_ts()


# =============================================================================
# Poll-cycle queries
# =============================================================================

def find_clusters_by_size(
    conn: sqlite3.Connection,
    min_cluster_size: int = 3,
    min_age_min: float = 5.0,
    batch_size: int = 200,
) -> list[dict]:
    """
    Find (category, scope, agent_id) groups with >= min_cluster_size
    un-consolidated members that are at least min_age_min old.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT agent_id, category, scope, COUNT(*) as cnt
        FROM memories
        WHERE retired_at IS NULL
          AND temporal_class IN ('ephemeral', 'short')
          AND (julianday('now') - julianday(created_at)) * 1440 >= :min_age_min
        GROUP BY agent_id, category, scope
        HAVING cnt >= :min_size
        ORDER BY cnt DESC
        LIMIT :batch_size
    """, {"min_age_min": min_age_min, "min_size": min_cluster_size, "batch_size": batch_size}).fetchall()
    return [dict(r) for r in rows]


def fetch_cluster_members(
    conn: sqlite3.Connection,
    agent_id: str,
    category: str,
    scope: str,
    temporal_classes: tuple = ("ephemeral", "short"),
    limit: int = 100,
) -> list[dict]:
    """Fetch active un-consolidated memories for a cluster."""
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(temporal_classes))
    rows = conn.execute(f"""
        SELECT id, agent_id, category, scope, content, confidence,
               temporal_class, recalled_count, created_at
        FROM memories
        WHERE agent_id = ? AND category = ? AND scope = ?
          AND retired_at IS NULL
          AND temporal_class IN ({placeholders})
        ORDER BY created_at ASC
        LIMIT ?
    """, [agent_id, category, scope] + list(temporal_classes) + [limit]).fetchall()
    return [dict(r) for r in rows]


def find_age_watermark_memories(
    conn: sqlite3.Connection,
    temporal_class: str,
    max_age_min: float,
    batch_size: int = 100,
) -> list[dict]:
    """Find individual memories past the age watermark regardless of cluster size."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, agent_id, category, scope, content, confidence,
               temporal_class, recalled_count, created_at
        FROM memories
        WHERE retired_at IS NULL
          AND temporal_class = ?
          AND (julianday('now') - julianday(created_at)) * 1440 >= ?
        ORDER BY created_at ASC
        LIMIT ?
    """, (temporal_class, max_age_min, batch_size)).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# Incremental consolidation
# =============================================================================

def incremental_consolidate(
    conn: sqlite3.Connection,
    cluster_state: ClusterState,
    members: list[dict],
    summarizer_fn: Optional[Callable] = None,
    summarize_every_k: int = 3,
    full_resummary_threshold: int = 30,
    dry_run: bool = False,
) -> Optional[int]:
    """
    Incrementally consolidate a cluster using a running summary.
    Returns new consolidated memory ID, or None on dry_run/empty.

    summarizer_fn(existing_summary: str | None, new_texts: list[str]) -> str
      If None, uses naive concatenation.
    """
    if not members:
        return None

    new_members = [
        m for m in members
        if m["id"] not in cluster_state.member_ids
    ]
    if not new_members and cluster_state.running_summary:
        return None  # nothing new to consolidate

    all_ids = cluster_state.member_ids + [m["id"] for m in new_members]
    new_count = len(new_members)
    total_count = len(all_ids)

    # Decide whether to re-summarize
    should_summarize = (
        cluster_state.running_summary is None
        or new_count >= summarize_every_k
        or total_count >= full_resummary_threshold
    )

    if not should_summarize:
        # Just update the member list, defer LLM call
        cluster_state.member_ids = all_ids
        return None

    # Summarize
    if summarizer_fn:
        new_texts = [m["content"] for m in new_members]
        if total_count >= full_resummary_threshold:
            # Full re-summary: include all members
            all_texts = [m["content"] for m in members]
            summary = summarizer_fn(None, all_texts)
        else:
            # Incremental: update running summary with new texts
            summary = summarizer_fn(cluster_state.running_summary, new_texts)
    else:
        # Naive fallback: deduplicated sentence extraction
        seen: set = set()
        parts = []
        all_texts = [m["content"] for m in members]
        for text in all_texts:
            for sent in text.split(". "):
                s = sent.strip()
                if s and s not in seen:
                    seen.add(s)
                    parts.append(s)
        summary = ". ".join(parts[:10])

    if dry_run:
        cluster_state.running_summary = summary
        cluster_state.summary_member_count = total_count
        cluster_state.member_ids = all_ids
        return None

    # Write consolidated memory
    category = members[0]["category"]
    scope = members[0]["scope"]
    agent_id = members[0]["agent_id"]
    avg_confidence = sum(m["confidence"] for m in members) / len(members)
    total_recalls = sum(m["recalled_count"] for m in members)

    cur = conn.execute("""
        INSERT INTO memories
            (agent_id, category, scope, content, confidence, temporal_class,
             recalled_count, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'medium', ?, ?, datetime('now'), datetime('now'))
    """, (
        agent_id, category, scope, summary,
        min(1.0, avg_confidence + 0.05),
        total_recalls,
        json.dumps(["consolidated", "continuous", f"cluster:{cluster_state.cluster_key[:40]}"]),
    ))
    new_id = cur.lastrowid

    # Retire source memories
    for m in members:
        conn.execute("""
            UPDATE memories
            SET retired_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?
        """, (m["id"],))
        conn.execute("""
            INSERT OR REPLACE INTO knowledge_edges
                (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
            VALUES ('memories', ?, 'memories', ?, 'derived_from', 0.9, ?)
        """, (new_id, m["id"], agent_id))

    # Update cluster state
    cluster_state.running_summary = summary
    cluster_state.summary_member_count = total_count
    cluster_state.member_ids = []  # reset — all members retired, cluster starts fresh

    return new_id


# =============================================================================
# Main continuous consolidator
# =============================================================================

class ContinuousConsolidator:
    """
    Hybrid event-driven + polling consolidation service.

    Usage (background thread):
        consolidator = ContinuousConsolidator(db_path=DB_PATH)
        thread = threading.Thread(target=consolidator.run, daemon=True)
        thread.start()

    Usage (single poll for testing):
        consolidator = ContinuousConsolidator(db_path=DB_PATH, dry_run=True)
        report = consolidator.poll_once()
    """

    def __init__(
        self,
        db_path: str = DB_PATH,
        config: Optional[ContinuousConsolidatorConfig] = None,
        summarizer_fn: Optional[Callable] = None,
        dry_run: bool = False,
    ):
        self.db_path = db_path
        self.cfg = config or ContinuousConsolidatorConfig()
        self.summarizer_fn = summarizer_fn
        self.dry_run = dry_run
        self.rate_limiter = RateLimiter(self.cfg.llm_calls_per_hour)
        self.registry = ClusterRegistry()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Main loop — runs until stop() is called."""
        while not self._stop_event.is_set():
            try:
                report = self.poll_once()
                _log_event(self.db_path, report, dry_run=self.dry_run)
            except Exception as e:
                # Log but don't crash the background service
                _log_event(self.db_path, {"error": str(e), "ts": now_iso()},
                           dry_run=self.dry_run)
            self._stop_event.wait(timeout=self.cfg.poll_interval_sec)

    def poll_once(self) -> dict:
        """Single poll cycle. Returns cycle report."""
        conn = sqlite3.connect(self.db_path)
        report = {
            "started_at": now_iso(),
            "dry_run": self.dry_run,
            "clusters_found": 0,
            "clusters_consolidated": 0,
            "clusters_skipped_cooldown": 0,
            "memories_retired": 0,
            "llm_calls": 0,
            "age_watermark_processed": 0,
        }

        try:
            # --- 1. Cluster-size sweep ---
            clusters = find_clusters_by_size(
                conn,
                min_cluster_size=self.cfg.min_cluster_size,
                min_age_min=5.0,
                batch_size=self.cfg.batch_size_per_poll,
            )
            report["clusters_found"] = len(clusters)

            for cluster_row in clusters:
                key = f"{cluster_row['category']}::{cluster_row['scope']}::{cluster_row['agent_id']}"

                if self.registry.is_on_cooldown(key, self.cfg.cooldown_sec):
                    report["clusters_skipped_cooldown"] += 1
                    continue

                # Acquire rate limit token before LLM work
                if self.summarizer_fn and not self.rate_limiter.acquire():
                    break  # exhausted budget for this poll cycle

                members = fetch_cluster_members(
                    conn,
                    agent_id=cluster_row["agent_id"],
                    category=cluster_row["category"],
                    scope=cluster_row["scope"],
                )
                if len(members) < self.cfg.min_cluster_size:
                    continue

                state = self.registry.get_or_create(key)
                new_id = incremental_consolidate(
                    conn, state, members,
                    summarizer_fn=self.summarizer_fn,
                    summarize_every_k=self.cfg.summarize_every_k,
                    full_resummary_threshold=self.cfg.full_resummary_threshold,
                    dry_run=self.dry_run,
                )

                if new_id is not None:
                    report["clusters_consolidated"] += 1
                    report["memories_retired"] += len(members)
                    if self.summarizer_fn:
                        report["llm_calls"] += 1
                    self.registry.mark_consolidated(key)

                if not self.dry_run:
                    conn.commit()

            # --- 2. Age watermark sweep (ephemeral) ---
            ephemeral_old = find_age_watermark_memories(
                conn,
                temporal_class="ephemeral",
                max_age_min=self.cfg.ephemeral_max_age_min,
                batch_size=self.cfg.batch_size_per_poll,
            )
            # Group by (agent_id, category, scope) and consolidate groups
            watermark_groups: dict[str, list[dict]] = {}
            for m in ephemeral_old:
                k = f"{m['category']}::{m['scope']}::{m['agent_id']}"
                watermark_groups.setdefault(k, []).append(m)

            for key, mems in watermark_groups.items():
                if len(mems) < self.cfg.min_cluster_size:
                    # Single old memories: just promote to short
                    if not self.dry_run:
                        for m in mems:
                            conn.execute("""
                                UPDATE memories SET temporal_class = 'short',
                                updated_at = datetime('now') WHERE id = ?
                            """, (m["id"],))
                    report["age_watermark_processed"] += len(mems)
                    continue

                if self.registry.is_on_cooldown(key, self.cfg.cooldown_sec):
                    continue

                if self.summarizer_fn and not self.rate_limiter.acquire():
                    break

                state = self.registry.get_or_create(key)
                new_id = incremental_consolidate(
                    conn, state, mems,
                    summarizer_fn=self.summarizer_fn,
                    summarize_every_k=self.cfg.summarize_every_k,
                    full_resummary_threshold=self.cfg.full_resummary_threshold,
                    dry_run=self.dry_run,
                )
                if new_id is not None:
                    report["memories_retired"] += len(mems)
                    report["age_watermark_processed"] += len(mems)
                    if self.summarizer_fn:
                        report["llm_calls"] += 1
                    self.registry.mark_consolidated(key)

                if not self.dry_run:
                    conn.commit()

        finally:
            report["completed_at"] = now_iso()
            conn.close()

        return report


# =============================================================================
# Logging
# =============================================================================

def _log_event(db_path: str, report: dict, dry_run: bool = False) -> None:
    if dry_run:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO events
                (agent_id, event_type, summary, detail, metadata, importance, created_at)
            VALUES (?, 'continuous_consolidation', ?, ?, ?, 0.6, datetime('now'))
        """, (
            CYCLE_AGENT_ID,
            f"Continuous poll: {report.get('clusters_consolidated', 0)} clusters, "
            f"{report.get('memories_retired', 0)} retired",
            json.dumps(report, indent=2),
            json.dumps(report),
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass  # don't crash background service on log failure


# =============================================================================
# CLI entrypoint
# =============================================================================

if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    print(f"Running single poll cycle (dry_run={dry})...")
    c = ContinuousConsolidator(db_path=DB_PATH, dry_run=dry)
    report = c.poll_once()
    print(json.dumps(report, indent=2))
