#!/usr/bin/env python3
"""Install brainctl hooks into Claude Code's user-level settings.

Merges entries into `~/.claude/settings.json` (or `$CLAUDE_HOME/settings.json`)
so Claude Code invokes this plugin's hook scripts on SessionStart,
UserPromptSubmit, PostToolUse, and SessionEnd. Existing hook entries are
preserved — this script is idempotent and only appends new commands if
they aren't already registered.

Usage:
    python3 plugins/claude-code/brainctl/install.py         # install
    python3 plugins/claude-code/brainctl/install.py --dry   # preview only
    python3 plugins/claude-code/brainctl/install.py --uninstall
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent / "hooks"

HOOK_EVENTS = {
    "SessionStart":    HOOKS_DIR / "session_start.py",
    "UserPromptSubmit": HOOKS_DIR / "user_prompt_submit.py",
    "PostToolUse":     HOOKS_DIR / "post_tool_use.py",
    "SessionEnd":      HOOKS_DIR / "session_end.py",
}


def settings_path() -> Path:
    base = os.environ.get("CLAUDE_HOME")
    if base:
        return Path(base).expanduser() / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except Exception as exc:
        print(f"[brainctl] failed to parse {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def build_command(script: Path) -> str:
    """Return the shell command Claude Code should run for a hook.

    We use `python3 <script>` rather than making the scripts executable,
    so the plugin works even when the file permission bits are stripped
    (e.g. after `pip install` or zip extraction)."""
    return f"python3 {script}"


def install_hooks(data: dict) -> tuple[dict, list[str]]:
    """Merge brainctl hooks into Claude Code settings. Returns (data, added)."""
    hooks = data.setdefault("hooks", {})
    added: list[str] = []

    for event, script in HOOK_EVENTS.items():
        command = build_command(script)
        entries = hooks.setdefault(event, [])
        # Claude Code stores hooks as list of { matcher, hooks: [{type, command}] }.
        already = False
        for group in entries:
            for h in (group.get("hooks") or []):
                if h.get("command") == command:
                    already = True
                    break
            if already:
                break
        if already:
            continue
        entries.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": command}],
        })
        added.append(event)

    return data, added


def uninstall_hooks(data: dict) -> tuple[dict, list[str]]:
    """Remove any hook entry whose command references this plugin's hooks/ dir."""
    removed: list[str] = []
    hooks = data.get("hooks") or {}
    marker = str(HOOKS_DIR)
    for event, entries in list(hooks.items()):
        new_entries = []
        for group in entries:
            kept_hooks = [h for h in (group.get("hooks") or []) if marker not in (h.get("command") or "")]
            if kept_hooks:
                group["hooks"] = kept_hooks
                new_entries.append(group)
            else:
                removed.append(event)
        if new_entries:
            hooks[event] = new_entries
        else:
            hooks.pop(event, None)
    return data, removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true", help="Print resulting settings without writing")
    ap.add_argument("--uninstall", action="store_true", help="Remove brainctl hook entries")
    ap.add_argument("--path", help="Override settings.json location")
    args = ap.parse_args()

    sp = Path(args.path).expanduser() if args.path else settings_path()
    data = load_settings(sp)

    if args.uninstall:
        data, removed = uninstall_hooks(data)
        action = f"removed {len(removed)} hook(s): {', '.join(removed) or '(none)'}"
    else:
        data, added = install_hooks(data)
        action = f"added {len(added)} hook(s): {', '.join(added) or '(already installed)'}"

    if args.dry:
        print(f"# Would write to: {sp}")
        print(f"# {action}")
        print(json.dumps(data, indent=2))
        return

    save_settings(sp, data)
    print(f"[brainctl] {sp}: {action}")


if __name__ == "__main__":
    main()
