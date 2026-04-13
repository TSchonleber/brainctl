#!/usr/bin/env python3
"""Claude Code SessionEnd hook — persists a wrap-up handoff packet.

Synthesizes a short summary from the current session's events (the ones
the PostToolUse hook just wrote), then calls `Brain.wrap_up` to create a
pending handoff the next `agent_orient` will surface.

If no meaningful activity happened (no session_start event in this run),
the hook no-ops — avoiding polluting the brain with empty handoffs when
Claude Code launches briefly for a single completion.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import read_hook_input, project_name, get_brain, safe_exit  # noqa: E402


def build_summary(brain, project: str | None) -> str:
    """Build a one-paragraph session summary from recent events."""
    try:
        # Use the brain's own event tail — keeps the hook self-contained.
        db = brain._get_conn()
        q = (
            "SELECT event_type, summary FROM events "
            "WHERE agent_id = ? "
            + ("AND project = ? " if project else "")
            + "ORDER BY id DESC LIMIT 30"
        )
        params: list = [brain.agent_id]
        if project:
            params.append(project)
        rows = db.execute(q, params).fetchall()
    except Exception:
        return "Session ended."

    tool_count = sum(1 for r in rows if (r["event_type"] or "") == "observation")
    error_count = sum(1 for r in rows if (r["event_type"] or "") == "error")
    recent_tools: list[str] = []
    for r in rows:
        s = (r["summary"] or "")
        if s.startswith("tool:") and len(recent_tools) < 5:
            # Extract the tool name between "tool:" and the first space.
            try:
                recent_tools.append(s.split()[0].split(":", 1)[1])
            except Exception:
                pass

    pieces = [f"{tool_count} tool calls"]
    if error_count:
        pieces.append(f"{error_count} errors")
    if recent_tools:
        pieces.append("tools=" + ",".join(recent_tools))
    return "Claude Code session ended: " + "; ".join(pieces)


def main() -> None:
    payload = read_hook_input()
    brain = get_brain(payload)
    if brain is None:
        safe_exit()

    project = project_name(payload)
    summary = build_summary(brain, project)

    try:
        brain.wrap_up(summary=summary, project=project)
    except Exception as exc:
        print(f"[brainctl-hook] wrap_up failed: {exc}", file=sys.stderr)

    safe_exit()


if __name__ == "__main__":
    main()
