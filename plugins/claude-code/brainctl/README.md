# brainctl for Claude Code

Persistent memory for [Claude Code](https://claude.com/claude-code) powered by
[brainctl](https://pypi.org/project/brainctl/). Every session starts with the
handoff packet from your last run injected as context, every tool call is
journaled as an observation event, and every session ends with a new handoff
packet written to `brain.db` — all via Claude Code's native lifecycle hooks.

No long-running worker, no HTTP port, no LLM calls. One SQLite file.

> Inspired by the lifecycle-hook approach pioneered by
> [claude-mem](https://github.com/thedotmack/claude-mem), but built natively on
> brainctl's existing `Brain.orient()` / `Brain.wrap_up()` primitives.

## What you get

| Claude Code hook | brainctl write |
|---|---|
| **SessionStart** | `Brain.orient()` snapshot (handoff + recent events + triggers + top memories) injected as `additionalContext` |
| **UserPromptSubmit** | User prompt logged as `observation` event (with `<private>` redaction) |
| **PostToolUse** | Each tool call logged as `observation` / `error` event (tool name + short input summary, no full outputs) |
| **SessionEnd** | `Brain.wrap_up()` creates a pending handoff packet for the next session |

## Install

```bash
pip install 'brainctl>=1.2.0'
python3 plugins/claude-code/brainctl/install.py
```

The installer merges hook entries into `~/.claude/settings.json` (or
`$CLAUDE_HOME/settings.json` if set) and is idempotent — rerunning is safe.

Dry-run first if you're nervous:

```bash
python3 plugins/claude-code/brainctl/install.py --dry
```

Uninstall:

```bash
python3 plugins/claude-code/brainctl/install.py --uninstall
```

## How session continuity works

1. Claude Code starts a session in `/your/project/`.
2. `SessionStart` hook runs `Brain.orient(project="your-project")` and prints a
   compact markdown brief to stdout under `hookSpecificOutput.additionalContext`.
3. Claude Code injects that brief into the model's opening system prompt.
4. Claude sees: *"Pending handoff from last session: Goal … Next step …"* and
   picks up where the last session left off.
5. As Claude works, every tool call gets a tiny event row.
6. When the session ends, `SessionEnd` writes a new handoff packet summarizing
   what happened. Next `SessionStart` picks it up.

## Config

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `BRAIN_DB` | `~/agentmemory/db/brain.db` | Override the SQLite brain path |
| `BRAINCTL_AGENT_ID` | `cc:<cwd-basename>` | Stable agent ID for the session |
| `BRAINCTL_PROJECT` | `<cwd-basename>` | Project scope for events and handoffs |

## Privacy

Wrap any text in `<private>…</private>` tags and it will be stripped before
being written to the brain. Applies to user prompts (UserPromptSubmit hook)
and, in the future, any other text written through `agentmemory.lib.privacy`.

```
Investigate the crash. <private>db_password=hunter2</private> Also check logs.
```

What ends up in `brain.db`: `user_prompt: Investigate the crash.   Also check logs.`

If the entire prompt is wrapped in `<private>`, nothing is logged at all.

## Graceful degradation

Every hook is a no-op if brainctl isn't installed, if `brain.db` is missing,
or if anything throws. **Your Claude Code session never breaks** because of a
memory system glitch — the worst case is you lose the events from that run.
Errors are logged to stderr with the prefix `[brainctl-hook]` so you can spot
them in your terminal scrollback.

## Manual invocation

The same capabilities are available as CLI commands, handy for scripts, cron
jobs, or non-Claude-Code agents:

```bash
# Session start: get pending handoff + recent events + triggers + stats
brainctl --agent cc:my-project orient --project my-project

# Session end: log session_end + create handoff packet
brainctl --agent cc:my-project wrap-up \
  --summary "Fixed the auth bug; added integration tests." \
  --goal "Ship v1.3" \
  --next-step "Run the full test suite"
```

## What this plugin does NOT do

- **No LLM summarization.** The `SessionEnd` handoff is synthesized from
  structured event rows, not a Claude call. If you want richer summaries,
  call `brainctl wrap-up` manually with your own summary text.
- **No full tool-output capture.** `PostToolUse` stores the tool name and a
  short input preview (~200 chars) — enough for forensics, not enough to leak
  file contents into `brain.db`.
- **No sidecar process.** Hooks run as one-shot Python scripts under the same
  runtime Claude Code uses, no background daemon.

## Storage footprint

- Plugin code: ~16 KB Python
- Per session: ~10–50 event rows at ~200 bytes each = <10 KB
- `brain.db` growth: sub-MB per month of normal use

## Compatibility

- Claude Code (any version that supports `SessionStart` / `UserPromptSubmit` /
  `PostToolUse` / `SessionEnd` hooks with JSON stdin/stdout protocol)
- brainctl ≥ 1.2.0
- Python ≥ 3.11

## License

MIT. Part of the [brainctl](https://github.com/TSchonleber/brainctl) project.
