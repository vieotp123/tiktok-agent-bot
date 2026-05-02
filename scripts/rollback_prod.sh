#!/usr/bin/env bash
# rollback_prod.sh <backup_path>
# Restore the repo from a backup tar.gz, restart services, run smoke test.
#
# Usage:
#   rollback_prod.sh                       # uses most recent backup
#   rollback_prod.sh path/to/prod_X.tar.gz # uses given backup

set -uo pipefail

REPO=/opt/tiktok-bot
BACKUP_DIR="$REPO/backups"
SCRIPTS="$REPO/scripts"
LOG_TAG="[rollback_prod]"

# Pick the backup
if [[ "${1:-}" != "" ]]; then
    BACKUP="$1"
else
    BACKUP=$(ls -1t "$BACKUP_DIR"/prod_*.tar.gz 2>/dev/null | head -1 || true)
fi
if [[ -z "$BACKUP" || ! -f "$BACKUP" ]]; then
    echo "$LOG_TAG ❌ no backup found. abort."
    exit 2
fi

if [[ "$EUID" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    echo "$LOG_TAG ❌ requires sudo. abort."
    exit 2
fi

echo "$LOG_TAG restoring from $BACKUP"

# Safety: require an "I_KNOW_WHAT_I_AM_DOING=yes" env var when invoking
# directly to avoid accidental rollback. deploy_prod.sh sets this.
if [[ "${CALLED_BY_DEPLOY:-}" != "1" && "${I_KNOW_WHAT_I_AM_DOING:-}" != "yes" ]]; then
    echo "$LOG_TAG ⏸ refuse to rollback without confirmation."
    echo "Either:"
    echo "  CALLED_BY_DEPLOY=1 $0 \"$BACKUP\""
    echo "or set I_KNOW_WHAT_I_AM_DOING=yes (run from cron/agent only with audit)."
    exit 3
fi

# Stop services first to avoid file-in-use issues.
sudo systemctl stop tiktok-bot tiktok-telegram tiktok-backend
sleep 2

# Extract OVER /opt/tiktok-bot — the tar was created from / containing
# tiktok-bot, so extracting from / restores it in place.
echo "$LOG_TAG extracting backup …"
tar -xzf "$BACKUP" -C / || {
    echo "$LOG_TAG ❌ tar extract failed"
    exit 4
}

# Restart services
sudo systemctl start tiktok-backend
sleep 3
sudo systemctl start tiktok-telegram tiktok-bot
sleep 3

# Verify
if bash "$SCRIPTS/smoke_test.sh"; then
    echo "$LOG_TAG ✅ rollback successful"
    exit 0
fi
echo "$LOG_TAG ❌ smoke test still failing after rollback — manual intervention required"
exit 5
