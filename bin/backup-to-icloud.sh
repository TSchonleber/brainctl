#!/bin/bash
# Nightly backup of agent memory DB to iCloud (as SQL dump — safe for sync)
# Run via cron or launchd

set -euo pipefail

DB="$HOME/agentmemory/db/brain.db"
BACKUP_DIR="$HOME/agentmemory/backups"
ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/AgentMemoryBackups"

mkdir -p "$BACKUP_DIR" "$ICLOUD_DIR"

TS=$(date +%Y%m%d_%H%M%S)

# 1. Local binary backup
cp "$DB" "$BACKUP_DIR/brain_${TS}.db"

# 2. SQL dump for iCloud (text-safe, no WAL corruption risk)
sqlite3 "$DB" .dump > "$ICLOUD_DIR/brain_${TS}.sql"

# 3. Compact latest dump for quick restore
cp "$ICLOUD_DIR/brain_${TS}.sql" "$ICLOUD_DIR/brain_latest.sql"

# 4. Prune old local backups (keep last 30)
ls -1t "$BACKUP_DIR"/brain_*.db 2>/dev/null | tail -n +31 | xargs rm -f
ls -1t "$BACKUP_DIR"/brain_*.sql 2>/dev/null | tail -n +31 | xargs rm -f

# 5. Prune old iCloud backups (keep last 14)
ls -1t "$ICLOUD_DIR"/brain_2*.sql 2>/dev/null | tail -n +15 | xargs rm -f

# 6. Report
SIZE=$(stat -f%z "$DB")
DUMP_SIZE=$(stat -f%z "$ICLOUD_DIR/brain_${TS}.sql")
echo "{\"ok\":true,\"timestamp\":\"${TS}\",\"db_bytes\":${SIZE},\"dump_bytes\":${DUMP_SIZE}}"
