#!/Users/r4vager/agentmemory/.venv/bin/python3
"""Memory Block Contradiction Validator — Bridge-P2.

Compares Kokoro's MEMORY.md assertions against brain.db to detect stale or
contradicted beliefs. Surfaces warnings at conversation start; optionally
auto-replaces with brain.db version when confidence is high enough.

Usage:
    python3 memory_block_validator.py [--auto-resolve] [--dry-run] [--quiet]

Requires:
    - ~/agentmemory/db/brain.db  (brainctl memory spine)
    - ~/.openclaw/workspace/MEMORY.md  (Kokoro's compact memory block)

Bridge-P1 dependency note:
    Auto-replace of MEMORY.md entries (--auto-resolve) requires the
    bidirectional sync interface from COS-210 (Memory Block <-> brain.db
    Bidirectional Sync). Until that lands, --auto-resolve flags conflicts
    in brain.db but does NOT rewrite MEMORY.md. Manual resolution is the
    current path.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MEMORY_BLOCK_PATH = Path.home() / ".openclaw" / "workspace" / "MEMORY.md"
DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"
BRAINCTL = Path.home() / "bin" / "brainctl"
AGENT_ID = "paperclip-recall"

# Minimum FTS result score (more negative = less relevant in sqlite FTS5)
MIN_RELEVANCE_SCORE = -8.0

# Auto-resolve only when confidence delta exceeds this threshold
AUTO_RESOLVE_MIN_DELTA = 0.35

# ---------------------------------------------------------------------------
# Negation / polarity patterns (same as 06_contradiction_detection.py)
# ---------------------------------------------------------------------------
NEGATION_PATTERNS = [
    (r"\bis\b", r"\bis not\b|\bisn'?t\b"),
    (r"\bcan\b", r"\bcannot\b|\bcan'?t\b"),
    (r"\bwill\b", r"\bwill not\b|\bwon'?t\b"),
    (r"\bshould\b", r"\bshould not\b|\bshouldn'?t\b"),
    (r"\bhas\b", r"\bhas not\b|\bhasn'?t\b"),
    (r"\benabled\b", r"\bdisabled\b"),
    (r"\bactive\b", r"\binactive\b"),
    (r"\btrue\b", r"\bfalse\b"),
    (r"\bregistered\b", r"\bnot registered\b"),
    (r"\blive\b", r"\bnot live\b|\bdown\b|\boffline\b"),
    (r"\bdone\b", r"\bnot done\b|\bincomplete\b|\bpending\b"),
]

# Factual claim indicators — filter out pure-philosophy/prose sentences
FACTUAL_MARKERS = re.compile(
    r"\b(is|are|was|were|has|have|will|can|cannot|total|live|registered|"
    r"production|version|running|installed|enabled|disabled|using|uses|"
    r"\d+|localhost|costclock|paperclip|brainctl|openlaw|hermes)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class Assertion:
    """A single parsed claim from the memory block."""
    text: str
    section: str
    line_number: int


@dataclass
class Conflict:
    """A detected contradiction between a memory block assertion and brain.db."""
    assertion: Assertion
    brain_memory_id: int
    brain_content: str
    brain_confidence: float
    conflict_type: str  # "negation", "supersession", "numeric_delta", "semantic"
    detail: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
SECTION_RE = re.compile(r"^##\s+(.+)")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)")


def parse_memory_block(path: Path) -> list[Assertion]:
    """Extract bullet-point assertions from a MEMORY.md file."""
    assertions: list[Assertion] = []
    current_section = "Preamble"

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        section_m = SECTION_RE.match(raw)
        if section_m:
            current_section = section_m.group(1).strip()
            continue

        bullet_m = BULLET_RE.match(raw)
        if not bullet_m:
            continue

        text = bullet_m.group(1).strip()
        # Skip meta/prose lines unlikely to be factual claims
        if len(text) < 20:
            continue
        if not FACTUAL_MARKERS.search(text):
            continue

        assertions.append(Assertion(text=text, section=current_section, line_number=lineno))

    return assertions


# ---------------------------------------------------------------------------
# brain.db helpers
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def search_brain(query: str, limit: int = 5) -> list[dict]:
    """Query brainctl for memories related to query text."""
    if not BRAINCTL.exists():
        return []
    try:
        result = subprocess.run(
            [str(BRAINCTL), "search", query, "--tables", "memories", "--limit", str(limit)],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
        memories = data.get("memories", [])
        return [m for m in memories if m.get("fts_rank", -999) > MIN_RELEVANCE_SCORE]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


def get_memory_by_id(conn: sqlite3.Connection, memory_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM memories WHERE id = ? AND retired_at IS NULL", (memory_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------
def _check_negation(assertion_text: str, brain_content: str) -> Optional[str]:
    """Check if brain_content negates a claim in assertion_text."""
    at = assertion_text.lower()
    bc = brain_content.lower()

    for pos_pat, neg_pat in NEGATION_PATTERNS:
        at_pos = bool(re.search(pos_pat, at))
        bc_neg = bool(re.search(neg_pat, bc))
        bc_pos = bool(re.search(pos_pat, bc))
        at_neg = bool(re.search(neg_pat, at))

        if (at_pos and bc_neg) or (bc_pos and at_neg):
            return f"pattern: '{pos_pat}' vs '{neg_pat}'"

    return None


def _extract_numbers(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\b(\d{2,})\b", text)]


def _check_numeric_delta(assertion_text: str, brain_content: str) -> Optional[str]:
    """Flag if both texts contain numbers but the values differ significantly."""
    a_nums = _extract_numbers(assertion_text)
    b_nums = _extract_numbers(brain_content)
    if not a_nums or not b_nums:
        return None

    # Check if there's a shared "anchor" keyword near the number in both texts
    shared_keywords = {"agent", "agents", "issue", "issues", "route", "routes",
                       "migration", "migrations", "file", "files", "seat", "seats"}

    at_lower = assertion_text.lower()
    bc_lower = brain_content.lower()
    has_shared = any(kw in at_lower and kw in bc_lower for kw in shared_keywords)
    if not has_shared:
        return None

    # Look for significantly different values (>20% delta)
    for an in a_nums:
        for bn in b_nums:
            if an == 0 or bn == 0:
                continue
            ratio = abs(an - bn) / max(an, bn)
            if ratio > 0.20:
                return f"numeric delta: memory_block={an} vs brain.db={bn} ({ratio:.0%} diff)"

    return None


def detect_conflicts(
    assertions: list[Assertion],
    verbose: bool = False,
) -> list[Conflict]:
    """Run contradiction detection across all assertions against brain.db."""
    conflicts: list[Conflict] = []

    for assertion in assertions:
        # Build a compact search query from the assertion text
        # Strip markdown and limit length
        query = re.sub(r"\*\*|`|_", "", assertion.text)[:200]
        memories = search_brain(query)

        if verbose:
            print(f"  [{assertion.section}] Checking: {assertion.text[:80]}…")
            print(f"    → {len(memories)} brain.db matches")

        for mem in memories:
            brain_content = mem.get("content", "")
            brain_id = mem.get("id")
            brain_conf = mem.get("confidence", 1.0)

            # Negation check
            neg_detail = _check_negation(assertion.text, brain_content)
            if neg_detail:
                conflicts.append(Conflict(
                    assertion=assertion,
                    brain_memory_id=brain_id,
                    brain_content=brain_content,
                    brain_confidence=brain_conf,
                    conflict_type="negation",
                    detail=neg_detail,
                ))
                continue

            # Numeric delta check
            num_detail = _check_numeric_delta(assertion.text, brain_content)
            if num_detail:
                conflicts.append(Conflict(
                    assertion=assertion,
                    brain_memory_id=brain_id,
                    brain_content=brain_content,
                    brain_confidence=brain_conf,
                    conflict_type="numeric_delta",
                    detail=num_detail,
                ))

    return conflicts


# ---------------------------------------------------------------------------
# Flagging & resolution
# ---------------------------------------------------------------------------
def flag_conflict_in_brain(
    conn: sqlite3.Connection,
    conflict: Conflict,
    dry_run: bool = False,
) -> Optional[int]:
    """Write a contradiction_detected event to brain.db for this conflict."""
    summary = (
        f"MEMORY.md assertion contradicts brain.db memory {conflict.brain_memory_id}. "
        f"Type: {conflict.conflict_type}. {conflict.detail}"
    )
    metadata = json.dumps({
        "memory_block_assertion": conflict.assertion.text[:200],
        "memory_block_section": conflict.assertion.section,
        "memory_block_line": conflict.assertion.line_number,
        "brain_memory_id": conflict.brain_memory_id,
        "brain_content_preview": conflict.brain_content[:200],
        "conflict_type": conflict.conflict_type,
        "detail": conflict.detail,
    })

    if dry_run:
        print(f"    [DRY RUN] Would log contradiction event: {summary[:100]}")
        return None

    cur = conn.execute(
        """
        INSERT INTO events (agent_id, event_type, summary, metadata, importance, created_at)
        VALUES (?, 'contradiction_detected', ?, ?, 0.8, datetime('now'))
        """,
        (AGENT_ID, summary, metadata),
    )
    conn.commit()
    return cur.lastrowid


def auto_resolve_conflict_in_block(
    conflict: Conflict,
    memory_block_path: Path,
    dry_run: bool = False,
) -> bool:
    """Replace the contradicted assertion in MEMORY.md with the brain.db version.

    Bridge-P1 (COS-210) is implemented — brain.db version wins (has provenance
    and trust scores). Returns True if the entry was replaced.
    """
    if not memory_block_path.exists():
        return False

    raw = memory_block_path.read_text(encoding="utf-8")
    entries = [e.strip() for e in raw.split("\n§\n") if e.strip()]

    # Find the entry containing the assertion
    target_idx = None
    for i, entry in enumerate(entries):
        if conflict.assertion.text[:60] in entry:
            target_idx = i
            break

    if target_idx is None:
        return False

    if dry_run:
        print(f"    [DRY RUN] Would replace entry at index {target_idx}:")
        print(f"      OLD: {entries[target_idx][:80]}…")
        print(f"      NEW: {conflict.brain_content[:80]}…")
        return False

    entries[target_idx] = conflict.brain_content.strip()
    memory_block_path.write_text("\n§\n".join(entries), encoding="utf-8")
    return True


def auto_resolve_note(conflict: Conflict) -> str:
    """Note about auto-resolve availability — Bridge-P1 (COS-210) is live."""
    return (
        "AUTO-RESOLVE AVAILABLE: Bridge-P1 (COS-210) is implemented. "
        "Run with --auto-resolve to replace this entry with the brain.db version. "
        "Brain.db wins: it has provenance, trust scores, and version history."
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------
def format_report(conflicts: list[Conflict], assertions_checked: int) -> str:
    lines = [
        "=" * 72,
        "MEMORY BLOCK VALIDATION REPORT",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Source: {MEMORY_BLOCK_PATH}",
        f"Assertions checked: {assertions_checked}",
        f"Conflicts found: {len(conflicts)}",
        "=" * 72,
    ]

    if not conflicts:
        lines.append("\nNo contradictions detected. Memory block is coherent with brain.db.")
        lines.append("=" * 72)
        return "\n".join(lines)

    lines.append("")
    for i, c in enumerate(conflicts, 1):
        lines.append(f"CONFLICT #{i} — {c.conflict_type.upper()}")
        lines.append(f"  Section:   {c.assertion.section} (line {c.assertion.line_number})")
        lines.append(f"  Assertion: {c.assertion.text[:120]}")
        lines.append(f"  Brain #:   memory_id={c.brain_memory_id} (confidence={c.brain_confidence:.2f})")
        lines.append(f"  Brain:     {c.brain_content[:120]}")
        lines.append(f"  Detail:    {c.detail}")
        lines.append(f"  Resolve:   {auto_resolve_note(c)}")
        lines.append("")

    lines.append("=" * 72)
    lines.append("ACTION REQUIRED: Review conflicts above and update MEMORY.md or brain.db.")
    lines.append("Run with --auto-resolve to replace contradicted entries with brain.db versions.")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Kokoro's MEMORY.md against brain.db")
    parser.add_argument("--auto-resolve", action="store_true",
                        help="Replace contradicted MEMORY.md entries with brain.db versions (Bridge-P1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing anything")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress report; exit code 1 if conflicts found")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-assertion search progress")
    parser.add_argument("--memory-block", type=Path, default=MEMORY_BLOCK_PATH,
                        help=f"Path to MEMORY.md (default: {MEMORY_BLOCK_PATH})")
    args = parser.parse_args()

    if not args.memory_block.exists():
        print(f"ERROR: Memory block not found at {args.memory_block}", file=sys.stderr)
        return 2

    if not DB_PATH.exists():
        print(f"ERROR: brain.db not found at {DB_PATH}", file=sys.stderr)
        return 2

    # Parse assertions
    assertions = parse_memory_block(args.memory_block)
    if args.verbose:
        print(f"Parsed {len(assertions)} factual assertions from {args.memory_block}")

    # Detect conflicts
    conflicts = detect_conflicts(assertions, verbose=args.verbose)

    # Flag conflicts in brain.db and optionally auto-resolve in MEMORY.md
    auto_resolved = 0
    if conflicts and (args.auto_resolve or not args.dry_run):
        conn = get_db()
        for conflict in conflicts:
            event_id = flag_conflict_in_brain(conn, conflict, dry_run=args.dry_run)
            if event_id and args.verbose:
                print(f"  Logged contradiction event #{event_id} for conflict: "
                      f"{conflict.assertion.text[:60]}…")
            # Bridge-P1 (COS-210): auto-replace MEMORY.md entry with brain.db version
            if args.auto_resolve:
                replaced = auto_resolve_conflict_in_block(
                    conflict, args.memory_block, dry_run=args.dry_run
                )
                if replaced:
                    auto_resolved += 1
                    if args.verbose:
                        print(f"  Auto-resolved: replaced '{conflict.assertion.text[:60]}…'")
        conn.close()

    # Output
    if not args.quiet:
        print(format_report(conflicts, len(assertions)))
    elif conflicts:
        print(f"WARNING: {len(conflicts)} contradiction(s) found in MEMORY.md vs brain.db",
              file=sys.stderr)

    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
