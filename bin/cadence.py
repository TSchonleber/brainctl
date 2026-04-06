#!/Users/r4vager/agentmemory/.venv/bin/python3
"""Cadence tracker for agentmemory events."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, last_seen_at, updated_at)
        VALUES (?, ?, 'paperclip', 'active', datetime('now'), datetime('now'))
        """,
        (agent_id, agent_id),
    )


def rolling_average(last_n_values: list[int], n: int = 7) -> float:
    if len(last_n_values) < n:
        padded = [0] * (n - len(last_n_values)) + last_n_values
    else:
        padded = last_n_values[-n:]
    return round(sum(padded) / n, 4)


def build_last_n_days(now: datetime, days: int) -> list[str]:
    return [(now - timedelta(days=offset)).date().isoformat() for offset in range(days - 1, -1, -1)]


def compute_current_streak(active_days: set[str], now: datetime) -> int:
    streak = 0
    cursor = now.date()
    while cursor.isoformat() in active_days:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


def compute_longest_gap_hours(events_30d: list[datetime], now: datetime) -> float:
    window_start = now - timedelta(days=30)
    if not events_30d:
        return round((now - window_start).total_seconds() / 3600, 2)

    ordered = sorted(events_30d)
    longest = (ordered[0] - window_start).total_seconds()
    for left, right in zip(ordered, ordered[1:]):
        longest = max(longest, (right - left).total_seconds())
    longest = max(longest, (now - ordered[-1]).total_seconds())
    return round(max(0.0, longest) / 3600, 2)


def detect_bursts(all_event_times: list[datetime]) -> list[dict]:
    bursts: list[dict] = []
    if not all_event_times:
        return bursts

    ordered = sorted(all_event_times)
    window = deque()
    for ts in ordered:
        window.append(ts)
        lower = ts - timedelta(hours=2)
        while window and window[0] < lower:
            window.popleft()
        if len(window) > 10:
            bursts.append(
                {
                    "start": window[0].isoformat(),
                    "end": ts.isoformat(),
                    "event_count": len(window),
                }
            )
    return bursts


