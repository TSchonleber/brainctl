#!/usr/bin/env python3
"""Seed the thalamus relay catalog from observed memory event traffic.

The script is intentionally one-shot and idempotent. It scans recent
``memory_events`` rows, clusters them by operation/category + source agent +
scope, chooses 10-20 representative channels across thalamus sectors, and
UPSERTs them into ``thalamic_relays``.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.paths import get_db_path

SECTOR_TARGETS = {
    "sensory_external": "event_add",
    "agent_efferent": "agent_orient",
    "memory_recall": "memory_search",
    "belief": "belief_tools",
    "consolidation": "consolidation_run",
    "pii_sensitive": "privacy_gate",
}


@dataclass(frozen=True)
class TrafficCluster:
    event_type: str
    agent_id: str
    category: str
    scope: str
    memory_type: str
    count: int
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class RelaySeed:
    channel_id: str
    sector: str
    driver_source: str
    modulator_sources_json: str
    target: str
    transport: str
    default_gain: float
    topographic_key: str
    efference_copy_target: str
    sample_count: int
    source_agent: str
    event_type: str
    category: str
    scope: str


def _slug(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return (cleaned or "unknown")[:max_len]


def _open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_thalamus_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='thalamic_relays'"
    ).fetchone()
    if not row:
        raise RuntimeError("thalamic_relays table missing; run migration 050 first")


def _fetch_clusters(conn: sqlite3.Connection, days: int, scan_limit: int) -> list[TrafficCluster]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """
        SELECT
            operation AS event_type,
            agent_id,
            category,
            scope,
            COALESCE(memory_type, 'episodic') AS memory_type,
            COUNT(*) AS count,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen
        FROM memory_events
        WHERE created_at >= ?
        GROUP BY operation, agent_id, category, scope, COALESCE(memory_type, 'episodic')
        ORDER BY count DESC, last_seen DESC
        LIMIT ?
        """,
        (cutoff, scan_limit),
    ).fetchall()
    return [
        TrafficCluster(
            event_type=row["event_type"] or "unknown",
            agent_id=row["agent_id"] or "unknown",
            category=row["category"] or "uncategorized",
            scope=row["scope"] or "global",
            memory_type=row["memory_type"] or "episodic",
            count=int(row["count"] or 0),
            first_seen=row["first_seen"] or "",
            last_seen=row["last_seen"] or "",
        )
        for row in rows
    ]


def _sector_for(cluster: TrafficCluster) -> str:
    haystack = " ".join(
        [
            cluster.agent_id,
            cluster.category,
            cluster.scope,
            cluster.memory_type,
            cluster.event_type,
        ]
    ).lower()
    if any(term in haystack for term in ("pii", "secret", "credential", "wallet", "private-key", "token")):
        return "pii_sensitive"
    if cluster.category in {"belief", "decision", "theory_of_mind", "tom"}:
        return "belief"
    if cluster.agent_id in {"hippocampus", "consolidation", "dream", "dream-test"}:
        return "consolidation"
    if cluster.category in {"user", "observation"} or cluster.scope.endswith("-desktop"):
        return "sensory_external"
    if cluster.agent_id.startswith(("hermes", "claude", "openclaw", "paperclip", "codex", "devin", "mcp")):
        if cluster.category in {"project", "handoff", "coordination", "result"}:
            return "agent_efferent"
    return "memory_recall"


def _transport_for(sector: str) -> str:
    if sector in {"sensory_external", "pii_sensitive"}:
        return "first_order"
    return "higher_order"


def _gain_for(cluster: TrafficCluster, sector: str) -> float:
    if sector == "pii_sensitive":
        return 0.35
    if sector == "sensory_external":
        return 1.1
    if sector == "agent_efferent":
        return 1.0
    if sector == "belief":
        return 0.95
    if sector == "consolidation":
        return 0.75
    if cluster.memory_type == "semantic":
        return 0.9
    return 0.8


def _relay_from_cluster(cluster: TrafficCluster) -> RelaySeed:
    sector = _sector_for(cluster)
    transport = _transport_for(sector)
    agent = _slug(cluster.agent_id, 28)
    scope = _slug(cluster.scope, 32)
    category = _slug(cluster.category, 24)
    event_type = _slug(cluster.event_type, 16)
    memory_type = _slug(cluster.memory_type, 12)
    channel_id = f"meb:{sector}:{agent}:{scope}:{event_type}:{category}:{memory_type}"
    if len(channel_id) > 180:
        channel_id = f"meb:{sector}:{agent}:{scope}:{event_type}:{category}"
    modulators = [
        f"agent:{cluster.agent_id}",
        f"scope:{cluster.scope}",
        f"category:{cluster.category}",
        f"memory_type:{cluster.memory_type}",
    ]
    return RelaySeed(
        channel_id=channel_id,
        sector=sector,
        driver_source=f"memory_events:{cluster.event_type}:{cluster.category}:{cluster.memory_type}",
        modulator_sources_json=json.dumps(modulators, sort_keys=True),
        target=SECTOR_TARGETS[sector],
        transport=transport,
        default_gain=_gain_for(cluster, sector),
        topographic_key=cluster.scope,
        efference_copy_target="event_add",
        sample_count=cluster.count,
        source_agent=cluster.agent_id,
        event_type=cluster.event_type,
        category=cluster.category,
        scope=cluster.scope,
    )


def build_relay_seeds(
    clusters: list[TrafficCluster],
    min_channels: int = 10,
    max_channels: int = 20,
) -> list[RelaySeed]:
    """Choose representative relays while preserving sector diversity."""
    if min_channels < 1 or max_channels < min_channels:
        raise ValueError("expected 1 <= min_channels <= max_channels")

    seen: set[str] = set()
    relays = [_relay_from_cluster(c) for c in clusters]
    buckets: dict[str, list[RelaySeed]] = {sector: [] for sector in SECTOR_TARGETS}
    for relay in relays:
        if relay.channel_id in seen:
            continue
        seen.add(relay.channel_id)
        buckets.setdefault(relay.sector, []).append(relay)

    selected: list[RelaySeed] = []
    selected_ids: set[str] = set()

    for sector in SECTOR_TARGETS:
        for relay in buckets.get(sector, [])[:3]:
            if len(selected) >= max_channels:
                break
            selected.append(relay)
            selected_ids.add(relay.channel_id)

    for relay in relays:
        if len(selected) >= max_channels:
            break
        if relay.channel_id in selected_ids:
            continue
        selected.append(relay)
        selected_ids.add(relay.channel_id)
        if len(selected) >= min_channels and len({r.sector for r in selected}) >= 4:
            # Keep filling only when the caller requested more than the minimum.
            continue

    return selected[:max_channels]


def seed_catalog(conn: sqlite3.Connection, relays: list[RelaySeed], dry_run: bool = False) -> dict:
    if dry_run:
        return {"inserted_or_updated": 0, "gate_rows": 0}

    for relay in relays:
        conn.execute(
            """
            INSERT INTO thalamic_relays (
                channel_id, sector, driver_source, modulator_sources_json,
                target, transport, default_gain, topographic_key,
                efference_copy_target, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            ON CONFLICT(channel_id) DO UPDATE SET
                sector = excluded.sector,
                driver_source = excluded.driver_source,
                modulator_sources_json = excluded.modulator_sources_json,
                target = excluded.target,
                transport = excluded.transport,
                default_gain = excluded.default_gain,
                topographic_key = excluded.topographic_key,
                efference_copy_target = excluded.efference_copy_target,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            """,
            (
                relay.channel_id,
                relay.sector,
                relay.driver_source,
                relay.modulator_sources_json,
                relay.target,
                relay.transport,
                relay.default_gain,
                relay.topographic_key,
                relay.efference_copy_target,
            ),
        )
        conn.execute(
            """
            INSERT INTO thalamic_gate (channel_id, sector, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            ON CONFLICT(channel_id) DO UPDATE SET
                sector = excluded.sector,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            """,
            (relay.channel_id, relay.sector),
        )

    conn.commit()
    return {"inserted_or_updated": len(relays), "gate_rows": len(relays)}


def _print_summary(relays: list[RelaySeed], dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "SEEDED"
    sectors = ", ".join(f"{s}:{sum(1 for r in relays if r.sector == s)}" for s in sorted({r.sector for r in relays}))
    print(f"{mode} {len(relays)} thalamic relay channels ({sectors})")
    print("sector | count | transport | source_agent | scope | event_type | category | channel_id")
    print("-" * 120)
    for relay in sorted(relays, key=lambda r: (-r.sample_count, r.sector, r.channel_id)):
        print(
            f"{relay.sector} | {relay.sample_count} | {relay.transport} | "
            f"{relay.source_agent} | {relay.scope} | {relay.event_type} | "
            f"{relay.category} | {relay.channel_id}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(get_db_path()), help="Path to brain.db")
    parser.add_argument("--days", type=int, default=30, help="Days of memory_events traffic to scan")
    parser.add_argument("--min-channels", type=int, default=10, help="Minimum channels to seed")
    parser.add_argument("--max-channels", type=int, default=20, help="Maximum channels to seed")
    parser.add_argument("--scan-limit", type=int, default=300, help="Maximum grouped traffic clusters to consider")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args(argv)

    conn = _open_db(args.db)
    try:
        _ensure_thalamus_schema(conn)
        clusters = _fetch_clusters(conn, args.days, args.scan_limit)
        if not clusters:
            print("No memory_events traffic found for the requested window.", file=sys.stderr)
            return 1
        relays = build_relay_seeds(clusters, args.min_channels, args.max_channels)
        result = seed_catalog(conn, relays, dry_run=args.dry_run)
        _print_summary(relays, dry_run=args.dry_run)
        print(json.dumps(result, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
