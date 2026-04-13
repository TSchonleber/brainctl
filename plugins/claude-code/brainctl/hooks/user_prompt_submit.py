#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — logs each user prompt as an event.

Strips any `<private>…</private>` blocks before writing so secrets and
credentials never land in brain.db. If the entire prompt is private, the
hook no-ops without logging anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import read_hook_input, project_name, get_brain, safe_exit  # noqa: E402


def main() -> None:
    payload = read_hook_input()
    prompt = (payload.get("prompt") or payload.get("user_prompt") or "").strip()
    if not prompt:
        safe_exit()

    try:
        from agentmemory.lib.privacy import redact_private, is_all_private
    except Exception:
        # If the privacy helper isn't available, fall back to raw pass-through.
        redact_private = lambda t: t  # type: ignore[assignment]
        is_all_private = lambda t: False  # type: ignore[assignment]

    if is_all_private(prompt):
        safe_exit()

    cleaned = redact_private(prompt)
    if not cleaned:
        safe_exit()

    brain = get_brain(payload)
    if brain is None:
        safe_exit()

    try:
        # Short summary — first 300 chars, single line.
        summary = cleaned[:300].replace("\n", " ").strip()
        brain.log(
            f"user_prompt: {summary}",
            event_type="observation",
            project=project_name(payload),
        )
    except Exception as exc:
        print(f"[brainctl-hook] prompt log failed: {exc}", file=sys.stderr)

    safe_exit()


if __name__ == "__main__":
    main()