def compute_report(conn: sqlite3.Connection, now: datetime) -> dict:
    rows = conn.execute(
        """
        SELECT agent_id, event_type, session_id, created_at
        FROM events
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()

    per_agent_day_events: dict[str, Counter] = {}
    per_agent_day_sessions: dict[str, Counter] = {}
    per_agent_day_session_ids: dict[str, dict[str, set[str]]] = {}
    per_agent_last_active: dict[str, datetime] = {}
    per_agent_30d_times: dict[str, list[datetime]] = {}
    global_day_events: Counter = Counter()
    global_day_active_agents: dict[str, set[str]] = {}
    all_event_times: list[datetime] = []

    for row in rows:
        agent_id = row["agent_id"]
        ts = parse_ts(row["created_at"])
        if ts is None:
            continue
        day = ts.date().isoformat()
        all_event_times.append(ts)

        per_agent_day_events.setdefault(agent_id, Counter())[day] += 1
        global_day_events[day] += 1
        global_day_active_agents.setdefault(day, set()).add(agent_id)

        session_id = row["session_id"]
        if session_id:
            per_agent_day_session_ids.setdefault(agent_id, {}).setdefault(day, set()).add(session_id)
        else:
            # Fallback for historical rows with null session_id: at least one session per active day.
            per_agent_day_sessions.setdefault(agent_id, Counter()).setdefault(day, 1)

        last = per_agent_last_active.get(agent_id)
        if last is None or ts > last:
            per_agent_last_active[agent_id] = ts

        if ts >= now - timedelta(days=30):
            per_agent_30d_times.setdefault(agent_id, []).append(ts)

    last_7_days = build_last_n_days(now, 7)

    per_agent = {}
    for agent_id, per_day_ids in per_agent_day_session_ids.items():
        session_counter = per_agent_day_sessions.setdefault(agent_id, Counter())
        for day, session_ids in per_day_ids.items():
            session_counter[day] = max(session_counter.get(day, 0), len(session_ids))

    for agent_id in sorted(per_agent_day_events.keys()):
        daily_event_counts = [per_agent_day_events[agent_id].get(day, 0) for day in last_7_days]
        daily_session_counts = [per_agent_day_sessions.get(agent_id, Counter()).get(day, 0) for day in last_7_days]
        active_days = {day for day, n in per_agent_day_events[agent_id].items() if n > 0}

        per_agent[agent_id] = {
            "agent_id": agent_id,
            "sessions_per_day_7d_avg": rolling_average(daily_session_counts, 7),
            "events_per_day_7d_avg": rolling_average(daily_event_counts, 7),
            "last_active_at": per_agent_last_active[agent_id].isoformat() if agent_id in per_agent_last_active else None,
            "longest_gap_hours_last_30d": compute_longest_gap_hours(per_agent_30d_times.get(agent_id, []), now),
            "current_streak_days": compute_current_streak(active_days, now),
            "window_last_7_days": last_7_days,
            "events_by_day_last_7d": dict(zip(last_7_days, daily_event_counts)),
            "sessions_by_day_last_7d": dict(zip(last_7_days, daily_session_counts)),
        }

    global_event_series = [global_day_events.get(day, 0) for day in last_7_days]
    global_agent_series = [len(global_day_active_agents.get(day, set())) for day in last_7_days]

    last_event_at = max(all_event_times).isoformat() if all_event_times else None
    silence_detected = True
    silence_hours = None
    if all_event_times:
        silence_delta = now - max(all_event_times)
        silence_hours = round(silence_delta.total_seconds() / 3600, 2)
        silence_detected = silence_delta > timedelta(hours=48)

    bursts = detect_bursts(all_event_times)

    return {
        "generated_at": now.isoformat(),
        "per_agent": per_agent,
        "global": {
            "total_events_per_day_7d_rolling": rolling_average(global_event_series, 7),
            "active_agents_per_day_7d_rolling": rolling_average(global_agent_series, 7),
            "events_by_day_last_7d": dict(zip(last_7_days, global_event_series)),
            "active_agents_by_day_last_7d": dict(zip(last_7_days, global_agent_series)),
            "bursts": bursts,
            "burst_detected": len(bursts) > 0,
            "silence_detected": silence_detected,
            "silence_hours_since_last_event": silence_hours,
            "last_event_at": last_event_at,
        },
    }


def persist_report(conn: sqlite3.Connection, writer_agent_id: str, report: dict) -> None:
    now_sql = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    report_json = json.dumps(report, sort_keys=True)

    conn.execute(
        """
        INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at)
        VALUES (?, 'cadence_report', ?, ?)
        """,
        (writer_agent_id, report_json, now_sql),
    )

    for agent_id, metrics in report["per_agent"].items():
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_state (agent_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (writer_agent_id, f"cadence_{agent_id}", json.dumps(metrics, sort_keys=True), now_sql),
        )

    conn.execute(
        """
        INSERT INTO events (agent_id, event_type, summary, metadata, project, importance, created_at)
        VALUES (?, 'cadence_updated', ?, ?, 'agentmemory', 0.6, ?)
        """,
        (
            writer_agent_id,
            "Cadence metrics refreshed and written to agent_state",
            json.dumps(
                {
                    "key": "cadence_report",
                    "per_agent_keys": [f"cadence_{agent_id}" for agent_id in report["per_agent"].keys()],
                    "agent_count": len(report["per_agent"]),
                    "burst_detected": report["global"]["burst_detected"],
                    "silence_detected": report["global"]["silence_detected"],
                },
                sort_keys=True,
            ),
            now_sql,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute and persist interaction cadence metrics.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Path to brain.db")
    parser.add_argument("--agent", default="hippocampus", help="Writer agent id for state/event attribution")
    parser.add_argument("--dry-run", action="store_true", help="Compute and print report without DB writes")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    conn = get_db(args.db_path)
    ensure_agent(conn, args.agent)

    report = compute_report(conn, now)
    if args.dry_run:
        print(json.dumps(report, indent=2))
        conn.rollback()
        conn.close()
        return

    persist_report(conn, args.agent, report)
    conn.commit()
    conn.close()
    print(
        json.dumps(
            {
                "ok": True,
                "agent": args.agent,
                "db_path": str(args.db_path),
                "generated_at": report["generated_at"],
                "agent_count": len(report["per_agent"]),
                "burst_detected": report["global"]["burst_detected"],
                "silence_detected": report["global"]["silence_detected"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
