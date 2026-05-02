#!/usr/bin/env bash
# smoke_test.sh — quick health probe before/after deploy.
#
# Exits 0 if all critical checks pass; non-zero otherwise.
# Deliberately keeps each curl bounded so this finishes in <30s.

set -uo pipefail

REPO=/opt/tiktok-bot
BACKEND=http://localhost:8000
PYTHON="$REPO/venv/bin/python3"

fail=0

ok()    { printf '✅ %s\n' "$1"; }
warn()  { printf '⚠ %s\n'  "$1"; }
bad()   { printf '❌ %s\n'  "$1"; fail=$((fail+1)); }

# 1. Python compile
if "$PYTHON" -m py_compile \
        "$REPO"/bot/telegram_bot.py \
        "$REPO"/bot/business_store.py \
        "$REPO"/bot/code_tasks.py \
        "$REPO"/bot/agent/planner.py \
        "$REPO"/bot/agent/executor.py \
        "$REPO"/bot/agent/risk.py \
        "$REPO"/bot/agent/self_check.py \
        "$REPO"/bot/agent/worker_roles.py \
        "$REPO"/backend/server.py 2>/dev/null; then
    ok "py_compile clean"
else
    bad "py_compile FAILED"
fi

# 2. systemd services
for svc in tiktok-bot tiktok-backend tiktok-telegram; do
    state=$(systemctl is-active "$svc" 2>/dev/null || true)
    if [[ "$state" == "active" ]]; then
        ok "$svc: active"
    else
        bad "$svc: $state"
    fi
done

# 3. No duplicate processes (expect 4: backend, tiktok_bot, playwright node, telegram_bot)
proc_count=$(pgrep -af "tiktok|uvicorn" | grep -v grep | wc -l)
if [[ "$proc_count" -ge 3 && "$proc_count" -le 6 ]]; then
    ok "process count: $proc_count"
else
    warn "unexpected process count: $proc_count"
fi

# 4. backend /health
if curl -fsS --max-time 8 "$BACKEND/health" >/dev/null 2>&1; then
    ok "backend /health"
else
    bad "backend /health FAILED"
fi

# 5. backend /router_status — reachable + role models populated
if rs_json=$(curl -fsS --max-time 12 "$BACKEND/router_status" 2>/dev/null); then
    if echo "$rs_json" | grep -q '"reachable":true'; then
        ok "9Router reachable"
    else
        bad "9Router NOT reachable"
    fi
    if echo "$rs_json" | grep -q 'cx/gpt-5.5'; then
        ok "chat model = cx/gpt-5.5"
    else
        warn "chat model NOT cx/gpt-5.5 (may be ENV override)"
    fi
else
    bad "/router_status FAILED"
fi

# 6. Backend /message — generic chat (uses telegram_chat role)
chat_reply=$(curl -fsS --max-time 25 -X POST "$BACKEND/message" \
    -H 'Content-Type: application/json' \
    -d '{"username":"smoke_test","content":"hello bro","source":"telegram"}' 2>/dev/null || true)
if [[ -n "$chat_reply" ]] && echo "$chat_reply" | grep -q '"reply"'; then
    ok "backend /message normal"
else
    bad "backend /message normal FAILED"
fi

# 7. Backend BTC realtime
btc_reply=$(curl -fsS --max-time 15 -X POST "$BACKEND/message" \
    -H 'Content-Type: application/json' \
    -d '{"username":"smoke_test","content":"giá btc","source":"telegram"}' 2>/dev/null || true)
if echo "$btc_reply" | grep -q "Bitcoin"; then
    ok "backend BTC"
else
    bad "backend BTC FAILED"
fi

# 8. Backend search task (DuckDuckGo + LLM summary)
search_reply=$(curl -fsS --max-time 35 -X POST "$BACKEND/message" \
    -H 'Content-Type: application/json' \
    -d '{"username":"smoke_test","content":"tìm thông tin mới nhất về eSIM Nhật","source":"telegram"}' \
    2>/dev/null || true)
if echo "$search_reply" | grep -q '"reply"'; then
    ok "backend search task"
else
    bad "backend search task FAILED"
fi

echo
if [[ "$fail" -eq 0 ]]; then
    echo "🎉 SMOKE TEST PASSED"
    exit 0
else
    echo "💥 SMOKE TEST FAILED — $fail check(s) failed"
    exit 1
fi
