#!/usr/bin/env python3
"""Claude Code PostToolUse hook — records tool executions as observations.

Captures a compact event each time Claude runs a tool (Read, Edit, Bash,
etc.) so the brain builds a lightweight audit trail of the session. Does
NOT store full tool results — only the tool name, a short input summary,
and success/failure — to keep brain.db small and avoid leaking secrets.

Private tags in the input are redacted before logging.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import read_hook_input, project_name, get_brain, safe_exit  # noqa: E402

# Tools whose inputs are noisy or uninteresting for memory — skip logging.
_SKIP_TOOLS = {"TodoWrite", "Glob", "Grep"}
# Maximum characters of input summary to persist.
_MAX_INPUT_CHARS = 200


def summarize_input(tool_name: str, tool_input: dict) -> str:
    """Build a short one-line summary of a tool's input payload."""
    if not isinstance(tool_input, dict):
        return ""
    # Common fields across Claude Code tools.
    for key in ("file_path", "command", "pattern", "url", "path", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return f"{key}={val.strip()[:_MAX_INPUT_CHARS]}"
    # Fallback — tiny JSON snippet.
    try:
        return json.dumps(tool_input, default=str)[:_MAX_INPUT_CHARS]
    except Exception:
        return ""


def main() -> None:
    payload = read_hook_input()
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    if not tool_name or tool_name in _SKIP_TOOLS:
        safe_exit()

    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_response = payload.get("tool_response") or payload.get("toolResponse") or {}

    try:
        from agentmemory.lib.privacy import redact_private
    except Exception:
        redact_private = lambda t: t  # type: ignore[assignment]

    summary_bits = summarize_input(tool_name, tool_input)
    summary_bits = redact_private(summary_bits) if summary_bits else ""

    # Determine success. Claude Code conventions vary; check a few fields.
    is_error = bool(
        (isinstance(tool_response, dict) and tool_response.get("is_error"))
        or payload.get("is_error")
    )
    status = "error" if is_error else "ok"

    brain = get_brain(payload)
    if brain is None:
        safe_exit()

    try:
        summary = f"tool:{tool_name} [{status}] {summary_bits}".strip()[:500]
        brain.log(
            summary,
            event_type="observation" if not is_error else "error",
            project=project_name(payload),
        )
    except Exception as exc:
        print(f"[brainctl-hook] tool log failed: {exc}", file=sys.stderr)

    safe_exit()


if __name__ == "__main__":
    main()
