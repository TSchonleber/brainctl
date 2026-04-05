#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
sync-memory-block.py — Bridge-P1 (COS-210)

Surfaces high-confidence brain.db memories that belong in Hermes/Kokoro's
compact memory block but are not yet present there.

Flow:
1. Read top-K memories from brain.db (by trust_score * confidence, filtered
   by scope or category).
2. Diff against current MEMORY.md / USER.md entries.
3. In --report mode (default): print candidates to stdout.
4. In --write mode: append missing entries to MEMORY.md or USER.md, respecting
   the character budget.

Conflict resolution: brain.db version wins. If a compact-block entry
contradicts a brain.db memory with higher confidence, the brain.db version
is proposed for replacement.

Usage:
    python3 sync-memory-block.py [--write] [--dry-run] [--top-k N] [--verbose]
    python3 sync-memory-block.py --memory-block ~/.hermes/memories/MEMORY.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
AGENTMEMORY = Path(os.environ.get("AGENTMEMORY", Path.home() / "agentmemory"))
DB_PATH = AGENTMEMORY / "db" / "brain.db"
BRAINCTL = AGENTMEMORY / "bin" / "brainctl"

HERMES_MEMORIES_DIR = Path.home() / ".hermes" / "memories"
MEMORY_MD = HERMES_MEMORIES_DIR / "MEMORY.md"
USER_MD = HERMES_MEMORIES_DIR / "USER.md"

ENTRY_DELIMITER = "\n§\n"

# brain.db categories that map to the compact memory block
MEMORY_CATEGORIES = {"identity", "convention", "decision", "environment", "integration", "lesson"}
USER_CATEGORIES = {"user", "preference"}

# Only surface memories above this combined trust × confidence threshold
MIN_SCORE = 0.75

# ---------------------------------------------------------------------------
# brain.db queries
# ---------------------------------------------------------------------------

def _brainctl(args: list[str], timeout: int = 15) -> Optional[dict]:
    """Run brainctl and return parsed JSON, or None on error."""
    if not BRAINCTL.exists():
        return None
    agent = os.environ.get("BRAINCTL_AGENT", "hermes")
    cmd = [str(BRAINCTL), "--agent", agent] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def fetch_top_memories(top_k: int = 30) -> list[dict]:
    """Return top-K active brain.db memories ranked by trust_score * confidence."""
    result = _brainctl(["memory", "list", "--limit", str(top_k * 3)])
    if not result:
        return []
    mems = result if isinstance(result, list) else result.get("memories", [])
    # Filter: active, non-expired, relevant categories
    relevant = []
    for m in mems:
        if m.get("retired_at") or m.get("retracted_at"):
            continue
        cat = m.get("category", "")
        if cat not in MEMORY_CATEGORIES | USER_CATEGORIES:
            continue
        score = float(m.get("trust_score", 1.0)) * float(m.get("confidence", 1.0))
        if score < MIN_SCORE:
            continue
        m["_score"] = score
        relevant.append(m)
    relevant.sort(key=lambda m: m["_score"], reverse=True)
    return relevant[:top_k]


# ---------------------------------------------------------------------------
# Compact block helpers
# ---------------------------------------------------------------------------

