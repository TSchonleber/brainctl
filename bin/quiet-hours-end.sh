#!/bin/bash
# quiet-hours-end.sh — Restore all Paperclip agent heartbeats after peak hours
# Runs at 2:00 PM ET (18:00 UTC) daily via cron

set -euo pipefail
exec /Users/r4vager/agentmemory/.venv/bin/python3 /Users/r4vager/agentmemory/bin/quiet-hours-end.py
