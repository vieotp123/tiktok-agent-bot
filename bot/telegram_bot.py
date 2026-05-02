"""
Telegram admin-control bot — long-polling, no external library.
Only responds to TELEGRAM_ADMIN_CHAT_ID.

Inline menu: /start | /help | /menu | /cancel
All LLM calls via 9Router → cx/gpt-5.5 for chat, cc/claude-opus-4-7 for coding.
"""
import asyncio
import json
import os
import subprocess
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")
sys.path.insert(0, "/opt/tiktok-bot")

from bot.agent.task_queue import list_tasks, get_task, cancel_task, create_task
from bot.agent.skill_registry import list_skills, get_skill
from bot.agent.permissions import list_pending, confirm_pending, cancel_pending
from bot.agent.audit_log import log_action, format_audit_recent
from bot.agent.runner import run_task
from bot.telegram_files import (
    download_telegram_file, list_files, get_file_record,
    is_safe_send_path, read_text_file,
)
from bot.worker_manager import format_workers_list, touch_worker
from bot.memory_store import (
    search_memory_simple, search_memory, format_search_results,
    list_lessons, format_lessons_list,
    add_memory, delete_memory, compact_memories,
    build_memory_context, list_memories,
)
from bot.code_tasks import (
    init_db as init_code_tasks_db,
    add_task as code_add_task,
    cancel_task as code_cancel_task,
    list_tasks as code_list_tasks,
    next_queued_task as code_next_task,
    format_tasks_list as code_format_list,
    format_task_detail as code_format_detail,
    format_status_summary as code_format_status,
    is_paused as code_is_paused,
    pause as code_pause, resume as code_resume,
)
try:
    init_code_tasks_db()
except Exception as _e:
    print(f"[tg] code_tasks init warning: {_e}", flush=True)

from bot.business_store import (
    init_business_db, seed_products_if_empty,
    list_products, add_product, update_product, get_product,
    verify_product, disable_product,
    detect_esim_intent, build_consult_reply, compute_lead_score,
    upsert_lead, get_lead, get_lead_by_sender, list_leads, add_conversation,
    add_consulting_log, list_consulting_logs,
    add_followup, list_followups,
    format_products_list, format_product_detail,
    format_leads_list, format_lead_detail,
    format_followups_list,
)

# Ensure business DB exists at import time
try:
    init_business_db()
    seed_products_if_empty()
except Exception as _e:
    print(f"[tg] business_store init warning: {_e}", flush=True)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ADMIN   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
BACKEND    = os.getenv("BACKEND_URL", "http://localhost:8000")
TG_BASE    = f"https://api.telegram.org/bot{TG_TOKEN}"

POLL_TIMEOUT  = 30
MAX_REPLY_LEN = 4000

SESSION_FILE     = Path("/opt/tiktok-bot/data/telegram/session_state.json")
MENU_STATE_FILE  = Path("/opt/tiktok-bot/data/telegram/menu_state.json")
SESSION_TTL      = 600   # 10 minutes
MENU_EDIT_TTL    = 86400 # 24h — older menu messages can no longer be edited reliably
SHORT_RESULT_MAX = 3500  # chars — under this, edit menu in place; above, send + park menu

# Task intent patterns for smart routing
_TASK_PATTERNS = [
    re.compile(r'^(tìm|search|kiểm tra|tóm tắt|phân tích|tạo báo cáo|generate)\s+\S', re.I),
    re.compile(r'^(find|check|summarize|analyze|create report|write report)\s+\S', re.I),
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][tg] {msg}", flush=True)


