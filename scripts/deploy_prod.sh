#!/usr/bin/env bash
# deploy_prod.sh — safe deploy for low/medium-risk changes.
#
# Flow:
#   1. Refuse to run as a regular user — needs sudo for systemctl restart.
#   2. Run smoke_test.sh BEFORE touching anything.
#   3. Create a backup tar.gz.
#   4. Restart tiktok-backend, tiktok-telegram, tiktok-bot in that order.
#      (backend first so the chat workers can talk to it.)
#   5. Run smoke_test.sh AGAIN. If it fails, run rollback_prod.sh.
#
# This script does NOT do `git pull` — code is expected to already be in
# /opt/tiktok-bot. Use this AFTER the code worker has committed and
# pushed (or after a manual git pull / sync).

set -uo pipefail

REPO=/opt/tiktok-bot
SCRIPTS="$REPO/scripts"
LOG_TAG="[deploy_prod]"

if [[ "$EUID" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    echo "$LOG_TAG ❌ requires sudo to restart services. abort."
    exit 2
fi

# Confirm the script is allowed to run (block if maintenance lock exists).
if [[ -f "$REPO/data/deploy.lock" ]]; then
    echo "$LOG_TAG ⏸ deploy.lock present — manual gate. abort."
    exit 3
fi

echo "$LOG_TAG step 1/5: smoke_test (pre)"
if ! bash "$SCRIPTS/smoke_test.sh"; then
    echo "$LOG_TAG ❌ pre-deploy smoke test failed. abort BEFORE backup/restart."
    exit 4
fi

echo "$LOG_TAG step 2/5: backup"
if ! bash "$SCRIPTS/backup_prod.sh"; then
    echo "$LOG_TAG ❌ backup failed. abort."
    exit 5
fi
LATEST_BACKUP=$(ls -1t "$REPO/backups"/prod_*.tar.gz 2>/dev/null | head -1 || true)
echo "$LOG_TAG using backup: $LATEST_BACKUP"

echo "$LOG_TAG step 3/5: restart tiktok-backend"
sudo systemctl restart tiktok-backend
sleep 3

echo "$LOG_TAG step 4/5: restart tiktok-telegram + tiktok-bot"
sudo systemctl restart tiktok-telegram
sudo systemctl restart tiktok-bot
sleep 4

echo "$LOG_TAG step 5/5: smoke_test (post)"
if bash "$SCRIPTS/smoke_test.sh"; then
    echo "$LOG_TAG ✅ deploy successful"
    exit 0
fi

echo "$LOG_TAG ❌ post-deploy smoke FAILED — invoking rollback"
bash "$SCRIPTS/rollback_prod.sh" "$LATEST_BACKUP"
exit 6
