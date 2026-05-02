"""
Telegram admin-control bot — long-polling, no external library.
Only responds to TELEGRAM_ADMIN_CHAT_ID.

Commands:
  /status         — systemctl status + last 10 log lines
  /router_status  — 9Router reachability + test pass
  /models         — list LLM model slots from .env
  Any plain text  — forwarded to backend /message (username="tg_admin")

Single process, no duplicate polling guard needed (systemd manages restarts).
"""
import asyncio
import os
import sys
import subprocess
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")
sys.path.insert(0, "/opt/tiktok-bot")

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ADMIN   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
BACKEND    = os.getenv("BACKEND_URL", "http://localhost:8000")
TG_BASE    = f"https://api.telegram.org/bot{TG_TOKEN}"

POLL_TIMEOUT  = 30   # seconds for long-poll
MAX_REPLY_LEN = 4000


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}][tg] {msg}", flush=True)


async def tg_call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{TG_BASE}/{method}", json=payload)
        return r.json()


async def send(chat_id: str | int, text: str) -> None:
    text = text[:MAX_REPLY_LEN]
    await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_status() -> str:
    lines = []
    for svc in ("tiktok-bot", "tiktok-backend"):
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", svc], text=True
            ).strip()
        except subprocess.CalledProcessError as e:
            out = e.output.strip() or "inactive"
        lines.append(f"<b>{svc}</b>: {out}")

    # Last 8 log lines from tiktok-bot
    try:
        journal = subprocess.check_output(
            ["sudo", "journalctl", "-u", "tiktok-bot", "-n", "8",
             "--no-pager", "--output=short"],
            text=True, stderr=subprocess.DEVNULL,
        )
        lines.append("\n<pre>" + journal[-2000:] + "</pre>")
    except Exception as e:
        lines.append(f"(log error: {e})")
    return "\n".join(lines)


async def handle_router_status() -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BACKEND}/router_status")
            d = r.json()
        if d.get("reachable"):
            return (
                f"✅ 9Router reachable\n"
                f"models: {d.get('model_count', '?')}\n"
                f"fast_model: <code>{d.get('fast_model','?')}</code>\n"
                f"test_pass: {d.get('test_pass')}\n"
                f"test_reply: <i>{d.get('test_reply','')[:60]}</i>"
            )
        else:
            return f"❌ 9Router unreachable: {d.get('reason','?')}"
    except Exception as e:
        return f"error: {e}"


async def handle_models() -> str:
    from bot.llm_client import ROLE_MODEL_ENV, ROLE_MODEL_DEFAULT
    lines = ["<b>Configured LLM models</b>"]
    for role, env_key in ROLE_MODEL_ENV.items():
        val = os.getenv(env_key, "").strip() or ROLE_MODEL_DEFAULT.get(role, "?")
        lines.append(f"  <b>{role}</b>: <code>{val}</code>")
    return "\n".join(lines)


async def handle_message(text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(
                f"{BACKEND}/message",
                json={"username": "tg_admin", "content": text},
            )
        if r.status_code == 200:
            d = r.json()
            return d.get("reply") or "(empty reply)"
        return f"backend error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"error: {e}"


# ── Long-poll loop ────────────────────────────────────────────────────────────

async def bot_loop() -> None:
    if not TG_TOKEN:
        log("TELEGRAM_BOT_TOKEN not set — exiting")
        return
    if not TG_ADMIN:
        log("TELEGRAM_ADMIN_CHAT_ID not set — exiting")
        return

    log(f"start admin_chat={TG_ADMIN}")

    # Drain any pending updates on startup (offset=-1 trick)
    offset = 0
    try:
        r = await tg_call("getUpdates", {"offset": -1, "timeout": 1})
        updates = r.get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception:
        pass

    log(f"polling offset={offset}")

    while True:
        try:
            r = await tg_call("getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message"],
            })
        except Exception as e:
            log(f"getUpdates error: {e}")
            await asyncio.sleep(5)
            continue

        updates = r.get("result", [])
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text = (msg.get("text") or "").strip()

            if not text:
                continue

            # Only accept from admin
            if chat_id != str(TG_ADMIN):
                log(f"ignored message from non-admin chat_id={chat_id}")
                continue

            log(f"cmd={text[:80]!r}")

            low = text.lower()
            if low in ("/status", "/status@"):
                reply = await handle_status()
            elif low.startswith("/router_status"):
                reply = await handle_router_status()
            elif low.startswith("/models"):
                reply = await handle_models()
            elif low.startswith("/"):
                reply = "Commands: /status /router_status /models\nOr send any text to chat with the bot."
            else:
                reply = await handle_message(text)

            try:
                await send(chat_id, reply)
            except Exception as e:
                log(f"send error: {e}")


if __name__ == "__main__":
    asyncio.run(bot_loop())
