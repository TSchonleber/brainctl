#!/bin/bash
# quiet-hours-start.sh — Disable all Paperclip agent heartbeats during peak hours
# Runs at 8:00 AM ET (12:00 UTC) daily via cron
# Saves prior state so quiet-hours-end.sh can restore exactly

set -euo pipefail
exec /Users/r4vager/agentmemory/.venv/bin/python3 /Users/r4vager/agentmemory/bin/quiet-hours-start.py