def _esc(text: str) -> str:
    """Escape HTML special characters in user-provided text."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _looks_like_task(text: str) -> bool:
    if len(text) < 25:
        return False
    return any(p.match(text) for p in _TASK_PATTERNS)


# ── Session state (pending input) ─────────────────────────────────────────────

def _session_save(action: str, prompt: str) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "action": action,
        "prompt": prompt,
        "expires_at": (datetime.now(timezone.utc).timestamp() + SESSION_TTL),
    }
    SESSION_FILE.write_text(json.dumps(data), encoding="utf-8")


def _session_get() -> Optional[dict]:
    if not SESSION_FILE.exists():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if datetime.now(timezone.utc).timestamp() > data.get("expires_at", 0):
            SESSION_FILE.unlink(missing_ok=True)
            return None
        return data
    except Exception:
        return None


def _session_clear() -> None:
    SESSION_FILE.unlink(missing_ok=True)


# ── Menu state (active menu message_id per chat) ──────────────────────────────

def _menu_state_save(chat_id: str | int, message_id: int, view: str = "main") -> None:
    """Remember the active menu message_id so we can edit instead of resend."""
    MENU_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(MENU_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data[str(chat_id)] = {
        "message_id": int(message_id),
        "view":       view,
        "ts":         datetime.now(timezone.utc).timestamp(),
    }
    MENU_STATE_FILE.write_text(json.dumps(data), encoding="utf-8")


def _menu_state_get(chat_id: str | int) -> Optional[dict]:
    if not MENU_STATE_FILE.exists():
        return None
    try:
        data = json.loads(MENU_STATE_FILE.read_text(encoding="utf-8"))
        st = data.get(str(chat_id))
        if not st:
            return None
        if datetime.now(timezone.utc).timestamp() - st.get("ts", 0) > MENU_EDIT_TTL:
            return None
        return st
    except Exception:
        return None


def _menu_state_clear(chat_id: str | int) -> None:
    try:
        if not MENU_STATE_FILE.exists():
            return
        data = json.loads(MENU_STATE_FILE.read_text(encoding="utf-8"))
        data.pop(str(chat_id), None)
        MENU_STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


# ── Low-level Telegram API ────────────────────────────────────────────────────

async def tg_call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{TG_BASE}/{method}", json=payload)
        return r.json()


async def tg_call_multipart(method: str, data: dict, files: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{TG_BASE}/{method}", data=data, files=files)
        return r.json()


def _strip_html_tags(text: str) -> str:
    """Naive HTML strip — for fallback when Telegram rejects parse_mode=HTML."""
    import re as _re
    return _re.sub(r"<[^>]+>", "", text)


async def send(chat_id: str | int, text: str,
               reply_markup: Optional[dict] = None) -> Optional[int]:
    """Send message with HTML; on parse failure, fall back to plain text.
    Returns message_id or None.
    """
    if not text:
        return None
    payload: dict = {
        "chat_id":    chat_id,
        "text":       text[:MAX_REPLY_LEN],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = await tg_call("sendMessage", payload)
    if not r.get("ok"):
        desc = (r.get("description") or "?")[:160]
        log(f"send error: {desc} text_preview={text[:40]!r}")
        if "parse" in desc.lower() or "entit" in desc.lower():
            # Retry as plain text — at least the admin sees something
            payload.pop("parse_mode", None)
            payload["text"] = _strip_html_tags(text)[:MAX_REPLY_LEN]
            r = await tg_call("sendMessage", payload)
            if r.get("ok"):
                log(f"send fallback=plain ok message_id="
                    f"{(r.get('result') or {}).get('message_id')}")
    mid = (r.get("result") or {}).get("message_id")
    if reply_markup and mid:
        log(f"menu sent chat_id={chat_id} message_id={mid}")
    return mid


# ── Intent-named wrappers around send/edit_msg ───────────────────────────────
#
# IMPORTANT distinction (this caused the v1.5 "menu drift" bug):
#   send_chat_reply  → ALWAYS a new sendMessage; does NOT touch the menu.
#                       Use for plain admin chat, command results, errors,
#                       pending-input results.
#   edit_menu_panel  → editMessageText on the active menu message.
#                       Use ONLY for menu navigation / button actions.
#
async def send_chat_reply(chat_id: str | int, text: str,
                           reply_markup: Optional[dict] = None
                           ) -> Optional[int]:
    """Always send a NEW chat message. Never edits the menu panel.

    This is the safe default for any reply that came from a normal text
    message (plain admin chat, command results, pending-input results,
    error reports). HTML→plain fallback inherited from send().
    """
    if not text:
        return None
    log(f"reply=send_chat chat_id={chat_id} text_preview={text[:40]!r}")
    return await send(chat_id, text, reply_markup)


async def edit_menu_panel(chat_id: str | int, message_id: int, text: str,
                           reply_markup: Optional[dict] = None) -> bool:
    """Edit the active menu panel in place. ONLY for menu/callback UI.

    Returns True on success.
    """
    log(f"reply=edit_menu chat_id={chat_id} message_id={message_id} "
        f"text_preview={text[:40]!r}")
    return await edit_msg(chat_id, message_id, text, reply_markup)


async def edit_msg(chat_id: str | int, message_id: int, text: str,
                   reply_markup: Optional[dict] = None) -> bool:
    """Edit message with HTML; on parse failure fall back to plain text.
    Returns True on success."""
    payload: dict = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text[:MAX_REPLY_LEN],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = await tg_call("editMessageText", payload)
    if not r.get("ok"):
        desc = (r.get("description") or "")[:160]
        if "not modified" in desc.lower():
            return True
        log(f"menu edit failed reason={desc!r} message_id={message_id}")
        if "parse" in desc.lower() or "entit" in desc.lower():
            payload.pop("parse_mode", None)
            payload["text"] = _strip_html_tags(text)[:MAX_REPLY_LEN]
            r = await tg_call("editMessageText", payload)
            if r.get("ok"):
                log(f"menu edit fallback=plain ok message_id={message_id}")
                return True
        return False
    return True


async def answer_cb(callback_query_id: str, text: str = "") -> None:
    await tg_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200],
    })


async def delete_message(chat_id: str | int, message_id: int) -> bool:
    """Best-effort delete. Returns True if Telegram accepted it."""
    try:
        r = await tg_call("deleteMessage",
                          {"chat_id": chat_id, "message_id": int(message_id)})
        return bool(r.get("ok"))
    except Exception:
        return False


async def is_admin_chat(chat_id: str | int) -> bool:
    """Authorize callback / message: only TELEGRAM_ADMIN_CHAT_ID may interact.
    Compares as strings to avoid int/str mismatch."""
    return str(chat_id) == str(TG_ADMIN)


async def send_document(chat_id: str | int, path: str, caption: str = "") -> bool:
    try:
        abs_path = os.path.realpath(path)
        with open(abs_path, "rb") as f:
            data_bytes = f.read()
        res = await tg_call_multipart(
            "sendDocument",
            {"chat_id": str(chat_id), "caption": caption[:1000]},
            {"document": (os.path.basename(abs_path), data_bytes)},
        )
        return res.get("ok", False)
    except Exception as e:
        log(f"send_document error: {e}")
        return False


async def send_photo(chat_id: str | int, path: str, caption: str = "") -> bool:
    try:
        abs_path = os.path.realpath(path)
        with open(abs_path, "rb") as f:
            data_bytes = f.read()
        res = await tg_call_multipart(
            "sendPhoto",
            {"chat_id": str(chat_id), "caption": caption[:1000]},
            {"photo": (os.path.basename(abs_path), data_bytes)},
        )
        return res.get("ok", False)
    except Exception as e:
        log(f"send_photo error: {e}")
        return False


# ── Inline keyboard builder ───────────────────────────────────────────────────

def make_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """rows = list of button rows; each button = (label, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


BACK_ROW   = [("◀ Back", "menu:main")]
RESULT_KB  = make_keyboard([[("◀ Back to Menu", "menu:main")]])


# ── Public menu-state helpers (per spec) ──────────────────────────────────────

def get_menu_state(chat_id: str | int) -> Optional[dict]:
    """Return {message_id, view, ts} for the active menu message, or None."""
    return _menu_state_get(chat_id)


def save_menu_state(chat_id: str | int, message_id: int,
                    last_menu: str = "main") -> None:
    """Persist the active menu message_id + last menu view."""
    _menu_state_save(chat_id, message_id, view=last_menu)


def result_keyboard(back_to: str = "main") -> dict:
    """Return a single-row [Back to Menu] inline keyboard."""
    target = "menu:main" if back_to == "main" else f"nav:{back_to}"
    return make_keyboard([[("◀ Back to Menu", target)]])


# ── High-level menu rendering (edit-in-place; resend only when needed) ────────

async def rebuild_menu_at_bottom(chat_id: str | int, text: str, keyboard: dict,
                                 menu_name: str = "main") -> int:
    """
    Force-place a fresh menu at the bottom of the chat:
      1. Send a new menu message → it appears at the bottom.
      2. Delete the previously-tracked menu message (if any) so the chat
         doesn't accumulate stale duplicate menus.
      3. Save the new message_id as active.

    Used for /menu, /start, /help — places where the user explicitly wants
    the menu in front of them right now.
    """
    old_state = get_menu_state(chat_id) or {}
    old_mid   = old_state.get("message_id")

    new_id = await send(chat_id, text, keyboard)
    if not new_id:
        log(f"menu mode=send FAILED — could not send fresh menu chat_id={chat_id}")
        return 0
    log(f"menu mode=send message_id={new_id} view={menu_name!r} (rebuild_at_bottom)")
    save_menu_state(chat_id, int(new_id), last_menu=menu_name)

    if old_mid and int(old_mid) != int(new_id):
        ok = await delete_message(chat_id, int(old_mid))
        log(f"menu old_mid={old_mid} deleted={ok}")
    return int(new_id)


async def send_or_edit_menu(chat_id: str | int, text: str, keyboard: dict,
                            menu_name: str = "main",
                            preferred_message_id: Optional[int] = None) -> int:
    """
    Render a menu, preferring to edit an existing menu message in place.

    Tries (in order):
      1. preferred_message_id (e.g. message_id from a callback)
      2. saved active menu_message_id from menu_state.json

    On edit failure (message gone, too old, etc.), sends a fresh message
    and updates state. Always logs mode=edit or mode=send.
    """
    target_id = preferred_message_id or (
        (get_menu_state(chat_id) or {}).get("message_id")
    )
    if target_id:
        ok = await edit_msg(chat_id, int(target_id), text, keyboard)
        if ok:
            log(f"menu mode=edit message_id={target_id} view={menu_name!r}")
            save_menu_state(chat_id, int(target_id), last_menu=menu_name)
            return int(target_id)
        # fall through to send

    new_id = await send(chat_id, text, keyboard)
    if new_id:
        log(f"menu mode=send message_id={new_id} view={menu_name!r}")
        save_menu_state(chat_id, int(new_id), last_menu=menu_name)
        return int(new_id)
    return 0


async def show_action_result(chat_id: str | int, result: str, *,
                              back_to: str = "main",
                              preferred_message_id: Optional[int] = None,
                              view: str = "result") -> None:
    """
    Render a command/action result via the menu message.

    SHORT (≤ SHORT_RESULT_MAX = 3500): edit the active menu message in
    place with the result + Back-to-Menu keyboard. No new message sent.

    LONG (> SHORT_RESULT_MAX): send the long result as one separate
    message, then EDIT the menu message to "Result sent above" + Back/Main
    keyboard. The menu message stays as the navigation anchor at its
    original position; we never spawn a fresh menu just because the result
    was long.
    """
    if not result:
        result = "(no result)"
    text = result.strip()
    back_kb = result_keyboard(back_to=back_to)

    target_id = preferred_message_id or (
        (get_menu_state(chat_id) or {}).get("message_id")
    )

    # ── Short path: edit menu with the result itself ──────────────────────
    if len(text) <= SHORT_RESULT_MAX:
        if target_id:
            ok = await edit_msg(chat_id, int(target_id), text, back_kb)
            if ok:
                log(f"menu mode=edit message_id={target_id} view={view!r} (result short)")
                save_menu_state(chat_id, int(target_id), last_menu=view)
                return
        # No menu to edit → result becomes the new menu anchor
        new_id = await send(chat_id, text, back_kb)
        if new_id:
            log(f"menu mode=send message_id={new_id} view={view!r} (result short)")
            save_menu_state(chat_id, int(new_id), last_menu=view)
        return

    # ── Long path: send result as a separate message, park the menu ───────
    await send(chat_id, text)
    log(f"result long len={len(text)} view={view!r} — sent separate + parking menu")
    parked_text = (
        "📨 <b>Result sent above</b> ⬆\n"
        f"<i>(view: {view})</i>"
    )
    if target_id:
        ok = await edit_msg(chat_id, int(target_id), parked_text, back_kb)
        if ok:
            log(f"menu mode=edit message_id={target_id} view={view!r} (parked)")
            save_menu_state(chat_id, int(target_id), last_menu=view)
            return
    # No menu → leave a small pointer with the back button
    new_id = await send(chat_id, parked_text, back_kb)
    if new_id:
        log(f"menu mode=send message_id={new_id} view={view!r} (parked)")
        save_menu_state(chat_id, int(new_id), last_menu=view)


# ── Backward-compatible wrappers (kept so older call sites still work) ───────

async def show_menu(chat_id: str | int, text: str, kb: dict, *,
                    msg_id: Optional[int] = None, view: str = "") -> int:
    return await send_or_edit_menu(chat_id, text, kb,
                                   menu_name=view or "menu",
                                   preferred_message_id=msg_id)


async def show_result(chat_id: str | int, result: str, *,
                      msg_id: Optional[int] = None,
                      back_kb: dict = RESULT_KB,
                      view: str = "result") -> None:
    # back_kb is ignored; show_action_result builds its own
    await show_action_result(chat_id, result,
                             preferred_message_id=msg_id, view=view)


# Common nav row: 🏠 Main Menu only at root sub-menus; deeper screens get
# both Back + Main. We expose helpers here so all menus look consistent.
HOME_BTN = ("🏠 Main Menu", "menu:main")
BACK_BTN = ("◀ Back",      "menu:main")  # rebuilt below per submenu


def _nav_row(back_to: str = "main") -> list[tuple[str, str]]:
    """Footer row for every sub-menu: Back to parent + 🏠 Main Menu."""
    if back_to == "main":
        return [HOME_BTN]
    return [("◀ Back", f"nav:{back_to}"), HOME_BTN]


def _panel(header_emoji: str, title: str, body: str = "", footer: str = "") -> str:
    """Render a consistent panel header + optional body block."""
    parts = [f"{header_emoji} <b>{title}</b>"]
    if body:
        parts.append(body.strip())
    if footer:
        parts.append(f"<i>{footer}</i>")
    return "\n\n".join(parts)


def menu_main() -> tuple[str, dict]:
    text = _panel(
        "🤖", "Agent Command Center",
        body=(
            "Status: ✅ online\n"
            "Router: <code>cx/gpt-5.5</code>\n"
            "Mode: Telegram Control Plane\n\n"
            "Choose a module:"
        ),
        footer="Type a message to chat with the agent · /help for commands",
    )
    kb = make_keyboard([
        [("📊 Status",   "nav:status"),  ("🤖 Models",     "nav:router")],
        [("🧠 Tasks",    "nav:tasks"),   ("🔎 Search",     "nav:search")],
        [("📁 Files",    "nav:files"),   ("🧩 Skills",     "nav:skills")],
        [("💾 Memory",   "nav:memory"),  ("💼 Sales CRM",  "nav:sales")],
        [("🛠 Code Worker", "nav:code"), ("🤖 Agent",      "nav:agent")],
        [("⚙️ Admin",    "nav:admin"),   ("❓ Help",       "do:help")],
    ])
    return text, kb


def menu_status() -> tuple[str, dict]:
    text = _panel("📊", "Status Center",
                  body="Service health, recent logs, and TikTok DM context.")
    kb = make_keyboard([
        [("🏥 Health",          "do:health"),
         ("📋 Logs",            "do:logs")],
        [("🎮 TikTok Chat",     "do:tiktok_chat_info")],
        _nav_row("main"),
    ])
    return text, kb


def menu_router() -> tuple[str, dict]:
    text = _panel("🤖", "Model Router",
                  body="9Router gateway — chat <code>cx/gpt-5.5</code>, "
                       "coding <code>cc/claude-sonnet-4-6</code>.")
    kb = make_keyboard([
        [("📡 Router Status", "do:router_status"),
         ("🗂 Model Policy",  "do:model_policy")],
        [("📑 Models",        "do:models")],
        _nav_row("main"),
    ])
    return text, kb


def menu_tasks() -> tuple[str, dict]:
    text = _panel("🧠", "Task Center",
                  body="Durable jobs: search, BTC, chat. High-risk needs confirm.")
    kb = make_keyboard([
        [("📋 Task List",       "do:tasks"),
         ("▶ Run Task",         "input:run_task")],
        [("⏳ Pending Actions", "do:pending_actions")],
        _nav_row("main"),
    ])
    return text, kb


def menu_search() -> tuple[str, dict]:
    text = _panel("🔎", "Research",
                  body="Web search via DuckDuckGo, summarised by 9Router.")
    kb = make_keyboard([
        [("🌐 Search Web",     "input:search_web"),
         ("🔬 Deep Research",  "input:deep_research")],
        _nav_row("main"),
    ])
    return text, kb


def menu_files() -> tuple[str, dict]:
    text = _panel("📁", "File Hub",
                  body="Telegram inbox: photos, docs, audio, video, voice.")
    kb = make_keyboard([
        [("📂 Recent Files",   "do:files"),
         ("📤 Send File",      "input:send_file")],
        [("📖 Upload Guide",   "do:upload_guide")],
        _nav_row("main"),
    ])
    return text, kb


def menu_skills() -> tuple[str, dict]:
    text = _panel("🧩", "Skills",
                  body="Risk-tagged capabilities the agent can invoke.")
    kb = make_keyboard([
        [("📋 List Skills",    "do:skills"),
         ("🔍 Skill Detail",   "input:skill_detail")],
        _nav_row("main"),
    ])
    return text, kb


def menu_admin() -> tuple[str, dict]:
    text = _panel("⚙️", "Admin",
                  body="High-risk admin actions. Restart requires confirm.")
    kb = make_keyboard([
        [("📊 Git Status",     "do:git_status"),
         ("💾 Backup",         "do:backup")],
        [("🔄 Restart Bot",    "confirm:restart_bot")],
        _nav_row("main"),
    ])
    return text, kb


def menu_sales() -> tuple[str, dict]:
    text = _panel("💼", "Sales / CRM",
                  body="Japan eSIM catalog · leads · consulting logs.\n"
                       "Only <b>active</b> products may be quoted to customers.")
    kb = make_keyboard([
        [("📦 All Products",   "do:products"),
         ("✅ Active",          "do:products_active")],
        [("⚠ Needs Update",    "do:products_needs_update"),
         ("🚫 Disabled",        "do:products_disabled")],
        [("➕ Add Product",     "input:product_add"),
         ("✏ Update Product",  "input:product_update")],
        [("✅ Verify",          "input:product_verify"),
         ("🚫 Disable",         "input:product_disable")],
        [("💬 Consult",         "input:consult"),
         ("👥 Leads",           "do:leads")],
        [("⏰ Followups",       "do:followups"),
         ("➕ Add Lead",        "input:lead_add")],
        _nav_row("main"),
    ])
    return text, kb


def menu_code() -> tuple[str, dict]:
    text = _panel("🛠", "Code Worker",
                  body="Queue coding tasks for the Claude/Codex worker. "
                       "Low-risk auto-runs after tests; high-risk waits for confirm.")
    kb = make_keyboard([
        [("📋 Code Tasks",    "do:code_tasks"),
         ("➕ New Code Task", "input:code_task")],
        [("▶ Run Once",       "do:code_run_once"),
         ("📡 Worker Status", "do:code_status")],
        [("⏸ Pause",          "do:code_pause"),
         ("▶ Resume",         "do:code_resume")],
        _nav_row("main"),
    ])
    return text, kb


def menu_agent() -> tuple[str, dict]:
    text = _panel("🤖", "Self-Operating Agent",
                  body="Plan/execute goals via the planner-executor loop. "
                       "Low-risk auto-runs; medium/high-risk → pending_action.")
    kb = make_keyboard([
        [("🩺 Agent Health",   "do:agent_health"),
         ("📜 Policy",         "do:agent_policy")],
        [("👥 Workers",        "do:agent_workers"),
         ("➡ Next Mission",    "do:agent_next")],
        [("🧭 Plan Goal",      "input:agent_plan"),
         ("▶ Run Goal",        "input:agent_run")],
        _nav_row("main"),
    ])
    return text, kb


def menu_memory() -> tuple[str, dict]:
    text = _panel("💾", "Memory",
                  body="Persistent multi-tier memory. "
                       "Hard prompt cap: 8 items · 6000 chars.")
    kb = make_keyboard([
        [("🔍 Search Memory",  "input:memory_search"),
         ("➕ Add Memory",     "input:memory_add")],
        [("📚 Lessons",        "do:memory_lessons"),
         ("🔎 Context Preview","input:memory_context")],
        _nav_row("main"),
    ])
    return text, kb


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


async def handle_health() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BACKEND}/health")
            d = r.json()
        return (
            f"✅ Backend: <b>{d.get('status', '?')}</b>\n"
            f"Time: <code>{d.get('time', '?')[:19]}</code>"
        )
    except Exception as e:
        return f"❌ Backend unreachable: {e}"


