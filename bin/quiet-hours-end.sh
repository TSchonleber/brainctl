#!/bin/bash
# quiet-hours-end.sh — Restore all task-tracker agent heartbeats after peak hours
# Runs at 2:00 PM ET (18:00 UTC) daily via cron

set -euo pipefail
# Resolve relative to this script so cron (which sets CWD=$HOME)
# finds the companion .py file. Audit I32.
cd "$(dirname "$0")"
exec python3 quiet-hours-end.py