def read_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def write_entries(path: Path, entries: list[str]) -> None:
    content = ENTRY_DELIMITER.join(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def char_count(entries: list[str]) -> int:
    if not entries:
        return 0
    return len(ENTRY_DELIMITER.join(entries))


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for rough dedup comparison."""
    return re.sub(r"[^\w\s]", "", text.lower().strip())


def is_already_present(content: str, entries: list[str]) -> bool:
    """True if the content is substantially covered by an existing entry."""
    norm_content = _normalize(content)
    # Use the first 60 chars as a fingerprint (avoids long-form duplicates)
    fingerprint = norm_content[:60]
    for entry in entries:
        if fingerprint and fingerprint in _normalize(entry):
            return True
        # Also check if >80% of words overlap
        words_content = set(norm_content.split())
        words_entry = set(_normalize(entry).split())
        if words_content and len(words_content & words_entry) / len(words_content) > 0.8:
            return True
    return False


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def run_sync(
    top_k: int = 20,
    write: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    memory_md: Path = MEMORY_MD,
    user_md: Path = USER_MD,
) -> dict:
    memories = fetch_top_memories(top_k)
    if verbose:
        print(f"Fetched {len(memories)} candidate memories from brain.db")

    memory_entries = read_entries(memory_md)
    user_entries = read_entries(user_md)

    memory_limit = 2200
    user_limit = 1375

    candidates_memory: list[dict] = []
    candidates_user: list[dict] = []

    for m in memories:
        content = m.get("content", "").strip()
        if not content:
            continue
        cat = m.get("category", "")
        if cat in USER_CATEGORIES:
            if not is_already_present(content, user_entries):
                candidates_user.append(m)
        else:
            if not is_already_present(content, memory_entries):
                candidates_memory.append(m)

    if verbose:
        print(f"  {len(candidates_memory)} new candidates for MEMORY.md")
        print(f"  {len(candidates_user)} new candidates for USER.md")

    added_memory: list[str] = []
    added_user: list[str] = []
    skipped: list[str] = []

    if write and not dry_run:
        # Append to MEMORY.md within budget
        for m in candidates_memory:
            content = m["content"].strip()
            new_entries = memory_entries + [content]
            if char_count(new_entries) <= memory_limit:
                memory_entries = new_entries
                added_memory.append(content)
            else:
                skipped.append(f"[memory budget] {content[:60]}…")
                break
        # Append to USER.md within budget
        for m in candidates_user:
            content = m["content"].strip()
            new_entries = user_entries + [content]
            if char_count(new_entries) <= user_limit:
                user_entries = new_entries
                added_user.append(content)
            else:
                skipped.append(f"[user budget] {content[:60]}…")
                break

        if added_memory:
            write_entries(memory_md, memory_entries)
        if added_user:
            write_entries(user_md, user_entries)

    return {
        "candidates_memory": [m["content"][:120] for m in candidates_memory],
        "candidates_user": [m["content"][:120] for m in candidates_user],
        "added_memory": added_memory,
        "added_user": added_user,
        "skipped": skipped,
        "memory_usage": f"{char_count(memory_entries)}/{memory_limit}",
        "user_usage": f"{char_count(user_entries)}/{user_limit}",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync high-confidence brain.db memories into Hermes compact block (Bridge-P1)"
    )
    parser.add_argument("--write", action="store_true",
                        help="Write surfaced memories to MEMORY.md / USER.md")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; do not write (overrides --write)")
    parser.add_argument("--top-k", type=int, default=20,
                        help="Number of top brain.db memories to consider (default: 20)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--memory-block", type=Path, default=MEMORY_MD,
                        help=f"Path to MEMORY.md (default: {MEMORY_MD})")
    parser.add_argument("--user-block", type=Path, default=USER_MD,
                        help=f"Path to USER.md (default: {USER_MD})")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON report")
    args = parser.parse_args()

    report = run_sync(
        top_k=args.top_k,
        write=args.write and not args.dry_run,
        dry_run=args.dry_run,
        verbose=args.verbose,
        memory_md=args.memory_block,
        user_md=args.user_block,
    )

    if args.json_output:
        print(json.dumps(report, indent=2))
        return 0

    print("=" * 70)
    print("BRAIN.DB → COMPACT BLOCK SYNC REPORT (Bridge-P1 / COS-210)")
    print("=" * 70)
    print(f"  Memory block usage:  {report['memory_usage']} chars")
    print(f"  User block usage:    {report['user_usage']} chars")
    print()

    if report["candidates_memory"] or report["candidates_user"]:
        print(f"CANDIDATES ({len(report['candidates_memory'])} memory, {len(report['candidates_user'])} user):")
        for c in report["candidates_memory"]:
            marker = "  [ADDED]" if c in " ".join(report["added_memory"]) else "  [candidate]"
            print(f"  [memory] {c[:90]}")
        for c in report["candidates_user"]:
            print(f"  [user]   {c[:90]}")
    else:
        print("No new candidates — compact block is up to date with brain.db.")

    if report["added_memory"] or report["added_user"]:
        print()
        print(f"WRITTEN: {len(report['added_memory'])} to MEMORY.md, {len(report['added_user'])} to USER.md")

    if report["skipped"]:
        print()
        print("SKIPPED (budget):")
        for s in report["skipped"]:
            print(f"  {s}")

    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