async def handle_router_status() -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{BACKEND}/router_status")
            d = r.json()
        if not d.get("reachable"):
            return f"❌ 9Router unreachable: {d.get('reason', '?')}"

        role_models = d.get("role_models", {})
        chat  = role_models.get("chat", d.get("chat_model", "?"))
        lines = [
            "✅ <b>9Router reachable</b>",
            f"Models available: {d.get('model_count', '?')}",
            "",
            "<b>Active model policy (live-resolved):</b>",
            f"  chat/fast       : <code>{chat}</code>",
            f"  tiktok_chat     : <code>{role_models.get('tiktok_chat', '?')}</code>",
            f"  telegram_chat   : <code>{role_models.get('telegram_chat', '?')}</code>",
            f"  search_summary  : <code>{role_models.get('search_summary', '?')}</code>",
            f"  reasoning       : <code>{role_models.get('reasoning', '?')}</code>",
            f"  coding          : <code>{role_models.get('coding', '?')}</code>",
            f"  critic          : <code>{role_models.get('critic', '?')}</code>",
            f"  cheap/fallback  : <code>{role_models.get('cheap', 'openai/gpt-4o-mini')}</code>",
            "",
            f"Test ({chat}): "
            f"{'✅ ' + d.get('test_reply', '')[:40] if d.get('test_pass') else '❌ failed'}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


async def handle_model_policy() -> str:
    """Show current live-resolved model for each role."""
    try:
        from bot.llm_client import get_role_models, ROLE_MODEL_DEFAULT
        role_models = await get_role_models()
        lines = ["<b>Model Policy (live-resolved)</b>"]
        for role in ("chat", "fast", "tiktok_chat", "telegram_chat", "search_summary",
                     "reasoning", "coding", "critic", "vision", "cheap", "fallback"):
            model   = role_models.get(role, "?")
            default = ROLE_MODEL_DEFAULT.get(role, "?")
            tag     = "" if model == default else " ⚠️"
            lines.append(f"  <b>{role}</b>: <code>{model}</code>{tag}")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


async def handle_models() -> str:
    try:
        from bot.llm_client import (
            get_role_models, list_models_relevant, ROLE_MODEL_DEFAULT,
        )
        role_models = await get_role_models()
        groups      = await list_models_relevant()

        # Active role assignments at the top
        lines = ["<b>🎯 Active role assignments</b>"]
        for role in ("chat", "tiktok_chat", "telegram_chat", "search_summary",
                     "reasoning", "coding", "critic", "cheap"):
            m = role_models.get(role, "?")
            lines.append(f"  <b>{role}</b>: <code>{m}</code>")

        # Relevant available models grouped
        lines.append("")
        lines.append("<b>📋 Available models (9Router)</b>")
        group_labels = [
            ("claude", "🧠 Claude/Sonnet/Opus"),
            ("codex",  "💻 Codex"),
            ("gpt5",   "⚡ GPT-5"),
            ("reasoning", "🔭 Reasoning (o3/o4)"),
            ("gpt4",   "📦 GPT-4"),
        ]
        for key, label in group_labels:
            items = groups.get(key, [])
            if items:
                lines.append(f"<i>{label} ({len(items)})</i>")
                for m in items[:6]:  # cap at 6 per group
                    lines.append(f"  <code>{m}</code>")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


def handle_skills_list() -> str:
    skills = list_skills()
    if not skills:
        return "No skills registered."
    RISK = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    lines = ["<b>Skills</b>"]
    for s in skills:
        name = _esc(s.name)
        desc = _esc(s.description[:60])
        lines.append(f"{'✅' if s.enabled else '❌'} {RISK.get(s.risk_level,'⚪')} "
                     f"<b>{name}</b> — {desc}")
    lines.append("\nUse /skill &lt;name&gt; for details.")
    return "\n".join(lines)


def handle_skill_detail(name: str) -> str:
    s = get_skill(name.strip())
    if not s:
        return f"Skill <b>{_esc(name)}</b> not found."
    RISK = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    parts = [
        f"<b>{_esc(s.name)}</b> {RISK.get(s.risk_level,'⚪')}",
        f"Description: {_esc(s.description)}",
        f"Risk: <b>{_esc(s.risk_level)}</b>",
        f"Enabled: {'yes' if s.enabled else 'no'}",
        f"Handler: <code>{_esc(s.handler)}</code>",
    ]
    if s.examples:
        parts.append("Examples: " + " | ".join(_esc(e) for e in s.examples[:3]))
    return "\n".join(parts)


def handle_tasks_list() -> str:
    tasks = list_tasks(limit=10)
    if not tasks:
        return "No tasks yet."
    ICON = {"done":"✅","failed":"❌","running":"⏳","queued":"🕐",
            "cancelled":"🚫","waiting_confirm":"⏸"}
    lines = ["<b>Recent tasks</b>"]
    for t in tasks:
        in_f = json.loads(t.get("input_files") or "[]")
        ftag = f" 📎{len(in_f)}" if in_f else ""
        goal_safe = _esc(t.get("goal", "")[:40])
        type_safe = _esc(t.get("type", ""))
        lines.append(
            f"{ICON.get(t['status'],'•')} <code>{t['task_id']}</code> "
            f"[{type_safe}]{ftag} {goal_safe} <i>({t['created_at'][11:16]})</i>"
        )
    lines.append("\nUse /task &lt;id&gt; for details.")
    return "\n".join(lines)


def handle_task_detail(task_id: str) -> str:
    t = get_task(task_id.strip())
    if not t:
        return f"Task <code>{_esc(task_id)}</code> not found."
    in_f  = json.loads(t.get("input_files")  or "[]")
    out_f = json.loads(t.get("output_files") or "[]")
    lines = [
        f"<b>Task {t['task_id']}</b>",
        f"Type: {_esc(t['type'])} | Status: <b>{t['status']}</b>",
        f"Goal: {_esc(t['goal'][:100])}",
        f"Created: {t['created_at']}",
    ]
    if in_f:
        lines.append(f"Input files: {', '.join(_esc(str(f)) for f in in_f)}")
    if out_f:
        lines.append(f"Output files: {', '.join(_esc(str(f)) for f in out_f)}")
    if t.get("result_summary"):
        lines.append(f"Result:\n<pre>{_esc(t['result_summary'][:400])}</pre>")
    if t.get("error"):
        lines.append(f"Error: {_esc(t['error'][:200])}")
    return "\n".join(lines)


def handle_cancel_task(task_id: str) -> str:
    ok = cancel_task(task_id.strip())
    return (f"✅ Task <code>{task_id}</code> cancelled."
            if ok else f"❌ Cannot cancel <code>{task_id}</code>.")


def handle_pending_list() -> str:
    items = list_pending(only_pending=True)
    if not items:
        return "No pending actions."
    lines = ["<b>Pending actions</b>"]
    for p in items:
        lines.append(
            f"🔴 <code>{p['action_id']}</code> — <b>{p['action']}</b>\n"
            f"   {p['goal'][:60]} <i>({p['created_at'][11:16]})</i>"
        )
    lines.append("\n/confirm_action <id>  |  /cancel_action <id>")
    return "\n".join(lines)


def handle_confirm_action(action_id: str) -> str:
    ok = confirm_pending(action_id.strip())
    if ok:
        log_action(user="tg_admin", action="confirm_action", risk_level="high",
                   status="confirmed", result_summary=f"action_id={action_id}")
        return f"✅ Action <code>{action_id}</code> confirmed."
    return f"❌ Action <code>{action_id}</code> not found."


def handle_cancel_action(action_id: str) -> str:
    ok = cancel_pending(action_id.strip())
    if ok:
        log_action(user="tg_admin", action="cancel_action", risk_level="high",
                   status="cancelled", result_summary=f"action_id={action_id}")
        return f"🚫 Action <code>{action_id}</code> cancelled."
    return f"❌ Action <code>{action_id}</code> not found."


def handle_files_list() -> str:
    files = list_files(10)
    if not files:
        return "Không có file nào. Gửi file/ảnh cho bot để lưu vào inbox."
    ICON = {"photo":"🖼","document":"📄","audio":"🎵","video":"🎬","voice":"🎤"}
    lines = ["<b>Files (inbox)</b>"]
    for f in files:
        kb = f.get("size", 0) // 1024
        cap = f" — {f['caption'][:30]!r}" if f.get("caption") else ""
        lines.append(
            f"{ICON.get(f.get('type',''),'📎')} "
            f"<code>{f['file_id']}</code> <b>{f['filename'][:30]}</b> ({kb}KB){cap}"
        )
    lines.append("\n/file <id> for details.")
    return "\n".join(lines)


def handle_file_detail(file_id: str) -> str:
    r = get_file_record(file_id.strip())
    if not r:
        return f"❌ File <code>{file_id}</code> not found."
    return "\n".join([
        f"<b>File {r['file_id']}</b>",
        f"Type: {r.get('type','?')} | MIME: {r.get('mime_type','?')}",
        f"Filename: <code>{r['filename']}</code>",
        f"Path: <code>{r['local_path']}</code>",
        f"Size: {r.get('size',0):,} bytes",
        f"Caption: {r.get('caption','') or '(none)'}",
        f"Created: {r.get('created_at','?')}",
    ])


async def handle_send_file(path: str, chat_id: str | int) -> str:
    path = path.strip()
    ok, reason = is_safe_send_path(path)
    if not ok:
        return reason
    ext = Path(path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        sent = await send_photo(chat_id, path, caption=f"📤 {os.path.basename(path)}")
    else:
        sent = await send_document(chat_id, path, caption=f"📤 {os.path.basename(path)}")
    return f"✅ Sent: <code>{path}</code>" if sent else f"❌ Failed to send: <code>{path}</code>"


def handle_upload_guide() -> str:
    return (
        "<b>📤 File Upload Guide</b>\n\n"
        "Send any file directly to this chat:\n"
        "• <b>photo</b> — image (jpg/png/webp)\n"
        "• <b>document</b> — any file type\n"
        "• <b>audio</b> — mp3/ogg/m4a\n"
        "• <b>video</b> — mp4\n"
        "• <b>voice</b> — voice message\n\n"
        "<b>Caption commands:</b>\n"
        "• <i>'tóm tắt'</i> → auto-summarise text files\n"
        "• <i>'ocr'</i> → queued (not yet supported)\n"
        "• (no caption) → store only, reply with file_id\n\n"
        "Use /files to list stored files.\n"
        "Use /send_file &lt;path&gt; to send a file back."
    )


async def handle_logs() -> str:
    try:
        out = subprocess.check_output(
            ["sudo", "journalctl", "-u", "tiktok-bot", "-n", "20",
             "--no-pager", "--output=short"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return "<pre>" + out[-3000:] + "</pre>"
    except Exception as e:
        return f"❌ journalctl error: {e}"


def handle_tiktok_chat_info() -> str:
    p = Path("/opt/tiktok-bot/data/chat_info.json")
    if not p.exists():
        return "❌ No chat_info yet — bot not started or not in chat."
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return f"❌ Read error: {e}"
    return "\n".join([
        "<b>TikTok Chat Info</b>",
        f"Chat: <b>{info.get('chatTitle') or info.get('chat_name','?')}</b>",
        f"Members (DOM): {info.get('memberCount',0)}",
        f"Avatar imgs in header: {info.get('membersVisible',0)}",
        f"Updated: <i>{info.get('updated_at','?')[:19]}</i>",
    ])


def handle_agent_blueprint() -> str:
    """Return a concise architecture summary from AGENT_BLUEPRINT.md."""
    bp = Path("/opt/tiktok-bot/docs/AGENT_BLUEPRINT.md")
    if not bp.exists():
        return "❌ Blueprint not found."
    # Extract the first ~40 lines (overview + brain table)
    lines = bp.read_text(encoding="utf-8").splitlines()
    # Skip markdown headers, show first meaningful sections
    out = [
        "<b>Agent Platform Blueprint</b>",
        "",
        "<b>Brain components:</b>",
        "• LLM Gateway (9Router → cx/gpt-5.5 / cc/claude-opus-4-7)",
        "• Task Queue (SQLite, durable task state)",
        "• Skill Registry (low/medium/high risk)",
        "• Worker Manager (channels + future agents)",
        "• Memory Store (raw_events / memories / lessons / skill_notes)",
        "• Permissions (pending_actions, confirm_action flow)",
        "• Audit Log (append-only JSONL)",
        "",
        "<b>Channels:</b> Telegram (cmd center) | TikTok (social worker)",
        "<b>Future:</b> Browser | OCR | Image-gen | Coding worker",
        "",
        "<b>Risk levels:</b> low → run | medium → log | high → confirm",
        "",
        "Full doc: /opt/tiktok-bot/docs/AGENT_BLUEPRINT.md",
    ]
    return "\n".join(out)


def handle_workers() -> str:
    """Return formatted worker list."""
    touch_worker("telegram_bot")   # update our own last_seen
    return format_workers_list()


def handle_lessons(arg: str = "") -> str:
    """Return episodic lessons list, optionally filtered by skill name."""
    skill = arg.strip() or None
    return format_lessons_list(skill=skill, limit=10)


def handle_audit_recent() -> str:
    """Return last 10 audit log entries."""
    return format_audit_recent(n=10)


def handle_help_panel() -> str:
    """Quick command reference shown via the ❓ Help button."""
    return (
        "<b>❓ Help</b>\n\n"
        "<b>Navigation:</b> tap any module button. The control panel edits "
        "in place. Use 🏠 Main Menu to jump back.\n\n"
        "<b>Plain text</b> sent to this chat is treated as a message to the "
        "agent (chat / search / BTC / sales consult routed automatically).\n\n"
        "<b>Common commands:</b>\n"
        "/menu — refresh control panel at the bottom of chat\n"
        "/cancel — clear pending input + return to main menu\n"
        "/status /health /router_status /tasks /skills\n"
        "/products /consult &lt;q&gt; /leads /lead &lt;id&gt;\n"
        "/memory_search &lt;q&gt; /lessons /audit_recent\n\n"
        "Long results land as a separate message; the panel stays put."
    )


async def handle_memory_search(query: str) -> str:
    """Search semantic memory for the admin namespace."""
    if not query.strip():
        return "Usage: /memory_search &lt;query&gt;"
    results = search_memory(query.strip(), namespace="tg_admin", limit=5)
    # Also search global
    global_results = search_memory(query.strip(), namespace="global", limit=3)
    seen = {r["id"] for r in results}
    for r in global_results:
        if r["id"] not in seen:
            results.append(r)
    return format_search_results(results, query.strip())


async def handle_memory_add(text: str) -> str:
    """
    Add a memory from Telegram.
    Format: Title | content | tag1,tag2
    or just:  content
    """
    text = text.strip()
    if not text:
        return "Usage: /memory_add &lt;title&gt; | &lt;content&gt; [| tag1,tag2]"
    parts = [p.strip() for p in text.split("|")]
    if len(parts) >= 3:
        title, content, tag_str = parts[0], parts[1], parts[2]
        tags = [t.strip() for t in tag_str.split(",") if t.strip()]
    elif len(parts) == 2:
        title, content = parts[0], parts[1]
        tags = []
    else:
        title, content, tags = "", parts[0], []

    if not content:
        return "❌ Content cannot be empty."

    mid = add_memory(
        title=title or content[:60],
        content=content,
        namespace="tg_admin",
        tags=tags,
        importance=5,
    )
    log_action(user="tg_admin", action="memory_add", risk_level="low",
               status="ok", result_summary=f"id={mid} title={title[:40]!r}")
    return (
        f"✅ Memory <code>{mid}</code> saved.\n"
        f"Title: <b>{_esc(title or content[:60])}</b>\n"
        + (f"Tags: {', '.join(tags)}" if tags else "")
    )


def handle_memory_forget(arg: str) -> str:
    """Delete a memory by ID."""
    arg = arg.strip()
    if not arg.isdigit():
        return "Usage: /memory_forget &lt;id&gt;  (get id from /memory_search)"
    mid = int(arg)
    ok = delete_memory(mid)
    if ok:
        log_action(user="tg_admin", action="memory_forget", risk_level="low",
                   status="ok", result_summary=f"deleted id={mid}")
        return f"🗑 Memory <code>{mid}</code> deleted."
    return f"❌ Memory <code>{mid}</code> not found."


async def handle_memory_compact() -> str:
    """Compact memories: keep top-50 by importance, delete the rest."""
    deleted = compact_memories(namespace="tg_admin", keep_top=50)
    deleted += compact_memories(namespace="global", keep_top=100)
    log_action(user="tg_admin", action="memory_compact", risk_level="low",
               status="ok", result_summary=f"deleted={deleted}")
    return f"🗜 Memory compacted. Removed {deleted} low-priority entries."


def handle_memory_context(query: str) -> str:
    """Preview the memory context that would be injected for a given query."""
    query = query.strip()
    if not query:
        return "Usage: /memory_context &lt;goal/query&gt;"
    ctx = build_memory_context(query, namespace="tg_admin")
    if not ctx:
        ctx = build_memory_context(query, namespace="global")
    if not ctx:
        return f"No memory context found for: <b>{_esc(query)}</b>"
    return f"<b>Memory context for:</b> {_esc(query)}\n\n<pre>{_esc(ctx[:3000])}</pre>"


# ── Sales / CRM handlers ──────────────────────────────────────────────────────

_PRODUCT_STATUS_ALIAS = {
    "active": "active", "verified": "active", "ok": "active",
    "needs_update": "needs_update", "needs": "needs_update",
    "pending": "needs_update", "unverified": "needs_update",
    "disabled": "disabled", "off": "disabled",
}


def handle_products(status_filter: str | None = None) -> str:
    """List products. status_filter ∈ {active, needs_update, disabled, None}."""
    s = None
    if status_filter:
        s = _PRODUCT_STATUS_ALIAS.get(status_filter.lower().strip())
        if s is None and status_filter.strip():
            return (f"Unknown status filter: <code>{_esc(status_filter)}</code>. "
                    "Try: active | needs_update | disabled.")
    return format_products_list(status=s)


def handle_product_detail(product_id: str) -> str:
    pid = product_id.strip()
    if not pid:
        return "Usage: /product &lt;product_id&gt;"
    return format_product_detail(pid)


def handle_product_verify(arg: str) -> str:
    pid = arg.strip()
    if not pid:
        return "Usage: /product_verify &lt;product_id&gt;"
    if not get_product(pid):
        return f"❌ Product <code>{_esc(pid)}</code> không tồn tại."
    if verify_product(pid):
        log_action(user="tg_admin", action="product_verify", risk_level="medium",
                   status="ok", result_summary=f"id={pid} -> active")
        return f"✅ <code>{pid}</code> đã được verify (status=active). Có thể quote khách."
    return f"❌ Verify failed for <code>{_esc(pid)}</code>."


def handle_product_disable(arg: str) -> str:
    pid = arg.strip()
    if not pid:
        return "Usage: /product_disable &lt;product_id&gt;"
    if not get_product(pid):
        return f"❌ Product <code>{_esc(pid)}</code> không tồn tại."
    if disable_product(pid):
        log_action(user="tg_admin", action="product_disable", risk_level="medium",
                   status="ok", result_summary=f"id={pid} -> disabled")
        return f"🚫 <code>{pid}</code> disabled — sẽ không suggest khách."
    return f"❌ Disable failed for <code>{_esc(pid)}</code>."


def handle_lead_by_sender(arg: str) -> str:
    """Lookup lead by sender_key (any platform). Format: [platform:]sender_key"""
    arg = arg.strip()
    if not arg:
        return "Usage: /lead_by_sender [platform:]&lt;sender_key&gt;"
    if ":" in arg:
        platform, sk = arg.split(":", 1)
        l = get_lead_by_sender(platform.strip(), sk.strip())
    else:
        # Try common platforms in order
        l = (get_lead_by_sender("tiktok", arg)
             or get_lead_by_sender("telegram", arg)
             or get_lead(arg))
    if not l:
        return f"Không tìm thấy lead cho <code>{_esc(arg)}</code>."
    return format_lead_detail(l["id"])


def handle_consulting_logs(arg: str = "") -> str:
    sk = arg.strip() or None
    logs = list_consulting_logs(sender_key=sk, limit=10)
    if not logs:
        target = f" cho sender_key={sk}" if sk else ""
        return f"Không có consulting log nào{target}."
    lines = [f"<b>Consulting logs</b>" + (f" — {sk}" if sk else "") + f" ({len(logs)})"]
    for l in logs:
        bar = "💎" * min(int(l.get("confidence", 0) * 4), 4) or "·"
        lines.append(
            f"{bar} [{l.get('platform', '?')}/{(l.get('sender_key') or '?')[:20]}]\n"
            f"   Q: {l.get('user_message', '')[:80]}\n"
            f"   A: {l.get('bot_reply', '')[:80]}\n"
            f"   <i>{l.get('created_at', '')[:16]}</i>"
        )
    return "\n\n".join(lines)


def handle_followup_add(spec: str) -> str:
    """Format: lead_id | remind_at_iso | note"""
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) < 2:
        return "Usage: /followup_add &lt;lead_id&gt; | &lt;remind_at_iso&gt; | [note]"
    pad = parts + [""] * (3 - len(parts))
    lead_id, remind_at, note = pad[:3]
    if not get_lead(lead_id):
        return f"❌ Lead <code>{_esc(lead_id)}</code> không tồn tại."
    fid = add_followup(lead_id=lead_id, remind_at=remind_at, note=note)
    log_action(user="tg_admin", action="followup_add", risk_level="low",
               status="ok", result_summary=f"id={fid} lead={lead_id}")
    return f"✅ Followup <code>{fid}</code> created for lead <code>{lead_id}</code>."


def handle_product_add(spec: str) -> str:
    """
    Parse: name | network | country | duration_days | data_amount | sms | hotspot | renew | notes
    """
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) < 2:
        return "Usage: name | network | country | duration_days | data_amount | sms(0/1) | hotspot(0/1) | renew(0/1) | notes"
    pad = parts + [""] * (9 - len(parts))
    name, network, country, dur, data_amt, sms, hot, renew, notes = pad[:9]
    try:
        pid = add_product(
            name=name, network=network, country=country or "JP",
            duration_days=int(dur or 0), data_amount=data_amt,
            supports_sms=bool(int(sms or 0)),
            supports_hotspot=bool(int(hot or 1)),
            renewable=bool(int(renew or 0)),
            notes=notes, status="needs_update",
        )
    except Exception as e:
        return f"❌ Add failed: {_esc(str(e))}"
    log_action(user="tg_admin", action="product_add", risk_level="low",
               status="ok", result_summary=f"id={pid} name={name[:40]!r}")
    return (f"✅ Product <code>{pid}</code> added (status=<b>needs_update</b> — "
            f"verify before customer use).")


def handle_product_update(spec: str) -> str:
    """
    Parse: <product_id> | field=value [| field=value ...]
    """
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) < 2:
        return "Usage: <product_id> | field=value | field=value ..."
    pid = parts[0]
    if not get_product(pid):
        return f"❌ Product <code>{_esc(pid)}</code> not found."
    fields: dict = {}
    for kv in parts[1:]:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in ("duration_days", "price_jpy", "price_vnd"):
            try: fields[k] = int(v)
            except ValueError: continue
        elif k in ("supports_sms", "supports_hotspot", "renewable"):
            fields[k] = v in ("1", "true", "yes", "True")
        else:
            fields[k] = v
    if not fields:
        return "❌ No valid field=value pairs."
    ok = update_product(pid, **fields)
    if ok:
        log_action(user="tg_admin", action="product_update", risk_level="low",
                   status="ok", result_summary=f"id={pid} fields={list(fields.keys())}")
        return f"✅ Updated <code>{pid}</code>: {', '.join(fields.keys())}"
    return f"❌ Update failed for <code>{pid}</code>."


async def handle_consult(text: str) -> str:
    """Run sales consult for an admin-provided customer question.
    Uses admin tone (direct, with warnings, no mày/tao softening)."""
    text = text.strip()
    if not text:
        return "Usage: /consult &lt;customer message&gt;"
    prefix = "" if detect_esim_intent(text) else "⚠️ Không detect intent eSIM rõ ràng, vẫn thử lookup:\n\n"
    reply, ids, confidence = build_consult_reply(text, audience="admin")
    score = compute_lead_score(text)

    # Log as Telegram-admin consult
    add_consulting_log(
        platform="telegram", sender_key="telegram_admin",
        sender_name="tg_admin", user_message=text, bot_reply=reply,
        products_used=ids, confidence=confidence,
    )
    try:
        from bot.memory_store import add_raw_event
        add_raw_event(
            source="telegram", action="sales_consult",
            summary=f"admin consult q={text[:50]!r} conf={confidence:.2f} score={score}",
            actor="telegram_admin", event_type="consulting",
            tags=["sales", "esim", "admin"],
        )
    except Exception:
        pass
    log_action(user="tg_admin", action="consult", risk_level="low",
               status="ok", result_summary=f"conf={confidence:.2f} score={score} products={len(ids)}",
               goal=text[:120])
    return (
        f"{prefix}<b>Consult result</b> "
        f"(confidence={confidence:.2f} · lead_score={score})\n"
        f"Products: {', '.join(ids) if ids else '(none)'}\n\n"
        f"<b>Reply (admin tone):</b>\n{_esc(reply)}"
    )


def handle_leads() -> str:
    return format_leads_list()


def handle_lead_detail(lead_id: str) -> str:
    return format_lead_detail(lead_id.strip())


def handle_lead_add(spec: str) -> str:
    """Parse: platform | sender_key | name | need_summary"""
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) < 2:
        return "Usage: platform | sender_key | name | need_summary"
    pad = parts + [""] * (4 - len(parts))
    platform, sk, name, need = pad[:4]
    if not platform or not sk:
        return "❌ platform and sender_key are required."
    lid = upsert_lead(
        platform=platform, sender_key=sk,
        username=name, display_name=name,
        need_summary=need, source="manual_admin",
    )
    log_action(user="tg_admin", action="lead_add", risk_level="low",
               status="ok", result_summary=f"id={lid}")
    return f"✅ Lead <code>{lid}</code> upserted ({platform}:{_esc(sk)})."


