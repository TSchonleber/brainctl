#!/usr/bin/env python3
"""brainctl-mcp-cleanup — diagnose and (optionally) clean up stale
``brainctl-mcp`` processes that pile up when MCP clients (Codex.app,
Claude Code, Claude Desktop) crash or hold idle pipes for hours.

Defaults to dry-run / report-only. Killing is opt-in.

Usage
-----
  brainctl-mcp-cleanup                       # report only
  brainctl-mcp-cleanup --json                # JSON report
  brainctl-mcp-cleanup --kill-orphans        # kill processes whose
                                             #   parent is launchd/init (PID 1)
  brainctl-mcp-cleanup --kill-stale --age-hours 24
                                             # kill processes older than N
                                             #   hours (asks for confirmation
                                             #   unless --yes is also passed)
  brainctl-mcp-cleanup --signal TERM         # default SIGTERM; pass KILL for -9

Status flags in the report
--------------------------
  ORPHAN     parent is launchd/init — safe to kill
  HOLDS_DB   has brain.db / brain.db-wal / brain.db-shm open
  STALE      older than --age-hours threshold
  LIVE       parent is alive, no flags set — DO NOT kill blindly
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal as _signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_AGE_HOURS = 24
BRAIN_DB_PATHS = [
    Path.home() / "agentmemory" / "db" / "brain.db",
    Path.home() / "agentmemory" / "db" / "brain.db-wal",
    Path.home() / "agentmemory" / "db" / "brain.db-shm",
]


def _parse_etime(raw: str) -> int | None:
    """Parse a BSD/POSIX ``etime`` value into seconds.

    macOS BSD ``ps`` does NOT support ``etimes`` (seconds-only) — only
    ``etime`` in one of three formats:

      ``MM:SS``           — under 1 hour
      ``HH:MM:SS``        — under 1 day
      ``DD-HH:MM:SS``     — 1 day or more

    Linux supports both ``etime`` and ``etimes``; using ``etime`` keeps
    this helper portable across both. Returns ``None`` for unparseable
    input rather than raising — a single garbled row should not poison
    the whole report.
    """
    raw = raw.strip()
    if not raw:
        return None
    days = 0
    if "-" in raw:
        d, _, raw = raw.partition("-")
        try:
            days = int(d)
        except ValueError:
            return None
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:           # MM:SS
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 3:         # HH:MM:SS
        h, m, s = nums
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def _ps_brainctl_mcp() -> list[dict]:
    """Return rows for every running ``brainctl-mcp`` process.

    Uses BSD-portable ``etime`` (NOT ``etimes`` — that flag is
    Linux-only and silently fails on macOS, which previously caused
    this helper to report an empty list while 25 zombies were live).
    """
    out = subprocess.run(
        ["ps", "-eo", "pid,ppid,etime,user,command"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        sys.stderr.write(
            f"warning: ps failed (rc={out.returncode}): "
            f"{out.stderr.strip()[:200]}\n"
        )
        return []
    rows = []
    for line in out.stdout.splitlines()[1:]:
        if "brainctl-mcp" not in line or "brainctl-mcp-cleanup" in line or "grep " in line:
            continue
        # Split into 5 fields max so the command (which contains spaces)
        # stays intact in parts[4].
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        age_sec = _parse_etime(parts[2])
        if age_sec is None:
            # Don't drop the row — a missing age is recoverable; the
            # operator can still see the PID/parent/command and decide
            # what to do. Mark age as 0 so STALE never trips on it.
            age_sec = 0
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "age_sec": age_sec,
            "user": parts[3],
            "command": parts[4],
        })
    return rows


def _proc_name(pid: int) -> str:
    """Best-effort short name for a PID (or '<gone>')."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, check=False,
        )
        return out.stdout.strip() or "<gone>"
    except Exception:
        return "<unknown>"


def _holders_of_brain_db() -> set[int]:
    """PIDs that have any brain.db file currently open. Returns a set
    of PIDs. Uses lsof; returns empty set if lsof isn't available."""
    if not shutil.which("lsof"):
        return set()
    pids: set[int] = set()
    for path in BRAIN_DB_PATHS:
        if not path.exists():
            continue
        out = subprocess.run(
            ["lsof", "-Fp", str(path)],
            capture_output=True, text=True, check=False,
        )
        for line in out.stdout.splitlines():
            if line.startswith("p"):
                try:
                    pids.add(int(line[1:]))
                except ValueError:
                    pass
    return pids


