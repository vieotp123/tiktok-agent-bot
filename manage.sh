#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/tiktok-bot"
VENV="$PROJECT_DIR/venv"
ENV_FILE="$PROJECT_DIR/.env"
BACKEND_SERVICE="tiktok-backend"
BOT_SERVICE="tiktok-bot"

cd "$PROJECT_DIR"

# Load .env for local commands
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"

case "${1:-help}" in

# ── install ───────────────────────────────────────────────────────────────────
install)
    echo "[install] Creating venv..."
    python3 -m venv "$VENV"

    echo "[install] Installing requirements..."
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -r requirements.txt -q

    echo "[install] Installing Playwright Chromium..."
    "$VENV/bin/playwright" install chromium

    echo "[install] Installing system deps for Playwright..."
    "$VENV/bin/playwright" install-deps chromium 2>/dev/null || true

    echo "[install] Checking .env..."
    if [ ! -f "$ENV_FILE" ]; then
        cp .env.example .env
        echo "  ⚠️  .env created from example — edit it before starting."
    fi

    echo "[install] Checking storage_state..."
    if [ ! -f "$PROJECT_DIR/tiktok_storage_state.json" ]; then
        echo "  ⚠️  tiktok_storage_state.json NOT found — copy it before starting bot."
    fi

    echo "[install] Installing systemd services..."
    sudo cp systemd/tiktok-backend.service /etc/systemd/system/
    sudo cp systemd/tiktok-bot.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable "$BACKEND_SERVICE" "$BOT_SERVICE" 2>/dev/null || true

    echo "[install] Done. Run: ./manage.sh start"
    ;;

# ── start ─────────────────────────────────────────────────────────────────────
start)
    echo "[start] Starting backend..."
    sudo systemctl start "$BACKEND_SERVICE"
    sleep 2

    echo "[start] Starting bot..."
    sudo systemctl start "$BOT_SERVICE"

    echo "[start] Done. Check logs: ./manage.sh logs"
    ;;

# ── stop ──────────────────────────────────────────────────────────────────────
stop)
    echo "[stop] Stopping services..."
    sudo systemctl stop "$BOT_SERVICE" 2>/dev/null || true
    sudo systemctl stop "$BACKEND_SERVICE" 2>/dev/null || true
    echo "[stop] Done."
    ;;

# ── restart ───────────────────────────────────────────────────────────────────
restart)
    echo "[restart] Restarting services..."
    sudo systemctl restart "$BACKEND_SERVICE"
    sleep 2
    sudo systemctl restart "$BOT_SERVICE"
    echo "[restart] Done."
    ;;

# ── status ────────────────────────────────────────────────────────────────────
status)
    echo "=== Backend ==="
    sudo systemctl status "$BACKEND_SERVICE" --no-pager -l | head -20
    echo ""
    echo "=== Bot ==="
    sudo systemctl status "$BOT_SERVICE" --no-pager -l | head -20
    ;;

# ── logs ──────────────────────────────────────────────────────────────────────
logs)
    echo "=== [backend] ==="
    sudo journalctl -u "$BACKEND_SERVICE" -n 50 --no-pager
    echo ""
    echo "=== [bot] ==="
    sudo journalctl -u "$BOT_SERVICE" -n 80 --no-pager
    ;;

logs-bot)
    sudo journalctl -u "$BOT_SERVICE" -f
    ;;

logs-backend)
    sudo journalctl -u "$BACKEND_SERVICE" -f
    ;;

# ── test-backend ──────────────────────────────────────────────────────────────
test-backend)
    echo "[test-backend] POST /message {giá bitcoin hôm nay}..."
    curl -s -X POST "$BACKEND_URL/message" \
        -H "Content-Type: application/json" \
        -d '{"username":"test","content":"giá bitcoin hôm nay"}' | python3 -m json.tool
    ;;

# ── test-storage ──────────────────────────────────────────────────────────────
test-storage)
    echo "[test-storage] Checking tiktok_storage_state.json..."
    STORAGE="$PROJECT_DIR/tiktok_storage_state.json"
    if [ ! -f "$STORAGE" ]; then
        echo "  ❌ File NOT found: $STORAGE"
        exit 1
    fi
    SIZE=$(wc -c < "$STORAGE")
    echo "  ✅ Found, size: $SIZE bytes"
    python3 -c "
import json, sys
with open('$STORAGE') as f:
    d = json.load(f)
cookies = d.get('cookies', [])
tiktok_cookies = [c for c in cookies if 'tiktok' in c.get('domain','')]
print(f'  Total cookies: {len(cookies)}')
print(f'  TikTok cookies: {len(tiktok_cookies)}')
if tiktok_cookies:
    print('  ✅ TikTok cookies present')
else:
    print('  ⚠️  No TikTok cookies found — may need re-login')
"
    ;;

# ── compile-check ─────────────────────────────────────────────────────────────
compile-check)
    echo "[compile] Checking Python syntax..."
    "$VENV/bin/python" -m py_compile \
        backend/server.py \
        bot/tiktok_bot.py \
        bot/tools.py \
        bot/memory.py \
        bot/reminders.py
    echo "  ✅ All files OK"
    ;;

# ── help ──────────────────────────────────────────────────────────────────────
help|*)
    cat <<EOF
Usage: ./manage.sh <command>

Commands:
  install        Install venv, pip packages, playwright, systemd services
  start          Start backend + bot services
  stop           Stop all services
  restart        Restart all services
  status         Show service status
  logs           Show recent logs (backend + bot)
  logs-bot       Follow bot logs live
  logs-backend   Follow backend logs live
  test-backend   Test backend with BTC price query
  test-storage   Check tiktok_storage_state.json
  compile-check  Python syntax check all files
EOF
    ;;
esac