def handle_followups() -> str:
    return format_followups_list()


# ── Code Worker handlers ──────────────────────────────────────────────────────

def handle_code_task_add(spec: str) -> str:
    """Parse: title | description | risk | priority"""
    spec = spec.strip()
    if not spec:
        return ("Usage: /code_task &lt;title&gt; | [description] | "
                "[risk=low|medium|high] | [priority 1-9]")
    parts = [p.strip() for p in spec.split("|")]
    pad = parts + [""] * (4 - len(parts))
    title, desc, risk, prio = pad[:4]
    risk = (risk or "low").lower()
    if risk not in ("low", "medium", "high"):
        risk = "low"
    try:
        priority = int(prio) if prio else 5
    except ValueError:
        priority = 5
    if not title:
        return "❌ Title required."
    tid = code_add_task(title=title, description=desc,
                        risk_level=risk, priority=priority,
                        created_by="tg_admin")
    log_action(user="tg_admin", action="code_task_add", risk_level="low",
               status="ok", result_summary=f"id={tid} risk={risk}")
    return (f"✅ Code task <code>{tid}</code> queued\n"
            f"<b>{_esc(title)}</b>\n"
            f"risk={risk} priority={priority}\n"
            f"<i>Worker picks up next pass; high-risk waits for confirm.</i>")