def _classify(rows: list[dict], age_hours: float) -> list[dict]:
    holders = _holders_of_brain_db()
    age_sec = age_hours * 3600
    enriched = []
    for r in rows:
        flags: list[str] = []
        parent_name = _proc_name(r["ppid"])
        if r["ppid"] == 1:
            flags.append("ORPHAN")
        if r["pid"] in holders:
            flags.append("HOLDS_DB")
        if r["age_sec"] > age_sec:
            flags.append("STALE")
        if not flags:
            flags.append("LIVE")
        enriched.append({
            **r,
            "parent": parent_name,
            "age_h": round(r["age_sec"] / 3600.0, 1),
            "flags": flags,
        })
    return enriched


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No brainctl-mcp processes running.")
        return
    print(f"{'PID':>7} {'PPID':>7} {'AGE_H':>6}  {'PARENT':<22} {'FLAGS':<24}  COMMAND")
    print("-" * 100)
    for r in rows:
        flags = ",".join(r["flags"])
        cmd = r["command"]
        if len(cmd) > 50:
            cmd = "..." + cmd[-47:]
        parent = r["parent"][:22]
        print(f"{r['pid']:>7} {r['ppid']:>7} {r['age_h']:>6}  {parent:<22} {flags:<24}  {cmd}")


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _kill(pids: list[int], sig: int) -> dict:
    killed, missed = [], []
    for pid in pids:
        try:
            os.kill(pid, sig)
            killed.append(pid)
        except ProcessLookupError:
            missed.append(pid)  # already gone
        except PermissionError as e:
            missed.append(pid)
            print(f"  permission denied for pid {pid}: {e}", file=sys.stderr)
    # Give signals time to land, then verify.
    if killed:
        time.sleep(0.5)
        still = []
        for pid in killed:
            try:
                os.kill(pid, 0)
                still.append(pid)
            except ProcessLookupError:
                pass
        return {"killed": killed, "missed": missed, "still_alive": still}
    return {"killed": killed, "missed": missed, "still_alive": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brainctl-mcp-cleanup",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON instead of a table")
    parser.add_argument("--age-hours", type=float, default=DEFAULT_AGE_HOURS,
                        help=f"threshold for STALE flag (default: {DEFAULT_AGE_HOURS})")
    parser.add_argument("--kill-orphans", action="store_true",
                        help="kill processes flagged ORPHAN (ppid==1). Always safe.")
    parser.add_argument("--kill-stale", action="store_true",
                        help="kill processes flagged STALE. Asks confirmation"
                             " unless --yes is given.")
    parser.add_argument("--kill-holders", action="store_true",
                        help="kill processes flagged HOLDS_DB. RISKY — may"
                             " interrupt a live write. Asks confirmation.")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="skip confirmation prompts (use with care)")
    parser.add_argument("--signal", default="TERM",
                        choices=["TERM", "INT", "HUP", "KILL"],
                        help="signal to send (default TERM, graceful)")
    args = parser.parse_args(argv)

    rows = _ps_brainctl_mcp()
    classified = _classify(rows, args.age_hours)

    if args.json:
        print(json.dumps({"processes": classified, "age_hours": args.age_hours},
                         indent=2))
    else:
        _print_table(classified)

    actions: dict[str, dict] = {}
    sig = getattr(_signal, f"SIG{args.signal}")

    def _maybe_kill(label: str, candidates: list[dict], dangerous: bool) -> None:
        if not candidates:
            return
        pids = [c["pid"] for c in candidates]
        if dangerous and not args.yes:
            ok = _confirm(
                f"\nAbout to send SIG{args.signal} to {len(pids)} {label} "
                f"processes: {pids}. Confirm?"
            )
            if not ok:
                actions[label] = {"skipped": True, "candidates": pids}
                print(f"  skipped {label}")
                return
        print(f"\nSending SIG{args.signal} to {len(pids)} {label} processes...",
              file=sys.stderr)
        actions[label] = _kill(pids, sig)
        print(f"  result: {actions[label]}", file=sys.stderr)

    if args.kill_orphans:
        candidates = [c for c in classified if "ORPHAN" in c["flags"]]
        _maybe_kill("orphans", candidates, dangerous=False)

    if args.kill_stale:
        candidates = [c for c in classified
                      if "STALE" in c["flags"] and "ORPHAN" not in c["flags"]]
        _maybe_kill("stale", candidates, dangerous=True)

    if args.kill_holders:
        candidates = [c for c in classified if "HOLDS_DB" in c["flags"]]
        _maybe_kill("holders", candidates, dangerous=True)

    if args.json and actions:
        # Emit a second JSON line so callers can distinguish report
        # output from action output.
        print(json.dumps({"actions": actions}, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
