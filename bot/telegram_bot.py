"""
Telegram admin-control bot — long-polling, no external library.
Only responds to TELEGRAM_ADMIN_CHAT_ID.

Commands:
  /status              — systemctl status + last log lines
  /router_status       — 9Router reachability + test
  /models              — list LLM model slots
  /skills              — list all registered skills
  /skill <name>        — show skill detail
  /tasks               — list recent tasks
  /task <id>           — show task detail
  /run_task <goal>     — create + execute a task
  /cancel_task <id>    — cancel a queued/running task
  /pending_actions     — list actions waiting for confirmation
  /confirm_action <id> — approve a high-risk pending action
  /cancel_action <id>  — reject a pending action
  /tiktok_chat_info    — show current TikTok chat metadata
  Any plain text       — forwarded to backend /message (9Router)
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")
sys.path.insert(0, "/opt/tiktok-bot")

from bot.agent.task_queue import list_tasks, get_task, cancel_task
from bot.agent.skill_registry import list_skills, get_skill
from bot.agent.permissions import list_pending, confirm_pending, cancel_pending
from bot.agent.audit_log import log_action
from bot.agent.runner import run_task

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ADMIN   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
BACKEND    = os.getenv("BACKEND_URL", "http://localhost:8000")
TG_BASE    = f"https://api.telegram.org/bot{TG_TOKEN}"

POLL_TIMEOUT  = 30
MAX_REPLY_LEN = 4000


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][tg] {msg}", flush=True)


async def tg_call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{TG_BASE}/{method}", json=payload)
        return r.json()


async def send(chat_id: str | int, text: str) -> None:
    if not text:
        return
    text = text[:MAX_REPLY_LEN]
    await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_status() -> str:
    lines = []
    for svc in ("tiktok-bot", "tiktok-backend", "tiktok-telegram"):
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", svc], text=True
            ).strip()
        except subprocess.CalledProcessError as e:
            out = (e.output or b"").strip() if isinstance(e.output, bytes) else "inactive"
        lines.append(f"<b>{svc}</b>: {out}")
    try:
        journal = subprocess.check_output(
            ["sudo", "journalctl", "-u", "tiktok-bot", "-n", "8",
             "--no-pager", "--output=short"],
            text=True, stderr=subprocess.DEVNULL,
        )
        lines.append("\n<pre>" + journal[-2000:] + "</pre>")
    except Exception as e:
        lines.append(f"(log: {e})")
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
                f"fast_model: <code>{d.get('fast_model', '?')}</code>\n"
                f"test_pass: {d.get('test_pass')}\n"
                f"test_reply: <i>{d.get('test_reply', '')[:60]}</i>"
            )
        return f"❌ unreachable: {d.get('reason', '?')}"
    except Exception as e:
        return f"error: {e}"


async def handle_models() -> str:
    from bot.llm_client import ROLE_MODEL_ENV, ROLE_MODEL_DEFAULT
    lines = ["<b>Configured LLM models</b>"]
    for role, env_key in ROLE_MODEL_ENV.items():
        val = os.getenv(env_key, "").strip() or ROLE_MODEL_DEFAULT.get(role, "?")
        lines.append(f"  <b>{role}</b>: <code>{val}</code>")
    return "\n".join(lines)


def handle_skills_list() -> str:
    skills = list_skills()
    if not skills:
        return "No skills registered."
    lines = ["<b>Skills</b>"]
    RISK_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    for s in skills:
        icon = RISK_ICON.get(s.risk_level, "⚪")
        status = "✅" if s.enabled else "❌"
        lines.append(f"{status} {icon} <b>{s.name}</b> — {s.description[:60]}")
    lines.append("\nUse /skill <name> for details.")
    return "\n".join(lines)


def handle_skill_detail(name: str) -> str:
    s = get_skill(name.strip())
    if not s:
        return f"Skill <b>{name}</b> not found. Use /skills to list all."
    RISK_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    icon = RISK_ICON.get(s.risk_level, "⚪")
    lines = [
        f"<b>{s.name}</b> {icon}",
        f"Description: {s.description}",
        f"Risk: <b>{s.risk_level}</b>",
        f"Enabled: {'yes' if s.enabled else 'no'}",
        f"Handler: <code>{s.handler}</code>",
    ]
    if s.examples:
        lines.append("Examples: " + " | ".join(s.examples[:3]))
    return "\n".join(lines)


def handle_tasks_list() -> str:
    tasks = list_tasks(limit=10)
    if not tasks:
        return "No tasks yet. Use /run_task <goal> to create one."
    STATUS_ICON = {
        "done": "✅", "failed": "❌", "running": "⏳",
        "queued": "🕐", "cancelled": "🚫", "waiting_confirm": "⏸",
    }
    lines = ["<b>Recent tasks</b>"]
    for t in tasks:
        icon = STATUS_ICON.get(t["status"], "•")
        ts = t["created_at"][11:16]  # HH:MM
        short = t["goal"][:40]
        lines.append(f"{icon} <code>{t['task_id']}</code> [{t['type']}] {short} <i>({ts})</i>")
    lines.append("\nUse /task <id> for details.")
    return "\n".join(lines)


def handle_task_detail(task_id: str) -> str:
    t = get_task(task_id.strip())
    if not t:
        return f"Task <code>{task_id}</code> not found."
    lines = [
        f"<b>Task {t['task_id']}</b>",
        f"Type: {t['type']}",
        f"Status: <b>{t['status']}</b>",
        f"Goal: {t['goal'][:100]}",
        f"Created: {t['created_at']}",
    ]
    if t.get("result_summary"):
        lines.append(f"Result:\n<pre>{t['result_summary'][:400]}</pre>")
    if t.get("error"):
        lines.append(f"Error: {t['error'][:200]}")
    return "\n".join(lines)


def handle_cancel_task(task_id: str) -> str:
    ok = cancel_task(task_id.strip())
    if ok:
        return f"✅ Task <code>{task_id}</code> cancelled."
    return f"❌ Could not cancel task <code>{task_id}</code> (not found or already terminal)."


def handle_pending_list() -> str:
    items = list_pending(only_pending=True)
    if not items:
        return "No pending actions."
    lines = ["<b>Pending actions (high-risk)</b>"]
    for p in items:
        ts = p["created_at"][11:16]
        lines.append(
            f"🔴 <code>{p['action_id']}</code> — <b>{p['action']}</b>\n"
            f"   Goal: {p['goal'][:60]} <i>({ts})</i>"
        )
    lines.append("\nUse /confirm_action <id> or /cancel_action <id>.")
    return "\n".join(lines)


def handle_confirm_action(action_id: str) -> str:
    ok = confirm_pending(action_id.strip())
    if ok:
        log_action(user="tg_admin", action="confirm_action", risk_level="high",
                   status="confirmed", result_summary=f"action_id={action_id}")
        return f"✅ Action <code>{action_id}</code> confirmed."
    return f"❌ Action <code>{action_id}</code> not found or already resolved."


def handle_cancel_action(action_id: str) -> str:
    ok = cancel_pending(action_id.strip())
    if ok:
        log_action(user="tg_admin", action="cancel_action", risk_level="high",
                   status="cancelled", result_summary=f"action_id={action_id}")
        return f"🚫 Action <code>{action_id}</code> cancelled."
    return f"❌ Action <code>{action_id}</code> not found or already resolved."


def handle_tiktok_chat_info() -> str:
    path = Path("/opt/tiktok-bot/data/chat_info.json")
    if not path.exists():
        return "❌ Chưa có thông tin chat — bot chưa khởi động hoặc chưa vào chat."
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"❌ Đọc chat_info lỗi: {e}"
    title    = info.get("chatTitle") or info.get("chat_name") or "?"
    members  = info.get("memberCount", 0)
    visible  = info.get("membersVisible", 0)
    updated  = info.get("updated_at", "?")[:19]
    lines = [
        f"<b>TikTok Chat Info</b>",
        f"Chat: <b>{title}</b>",
        f"Members (DOM count): {members}",
        f"Avatar imgs in header: {visible}",
        f"Updated: <i>{updated}</i>",
    ]
    return "\n".join(lines)


async def handle_run_task(goal: str) -> str:
    if not goal:
        return "Usage: /run_task <goal>\nExample: /run_task tìm thông tin mới nhất về eSIM Nhật"
    log(f"run_task goal={goal[:80]!r}")
    result = await run_task(goal=goal, user="tg_admin")
    task_id = result["task_id"]
    status = result["status"]
    task_result = result.get("result", "")
    icon = "✅" if status == "done" else "❌"
    lines = [
        f"{icon} Task <code>{task_id}</code> [{result['type']}] {status}",
        f"Goal: {goal[:80]}",
    ]
    if task_result:
        lines.append(f"\nResult:\n{task_result[:1000]}")
    return "\n".join(lines)


async def handle_message(text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(
                f"{BACKEND}/message",
                json={"username": "tg_admin", "content": text},
            )
        if r.status_code == 200:
            return r.json().get("reply") or "(empty reply)"
        return f"backend error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"error: {e}"


# ── Route command ─────────────────────────────────────────────────────────────

async def dispatch(text: str) -> str:
    """Route text or command to the correct handler."""
    low = text.lower().strip()
    parts = text.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/status",):
        return await handle_status()
    if cmd in ("/router_status",):
        return await handle_router_status()
    if cmd in ("/models",):
        return await handle_models()
    if cmd in ("/skills",):
        return handle_skills_list()
    if cmd in ("/skill",):
        return handle_skill_detail(arg)
    if cmd in ("/tasks",):
        return handle_tasks_list()
    if cmd in ("/task",):
        return handle_task_detail(arg)
    if cmd in ("/run_task",):
        return await handle_run_task(arg)
    if cmd in ("/cancel_task",):
        return handle_cancel_task(arg)
    if cmd in ("/pending_actions",):
        return handle_pending_list()
    if cmd in ("/confirm_action",):
        return handle_confirm_action(arg)
    if cmd in ("/cancel_action",):
        return handle_cancel_action(arg)
    if cmd in ("/tiktok_chat_info",):
        return handle_tiktok_chat_info()
    if low.startswith("/"):
        return (
            "Commands:\n"
            "/status · /router_status · /models\n"
            "/skills · /skill <name>\n"
            "/tasks · /task <id> · /run_task <goal> · /cancel_task <id>\n"
            "/pending_actions · /confirm_action <id> · /cancel_action <id>\n"
            "/tiktok_chat_info\n"
            "\nOr send plain text to chat with the bot."
        )
    # Plain text → backend
    return await handle_message(text)


# ── Long-poll loop ────────────────────────────────────────────────────────────

async def bot_loop() -> None:
    if not TG_TOKEN:
        log("TELEGRAM_BOT_TOKEN not set — exiting")
        return
    if not TG_ADMIN:
        log("TELEGRAM_ADMIN_CHAT_ID not set — exiting")
        return

    log(f"start admin_chat={TG_ADMIN}")

    # Drain pending updates on startup
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

        for update in r.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text = (msg.get("text") or "").strip()

            if not text:
                continue

            if chat_id != str(TG_ADMIN):
                log(f"ignored non-admin chat_id={chat_id}")
                continue

            log(f"recv: {text[:80]!r}")

            try:
                reply = await dispatch(text)
                await send(chat_id, reply)
                log_action(
                    user="tg_admin",
                    action=text.split()[0][:30] if text.startswith("/") else "chat",
                    risk_level="low",
                    status="ok",
                    result_summary=reply[:100],
                )
            except Exception as e:
                log(f"dispatch error: {e}")
                try:
                    await send(chat_id, f"Error: {e}")
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(bot_loop())