async def handle_code_run_once() -> str:
    """Surface the next queued task for the worker. The actual coding work
    is performed by a separate Claude/Codex CLI session — see
    docs/CLAUDE_CODE_WORKER.md. This command shows what the worker would
    pick up next, without running the model in the Telegram process."""
    if code_is_paused():
        return "⏸ Code worker is paused. /code_worker_resume to enable."
    nxt = code_next_task()
    if not nxt:
        return "📭 No queued code tasks."
    return ("<b>🛠 Next code task</b>\n"
            f"<code>{nxt['id']}</code> — {_esc(nxt['title'])}\n"
            f"risk={nxt['risk_level']} priority={nxt['priority']}\n\n"
            "<i>To run: open a Claude/Codex CLI session in /opt/tiktok-bot\n"
            "and read docs/CLAUDE_CODE_WORKER.md.</i>")


# ── Self-operating agent handlers ─────────────────────────────────────────────

async def handle_agent_plan(goal: str) -> str:
    goal = goal.strip()
    if not goal:
        return "Usage: /agent_plan &lt;goal&gt;"
    from bot.agent.planner import plan_goal
    plan = plan_goal(goal)
    return _format_plan(goal, plan)


async def handle_agent_run(goal: str) -> str:
    goal = goal.strip()
    if not goal:
        return "Usage: /agent_run &lt;goal&gt;"
    from bot.agent.executor import execute_goal
    result = await execute_goal(goal, user="tg_admin")
    return _format_run_result(goal, result)


def _format_plan(goal: str, plan: dict) -> str:
    lines = [f"<b>🧭 Plan</b> — <i>{_esc(goal[:80])}</i>",
             f"Risk: <b>{plan.get('risk', '?')}</b>"]
    for i, step in enumerate(plan.get("steps", []), 1):
        lines.append(f"{i}. {_esc(step.get('action', ''))} "
                     f"<i>(risk={step.get('risk', 'low')})</i>")
        if step.get("note"):
            lines.append(f"   <i>{_esc(step['note'])}</i>")
    if plan.get("rationale"):
        lines.append(f"\n<i>{_esc(plan['rationale'][:200])}</i>")
    return "\n".join(lines)


def _format_run_result(goal: str, result: dict) -> str:
    icon = {"done":"✅", "pending_action":"⏸", "rejected":"🚫",
            "failed":"❌", "partial":"⚠"}.get(result.get("status",""), "•")
    lines = [f"{icon} <b>Agent run</b> — {_esc(goal[:80])}",
             f"Status: <b>{result.get('status','?')}</b>"]
    if result.get("risk"):
        lines.append(f"Risk: {result['risk']}")
    if result.get("pending_action_id"):
        lines.append(f"Pending action: <code>{result['pending_action_id']}</code>")
        lines.append(f"<i>Use /confirm_action {result['pending_action_id']} to approve.</i>")
    if result.get("output"):
        lines.append(f"\n{_esc(str(result['output'])[:1500])}")
    if result.get("error"):
        lines.append(f"\n<i>Error: {_esc(result['error'][:200])}</i>")
    return "\n".join(lines)


