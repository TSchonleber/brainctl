#!/usr/bin/env python3
"""Claude Code SessionStart hook — injects prior context via `Brain.orient`.

Runs when a Claude Code session begins. Pulls the pending handoff packet,
recent events, active triggers, top memories, and stats from brainctl,
formats them as a compact markdown block, and returns it to Claude Code
as `hookSpecificOutput.additionalContext` so Claude sees them in its
opening system prompt.

Silently no-ops if brainctl is not installed or the DB is unavailable —
the user's session must never break because of a missing memory backend.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import read_hook_input, project_name, get_brain, safe_exit  # noqa: E402


def format_context(snap: dict) -> str:
    """Render an orient snapshot as a compact markdown brief for Claude."""
    lines: list[str] = ["## brainctl session context"]

    handoff = snap.get("handoff")
    if handoff:
        lines.append("")
        lines.append("**Pending handoff from last session:**")
        if handoff.get("goal"):
            lines.append(f"- Goal: {handoff['goal']}")
        if handoff.get("current_state"):
            lines.append(f"- State: {handoff['current_state']}")
        if handoff.get("open_loops"):
            lines.append(f"- Open loops: {handoff['open_loops']}")
        if handoff.get("next_step"):
            lines.append(f"- Next step: {handoff['next_step']}")

    events = snap.get("recent_events") or []
    if events:
        lines.append("")
        lines.append("**Recent events:**")
        for ev in events[:5]:
            lines.append(f"- [{ev.get('event_type', 'event')}] {ev.get('summary', '')}")

    triggers = snap.get("triggers") or []
    if triggers:
        lines.append("")
        lines.append("**Active triggers:**")
        for tr in triggers[:5]:
            cond = tr.get("trigger_condition") or tr.get("trigger_keywords") or ""
            lines.append(f"- ({tr.get('priority', 'med')}) {cond}")

    memories = snap.get("memories") or []
    if memories:
        lines.append("")
        lines.append("**Relevant memories:**")
        for m in memories[:5]:
            content = (m.get("content") or "").strip().splitlines()[0][:200]
            lines.append(f"- [{m.get('category', '?')}] {content}")

    stats = snap.get("stats") or {}
    if stats:
        lines.append("")
        lines.append(
            f"_Brain stats: {stats.get('active_memories', 0)} memories, "
            f"{stats.get('total_events', 0)} events, "
            f"{stats.get('total_entities', 0)} entities._"
        )

    if len(lines) == 1:
        # No actual context — skip injection entirely.
        return ""
    return "\n".join(lines)


def main() -> None:
    payload = read_hook_input()
    brain = get_brain(payload)
    if brain is None:
        safe_exit()

    try:
        snap = brain.orient(project=project_name(payload))
    except Exception as exc:
        print(f"[brainctl-hook] orient failed: {exc}", file=sys.stderr)
        safe_exit()

    text = format_context(snap or {})
    if not text:
        safe_exit()

    # Claude Code SessionStart hook protocol: return additionalContext so
    # the rendered markdown is injected into the opening system prompt.
    safe_exit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    })


if __name__ == "__main__":
    main()
