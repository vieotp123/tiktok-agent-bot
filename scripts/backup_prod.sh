#!/usr/bin/env bash
# backup_prod.sh — create a tar.gz of the live repo + runtime DBs.
#
# Excludes: venv, __pycache__, node_modules, backups/, *.log, .git/objects.
# Output:   /opt/tiktok-bot/backups/prod_<UTC-timestamp>.tar.gz
# Never logs secrets. Returns 0 on success.

set -euo pipefail

REPO=/opt/tiktok-bot
BACKUP_DIR="$REPO/backups"
mkdir -p "$BACKUP_DIR"

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/prod_${TS}.tar.gz"

# Build exclude list. We DO archive .env and storage_state because the
# backup is local-only (gitignored, not pushed). If you later off-site
# this archive, encrypt it first.
# tar from the parent directory so the archive contains "tiktok-bot/…"
PARENT=$(dirname "$REPO")
NAME=$(basename "$REPO")
tar --exclude="$NAME/venv" \
    --exclude="**/__pycache__" \
    --exclude="$NAME/node_modules" \
    --exclude="$NAME/backups" \
    --exclude="$NAME/screenshots" \
    --exclude="$NAME/*.log" \
    --exclude="$NAME/.git/objects" \
    --exclude="$NAME/data/uploads" \
    --exclude="$NAME/data/cache" \
    -czf "$OUT" \
    -C "$PARENT" "$NAME"

echo "✅ backup written: $OUT"
echo "size: $(du -h "$OUT" | awk '{print $1}')"

# Retention: keep last 10 backups, delete older.
ls -1t "$BACKUP_DIR"/prod_*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f
echo "retained $(ls -1 "$BACKUP_DIR"/prod_*.tar.gz 2>/dev/null | wc -l) backup(s)"