async def handle_agent_health() -> str:
    """Quick agent platform health check."""
    from bot.agent.self_check import run_self_check
    return await run_self_check()


def handle_agent_policy() -> str:
    from bot.agent.risk import POLICY_SUMMARY
    return POLICY_SUMMARY


def handle_agent_workers() -> str:
    from bot.agent.worker_roles import format_worker_roles
    return format_worker_roles()


def handle_agent_next() -> str:
    """Suggest the next 3 missions based on the roadmap."""
    p = Path("/opt/tiktok-bot/docs/ROADMAP.md")
    if not p.exists():
        return ("<b>➡ Next mission</b>\n"
                "Roadmap not available. See docs/CURRENT_STATUS.md.")
    txt = p.read_text(encoding="utf-8")
    import re
    items = re.findall(r"^- \[ \] (.+)$", txt, re.MULTILINE)
    if not items:
        return "<b>➡ Next mission</b>\nNo unchecked items in ROADMAP.md."
    lines = ["<b>➡ Next missions</b>"]
    for i, it in enumerate(items[:3], 1):
        lines.append(f"{i}. {_esc(it)}")
    return "\n".join(lines)


async def handle_agent_plan_json(goal: str) -> str:
    """Return the structured plan as readable JSON + summary."""
    goal = goal.strip()
    if not goal:
        return "Usage: /agent_plan_json &lt;goal&gt;"
    from bot.agent.planner import plan_goal
    plan = plan_goal(goal)
    # Compact summary up top, then JSON in <pre>
    import json as _json
    head = (f"<b>🧭 Plan</b> <code>{plan['plan_id']}</code> · "
            f"risk=<b>{plan['risk_level']}</b> · "
            f"role=<code>{plan['model_role']}</code> · "
            f"steps={len(plan['steps'])}\n"
            f"<i>{_esc(plan.get('rationale','')[:160])}</i>")
    body = _json.dumps(plan, ensure_ascii=False, indent=2)
    return f"{head}\n<pre>{_esc(body)[:3000]}</pre>"


async def handle_agent_status() -> str:
    from bot.agent.self_check import run_agent_status
    return await run_agent_status()


async def handle_agent_metrics() -> str:
    from bot.agent.self_check import run_agent_metrics
    return await run_agent_metrics()


async def handle_agent_evals() -> str:
    from bot.agent.evals import run_all_evals, format_report
    rep = await run_all_evals()
    return format_report(rep, html=True)


async def handle_make_prompt(spec: str) -> str:
    """/make_prompt <description> — generate a Claude prompt for a
    candidate code task and save it under data/code_prompts/.
    Does NOT enqueue the task automatically."""
    spec = spec.strip()
    if not spec:
        return "Usage: /make_prompt &lt;description&gt;"
    from bot.agent.prompt_builder import (build_coding_prompt,
                                            save_prompt_for_task,
                                            estimate_code_task_risk,
                                            classify_coding_task)
    import uuid as _uuid
    pseudo_id = f"draft_{_uuid.uuid4().hex[:8]}"
    risk = estimate_code_task_risk(spec)
    cls  = classify_coding_task(spec)
    task = {"id": pseudo_id, "title": spec[:100], "description": spec,
            "risk_level": risk, "branch": "dev-agent"}
    prompt = build_coding_prompt(task)
    path = save_prompt_for_task(pseudo_id, prompt)
    log_action(user="tg_admin", action="make_prompt", risk_level="low",
               status="ok", result_summary=f"id={pseudo_id} risk={risk}")
    return (f"📝 <b>Prompt drafted</b> <code>{pseudo_id}</code>\n"
            f"Class: {cls} · Risk: <b>{risk}</b>\n"
            f"File: <code>{path}</code> ({len(prompt)} chars)\n\n"
            "Use /code_task to actually queue the task; the worker will "
            "regenerate the prompt with the real task_id.")


async def handle_self_improve_once() -> str:
    from bot.agent.self_improve import (self_improve_once,
                                          format_self_improve_report)
    result = self_improve_once(user="tg_admin")
    log_action(user="tg_admin", action="self_improve_once",
               risk_level="low", status="ok",
               result_summary=f"status={result.get('status','?')}")
    return format_self_improve_report(result)


# ── Permission sessions ───────────────────────────────────────────────────────

def handle_permissions_view() -> str:
    from bot.agent.sessions import format_session_panel
    return format_session_panel()


def handle_grant_session(spec: str) -> str:
    """/grant_session <scope> <minutes>"""
    parts = spec.strip().split()
    if len(parts) != 2:
        from bot.agent.sessions import VALID_SCOPES
        return ("Usage: /grant_session &lt;scope&gt; &lt;minutes&gt;\n"
                f"Scopes: {', '.join(VALID_SCOPES)}")
    scope, mins = parts[0], parts[1]
    try:
        minutes = int(mins)
    except ValueError:
        return "❌ minutes must be an integer."
    from bot.agent.sessions import grant_session, VALID_SCOPES
    if scope not in VALID_SCOPES:
        return f"❌ invalid scope. Valid: {', '.join(VALID_SCOPES)}"
    rec = grant_session(scope, minutes, user="tg_admin")
    return (f"✅ Session granted: <b>{rec['scope']}</b> for "
            f"<b>{minutes}</b> min.\n"
            "<i>High-risk public actions still require /confirm_action.</i>")


def handle_revoke_session() -> str:
    from bot.agent.sessions import revoke_session
    if revoke_session(user="tg_admin"):
        return "🚫 Session revoked. Default scope <b>low_only</b> restored."
    return "(no active session to revoke)"


async def handle_run_task(goal: str) -> str:
    if not goal:
        return "Usage: /run_task <goal>"
    log(f"run_task goal={goal[:80]!r}")
    result = await run_task(goal=goal, user="tg_admin")
    icon = "✅" if result["status"] == "done" else "❌"
    lines = [
        f"{icon} Task <code>{result['task_id']}</code> [{result['type']}] {result['status']}",
        f"Goal: {goal[:80]}",
    ]
    if result.get("result"):
        lines.append(f"\nResult:\n{result['result'][:1200]}")
    return "\n".join(lines)


async def handle_message_backend(text: str) -> str:
    """Forward to backend /message with source=telegram → uses role=telegram_chat."""
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(
                f"{BACKEND}/message",
                json={"username": "tg_admin", "content": text, "source": "telegram"},
            )
        if r.status_code == 200:
            return r.json().get("reply") or "(empty reply)"
        return f"backend error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"error: {e}"


async def _summarize_via_backend(content: str, caption: str, file_id: str) -> str:
    try:
        truncated = content[:4000]
        tail = f"\n\n[... {len(content)-4000} more chars]" if len(content) > 4000 else ""
        prompt = (
            f"Admin gửi file (file_id={file_id}) kèm yêu cầu: \"{caption}\"\n\n"
            f"Nội dung:\n{truncated}{tail}\n\nTóm tắt/phân tích theo yêu cầu."
        )
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                f"{BACKEND}/message",
                json={"username": "tg_admin_file", "content": prompt, "source": "file_summary"},
            )
        if r.status_code == 200:
            return r.json().get("reply", "(empty)")
        return f"Backend {r.status_code}"
    except Exception as e:
        return f"Error: {e}"


async def handle_git_status() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", "/opt/tiktok-bot", "status", "--short"],
            text=True, stderr=subprocess.STDOUT,
        )
        log_out = subprocess.check_output(
            ["git", "-C", "/opt/tiktok-bot", "log", "--oneline", "-5"],
            text=True,
        )
        return (
            "<b>Git Status</b>\n"
            f"<pre>{out[:800] or '(clean)'}</pre>\n"
            "<b>Recent commits</b>\n"
            f"<pre>{log_out[:500]}</pre>"
        )
    except Exception as e:
        return f"git error: {e}"


async def handle_backup() -> str:
    return (
        "⚠️ Backup not automated.\n"
        "Safe files:\n"
        "  data/tasks.db\n"
        "  data/audit/actions.jsonl\n"
        "  data/telegram/files.jsonl\n\n"
        "Use /send_file to retrieve specific files."
    )


# ── File inbox handler ────────────────────────────────────────────────────────

_SUMMARIZE_KW = ("tóm tắt", "summarize", "đọc file", "analyze", "phân tích file")
_OCR_KW       = ("ocr", "phân tích ảnh", "xem ảnh", "read image")


