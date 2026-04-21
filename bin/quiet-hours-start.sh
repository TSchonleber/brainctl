#!/bin/bash
# quiet-hours-start.sh — Disable all task-tracker agent heartbeats during peak hours
# Runs at 8:00 AM ET (12:00 UTC) daily via cron
# Saves prior state so quiet-hours-end.sh can restore exactly

set -euo pipefail
# Resolve relative to this script so cron (which sets CWD=$HOME)
# finds the companion .py file. Audit I32.
cd "$(dirname "$0")"
exec python3 quiet-hours-start.py