async def handle_file_message(msg: dict, chat_id: str | int) -> str:
    caption = (msg.get("caption") or "").strip()
    if "photo" in msg:
        photos = msg["photo"]
        p = max(photos, key=lambda x: x.get("file_size", 0))
        tg_fid, ftype = p["file_id"], "photo"
        filename = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        mime, size = "image/jpeg", p.get("file_size", 0)
    elif "document" in msg:
        doc = msg["document"]
        tg_fid, ftype = doc["file_id"], "document"
        filename = doc.get("file_name", f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        mime, size = doc.get("mime_type", ""), doc.get("file_size", 0)
    elif "audio" in msg:
        a = msg["audio"]
        tg_fid, ftype = a["file_id"], "audio"
        filename = a.get("file_name", f"audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3")
        mime, size = a.get("mime_type", "audio/mpeg"), a.get("file_size", 0)
    elif "video" in msg:
        v = msg["video"]
        tg_fid, ftype = v["file_id"], "video"
        filename = v.get("file_name", f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        mime, size = v.get("mime_type", "video/mp4"), v.get("file_size", 0)
    elif "voice" in msg:
        vo = msg["voice"]
        tg_fid, ftype = vo["file_id"], "voice"
        filename = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
        mime, size = vo.get("mime_type", "audio/ogg"), vo.get("file_size", 0)
    else:
        return "⚠️ File type not recognised."

    log(f"file_recv type={ftype} filename={filename!r} size={size}")
    record = await download_telegram_file(
        TG_TOKEN, tg_fid, filename, ftype,
        caption=caption, mime_type=mime, size=size,
    )
    if not record:
        return "❌ Download failed (file too large or API error)."

    reply = (
        f"✅ <code>{record['file_id']}</code> <b>{filename}</b> ({size//1024}KB)\n"
        f"💾 <code>{record['local_path']}</code>"
    )
    if caption:
        reply += f"\n📝 <i>{caption[:80]}</i>"

    cap_low = caption.lower()
    if any(kw in cap_low for kw in _SUMMARIZE_KW):
        ok, content = read_text_file(record["local_path"])
        if ok:
            summary = await _summarize_via_backend(content, caption, record["file_id"])
            reply += f"\n\n<b>Summary:</b>\n{summary[:2000]}"
            create_task("summarize_file", f"Tóm tắt file {filename}",
                        status="done", input_files=[record["local_path"]])
        else:
            reply += f"\n\n{content}"
    elif any(kw in cap_low for kw in _OCR_KW):
        reply += "\n\n⚠️ OCR/vision not yet supported. File saved."
    return reply


# ── Callback query dispatcher ─────────────────────────────────────────────────

_CONFIRM_PENDING: dict[str, str] = {}  # callback_id -> action


async def dispatch_callback(cb: dict, chat_id: str | int) -> None:
    """Handle an inline keyboard button press. Always answerCallbackQuery first.
    Navigation = edit existing menu in place.
    Action  = edit menu with result (short) OR send result + refresh menu (long).
    Input   = edit menu with prompt; pending_input handled when user replies.
    """
    cb_id   = cb["id"]
    data    = cb.get("data", "")
    msg_id  = (cb.get("message") or {}).get("message_id")

    await answer_cb(cb_id)   # always ACK
    log(f"callback data={data!r} chat_id={chat_id} msg_id={msg_id}")

    parts = data.split(":", 1)
    kind  = parts[0]
    val   = parts[1] if len(parts) > 1 else ""

    # ── Navigation: show sub-menu (edit-in-place) ─────────────────────────────
    if kind == "nav":
        menu_fn = {
            "status": menu_status, "router": menu_router, "tasks": menu_tasks,
            "search": menu_search, "files":  menu_files,  "skills": menu_skills,
            "admin":  menu_admin,  "memory": menu_memory,
            "sales":  menu_sales,  "code":   menu_code,
            "agent":  menu_agent,
        }.get(val)
        if menu_fn:
            text, kb = menu_fn()
        else:
            text, kb = menu_main()
        await send_or_edit_menu(chat_id, text, kb,
                                menu_name=val or "main",
                                preferred_message_id=msg_id)
        return

    if kind == "menu" and val == "main":
        text, kb = menu_main()
        await send_or_edit_menu(chat_id, text, kb,
                                menu_name="main",
                                preferred_message_id=msg_id)
        return

    # ── Immediate action: edit menu with result (or park if long) ─────────────
    if kind == "do":
        try:
            result = await _execute_action(val, chat_id)
        except Exception as e:
            result = f"❌ Action error: {e}"
        # back_to: stay in the parent menu where the action was triggered.
        # We don't know the parent reliably from `do:` data, so default "main".
        # Telegram users who want to keep navigating tap Back-to-Menu.
        await show_action_result(chat_id, result,
                                 preferred_message_id=msg_id,
                                 view=f"do:{val}",
                                 back_to="main")
        return

    # ── Input required: edit menu with prompt + Cancel button ─────────────────
    if kind == "input":
        prompts = {
            "run_task":      "✏️ Enter the task goal:",
            "search_web":    "🔎 Enter keyword to search:",
            "deep_research": "🔬 Enter research topic:",
            "send_file":     "📤 Enter file path or file_id:",
            "skill_detail":  "🧩 Enter skill name:",
            "memory_search": "🔍 Enter memory search query:",
            "memory_add":    "➕ Enter memory: Title | content | tag1,tag2",
            "memory_context":"🔎 Enter goal/query for context preview:",
            "consult":       "💬 Enter customer question for sales consult:",
            "product_add":   ("✏ Enter product (name | network | country | "
                              "duration_days | data_amount | sms(0/1) | hotspot(0/1) | renew(0/1) | notes):"),
            "product_update":"🔧 Enter: &lt;product_id&gt; | field=value | field=value ...",
            "product_verify":"✅ Enter product_id to mark as <b>active</b> (verified):",
            "product_disable":"🚫 Enter product_id to <b>disable</b>:",
            "lead_add":      "➕ Enter lead: platform | sender_key | name | need",
            "followup_add":  "⏰ Enter followup: lead_id | remind_at (ISO date) | note",
            "code_task":     ("🛠 New code task — format:\n"
                              "<code>title | description | risk(low|medium|high) | priority(1-9)</code>\n"
                              "Example: <code>fix btc cache | reuse cached price within 30s | low | 6</code>"),
            "agent_plan":    "🧭 Enter goal to PLAN (planner only, no execution):",
            "agent_run":     ("▶ Enter goal to RUN (low-risk auto-runs; "
                              "medium/high creates pending_action):"),
        }
        prompt = prompts.get(val, "✏️ Enter input:")
        _session_save(val, prompt)
        log(f"pending_input={val} chat_id={chat_id}")
        cancel_kb = make_keyboard([[("❌ Cancel", "menu:main")]])
        await send_or_edit_menu(
            chat_id,
            f"{prompt}\n\n<i>Send /cancel to abort.</i>",
            cancel_kb,
            menu_name=f"input:{val}",
            preferred_message_id=msg_id,
        )
        return

    # ── Confirm dangerous actions ─────────────────────────────────────────────
    if kind == "confirm":
        if val == "restart_bot":
            confirm_kb = make_keyboard([
                [("✅ Yes, restart", "do:restart_bot"),
                 ("❌ Cancel",       "menu:main")],
            ])
            await send_or_edit_menu(
                chat_id,
                "⚠️ <b>Restart tiktok-bot?</b>\n"
                "This will briefly drop the TikTok session.",
                confirm_kb,
                menu_name="confirm:restart_bot",
                preferred_message_id=msg_id,
            )
        return


async def _execute_action(action: str, chat_id: str | int) -> str:
    """Run a 'do:' action and return the result string."""
    if action == "health":
        return await handle_health()
    if action == "logs":
        return await handle_logs()
    if action == "tiktok_chat_info":
        return handle_tiktok_chat_info()
    if action == "router_status":
        return await handle_router_status()
    if action == "models":
        return await handle_models()
    if action == "model_policy":
        return await handle_model_policy()
    if action == "tasks":
        return handle_tasks_list()
    if action == "pending_actions":
        return handle_pending_list()
    if action == "files":
        return handle_files_list()
    if action == "upload_guide":
        return handle_upload_guide()
    if action == "skills":
        return handle_skills_list()
    if action == "git_status":
        return await handle_git_status()
    if action == "backup":
        return await handle_backup()
    if action == "memory_lessons":
        return handle_lessons("")
    if action == "memory_compact":
        return await handle_memory_compact()
    if action == "help":
        return handle_help_panel()
    if action == "code_tasks":
        return code_format_list(limit=15)
    if action == "code_status":
        return code_format_status()
    if action == "code_run_once":
        return await handle_code_run_once()
    if action == "code_pause":
        code_pause()
        return "⏸ Code worker paused. New tasks queue but won't auto-run."
    if action == "code_resume":
        code_resume()
        return "▶ Code worker resumed."
    if action == "agent_health":
        return await handle_agent_health()
    if action == "agent_policy":
        return handle_agent_policy()
    if action == "agent_workers":
        return handle_agent_workers()
    if action == "agent_next":
        return handle_agent_next()
    if action == "products":
        return handle_products()
    if action == "products_active":
        return handle_products("active")
    if action == "products_needs_update":
        return handle_products("needs_update")
    if action == "products_disabled":
        return handle_products("disabled")
    if action == "leads":
        return handle_leads()
    if action == "followups":
        return handle_followups()
    if action == "restart_bot":
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", "tiktok-bot"],
                check=True, capture_output=True,
            )
            log_action(user="tg_admin", action="restart_bot", risk_level="high",
                       status="ok", result_summary="systemctl restart tiktok-bot")
            return "✅ tiktok-bot restarted."
        except Exception as e:
            return f"❌ Restart failed: {e}"
    return f"Unknown action: {action}"


# ── Pending input handler ─────────────────────────────────────────────────────

async def handle_pending_input(action: str, text: str, chat_id: str | int) -> str:
    """Process text that was submitted for a pending session action."""
    _session_clear()
    if action == "run_task":
        return await handle_run_task(text)
    if action == "search_web":
        return await handle_run_task(f"tìm thông tin: {text}")
    if action == "deep_research":
        return await handle_run_task(f"nghiên cứu chuyên sâu: {text}")
    if action == "send_file":
        return await handle_send_file(text, chat_id)
    if action == "skill_detail":
        return handle_skill_detail(text)
    if action == "memory_search":
        return await handle_memory_search(text)
    if action == "memory_add":
        return await handle_memory_add(text)
    if action == "memory_forget":
        return handle_memory_forget(text)
    if action == "memory_context":
        return handle_memory_context(text)
    if action == "consult":
        return await handle_consult(text)
    if action == "product_add":
        return handle_product_add(text)
    if action == "product_update":
        return handle_product_update(text)
    if action == "product_verify":
        return handle_product_verify(text)
    if action == "product_disable":
        return handle_product_disable(text)
    if action == "lead_add":
        return handle_lead_add(text)
    if action == "followup_add":
        return handle_followup_add(text)
    if action == "code_task":
        return handle_code_task_add(text)
    if action == "agent_plan":
        return await handle_agent_plan(text)
    if action == "agent_run":
        return await handle_agent_run(text)
    return f"Unknown action: {action}"


# ── Main command dispatcher ───────────────────────────────────────────────────

async def dispatch(text: str, chat_id: str | int = "") -> str:
    low   = text.lower().strip()
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        # Always rebuild at the bottom so the menu is in front of the admin.
        text_m, kb = menu_main()
        await rebuild_menu_at_bottom(chat_id, text_m, kb, menu_name="main")
        return ""
    if cmd == "/menu":
        # /menu = "bring me the panel" → fresh send at bottom + delete stale.
        text_m, kb = menu_main()
        await rebuild_menu_at_bottom(chat_id, text_m, kb, menu_name="main")
        return ""
    if cmd == "/cancel":
        _session_clear()
        text_m, kb = menu_main()
        await rebuild_menu_at_bottom(chat_id, text_m, kb, menu_name="main")
        log("cancel: pending input cleared, menu refreshed at bottom")
        return ""

    if cmd == "/status":       return await handle_status()
    if cmd == "/health":       return await handle_health()
    if cmd == "/router_status":return await handle_router_status()
    if cmd == "/models":       return await handle_models()
    if cmd == "/model_policy": return await handle_model_policy()
    if cmd == "/skills":       return handle_skills_list()
    if cmd == "/skill":        return handle_skill_detail(arg)
    if cmd == "/tasks":        return handle_tasks_list()
    if cmd == "/task":         return handle_task_detail(arg)
    if cmd == "/run_task":     return await handle_run_task(arg)
    if cmd == "/cancel_task":  return handle_cancel_task(arg)
    if cmd == "/pending_actions": return handle_pending_list()
    if cmd == "/confirm_action":  return handle_confirm_action(arg)
    if cmd == "/cancel_action":   return handle_cancel_action(arg)
    if cmd == "/files":        return handle_files_list()
    if cmd == "/file":         return handle_file_detail(arg)
    if cmd == "/send_file":    return await handle_send_file(arg, chat_id)
    if cmd == "/logs":         return await handle_logs()
    if cmd == "/tiktok_chat_info": return handle_tiktok_chat_info()
    if cmd == "/agent_blueprint":  return handle_agent_blueprint()
    if cmd == "/workers":          return handle_workers()
    if cmd == "/products":
        # /products [active|needs_update|disabled]
        return handle_products(arg.strip() or None)
    if cmd == "/product":          return handle_product_detail(arg)
    if cmd == "/product_add":      return handle_product_add(arg)
    if cmd == "/product_update":   return handle_product_update(arg)
    if cmd == "/product_verify":   return handle_product_verify(arg)
    if cmd == "/product_disable":  return handle_product_disable(arg)
    if cmd == "/consult":          return await handle_consult(arg)
    if cmd == "/leads":            return handle_leads()
    if cmd == "/lead":             return handle_lead_detail(arg)
    if cmd == "/lead_by_sender":   return handle_lead_by_sender(arg)
    if cmd == "/lead_add":         return handle_lead_add(arg)
    if cmd == "/consulting_logs":  return handle_consulting_logs(arg)
    if cmd == "/followups":        return handle_followups()
    if cmd == "/followup_add":     return handle_followup_add(arg)
    # ── Code Worker ───────────────────────────────────────────────────────
    if cmd == "/code_task":        return handle_code_task_add(arg)
    if cmd == "/code_tasks":       return code_format_list(limit=20)
    if cmd == "/code_task_info":   return code_format_detail(arg.strip())
    if cmd == "/code_cancel":
        ok = code_cancel_task(arg.strip())
        return f"🚫 Code task <code>{arg.strip()}</code> cancelled." if ok \
               else f"❌ Code task <code>{arg.strip()}</code> not found."
    if cmd == "/code_status":      return code_format_status()
    if cmd == "/code_worker_run_once": return await handle_code_run_once()
    if cmd == "/code_worker_pause":
        code_pause(); return "⏸ Code worker paused."
    if cmd == "/code_worker_resume":
        code_resume(); return "▶ Code worker resumed."
    # ── Self-operating agent ──────────────────────────────────────────────
    if cmd == "/agent_plan":      return await handle_agent_plan(arg)
    if cmd == "/agent_plan_json": return await handle_agent_plan_json(arg)
    if cmd == "/agent_run":       return await handle_agent_run(arg)
    if cmd == "/agent_health":    return await handle_agent_health()
    if cmd == "/agent_status":    return await handle_agent_status()
    if cmd == "/agent_metrics":   return await handle_agent_metrics()
    if cmd == "/agent_policy":    return handle_agent_policy()
    if cmd == "/agent_workers":   return handle_agent_workers()
    if cmd == "/agent_next":      return handle_agent_next()
    if cmd == "/agent_evals":     return await handle_agent_evals()
    if cmd == "/make_prompt":     return await handle_make_prompt(arg)
    if cmd == "/self_improve_once": return await handle_self_improve_once()
    # ── Permission sessions ───────────────────────────────────────────────
    if cmd == "/permissions":     return handle_permissions_view()
    if cmd == "/grant_session":   return handle_grant_session(arg)
    if cmd == "/revoke_session":  return handle_revoke_session()
    if cmd == "/memory_search":    return await handle_memory_search(arg)
    if cmd == "/memory_add":       return await handle_memory_add(arg)
    if cmd == "/memory_forget":    return handle_memory_forget(arg)
    if cmd == "/memory_compact":   return await handle_memory_compact()
    if cmd == "/memory_context":   return handle_memory_context(arg)
    if cmd == "/lessons":          return handle_lessons(arg)
    if cmd == "/audit_recent":     return handle_audit_recent()

    if low.startswith("/"):
        return (
            "Commands: /menu /status /health /router_status /models /model_policy\n"
            "/skills /skill &lt;name&gt; /tasks /task &lt;id&gt; /run_task &lt;goal&gt; /cancel_task &lt;id&gt;\n"
            "/pending_actions /confirm_action &lt;id&gt; /cancel_action &lt;id&gt;\n"
            "/files /file &lt;id&gt; /send_file &lt;path&gt; /logs /tiktok_chat_info\n"
            "/agent_blueprint /workers /lessons [skill] /audit_recent\n"
            "/memory_search &lt;q&gt; /memory_add /memory_forget /memory_compact /memory_context\n"
            "/products /product_add /product_update /consult &lt;q&gt; "
            "/leads /lead &lt;id&gt; /lead_add /followups\n"
            "/start /help /menu /cancel"
        )

    # Smart routing for plain text
    if _looks_like_task(text):
        return await handle_run_task(text)
    return await handle_message_backend(text)


# ── Long-poll loop ────────────────────────────────────────────────────────────

async def bot_loop() -> None:
    if not TG_TOKEN:
        log("TELEGRAM_BOT_TOKEN not set — exiting")
        return
    if not TG_ADMIN:
        log("TELEGRAM_ADMIN_CHAT_ID not set — exiting")
        return

    log(f"start admin_chat={TG_ADMIN}")

    # Resume from disk-persisted offset to avoid dropping messages during
    # bot restarts. If no offset file, use 0 = "give me all pending updates".
    OFFSET_FILE = Path("/opt/tiktok-bot/data/telegram/getupdates_offset.txt")
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip() or "0")
        except Exception:
            offset = 0
    log(f"polling offset={offset} (resumed from disk)")

    def _save_offset(new_off: int) -> None:
        try:
            OFFSET_FILE.write_text(str(int(new_off)))
        except Exception:
            pass

    poll_count = 0
    while True:
        poll_count += 1
        try:
            r = await tg_call("getUpdates", {
                "offset":          offset,
                "timeout":         POLL_TIMEOUT,
                "allowed_updates": ["message", "callback_query"],
            })
        except Exception as e:
            log(f"getUpdates error #{poll_count}: {type(e).__name__}: {e}")
            await asyncio.sleep(5)
            continue

        if not r.get("ok", True):
            log(f"getUpdates rejected #{poll_count}: code={r.get('error_code')} "
                f"desc={r.get('description','?')[:120]} offset={offset}")
            await asyncio.sleep(3)
            continue

        results = r.get("result") or []
        # Heartbeat every 5 polls (~150s) so journal shows the loop is alive
        if poll_count % 5 == 1 or results:
            log(f"poll #{poll_count} offset_in={offset} updates={len(results)}")

        for update in results:
            offset = update["update_id"] + 1
            _save_offset(offset)

            # ── Callback query (inline button press) ──────────────────────────
            cb = update.get("callback_query")
            if cb:
                cb_chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", TG_ADMIN))
                cb_data    = cb.get("data", "")
                authorized = await is_admin_chat(cb_chat_id)
                log(f"update type=callback chat_id={cb_chat_id} data={cb_data!r}")
                log(f"auth ok={authorized} chat_id={cb_chat_id}")
                if not authorized:
                    await tg_call("answerCallbackQuery", {
                        "callback_query_id": cb["id"],
                        "text": "Not authorized.",
                    })
                    continue
                try:
                    await dispatch_callback(cb, cb_chat_id)
                    log(f"callback answered data={cb_data!r}")
                except Exception as e:
                    log(f"callback error data={cb_data!r} err={e}")
                    try:
                        await answer_cb(cb["id"])
                        await send(cb_chat_id, f"❌ Error: {e}")
                    except Exception:
                        pass
                continue

            # ── Text / file message ───────────────────────────────────────────
            msg = update.get("message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text    = (msg.get("text") or "").strip()
            authorized = await is_admin_chat(chat_id)
            log(f"update type=message chat_id={chat_id} text={text[:60]!r}")
            log(f"auth ok={authorized} chat_id={chat_id}")

            if not authorized:
                try:
                    await tg_call("sendMessage",
                                  {"chat_id": chat_id, "text": "Not authorized."})
                except Exception:
                    pass
                continue

            # ── File upload (no text) ─────────────────────────────────────────
            has_file = any(k in msg for k in ("photo","document","audio","video","voice"))
            if not text and has_file:
                log(f"file_recv chat_id={chat_id}")
                try:
                    reply = await handle_file_message(msg, chat_id)
                    # File-upload result is a normal chat message, not a
                    # menu edit.
                    await send_chat_reply(chat_id, reply)
                    log_action(user="tg_admin", action="file_upload",
                               channel="telegram", risk_level="low",
                               status="ok", result_summary=reply[:100])
                except Exception as e:
                    log(f"error handler=file_message message={e}")
                    try:
                        await send_chat_reply(chat_id, f"❌ File error: {e}")
                    except Exception:
                        pass
                continue
            if not text:
                continue

            # ── Outer guard: every admin text must produce a reply ────────────
            try:
                # ── 1. pending_input ─────────────────────────────────────────
                # Active iff:
                #   - session exists and not expired (TTL=600s in _session_get)
                #   - text is not a slash-command (so /cancel etc. still work)
                session = _session_get()
                if session and not text.startswith("/"):
                    pending_action = session["action"]
                    log(f"route=pending_input action={pending_action} "
                        f"chat_id={chat_id}")
                    # Consume immediately — clear before processing so
                    # any error doesn't leave the session "stuck".
                    _session_clear()
                    log(f"pending cleared reason=consumed action={pending_action}")
                    try:
                        reply = await handle_pending_input(
                            pending_action, text, chat_id,
                        )
                    except Exception as e:
                        import traceback as _tb
                        log(f"error handler=pending_input action={pending_action} "
                            f"message={e}\n{_tb.format_exc()[:500]}")
                        reply = f"❌ Pending-input error: {e}"
                    # Result of a pending input is delivered as a NEW chat
                    # message — never edits the menu panel.
                    await send_chat_reply(chat_id, reply or "(empty reply)")
                    log_action(user="tg_admin",
                               action=f"input:{pending_action}",
                               channel="telegram", risk_level="low",
                               status="ok",
                               result_summary=(reply or "")[:100])
                    continue

                # ── 2. command vs plain text ─────────────────────────────────
                if text.startswith("/"):
                    cmd_name = text.split()[0].lower()
                    log(f"route=command command={cmd_name} chat_id={chat_id}")
                else:
                    log(f"route=plain_text chat_id={chat_id} "
                        f"text={text[:60]!r}")

                try:
                    reply = await dispatch(text, chat_id)
                except Exception as e:
                    import traceback as _tb
                    log(f"error handler=dispatch text={text[:30]!r} "
                        f"message={e}\n{_tb.format_exc()[:500]}")
                    reply = f"❌ Error: {e}"

                # ── 3. Reply policy ──────────────────────────────────────────
                # Plain text + most command results → NEW chat message.
                # Menu commands (/menu /start /help /cancel) handle their
                # own panel rendering inside dispatch and return "".
                if reply:
                    await send_chat_reply(chat_id, reply)
                    log_action(
                        user="tg_admin",
                        action=text.split()[0][:30] if text.startswith("/")
                                                    else "chat",
                        channel="telegram", risk_level="low",
                        status="ok", result_summary=reply[:100],
                    )
                elif text.startswith("/"):
                    log(f"cmd_done cmd={text.split()[0]} "
                        "(rendered via menu helper inside dispatch)")
                else:
                    # Plain text returned empty — never silently drop.
                    fallback = "🤔 (empty reply from agent — try rephrasing)"
                    log(f"plain_text reply EMPTY — sending fallback "
                        f"chat_id={chat_id}")
                    await send_chat_reply(chat_id, fallback)
            except Exception as e:
                # Last-resort safety net — must not crash the polling loop.
                import traceback as _tb
                log(f"error handler=outer text={text[:30]!r} message={e}\n"
                    f"{_tb.format_exc()[:500]}")
                try:
                    await send_chat_reply(
                        chat_id, f"⚠️ Telegram handler error: {e}",
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(bot_loop())
