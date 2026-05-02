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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Owner timezone for friendly display in Telegram panels.
# Owner's clock shows JST (5:29 AM = UTC 20:29 + 9). They asked to
# "dừng JST" — meaning drop the literal "JST" label, just render
# the local clock as plain "5:29 AM". So we keep JST as the default
# but format without any TZ label. Override via OWNER_TIMEZONE env.
try:
    from zoneinfo import ZoneInfo
    _OWNER_TZ_NAME = os.getenv("OWNER_TIMEZONE", "Asia/Tokyo")
    _OWNER_TZ = ZoneInfo(_OWNER_TZ_NAME)
except Exception:
    _OWNER_TZ = timezone.utc
    _OWNER_TZ_NAME = "UTC"


def _fmt_iso_local(iso_str: str | None) -> str:
    """Convert a UTC ISO string into 12-hour AM/PM local time.

    Owner request: "chỉ cần ghi AM PM là được". Format:
        "5:29:17 AM" (no extra labels — owner's clock is local)
    Returns the original string on parse failure so we never crash
    a panel just because of a bad timestamp. Empty/None → "".
    """
    if not iso_str:
        return ""
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(_OWNER_TZ)
        # 12-hour AM/PM, drop leading zero on hour (e.g. "5:29:17 AM")
        h12 = int(dt_local.strftime("%I"))
        ampm = dt_local.strftime("%p")
        return f"{h12}:{dt_local.strftime('%M:%S')} {ampm}"
    except Exception:
        return iso_str


def _humanize_age(iso_str: str | None) -> str:
    """Return "Ns ago" / "Nm ago" / "Nh ago" relative to now (UTC).

    Used to show "log gần nhất X giây trước" so admin can tell at a
    glance if the worker is actually progressing.
    """
    if not iso_str:
        return ""
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt_utc
        secs = int(delta.total_seconds())
        if secs < 0:
            secs = 0
        if secs < 60:
            return f"{secs}s trước"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s trước"
        h, rem = divmod(secs, 3600)
        return f"{h}h {rem // 60}m trước"
    except Exception:
        return ""

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
    get_task as code_get_task,
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

# Coding worker bridge + Claude quota scheduler
from bot import coding_worker_bridge as _cwb
from bot import claude_quota         as _cq
try:
    _cq.start_scheduler_thread()
except Exception as _e:
    print(f"[tg] claude_quota scheduler warning: {_e}", flush=True)

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
        [("📊 Progress",       "do:agent_progress")],
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


def _resolve_file_arg(arg: str) -> tuple[str, str]:
    """Resolve a /send_file or /send_photo arg to a local path.

    Accepts either an internal file_id from /files (8-12 hex chars) or a
    raw filesystem path. Returns (path, label_for_caption).
    """
    arg = arg.strip()
    if arg and "/" not in arg and "\\" not in arg and \
       all(c in "0123456789abcdefABCDEF" for c in arg) and 4 <= len(arg) <= 16:
        rec = get_file_record(arg)
        if rec and rec.get("local_path"):
            return rec["local_path"], rec.get("filename") or arg
    return arg, os.path.basename(arg)


async def handle_send_file(arg: str, chat_id: str | int) -> str:
    if not arg.strip():
        return ("Usage: <code>/send_file &lt;file_id|path&gt;</code>\n"
                "file_id from /files, or absolute path under an allowed root.")
    path, label = _resolve_file_arg(arg)
    ok, reason = is_safe_send_path(path)
    if not ok:
        return reason
    ext = Path(path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        sent = await send_photo(chat_id, path, caption=f"📤 {label}")
    else:
        sent = await send_document(chat_id, path, caption=f"📤 {label}")
    return (f"✅ Sent: <code>{label}</code>" if sent
            else f"❌ Failed to send: <code>{label}</code>")


async def handle_send_photo(arg: str, chat_id: str | int) -> str:
    if not arg.strip():
        return ("Usage: <code>/send_photo &lt;file_id|path&gt;</code>\n"
                "file_id from /files, or path to an image (jpg/png/webp/gif).")
    path, label = _resolve_file_arg(arg)
    ok, reason = is_safe_send_path(path)
    if not ok:
        return reason
    ext = Path(path).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return (f"❌ <code>{label}</code> is not an image extension. "
                "Use /send_file for non-image files.")
    sent = await send_photo(chat_id, path, caption=f"📤 {label}")
    return (f"✅ Photo sent: <code>{label}</code>" if sent
            else f"❌ Failed to send photo: <code>{label}</code>")


async def handle_ocr(arg: str, chat_id: str | int) -> str:
    """Trích text từ ảnh đã upload (file_id) hoặc đường dẫn ảnh
    nằm trong allow-list. Audit-logged + redaction trước khi reply.
    """
    from bot.ocr import ocr_image, OCR_ALLOWED_EXTS
    arg = (arg or "").strip()
    if not arg:
        return ("Usage: <code>/ocr &lt;file_id|path&gt; [prompt]</code>\n"
                "file_id lấy từ <code>/files</code>, hoặc đường dẫn ảnh "
                "nằm trong allow-list. "
                f"Định dạng hỗ trợ: {', '.join(sorted(OCR_ALLOWED_EXTS))}.")

    parts = arg.split(None, 1)
    target = parts[0]
    prompt = parts[1].strip() if len(parts) > 1 else None

    path, label = _resolve_file_arg(target)
    res = await ocr_image(path, prompt=prompt, user="tg_admin")
    if not res.get("ok"):
        return (f"❌ OCR <code>{_esc(label)}</code> lỗi.\n"
                f"<i>{_esc(res.get('error', ''))[:300]}</i>")

    text = (res.get("text") or "").strip() or "(không phát hiện text)"
    redactions = int(res.get("redactions") or 0)
    chars = int(res.get("chars") or 0)
    model = res.get("model") or ""

    # Telegram message cap (~3500 chars for body); truncate gracefully.
    body = text if len(text) <= 3200 else (text[:3200] + "\n…[truncated]")
    redaction_note = (f"\n🛡 redactions: <b>{redactions}</b>"
                      if redactions else "")
    return (
        f"🔎 <b>OCR</b> <code>{_esc(label)}</code> "
        f"(<i>{chars} chars, {model}</i>){redaction_note}\n\n"
        f"<pre>{_esc(body)}</pre>"
    )


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
        "/memory_search &lt;q&gt; /lessons /audit_recent\n"
        "/agent_progress — what the agent is doing right now\n\n"
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


async def _bridge_run_in_background(chat_id: str | int, n: int = 1,
                                       formatter=None) -> None:
    """Run bridge.run_once / run_batch as an asyncio background task and
    send the result to the admin via send_chat_reply when done. The
    polling loop keeps running while this task is in progress, so the
    admin can keep chatting and the bot replies immediately to new
    messages instead of going silent for 1-3 minutes per Claude run.
    """
    try:
        if n <= 1:
            res = await _cwb.run_once(user="tg_admin")
            text = (formatter or _cwb.format_run_result)(res)
            await send_chat_reply(chat_id, text)
        else:
            results = await _cwb.run_batch(n, user="tg_admin")
            if not results:
                await send_chat_reply(chat_id, "💤 No tasks to run.")
                return
            parts = [f"<b>🛠 Batch result ({len(results)})</b>"]
            for r in results:
                parts.append((formatter or _cwb.format_run_result)(r))
                parts.append("---")
            await send_chat_reply(
                chat_id,
                "\n\n".join(parts).rstrip("---\n").rstrip(),
            )
    except Exception as e:
        import traceback as _tb
        log(f"bridge bg error: {e}\n{_tb.format_exc()[:300]}")
        try:
            await send_chat_reply(
                chat_id,
                f"⚠️ Agent lỗi khi chạy bridge: <code>{_esc(str(e))[:200]}</code>",
            )
        except Exception:
            pass


async def handle_code_run_once(chat_id: str | int = None) -> str:
    """Drive ONE queued code_task end-to-end through the bridge.

    Non-blocking: sends an immediate ack, spawns the bridge run as a
    background asyncio task, and returns _REPLY_HANDLED so the polling
    loop can keep processing new messages during the (often 1-3 min)
    Claude/Codex run.
    """
    if chat_id is None:
        # Backward compat — sync path. Kept for callers that still want
        # the blocking semantics; the long-running case below is the
        # default for Telegram dispatch.
        result = await _cwb.run_once(user="tg_admin")
        return _cwb.format_run_result(result)

    # Pre-check: surface what's about to run so admin sees an immediate
    # acknowledgment before Claude starts grinding.
    try:
        nx = code_next_task()
        if nx:
            preview = (f"🛠 Bridge bắt đầu chạy task "
                       f"<code>{_esc(str(nx['id']))}</code>:\n"
                       f"<i>{_esc((nx.get('title') or '')[:80])}</i>\n\n"
                       f"Em sẽ báo khi xong (1-3 phút). Trong lúc đó "
                       f"bro vẫn chat được — bot không bị block nữa.")
        else:
            preview = ("💤 Không có task nào queued. Tạo task trước "
                       "(<i>“tạo task code …”</i>) rồi chạy lại.")
    except Exception:
        preview = ("🛠 Bridge bắt đầu… em báo khi xong.")
    await send_chat_reply(chat_id, preview)
    asyncio.create_task(_bridge_run_in_background(chat_id, n=1))
    return _REPLY_HANDLED


async def handle_code_run_batch(arg: str, chat_id: str | int = None) -> str:
    """/code_worker_run_batch <n>  (n=1..3) — non-blocking variant."""
    arg = (arg or "").strip()
    try:
        n = int(arg) if arg else 1
    except ValueError:
        return "Usage: /code_worker_run_batch &lt;n&gt;  (1..3)"
    n = max(1, min(n, 3))
    if chat_id is None:
        # Backward-compat sync path
        results = await _cwb.run_batch(n, user="tg_admin")
        if not results:
            return "💤 No tasks to run."
        parts = [f"<b>🛠 Batch result ({len(results)})</b>"]
        for r in results:
            parts.append(_cwb.format_run_result(r))
            parts.append("---")
        return "\n\n".join(parts).rstrip("---\n").rstrip()

    await send_chat_reply(
        chat_id,
        f"🛠 Bridge bắt đầu batch <b>{n}</b> task. Em báo khi xong.",
    )
    asyncio.create_task(_bridge_run_in_background(chat_id, n=n))
    return _REPLY_HANDLED


def handle_code_worker_status() -> str:
    """Coding-tool detection + bridge state."""
    parts = [_cwb.coding_tool_status()]
    parts.append("")
    parts.append(f"Bridge paused: <b>{'yes' if _cwb.is_paused() else 'no'}</b>")
    parts.append(code_format_status())
    logs = _cwb.recent_logs(3)
    if logs:
        parts.append("")
        parts.append("<b>Recent worker logs:</b>")
        for p in logs:
            parts.append(f"  <code>{p.name}</code> ({p.stat().st_size//1024}KB)")
    return "\n".join(parts)


async def handle_agent_autonomy_status() -> str:
    """High-level autonomy dashboard: bridge + sessions + quota +
    pending actions + worker queue."""
    from bot.agent.sessions import current_session, current_scope, \
        remaining_minutes
    from bot.agent.permissions import list_pending

    parts = ["<b>🤖 Agent Autonomy</b>"]
    # Coding tool
    pref = _cwb.get_preferred_coding_tool()
    if pref:
        parts.append(f"Coding tool: <code>{pref.name}</code> "
                     f"({'non-interactive' if pref.noninteractive_ok else 'interactive-only'})")
    else:
        parts.append("Coding tool: <b>not installed</b> (manual setup required)")
    parts.append(f"Bridge paused: <b>{'yes' if _cwb.is_paused() else 'no'}</b>")

    # Session grant
    s = current_session()
    if s:
        parts.append(f"Session: <b>{current_scope()}</b> "
                     f"({remaining_minutes()} min left)")
    else:
        parts.append(f"Session: <b>{current_scope()}</b> (default)")

    # Quota
    parts.append("")
    parts.append(_cq.status_summary())

    # Worker queue + pending
    parts.append("")
    parts.append(code_format_status())
    pa = list_pending(only_pending=True)
    parts.append(f"\nPending actions: <b>{len(pa)}</b>")
    for p in pa[:3]:
        parts.append(f"  • <code>{p['action_id']}</code> {p['action']}")

    # Git remote sanity
    import subprocess
    try:
        r = subprocess.check_output(
            ["git", "-C", "/opt/tiktok-bot", "remote", "-v"],
            text=True, stderr=subprocess.DEVNULL, timeout=5)
        clean = "x-access-token" not in r and "ghp_" not in r
        parts.append(f"\nGit remote clean: <b>{'yes' if clean else 'NO'}</b>")
    except Exception:
        pass
    return "\n".join(parts)


async def handle_brain_evolve_start(arg: str = "") -> str:
    """Start the brain-evolution loop. arg = optional max_tasks (1..3)."""
    from bot.agent import brain_evolve as _be
    try:
        n = int((arg or "").strip()) if arg else 1
    except ValueError:
        n = 1
    n = max(1, min(n, 3))
    _be.start(max_tasks=n, user="tg_admin")
    # Kick off the FIRST pass immediately so the admin sees movement.
    initial_msg = (f"🟢 Brain Evolution loop đã bật "
                   f"(max <b>{n}</b> task/lần).\n"
                   f"Đang chạy task đầu tiên…")
    await send_chat_reply(0 if False else
                          (await _tg_admin_id_or_zero()), initial_msg)
    # Use a background task so the Telegram reply isn't blocked
    asyncio.create_task(_brain_evolve_first_pass(n))
    return _REPLY_HANDLED


async def _brain_evolve_first_pass(n: int) -> None:
    from bot.agent import brain_evolve as _be
    chat = await _tg_admin_id_or_zero()
    for i in range(n):
        if not _be.is_enabled():
            break
        result = await _be.advance_one(user="tg_admin")
        try:
            msg = _vi_format_run_result(result)
        except Exception as e:
            msg = f"❌ format error: {e}"
        try:
            await send_chat_reply(chat,
                f"<b>Brain-evolve {i+1}/{n}</b>\n{msg}")
        except Exception:
            pass
        # Respect bridge signals: stop loop on pause / pending / failure
        if not _be.on_task_done(result, user="tg_admin"):
            break


async def _tg_admin_id_or_zero() -> int:
    try:
        return int(TG_ADMIN)
    except Exception:
        return 0


def handle_brain_evolve_stop() -> str:
    from bot.agent import brain_evolve as _be
    _be.stop(user="tg_admin", reason="user")
    return ("🛑 Đã dừng Brain Evolution loop.\n"
            "<i>Có thể bật lại bất kỳ lúc nào: "
            "<code>/brain_evolve_start</code> hoặc nói "
            "“tự cải thiện brain đi”.</i>")


def handle_brain_evolve_status() -> str:
    from bot.agent import brain_evolve as _be
    return _be.status_panel_vi()


# ── Agent Autorun (10-12h owner work session) ────────────────────────────────

def _autorun_control_keyboard() -> dict:
    """Inline-keyboard panel for the most common autorun commands.

    Owner asked: "các lệnh để điều khiển trong bot telegram nên hiện
    kiểu button". One row per logical group; callback ids match
    existing dispatch_callback do:* handlers.
    """
    return make_keyboard([
        [("📊 Tiến độ",      "do:agent_progress"),
         ("🩺 Diag",          "do:agent_diag")],
        [("✅ Probe Claude",  "do:claude_probe"),
         ("📅 Quota status",  "do:claude_status")],
        [("⏹ Dừng autorun",  "do:autorun_stop"),
         ("📋 Code queue",    "do:code_status")],
    ])


async def handle_agent_autorun_start(arg: str = "",
                                       auto_self_improve: bool = False,
                                       chat_id: str | int | None = None,
                                       ) -> str:
    """Start owner-directed long-horizon autorun loop.

    Usage: /agent_autorun_start [hours] [max_tasks] [objective...]
    Or NL: "làm việc độc lập 12 tiếng"
    Or NL self-improve: "tự hoàn thiện agent đi"
    """
    from bot.agent import agent_autorun as _aa
    arg = (arg or "").strip()
    hours = 24.0 if auto_self_improve else 12.0
    max_tasks = 999 if auto_self_improve else 20
    objective = ""
    if arg:
        parts = arg.split(None, 2)
        try:
            hours = float(parts[0]) if parts else hours
        except ValueError:
            pass
        if len(parts) >= 2:
            try:
                max_tasks = int(parts[1])
            except ValueError:
                pass
        if len(parts) >= 3:
            objective = parts[2]
    d = _aa.start(hours=hours, max_tasks=max_tasks,
                  objective=objective, user="tg_admin",
                  auto_self_improve=auto_self_improve)
    mode_label = ("self-improve (auto-populate từ roadmap)"
                   if auto_self_improve else "owner-directed")

    # Spawn the actual pump loop in background. Without this, start()
    # only sets state and nothing drives advance_one — the loop only
    # wakes up when claude_quota._on_due fires (i.e. on quota reset),
    # which means autorun starts in name only and never makes progress
    # while Claude is available. The pump loop sleeps when paused and
    # exits cleanly when is_due_to_stop returns True or admin stops.
    async def _autorun_report(text: str) -> None:
        try:
            await send_chat_reply(TG_ADMIN, text)
        except Exception:
            pass
    asyncio.create_task(
        _aa.pump_loop(user="tg_admin",
                       report_callback=_autorun_report,
                       # Was 8s — owner asked for faster cycles since
                       # Claude Opus 4.7 is fast. 2s gives Telegram
                       # polling enough time to process admin messages
                       # between cycles without burning CPU.
                       poll_interval_sec=2.0),
    )

    # Surface the control panel as inline buttons so admin can pause /
    # check progress / probe Claude without typing commands.
    if chat_id is not None:
        try:
            asyncio.create_task(send(
                chat_id,
                "<b>🎛 Autorun control panel</b>\n"
                "<i>Bấm nút để điều khiển — không cần gõ lệnh.</i>",
                reply_markup=_autorun_control_keyboard(),
            ))
        except Exception:
            pass

    return (f"🟢 <b>Agent Autorun started</b>\n"
            f"Mode: <b>{mode_label}</b>\n"
            f"Thời lượng: <b>{d['hours']}h</b> · "
            f"max <b>{d['max_tasks']}</b> task\n"
            f"stop_at: <code>{d['stop_at']}</code>\n"
            f"Mục tiêu: <i>{_esc((objective or '(tự chọn từ roadmap/queue)')[:200])}</i>\n\n"
            + ("Em sẽ tự pick task từ <code>docs/ROADMAP.md</code> → "
               "queue qua self_improve → refine prompt qua GPT-5.5 → "
               "Claude CLI edit + test + commit + push → loop. "
               "Hết quota thì pause 1h rồi probe lại tự resume. "
               "Chỉ dừng khi anh bảo dừng, hoặc 2 fail liên tiếp, "
               "hoặc hết hours."
               if auto_self_improve else
               "Em sẽ tự pick task → refine prompt qua GPT-5.5 → "
               "chạy Claude CLI → test → commit/push → report. "
               "Hết quota thì pause 1h rồi probe lại.")
            + "\n\nLệnh dừng: <code>/agent_autorun_stop</code> hoặc "
              "nhắn <i>“dừng”</i>.")


def handle_agent_autorun_stop() -> str:
    from bot.agent import agent_autorun as _aa
    d = _aa.stop(user="tg_admin", reason="user")
    return (f"🛑 <b>Agent Autorun stopped</b>\n"
            f"Đã xong: <b>{d.get('completed_tasks', 0)}</b> task")


def handle_agent_autorun_status() -> str:
    from bot.agent import agent_autorun as _aa
    return _aa.status_panel_vi()


# ── /agent_progress — current task progress ──────────────────────────────────

async def handle_agent_progress() -> str:
    """Vietnamese progress reporter — what's happening RIGHT NOW.

    Reads:
      - agent_autorun state (active task / paused_reason / stop_at)
      - brain_evolve state (run_count / last_status)
      - claude quota state (status / next_probe_at)
      - last queued / running code_task
      - last 3 audit lines
    """
    lines = ["<b>📊 Tiến độ agent</b>"]

    # 1. Autorun
    try:
        from bot.agent import agent_autorun as _aa
        d = _aa.state()
        if d.get("enabled"):
            lines.append(f"🟢 <b>Autorun đang chạy</b> ({d.get('hours', 0)}h)")
            lines.append(f"  ✓ Đã xong: <b>{d.get('completed_tasks', 0)}</b> "
                         f"/ <b>{d.get('max_tasks', 0)}</b> task")
            if d.get("paused_reason"):
                lines.append(f"  ⏸ Pause: <i>{_esc(str(d['paused_reason']))}</i>")
                if d.get("next_probe_at"):
                    lines.append(f"  thử lại: {_fmt_iso_local(d['next_probe_at'])}")
            if d.get("last_task_id"):
                lines.append(
                    f"  Task gần nhất: <code>{_esc(str(d['last_task_id']))}</code> "
                    f"<i>{_esc(str(d.get('last_status', '?')))}</i>")
            if d.get("stop_at"):
                lines.append(f"  Stop_at: {_fmt_iso_local(d['stop_at'])}")
        else:
            lines.append("⚪ Autorun: <b>off</b>")
    except Exception as e:
        lines.append(f"⚠ autorun: {_esc(str(e))[:80]}")

    # 2. Brain evolve
    try:
        from bot.agent import brain_evolve as _be
        bs = _be.state()
        if bs.get("enabled"):
            lines.append(f"🟢 <b>Brain Evolution active</b> "
                         f"run_count={bs.get('run_count', 0)} "
                         f"failures={bs.get('consecutive_failures', 0)}")
            if bs.get("last_status"):
                lines.append(f"  last_status: <i>{_esc(str(bs['last_status']))[:60]}</i>")
        else:
            lines.append(f"⚪ Brain Evolution: <b>off</b> "
                         f"(run_count={bs.get('run_count', 0)})")
    except Exception:
        pass

    # 3. Claude quota
    try:
        cqs = _cq.get_quota_state()
        st = cqs.get("status", "unknown")
        icon = {"available": "🟢", "limited": "🔴",
                "auth_required": "🔒", "error": "❓",
                "unknown": "⚪"}.get(st, "❓")
        lines.append(f"{icon} <b>Claude:</b> {st}")
        if cqs.get("next_probe_at"):
            lines.append(f"  next_probe: {_fmt_iso_local(cqs['next_probe_at'])}")
        if cqs.get("reset_at"):
            lines.append(f"  reset_at: {_fmt_iso_local(cqs['reset_at'])}")
    except Exception:
        pass

    # 4. Code queue snapshot — currently running / next queued
    # Plus a Claude/Codex process-alive indicator with elapsed time so
    # admin can tell at a glance whether the worker is genuinely
    # grinding vs the bridge has died. Without this, --print mode looks
    # static: the log file only gets the final response when Claude
    # exits, so panels appeared "đứng yên" mid-task.
    try:
        all_q = code_list_tasks(limit=200)
        running = [t for t in all_q if t.get("status") == "running"]
        queued = [t for t in all_q if t.get("status") == "queued"]
        # Probe live worker process(es) via pgrep — best-effort, never
        # raise. Returns list of (pid, etime_seconds, pcpu_str).
        worker_procs: list[tuple[str, int, str]] = []
        try:
            import subprocess as _sp
            r = _sp.run(
                ["ps", "-eo", "pid,etime,pcpu,comm,cmd"],
                capture_output=True, text=True, timeout=3,
            )
            for ln in (r.stdout or "").splitlines():
                if "claude" not in ln and "codex" not in ln:
                    continue
                if "--print" not in ln:
                    continue
                if "grep" in ln:
                    continue
                parts = ln.split(None, 4)
                if len(parts) < 4:
                    continue
                pid_, etime_, pcpu_, comm_ = parts[0], parts[1], parts[2], parts[3]
                # Parse etime: [[DD-]HH:]MM:SS
                secs = 0
                try:
                    et = etime_
                    if "-" in et:
                        days, rest = et.split("-", 1)
                        secs += int(days) * 86400
                        et = rest
                    bits = et.split(":")
                    bits = [int(b) for b in bits]
                    if len(bits) == 3:
                        secs += bits[0] * 3600 + bits[1] * 60 + bits[2]
                    elif len(bits) == 2:
                        secs += bits[0] * 60 + bits[1]
                    elif len(bits) == 1:
                        secs += bits[0]
                except Exception:
                    secs = 0
                worker_procs.append((pid_, secs, pcpu_))
        except Exception:
            pass
        if running:
            lines.append(f"<b>🔧 Đang chạy:</b>")
            for t in running[:2]:
                lines.append(f"  • <code>{_esc(str(t['id']))}</code> "
                             f"<i>{_esc((t.get('title') or '')[:60])}</i>")
        # Show live worker even if no task in DB is "running"
        # (the bridge may be between snapshots).
        if worker_procs:
            for pid_, secs, pcpu_ in worker_procs[:2]:
                m, s = divmod(secs, 60)
                h, m = divmod(m, 60)
                if h:
                    elapsed = f"{h}h {m}m {s}s"
                elif m:
                    elapsed = f"{m}m {s}s"
                else:
                    elapsed = f"{s}s"
                lines.append(
                    f"  ⚙ Claude PID <code>{pid_}</code> alive "
                    f"<b>{elapsed}</b> ({pcpu_}% CPU)")
        elif running:
            # Task in DB says running but no worker process exists →
            # auto-recover by resetting to queued so the next autorun
            # cycle / /code_worker_run_once picks it up cleanly.
            # Owner: "ko biết worker có đang chạy ko nữa" — fix is to
            # not just flag stale, but actively heal it.
            recovered: list[str] = []
            for t in running:
                tid = t.get("id", "")
                # Be conservative: only auto-reset if the running task
                # was started > 60s ago (so we don't race with a
                # bridge call that just flipped status=running but
                # hasn't spawned the subprocess yet).
                started = t.get("updated_at") or t.get("created_at") or ""
                age_ok = True
                try:
                    s = started
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt_started = datetime.fromisoformat(s)
                    if dt_started.tzinfo is None:
                        dt_started = dt_started.replace(tzinfo=timezone.utc)
                    age_sec = (datetime.now(timezone.utc) -
                               dt_started).total_seconds()
                    age_ok = age_sec > 60
                except Exception:
                    pass
                if age_ok and tid:
                    try:
                        from bot.code_tasks import update_task as _ut
                        _ut(tid, status="queued")
                        recovered.append(tid)
                    except Exception:
                        pass
            if recovered:
                lines.append(
                    f"  🩹 Auto-recovered {len(recovered)} stale task(s) "
                    f"→ <code>queued</code>: "
                    + ", ".join(f"<code>{_esc(t)}</code>" for t in recovered[:3]))
            else:
                lines.append("  ⚠ <i>Task DB =running nhưng không thấy "
                             "Claude/Codex process — chờ thêm để chắc</i>")
        nx = code_next_task() if queued else None
        if nx:
            lines.append(f"<b>📋 Next queued:</b> "
                         f"<code>{_esc(str(nx['id']))}</code> "
                         f"<i>{_esc((nx.get('title') or '')[:60])}</i>")
        if not running and not nx and not worker_procs:
            lines.append("💤 Không có task nào đang chạy / queued.")
    except Exception as e:
        lines.append(f"⚠ queue: {_esc(str(e))[:80]}")

    # 5. "Last activity" — answers owner's question: "có đang làm ko?"
    # Compute the freshest timestamp across:
    #   (a) most recent audit log entry
    #   (b) most recent code worker log file mtime (Claude is writing)
    #   (c) most recent task updated_at
    # Surface as "X giây/phút trước" so admin sees at a glance
    # whether anything moved in the last minute.
    try:
        latest_iso: str | None = None
        latest_label: str = ""

        def _maybe_update(iso: str | None, label: str) -> None:
            nonlocal latest_iso, latest_label
            if not iso:
                return
            if (latest_iso is None) or (iso > latest_iso):
                latest_iso = iso
                latest_label = label

        # (a) audit log — tail_audit returns newest LAST
        try:
            from bot.agent.audit_log import tail_audit
            audit_recent = tail_audit(3)
            if audit_recent:
                latest_audit = audit_recent[-1]
                _maybe_update(latest_audit.get("timestamp"),
                                f"audit: "
                                f"{latest_audit.get('action','?')[:30]}")
        except Exception:
            pass

        # (b) most recent code worker log file mtime
        try:
            from pathlib import Path as _P
            log_dir = _P("/opt/tiktok-bot/data/code_worker_logs")
            if log_dir.exists():
                files = sorted(log_dir.glob("*.log"),
                                key=lambda p: p.stat().st_mtime,
                                reverse=True)
                if files:
                    mtime = datetime.fromtimestamp(
                        files[0].stat().st_mtime, tz=timezone.utc)
                    _maybe_update(
                        mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        f"worker log: {files[0].name[:30]}")
        except Exception:
            pass

        # (c) most recent code_task updated_at
        try:
            tasks = code_list_tasks(limit=20)
            for t in tasks:
                u = t.get("updated_at") or t.get("created_at")
                if u:
                    _maybe_update(u,
                                    f"task {t.get('id','?')}: "
                                    f"{t.get('status','?')}")
                    break
        except Exception:
            pass

        if latest_iso:
            lines.append("")
            lines.append(
                f"<b>⏱ Hoạt động gần nhất:</b> "
                f"{_humanize_age(latest_iso)} "
                f"(<i>{_esc(latest_label[:60])}</i>)")
    except Exception:
        pass

    # 6. Last audit lines (history)
    try:
        from bot.agent.audit_log import tail_audit
        recent = tail_audit(3)
        if recent:
            lines.append("<b>📝 Audit gần nhất:</b>")
            for r in recent:
                lines.append(
                    f"  <code>{_esc(str(r.get('action', '')))[:25]}</code> "
                    f"{_esc(str(r.get('result_summary', '')))[:70]}")
    except Exception:
        pass

    lines.append("")
    lines.append("<i>Gợi ý: <code>/agent_diag</code> để xem state đầy đủ; "
                 "<code>/agent_autorun_status</code> để xem autorun.</i>")
    return "\n".join(lines)


# ── EsimAccess intents ───────────────────────────────────────────────────────

def handle_esim_docs() -> str:
    """Look up EsimAccess docs from memory + docs/ first. Don't make up."""
    from pathlib import Path as _P
    lines = ["<b>📚 EsimAccess docs (best-effort)</b>"]
    found_any = False

    # 1. memory search
    try:
        from bot.memory_store import search_memory as _sm
        rows = _sm("esimaccess esim access api docs", limit=5)
        if rows:
            lines.append("<b>Memory:</b>")
            for r in rows:
                title = (r.get("title") or "")[:60]
                lines.append(f"  • <code>id={r.get('id')}</code> "
                             f"<i>{_esc(title)}</i>")
            found_any = True
    except Exception:
        pass

    # 2. docs / research / data files
    for root in ("docs", "research", "data/docs"):
        try:
            p = _P("/opt/tiktok-bot") / root
            if not p.exists():
                continue
            hits = []
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                name = f.name.lower()
                if "esim" in name or "access" in name:
                    hits.append(str(f.relative_to("/opt/tiktok-bot")))
            if hits:
                lines.append(f"<b>{root}/:</b>")
                for h in hits[:8]:
                    lines.append(f"  • <code>{_esc(h)}</code>")
                found_any = True
        except Exception:
            pass

    if not found_any:
        lines.append("⚠ Em chưa tìm thấy docs EsimAccess trong "
                     "memory hoặc <code>docs/</code>.")
        lines.append("")
        lines.append("Để em tích hợp, anh gửi:")
        lines.append("  • Link API docs (ví dụ "
                     "<code>https://docs.esimaccess.com</code>)")
        lines.append("  • API key (em sẽ tự lưu vào <code>.env</code>, "
                     "không log/commit)")
        lines.append("  • Endpoint base URL")
        lines.append("")
        lines.append("Hoặc gõ <code>/build_missing_tool EsimAccess API "
                     "integration</code> để em tạo task code build "
                     "tool sẵn skeleton.")
    return "\n".join(lines)


def handle_esim_curl_dry(raw: str = "") -> str:
    """Build a dry-run curl template — does NOT call real API."""
    import os as _os
    base   = (_os.getenv("ESIM_ACCESS_API_BASE") or "").strip()
    key    = (_os.getenv("ESIM_ACCESS_API_KEY") or "").strip()
    has_key = bool(key)
    lines = ["<b>🧪 EsimAccess curl dry-run</b>"]
    lines.append("<i>Đây là template — KHÔNG chạy thật, không tạo order.</i>")
    if not base:
        lines.append("")
        lines.append("⚠ Thiếu <code>ESIM_ACCESS_API_BASE</code> trong "
                     "<code>.env</code>.")
    if not has_key:
        lines.append("⚠ Thiếu <code>ESIM_ACCESS_API_KEY</code> trong "
                     "<code>.env</code>.")
    lines.append("")
    lines.append("<b>Template:</b>")
    lines.append("<pre>")
    lines.append("# 1. List available eSIM packages (read-only)")
    lines.append(f"curl -s '{base or '<base_url>'}/packages' \\")
    lines.append("  -H 'Authorization: Bearer <KEY>' \\")
    lines.append("  -H 'Content-Type: application/json' \\")
    lines.append("  --get --data-urlencode 'country=JP'")
    lines.append("")
    lines.append("# 2. Dry-run order (only if API supports `dry_run=true`)")
    lines.append(f"curl -s '{base or '<base_url>'}/orders' \\")
    lines.append("  -H 'Authorization: Bearer <KEY>' \\")
    lines.append("  -H 'Content-Type: application/json' \\")
    lines.append("  -d '{\"package_id\":\"<id>\",\"dry_run\":true}'")
    lines.append("</pre>")
    if not (base and has_key):
        lines.append("")
        lines.append("⚠ <b>Thiếu config</b>. Em chỉ in template — chưa "
                     "thể chạy thật. Cung cấp endpoint + key để em "
                     "tích hợp đầy đủ (em sẽ KHÔNG echo key ra chat).")
    return "\n".join(lines)


def handle_esim_order_real_pending(raw: str, chat_id) -> dict:
    """Create a pending_action for a REAL eSIM order. Caller (telegram_bot
    NL dispatch) must invoke _ask_confirm_action with the payload returned."""
    return {
        "action": "esim_order_real",
        "goal":   raw[:300],
        "pretty": ("Việc này sẽ <b>gọi API EsimAccess thật</b> và "
                   "<b>tạo order mất tiền</b>. Không hoàn lại được.\n"
                   "Em đang ở chế độ <b>chỉ chạy sau khi anh bấm "
                   "✅ Đồng ý</b>. Bấm ❌ Hủy nếu chưa muốn."),
        "payload": {"esim_order_real": True, "raw": raw},
    }


# ── Web search / browser intents ─────────────────────────────────────────────

async def handle_web_search_nl(query: str) -> str:
    """Best-effort: route through existing search backend. If query is empty,
    return guidance instead of failing silently."""
    q = (query or "").strip()
    if not q:
        return ("Cú pháp: <i>“search web &lt;keyword&gt;”</i>. "
                "Ví dụ: <i>“search web nhà cung cấp eSIM Nhật”</i>.")
    try:
        # Reuse the legacy search routing inside handle_run_task
        return await handle_run_task(f"tìm thông tin {q}")
    except Exception as e:
        return (f"⚠ Search lỗi: {_esc(str(e))[:120]}\n"
                f"<i>Có thể thiếu key search engine. Em sẽ "
                f"tạo code_task build search worker tốt hơn nếu cần — "
                f"gõ <code>/build_missing_tool web_search v2</code>.</i>")


def handle_browser_task(raw: str) -> str:
    """Browser/Playwright tasks — currently no live worker; surface this
    honestly and propose code_task to build it."""
    # Probe Playwright availability — don't fail; just report.
    try:
        import playwright  # noqa: F401
        playwright_ok = True
    except Exception:
        playwright_ok = False

    lines = ["<b>🌐 Browser task</b>"]
    if playwright_ok:
        lines.append("✅ Playwright đã cài. Browser worker chưa wire vào "
                     "task queue.")
        lines.append("Em đang tạo code_task để build browser worker.")
        title = "Browser worker (Playwright) — implement and wire to queue"
    else:
        lines.append("⚠ Playwright chưa cài. Em sẽ tạo code_task: "
                     "(1) cài Playwright, (2) build browser worker.")
        title = "Browser worker — install Playwright + implement worker"
    try:
        tid = code_add_task(
            title=title,
            description=f"Owner asked: {raw[:200]}\n\n"
                        f"Build a Playwright-based browser worker that can "
                        f"read URLs and extract structured data. Wire to "
                        f"task queue. Add deterministic eval cases.",
            risk_level="medium", priority=5, created_by="tg_admin_nl",
        )
        lines.append(f"📋 Đã tạo task: <code>{tid}</code>")
    except Exception as e:
        lines.append(f"⚠ Không tạo được task: {_esc(str(e))[:120]}")
    return "\n".join(lines)


# ── System action handlers (apt/restart/git/secret) ──────────────────────────

def _has_grant(scope_needed: str = "low_medium") -> bool:
    """Check if current session has scope_needed grant."""
    try:
        from bot.agent.sessions import current_session
        s = current_session()
        if not s:
            return False
        sc = s.get("scope") or ""
        return scope_needed in sc or sc == "low_medium" or sc == "code_low_medium"
    except Exception:
        return False


def handle_system_pkg_install(raw: str) -> str:
    """apt install / pip install — check grant, else create code_task."""
    if _has_grant("low_medium"):
        # Owner has session grant — surface a code_task that the bridge
        # can run. We don't directly subprocess from Telegram handler.
        try:
            tid = code_add_task(
                title=f"[apt/pkg] {raw[:80]}",
                description=f"Owner via Telegram: {raw[:200]}\n\n"
                            f"Run package install in a controlled subprocess. "
                            f"Audit log the result. Do NOT run with sudo "
                            f"unless explicit.",
                risk_level="medium", priority=4,
                created_by="tg_admin_nl",
            )
            return (f"📦 Đã queue task package-install: "
                    f"<code>{tid}</code>\n"
                    f"<i>(Có grant — bridge sẽ chạy khi worker pick.)</i>")
        except Exception as e:
            return f"⚠ {_esc(str(e))[:120]}"
    return ("📦 <b>Cài package</b> — cần grant trước.\n"
            "Gõ <code>/grant_session low_medium 60</code> rồi nhắc lại "
            "yêu cầu, hoặc gõ <code>/confirm_action latest</code> sau "
            "khi tạo pending_action.")


def handle_system_restart(raw: str) -> str:
    """systemctl restart — check grant, else create code_task."""
    if _has_grant("low_medium"):
        try:
            tid = code_add_task(
                title=f"[restart] {raw[:80]}",
                description=f"Owner via Telegram: {raw[:200]}\n\n"
                            f"Restart the relevant service via systemctl. "
                            f"Verify status afterward.",
                risk_level="medium", priority=4,
                created_by="tg_admin_nl",
            )
            return (f"🔁 Đã queue task restart: <code>{tid}</code>\n"
                    f"<i>(Có grant — bridge sẽ chạy.)</i>")
        except Exception as e:
            return f"⚠ {_esc(str(e))[:120]}"
    return ("🔁 <b>Restart service</b> — cần grant trước.\n"
            "Gõ <code>/grant_session low_medium 30</code> rồi nhắc lại.")


def handle_system_git_push(raw: str) -> str:
    """commit + push dev-agent — check grant, else create code_task."""
    if _has_grant("low_medium"):
        try:
            tid = code_add_task(
                title="[git] commit + push dev-agent",
                description=f"Owner via Telegram: {raw[:200]}\n\n"
                            f"Run git status, stage explicit paths "
                            f"(bot docs README.md .gitignore scripts), "
                            f"create commit, push to origin/dev-agent "
                            f"with inline-token URL. Reset remote URL.",
                risk_level="medium", priority=3,
                created_by="tg_admin_nl",
            )
            return (f"📦 Đã queue task commit+push: <code>{tid}</code>")
        except Exception as e:
            return f"⚠ {_esc(str(e))[:120]}"
    return ("📦 <b>Commit + push dev-agent</b> — cần grant.\n"
            "Gõ <code>/grant_session code_low_medium 30</code> rồi "
            "nhắc lại.")


def handle_system_secret_dump(raw: str) -> str:
    """Refuse to dump secret values; offer safe alternative."""
    return ("🔒 <b>Em không dump giá trị secret ra chat.</b>\n"
            "Em sẽ KHÔNG <code>cat .env</code> hay echo key vào "
            "Telegram (rule trong <code>OPERATING_RULES.md §1</code>).\n\n"
            "<b>Lựa chọn an toàn:</b>\n"
            "  • <i>Xem tên các key đang có:</i> "
            "<code>grep -E '^[A-Z_]+=' .env | cut -d= -f1</code> "
            "(em chạy được nếu có grant).\n"
            "  • <i>Xem key cụ thể có set chưa:</i> nói "
            "<code>“key X có set không”</code> — em trả set/unset, "
            "không trả giá trị.\n"
            "  • <i>Edit qua Telegram:</i> chưa support — phải SSH "
            "vào VPS với key auth.")


# ── /agent_diag — single-screen diagnostic ───────────────────────────────────

# ── Self-introspection handlers ──────────────────────────────────────────────

async def handle_whoami_model() -> str:
    """Report which models are active (chat/coding/etc) using live
    /router_status data. Adds the actual_model from claude_quota probe."""
    lines = ["<b>🤖 Model đang dùng</b>"]
    # Live /router_status
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{BACKEND}/router_status")
        d = r.json() if r.status_code == 200 else {}
        rms = d.get("role_models", {})
        lines.append("<b>Roles (live từ 9Router):</b>")
        for role, label in [
            ("chat",            "chat / Telegram / TikTok"),
            ("telegram_chat",   "telegram_chat"),
            ("tiktok_chat",     "tiktok_chat"),
            ("search_summary",  "search_summary"),
            ("reasoning",       "reasoning"),
            ("coding",          "coding (qua llm_client API)"),
            ("critic",          "critic"),
            ("vision",          "vision (OCR / image)"),
            ("cheap",           "cheap / fallback"),
        ]:
            v = rms.get(role, "?")
            lines.append(f"  • {label}: <code>{_esc(v)}</code>")
    except Exception as e:
        lines.append(f"  <i>router_status lỗi: {_esc(str(e))[:120]}</i>")

    # Claude CLI worker model (from env + last probe)
    try:
        from bot.coding_worker_bridge import (CLAUDE_CODE_MODEL,
                                                CLAUDE_FALLBACK_MODEL,
                                                get_preferred_coding_tool)
        cli_state = (_cq.get_quota_state() or {}).get("status", "unknown")
        actual = (_cq.get_quota_state() or {}).get("actual_model", "")
        tool = get_preferred_coding_tool()
        lines.append("")
        lines.append("<b>Coding worker (Claude CLI):</b>")
        lines.append(f"  • primary:  <code>{_esc(CLAUDE_CODE_MODEL)}</code>")
        lines.append(f"  • fallback: <code>{_esc(CLAUDE_FALLBACK_MODEL)}</code>")
        if tool:
            lines.append(f"  • binary:   <code>{_esc(tool.binary)}</code> "
                         f"({_esc(tool.version)})")
        lines.append(f"  • status:   <b>{_esc(cli_state)}</b>")
        if actual:
            lines.append(f"  • last_probe_model: <code>{_esc(actual)}</code>")
    except Exception as e:
        lines.append(f"  <i>CLI lookup lỗi: {_esc(str(e))[:120]}</i>")
    return "\n".join(lines)


async def handle_whoami_runtime() -> str:
    """Explain the dual-runtime architecture in Vietnamese."""
    lines = [
        "<b>🧩 Runtime kiến trúc</b>",
        "",
        "<b>1. Telegram chat / TikTok / search → 9Router</b>",
        "  Đi qua <code>bot/llm_client.complete(role=...)</code> "
        "(HTTP API, async).",
        "  Roles dùng <code>cx/gpt-5.5</code> (chat / tiktok_chat / "
        "telegram_chat / search_summary).",
        "  Backend xử lý: <code>backend/server.py /message</code>.",
        "",
        "<b>2. Coding tasks → Claude CLI (Opus 4.7)</b>",
        "  Đi <b>trực tiếp</b> qua local Claude CLI binary "
        "(<code>~/.local/bin/claude --print</code>), KHÔNG qua 9Router.",
        "  Module: <code>bot/coding_worker_bridge.py</code>. "
        "Spawn <code>sudo -u levanrin2404 -H env "
        "ANTHROPIC_MODEL=opus claude --model opus "
        "--fallback-model sonnet --print &lt; prompt.md</code>.",
        "  Quota tracking: <code>bot/claude_quota.py</code> probe + "
        "error parser + 1h backoff.",
        "",
        "<b>3. OCR / Vision → 9Router (openai/gpt-4o)</b>",
        "  Module: <code>bot/ocr.py</code>, "
        "<code>llm_client.complete(role='vision')</code>.",
        "",
        "<b>Lý do dùng cả hai:</b>",
        "  • cx/gpt-5.5 nhanh + rẻ cho chat / tóm tắt / sales consult.",
        "  • Claude Opus 4.7 mạnh nhất cho coding self-improvement.",
        "  • Tách 2 đường giúp chat không tốn quota Claude và ngược lại.",
    ]
    # Append live status
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{BACKEND}/router_status")
        d = r.json() if r.status_code == 200 else {}
        if d.get("reachable"):
            lines.append("")
            lines.append(f"<i>9Router live: ✅ reachable ({d.get('model_count','?')} models)</i>")
    except Exception:
        pass
    cqs = _cq.get_quota_state() or {}
    lines.append(f"<i>Claude CLI: {cqs.get('status', 'unknown')}</i>")
    return "\n".join(lines)


async def handle_recent_activity() -> str:
    """Summarise last N audit-log entries + last code_task done +
    brain_evolve last action."""
    lines = ["<b>📋 Hoạt động gần đây</b>"]
    # Last 5 audit lines
    try:
        from bot.agent.audit_log import tail_audit
        recent = tail_audit(8)
        if recent:
            lines.append("<b>Audit (8 mới nhất):</b>")
            for a in reversed(recent):  # newest first
                ts     = (a.get("timestamp") or "")[:16].replace("T", " ")
                action = _esc((a.get("action") or "")[:30])
                summary = _esc((a.get("result_summary") or "")[:80])
                risk   = a.get("risk_level", "low")
                icon   = {"low": "🟢", "medium": "🟡",
                          "high": "🔴"}.get(risk, "⚪")
                lines.append(f"  {icon} <code>{action}</code> "
                             f"<i>{ts}</i> — {summary}")
    except Exception as e:
        lines.append(f"  <i>audit_log lỗi: {_esc(str(e))[:80]}</i>")

    # Last code_task done
    try:
        done = code_list_tasks(status="done", limit=1)
        if done:
            t = done[0]
            lines.append("")
            lines.append("<b>Code task done gần nhất:</b>")
            lines.append(f"  ✅ <code>{t['id']}</code> — "
                         f"{_esc((t.get('title') or '')[:60])}")
            if t.get("commit_hash"):
                lines.append(f"  commit <code>{_esc(t['commit_hash'])}</code>")
            if t.get("test_summary"):
                lines.append(f"  <i>{_esc(t['test_summary'][:120])}</i>")
    except Exception:
        pass

    # brain_evolve
    try:
        from bot.agent import brain_evolve as _be
        s = _be.state()
        lines.append("")
        lines.append(f"<b>Brain evolve:</b> "
                     f"{'🟢 enabled' if s.get('enabled') else '⚪ stopped'} "
                     f"· run_count={s.get('run_count', 0)}")
        if s.get("last_status"):
            lines.append(f"  last_status: <i>{_esc(str(s['last_status']))[:80]}</i>")
        if s.get("last_summary"):
            lines.append(f"  <i>{_esc(s['last_summary'][:120])}</i>")
    except Exception:
        pass
    return "\n".join(lines)


async def handle_next_mission() -> str:
    """Vietnamese roadmap+queue overview — what's next on the agent's plate."""
    lines = ["<b>➡ Mục tiêu tiếp theo</b>"]
    # Roadmap unchecked items
    try:
        roadmap_path = Path("/opt/tiktok-bot/docs/ROADMAP.md")
        if roadmap_path.exists():
            txt = roadmap_path.read_text(encoding="utf-8")
            import re as _re
            done    = len(_re.findall(r"^- \[x\] ", txt, _re.MULTILINE))
            pending = _re.findall(r"^- \[ \] (.+)$", txt, _re.MULTILINE)
            lines.append(f"<b>Roadmap:</b> {done} done · {len(pending)} pending")
            for i, item in enumerate(pending[:5], 1):
                lines.append(f"  {i}. {_esc(item[:120])}")
            if not pending:
                lines.append("  <i>(roadmap đang sạch — chưa có item unchecked)</i>")
        else:
            lines.append("<i>docs/ROADMAP.md chưa có.</i>")
    except Exception as e:
        lines.append(f"<i>roadmap lỗi: {_esc(str(e))[:80]}</i>")

    # Top queued code_tasks
    try:
        queued = code_list_tasks(status="queued", limit=3)
        if queued:
            lines.append("")
            lines.append("<b>🛠 Code task đang queue:</b>")
            risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}
            for t in queued:
                ic = risk_icon.get(t.get("risk_level", "low"), "⚪")
                lines.append(f"  {ic} <code>{t['id']}</code> "
                             f"p{t.get('priority',5)} "
                             f"{_esc((t.get('title') or '')[:60])}")
        else:
            lines.append("")
            lines.append("<i>(chưa có code_task nào trong queue)</i>")
    except Exception:
        pass

    # Brain evolve hint
    try:
        from bot.agent import brain_evolve as _be
        s = _be.state()
        if s.get("enabled"):
            lines.append("")
            lines.append("<i>🟢 Brain evolution loop đang chạy — "
                         "sẽ tự pick task tiếp theo khi đủ điều kiện.</i>")
        else:
            lines.append("")
            lines.append("<i>Brain evolve hiện đã tắt. Gõ "
                         "<code>“tự cải thiện brain đi”</code> để bật.</i>")
    except Exception:
        pass
    return "\n".join(lines)


async def handle_whoami() -> str:
    """Mission statement + architecture summary in Vietnamese."""
    lines = [
        "<b>🤖 Em là gì</b>",
        "",
        "Em là <b>Business Agent Platform</b> của muaesim.vn / "
        "Chatgibiti — một AI agent đang tự train để trở thành "
        "<b>agent thông minh nhất cho doanh nghiệp eSIM Nhật Bản</b>.",
        "",
        "<b>Mission:</b>",
        "  • Trả lời TikTok DM (Chatgibiti) bằng product DB đã verify.",
        "  • Quản lý catalog eSIM + lead + consulting log qua Telegram.",
        "  • Self-improve liên tục: planner → code task → Claude Opus → "
        "smoke + evals → commit → push.",
        "",
        "<b>Học từ:</b> OpenClaw (gateway hub), LangGraph (durable "
        "graph), OpenHands (coding worker loop), CrewAI (role-based "
        "agents). Xem <code>research/agents/AGENT_FRAMEWORK_STUDY.md</code>.",
        "",
        "<b>Kiến trúc 2 đường:</b>",
        "  1. Chat / search / sales → 9Router <code>cx/gpt-5.5</code>",
        "  2. Coding self-improve → Claude CLI <code>opus</code> "
        "(fallback <code>sonnet</code>)",
        "",
        "<b>Quy tắc owner-tooling:</b>",
        "  • Tool có sẵn → chạy ngay (low/medium auto, high cần "
        "✅ Đồng ý).",
        "  • Tool thiếu → tạo code_task để build, không refuse.",
        "  • Không bao giờ post / DM / merge main / xóa data tự động.",
        "",
        "<b>Câu lệnh hữu ích:</b>",
        "  /agent_diag, /agent_status, /claude_status, "
        "/code_status, /brain_evolve_status, /workers_remote",
    ]
    return "\n".join(lines)


async def handle_agent_diag() -> str:
    """Vietnamese single-screen diagnostic. Always returns a non-empty
    answer even if some subsystem is broken.  No await on the long
    Claude probe — we only read cached state to avoid blocking."""
    lines = ["<b>🩺 Agent Diag</b>"]
    lines.append("Telegram bot: <b>alive</b>")

    try:
        all_q = code_list_tasks(limit=200)
        by_status: dict[str, int] = {}
        for t in all_q:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        nx = code_next_task()
        lines.append("<b>Code queue:</b> " + " · ".join(
            f"{k}={v}" for k, v in sorted(by_status.items())))
        if nx:
            lines.append(f"  Next queued: <code>{nx['id']}</code> — "
                         f"{_esc((nx.get('title') or '')[:50])}")
    except Exception as e:
        lines.append(f"<b>Code queue:</b> ❌ {_esc(str(e))[:120]}")

    try:
        from bot.coding_worker_bridge import (is_paused as _bridge_paused,
                                                recent_logs as _recent_logs)
        bp = _bridge_paused()
        cp = code_is_paused()
        lines.append(f"<b>Worker:</b> bridge="
                     f"{'⏸ paused' if bp else '▶ active'} · "
                     f"queue={'⏸ paused' if cp else '▶ active'}")
        logs = _recent_logs(2)
        if logs:
            lines.append("  Recent logs: " +
                          ", ".join(f"<code>{p.name}</code>" for p in logs))
    except Exception as e:
        lines.append(f"<b>Worker:</b> ❌ {_esc(str(e))[:120]}")

    try:
        from bot.agent import brain_evolve as _be
        s = _be.state()
        lines.append(f"<b>Brain evolve:</b> "
                     f"{'🟢 enabled' if s.get('enabled') else '⚪ stopped'} "
                     f"· run_count={s.get('run_count', 0)} "
                     f"· consec_failures={s.get('consecutive_failures', 0)}")
        if s.get("last_status"):
            lines.append(f"  last_status: "
                         f"<i>{_esc(str(s['last_status']))[:80]}</i>")
    except Exception as e:
        lines.append(f"<b>Brain evolve:</b> ❌ {_esc(str(e))[:120]}")

    # Claude — read cache only; don't fire a fresh probe (avoid blocking)
    try:
        cqs = _cq.get_quota_state()
        lines.append(f"<b>Claude:</b> status="
                     f"<b>{cqs.get('status', 'unknown')}</b>")
        if cqs.get("reset_at"):
            lines.append(f"  reset_at: <code>{cqs['reset_at']}</code>")
        if cqs.get("next_probe_at"):
            lines.append(f"  next_probe: <code>{cqs['next_probe_at']}</code>")
        if cqs.get("autorun"):
            lines.append(f"  autorun: <b>on</b> "
                         f"(max={cqs.get('max_tasks', 1)})")
        if cqs.get("last_error_summary"):
            lines.append(f"  <i>{_esc(cqs['last_error_summary'][:140])}</i>")
    except Exception as e:
        lines.append(f"<b>Claude:</b> ❌ {_esc(str(e))[:120]}")

    try:
        from bot.agent.permissions import list_pending
        pa = list_pending(only_pending=True)
        lines.append(f"<b>Pending actions:</b> {len(pa)}")
        for p in pa[:3]:
            lines.append(f"  • <code>{p['action_id']}</code> "
                         f"{_esc(p.get('action',''))[:40]}")
    except Exception as e:
        lines.append(f"<b>Pending actions:</b> ❌ {_esc(str(e))[:120]}")

    try:
        import subprocess as _sp
        r = _sp.run(["git", "-C", "/opt/tiktok-bot", "status", "--short"],
                    capture_output=True, text=True, timeout=5)
        dirty = bool(r.stdout.strip())
        lines.append(f"<b>Dirty tree:</b> "
                     f"{'⚠ yes' if dirty else '✅ no'}")
        if dirty:
            for ln in r.stdout.strip().splitlines()[:3]:
                lines.append(f"  <code>{_esc(ln[:80])}</code>")
    except Exception as e:
        lines.append(f"<b>Dirty tree:</b> ❌ {_esc(str(e))[:80]}")

    try:
        from bot.remote_workers import list_workers
        rws = list_workers()
        lines.append(f"<b>Remote workers:</b> {len(rws)} đăng ký")
        for w in rws[:3]:
            lines.append(f"  • <code>{w['id']}</code> "
                         f"{_esc(w.get('username',''))}@"
                         f"{_esc(w.get('host',''))}")
    except Exception as e:
        lines.append(f"<b>Remote workers:</b> ❌ {_esc(str(e))[:120]}")

    try:
        from bot.agent.audit_log import tail_audit
        recent = tail_audit(3)
        if recent:
            lines.append("<b>Audit gần nhất:</b>")
            for r in recent:
                lines.append(f"  <code>{_esc(r.get('action',''))[:30]}</code> "
                             f"{_esc(r.get('result_summary',''))[:80]}")
    except Exception:
        pass

    lines.append("")
    lines.append("<b>Gợi ý lệnh:</b>")
    lines.append("  /code_status · /claude_status · /pending_actions "
                 "· /code_worker_resume · /claude_probe "
                 "· /code_worker_run_once")
    return "\n".join(lines)


# ── Remote workers / SSH ──────────────────────────────────────────────────────

def handle_workers_remote() -> str:
    from bot.remote_workers import format_workers_list_vi
    return format_workers_list_vi()


def handle_worker_info(arg: str) -> str:
    from bot.remote_workers import format_worker_info_vi
    arg = arg.strip()
    if not arg:
        return "Usage: /worker_info &lt;worker_id&gt;"
    return format_worker_info_vi(arg)


def handle_worker_add(spec: str) -> str:
    """/worker_add <id> <host> <user> <key_path> [port] [tags=a,b]"""
    parts = spec.strip().split()
    if len(parts) < 4:
        return ("Usage: /worker_add &lt;id&gt; &lt;host&gt; &lt;user&gt; "
                "&lt;key_path&gt; [port] [tag1,tag2]\n"
                "Key file phải nằm trong "
                "<code>/opt/tiktok-bot/keys/</code> và chmod 600.\n"
                "Ví dụ: <code>/worker_add worker2 1.2.3.4 ubuntu "
                "/opt/tiktok-bot/keys/worker2_id_ed25519 22 ocr</code>")
    wid, host, user, key_path = parts[:4]
    port = 22
    tags: list[str] = []
    for extra in parts[4:]:
        if extra.isdigit():
            port = int(extra)
        else:
            tags = [t.strip() for t in extra.split(",") if t.strip()]
    from bot.remote_workers import add_worker
    ok, why = add_worker(worker_id=wid, host=host, username=user,
                          key_path=key_path, port=port, tags=tags)
    if not ok:
        return f"❌ Không thêm được worker: {_esc(why)}"
    log_action(user="tg_admin", action="worker_add", risk_level="medium",
               status="ok", result_summary=f"id={wid} host={host}")
    return (f"✅ Đã thêm worker <code>{wid}</code> "
            f"({user}@{host}:{port}). Test: "
            f"<code>/worker_test {wid}</code>")


async def handle_worker_test(arg: str) -> str:
    from bot.remote_workers import ssh_exec, get_worker, format_ssh_result_vi
    arg = arg.strip()
    if not arg:
        return "Usage: /worker_test &lt;worker_id&gt;"
    wid = arg.split()[0]
    if not get_worker(wid):
        return (f"❓ Worker <code>{_esc(wid)}</code> chưa đăng ký. "
                f"Gõ <code>/workers_remote</code> để xem danh sách hoặc "
                f"<code>/worker_add</code> để thêm.")
    # ssh_exec uses subprocess.run with up to 30s timeout. Run in
    # a worker thread so Telegram polling stays responsive.
    r = await asyncio.to_thread(ssh_exec, wid, "uptime", user="tg_admin")
    return format_ssh_result_vi(wid, "uptime", r)


async def handle_ssh_exec(spec: str) -> str:
    """/ssh_exec <worker_id> <command>"""
    parts = spec.strip().split(None, 1)
    if len(parts) < 2:
        return ("Usage: /ssh_exec &lt;worker_id&gt; &lt;command&gt;\n"
                "Lệnh low-risk chạy ngay; medium/high cần xác nhận.")
    wid, cmd = parts[0], parts[1]
    from bot.remote_workers import (ssh_exec, get_worker, classify_ssh_command,
                                       format_ssh_result_vi)
    if not get_worker(wid):
        return f"❓ Worker <code>{_esc(wid)}</code> chưa đăng ký."
    risk, reason = classify_ssh_command(cmd)
    # If high → ask confirm via inline buttons (payload carries cmd).
    if risk == "high":
        return await _ask_confirm_action(
            chat_id=int(TG_ADMIN),
            action="ssh_exec_high_risk",
            goal=f"ssh {wid} {cmd[:200]}",
            pretty=(f"Chạy SSH high-risk trên <code>{wid}</code>:\n"
                    f"<code>{_esc(cmd[:160])}</code>\n"
                    f"<i>Lý do: {_esc(reason[:120])}</i>"),
            payload={"ssh_exec": True,
                     "worker_id": wid,
                     "command": cmd,
                     "risk_override": "high"},
        )
    if risk == "blocked":
        return (f"🛑 Lệnh bị chặn cứng (không cho confirm bypass).\n"
                f"<i>{_esc(reason)}</i>")
    # low/medium → run with audit
    r = ssh_exec(wid, cmd, user="tg_admin")
    return format_ssh_result_vi(wid, cmd, r)


# ── Owner-tooling doctrine: "build a tool" intent ────────────────────────────

def _ensure_remote_workers_tool_present() -> bool:
    """True if the remote-worker tool is already in the repo."""
    return Path("/opt/tiktok-bot/bot/remote_workers.py").exists()


def _existing_tools_inventory() -> dict[str, bool]:
    """Lightweight check of which capability tools are present."""
    return {
        "remote_workers (SSH)":   _ensure_remote_workers_tool_present(),
        "claude_quota":           Path("/opt/tiktok-bot/bot/claude_quota.py").exists(),
        "coding_worker_bridge":   Path("/opt/tiktok-bot/bot/coding_worker_bridge.py").exists(),
        "brain_evolve":           Path("/opt/tiktok-bot/bot/agent/brain_evolve.py").exists(),
        "memory_store":           Path("/opt/tiktok-bot/bot/memory_store.py").exists(),
        "telegram_files (file hub)": Path("/opt/tiktok-bot/bot/telegram_files.py").exists(),
    }


def handle_build_missing_tool(description: str) -> str:
    """Owner-tooling doctrine: when admin asks for a capability:
       1. If the tool already exists, point them at it.
       2. Else, queue a code_task to build it. Never refuse generically.
    """
    desc = description.strip()
    # Heuristic: if user mentioned 'ssh' / 'remote worker' / 'vps', the tool
    # already exists (we just shipped it).
    inv = _existing_tools_inventory()
    low = desc.lower()
    matches: list[str] = []
    if any(k in low for k in ("ssh", "remote worker", "vps", "kết nối vps")):
        if inv["remote_workers (SSH)"]:
            matches.append("remote_workers (SSH)")
    if any(k in low for k in ("ocr", "vision")):
        # OCR doesn't exist yet
        pass
    if matches:
        return (f"✅ Tool đã có trong repo: <b>{', '.join(matches)}</b>.\n\n"
                f"Dùng ngay:\n"
                f"• <code>/workers_remote</code> — xem worker đã đăng ký\n"
                f"• <code>/worker_add &lt;id&gt; &lt;host&gt; &lt;user&gt; "
                f"&lt;key_path&gt;</code> — thêm worker mới\n"
                f"• <code>/worker_test &lt;id&gt;</code> — kiểm tra "
                f"liveness (uptime)\n"
                f"• <code>/ssh_exec &lt;id&gt; &lt;cmd&gt;</code> — chạy "
                f"lệnh (low-risk auto, high-risk hỏi)")

    # Dedup: if a recent queued/running task has nearly the same title,
    # don't create another one — point at the existing task.
    try:
        existing_q = code_list_tasks(status="queued", limit=20)
        existing_r = code_list_tasks(status="running", limit=5)
        for t in existing_q + existing_r:
            tt = (t.get("title") or "").lower()
            if tt and (desc[:30].lower() in tt or tt[:30] in desc.lower()):
                return (f"📋 Task tương tự đã có trong queue: "
                        f"<code>{t['id']}</code> — "
                        f"{_esc((t.get('title') or '')[:60])}\n"
                        f"<i>Trạng thái: {t.get('status','?')}</i>\n\n"
                        f"Gõ <i>“làm tiếp task code tiếp theo”</i> để chạy.")
    except Exception:
        pass

    # Otherwise queue a build task
    title = (f"Build tool: {desc[:60]}").strip()
    description_full = (
        f"Owner yêu cầu thêm tool / capability mới:\n\n"
        f"{desc}\n\n"
        f"Spec gợi ý:\n"
        f"- Đặt module trong bot/ hoặc bot/tools/.\n"
        f"- Tích hợp NL intent vào bot/agent/nl_router.py.\n"
        f"- Thêm Telegram command + handler trong bot/telegram_bot.py.\n"
        f"- Risk policy + audit log + redaction.\n"
        f"- Eval mới trong bot/agent/evals.py.\n"
        f"- Không touch .env/storage_state/main branch.\n"
        f"- Chạy scripts/smoke_test.sh trước commit."
    )
    tid = code_add_task(title=title, description=description_full,
                        risk_level="medium", priority=6,
                        created_by="tg_admin_doctrine")
    # Build prompt eagerly so the worker has it ready
    try:
        from bot.agent.prompt_builder import (build_coding_prompt,
                                                save_prompt_for_task)
        t = code_get_task(tid)
        if t:
            save_prompt_for_task(tid, build_coding_prompt(t))
    except Exception:
        pass

    # Tell admin exactly why the worker won't auto-run yet
    reasons: list[str] = []
    try:
        from bot.coding_worker_bridge import is_paused as _bp, get_preferred_coding_tool
        if _bp():
            reasons.append("bridge đang pause — gõ "
                           "<code>/code_worker_resume</code> để bật lại")
        if code_is_paused():
            reasons.append("queue đang pause")
        tool = get_preferred_coding_tool()
        if not tool:
            reasons.append("Claude/Codex CLI chưa có trên PATH")
    except Exception:
        pass
    try:
        cqs = _cq.get_quota_state()
        if cqs.get("status") == "limited":
            reasons.append(f"Claude limited — sẽ thử lại lúc "
                           f"{cqs.get('reset_at') or cqs.get('next_probe_at') or '?'}")
        elif cqs.get("status") == "auth_required":
            reasons.append("Claude cần <code>claude login</code> lại")
    except Exception:
        pass

    autorun_hint = ""
    if reasons:
        autorun_hint = ("\n\n⚠ Worker chưa tự chạy được vì:\n• "
                        + "\n• ".join(reasons))
    else:
        autorun_hint = ("\n\n✅ Worker sẵn sàng — gõ "
                        "<i>“làm tiếp task code tiếp theo”</i> hoặc "
                        "<code>/code_worker_run_once</code>.")

    return (f"🛠 <b>Tool này chưa có, em sẽ tạo task để tích hợp.</b>\n"
            f"Đã queue code task <code>{tid}</code>. Trạng thái: queued.\n"
            f"<i>{_esc(desc[:120])}</i>"
            + autorun_hint)


async def handle_claude_probe() -> str:
    """Force a fresh Claude probe and return the rich Vietnamese status.

    The probe spawns the Claude CLI synchronously (up to 60s). We run
    it in a thread so the asyncio event loop / Telegram polling stays
    responsive while the probe is in flight.
    """
    try:
        await asyncio.to_thread(_cq.probe_claude_available, True)
    except Exception as e:
        return f"❌ Probe lỗi: <code>{_esc(str(e))[:200]}</code>"
    return _cq.format_claude_status_vi()


def handle_claude_quota_reset(arg: str) -> str:
    if not arg.strip():
        return ("Usage: /claude_quota_reset &lt;YYYY-MM-DD HH:MM&gt; "
                "(UTC)\nExample: <code>/claude_quota_reset 2026-05-02 14:30</code>")
    try:
        d = _cq.set_reset_at(arg.strip())
    except Exception as e:
        return f"❌ {e}"
    return _cq.status_summary()


def handle_claude_quota_in(arg: str) -> str:
    if not arg.strip():
        return ("Usage: /claude_quota_in &lt;30m|2h|3h30m&gt;\n"
                "Example: <code>/claude_quota_in 2h30m</code>")
    try:
        d = _cq.set_reset_in(arg.strip())
    except Exception as e:
        return f"❌ {e}"
    return _cq.status_summary()


def handle_claude_autorun_on(arg: str) -> str:
    arg = arg.strip()
    try:
        n = int(arg) if arg else 1
    except ValueError:
        n = 1
    _cq.set_autorun(True, max_tasks=n)
    return _cq.status_summary()


def handle_code_task_from_file(spec: str) -> str:
    """/code_task_from_file <file_id> <description>"""
    parts = spec.strip().split(None, 1)
    if len(parts) < 2:
        return ("Usage: /code_task_from_file &lt;file_id&gt; &lt;description&gt;")
    fid, desc = parts[0], parts[1]
    rec = get_file_record(fid)
    if not rec:
        return f"❌ File <code>{_esc(fid)}</code> not found."
    excerpt = ""
    try:
        from pathlib import Path as _P
        p = _P(rec["local_path"])
        if p.exists() and p.stat().st_size <= 50_000:
            ok, content = read_text_file(rec["local_path"])
            if ok:
                excerpt = content[:8000]
    except Exception:
        pass
    description = (
        f"User attached file <{rec['filename']}> "
        f"(file_id={fid}, path={rec['local_path']}).\n\n"
        f"Task: {desc}\n\n"
    )
    if excerpt:
        description += "File excerpt (first 8KB):\n```\n" + excerpt + "\n```"
    else:
        description += (f"File too large or non-text — inspect path "
                        f"{rec['local_path']} during the task.")
    tid = code_add_task(title=f"[file] {desc[:80]}",
                        description=description, risk_level="medium",
                        priority=5, created_by="tg_admin")
    return (f"✅ Code task <code>{tid}</code> queued from file "
            f"<code>{fid}</code>.")


async def handle_run_task_with_file(spec: str) -> str:
    """/run_task_with_file <file_id> <goal>  — run goal as a /run_task,
    passing the file path to the runner via the goal text."""
    parts = spec.strip().split(None, 1)
    if len(parts) < 2:
        return "Usage: /run_task_with_file &lt;file_id&gt; &lt;goal&gt;"
    fid, goal = parts[0], parts[1]
    rec = get_file_record(fid)
    if not rec:
        return f"❌ File <code>{_esc(fid)}</code> not found."
    augmented = f"{goal}\n\n[attached file: {rec['local_path']}]"
    return await handle_run_task(augmented)


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
    """Suggest the next 3 missions based on the roadmap, plus the top
    queued code_tasks the worker would pick up."""
    lines: list[str] = []

    # ── Roadmap (top-3 unchecked) ─────────────────────────────────────
    p = Path("/opt/tiktok-bot/docs/ROADMAP.md")
    if p.exists():
        txt = p.read_text(encoding="utf-8")
        import re
        done    = len(re.findall(r"^- \[x\] ", txt, re.MULTILINE))
        pending = re.findall(r"^- \[ \] (.+)$", txt, re.MULTILINE)
        lines.append(f"<b>➡ Next missions</b> "
                     f"<i>({done} done · {len(pending)} pending)</i>")
        if pending:
            for i, it in enumerate(pending[:3], 1):
                lines.append(f"{i}. {_esc(it)}")
        else:
            lines.append("<i>No unchecked items in ROADMAP.md.</i>")
    else:
        lines.append("<b>➡ Next missions</b>")
        lines.append("<i>ROADMAP.md not available.</i>")

    # ── Top queued code_tasks (worker pickup order) ───────────────────
    try:
        queued = code_list_tasks(status="queued", limit=3)
    except Exception:
        queued = []
    if queued:
        lines.append("")
        lines.append("<b>🛠 Worker queue (top 3)</b>")
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}
        for t in queued:
            ic = risk_icon.get(t.get("risk_level", "low"), "⚪")
            title = _esc((t.get("title") or "")[:48])
            lines.append(f"  {ic} <code>{t['id']}</code> "
                         f"p{t['priority']} {title}")
    else:
        lines.append("")
        lines.append("<i>(No queued code_tasks. Use /code_task to add one.)</i>")

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
        if ftype == "photo" or Path(record["local_path"]).suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".webp", ".gif"):
            from bot.ocr import ocr_image as _ocr_run
            res = await _ocr_run(record["local_path"], user="tg_admin")
            if res.get("ok"):
                txt = (res.get("text") or "").strip() or "(không phát hiện text)"
                snippet = txt[:1800] + ("\n…[truncated]" if len(txt) > 1800 else "")
                rd = int(res.get("redactions") or 0)
                rd_note = f" 🛡 {rd}" if rd else ""
                reply += (f"\n\n<b>OCR result</b>"
                          f" (<i>{res.get('chars', 0)} chars, "
                          f"{res.get('model', '')}</i>){rd_note}\n"
                          f"<pre>{_esc(snippet)}</pre>")
                create_task("ocr_image", f"OCR ảnh {filename}",
                            status="done", input_files=[record["local_path"]])
            else:
                reply += f"\n\n❌ OCR lỗi: {_esc(res.get('error', ''))[:200]}"
        else:
            reply += "\n\n⚠️ OCR chỉ chạy trên ảnh (png/jpg/webp/gif)."
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
        # Any other confirm:<id> → treat as pending-action confirm
        # AND execute the stored after_confirm payload.
        from bot.agent.permissions import confirm_pending, get_pending
        action_id = val
        rec = get_pending(action_id)
        if not rec:
            await send_chat_reply(
                chat_id,
                f"❌ Hành động <code>{action_id}</code> không tồn tại "
                "hoặc đã hết hạn.",
            )
            return
        if rec.get("status") != "pending":
            await send_chat_reply(
                chat_id,
                f"⚠️ Hành động <code>{action_id}</code> đã ở trạng thái "
                f"<b>{rec.get('status')}</b>, không thể duyệt lại.",
            )
            return
        confirm_pending(action_id)
        log_action(user="tg_admin", action="nl_confirm_action",
                   risk_level="high", status="confirmed",
                   result_summary=f"action_id={action_id}")
        try:
            msg = await _execute_after_confirm(rec, chat_id)
        except Exception as e:
            import traceback as _tb
            log(f"error handler=execute_after_confirm pid={action_id} "
                f"message={e}\n{_tb.format_exc()[:300]}")
            msg = (f"❌ Đã duyệt <code>{action_id}</code> nhưng thực thi "
                   f"lỗi: <code>{_esc(str(e))[:200]}</code>")
        await send_chat_reply(chat_id, msg)
        return

    if kind == "cancel":
        # cancel:<action_id> from inline button.
        from bot.agent.permissions import cancel_pending, get_pending
        action_id = val
        rec = get_pending(action_id)
        if not rec:
            await send_chat_reply(
                chat_id,
                f"❌ Hành động <code>{action_id}</code> không tồn tại.",
            )
            return
        cancel_pending(action_id)
        log_action(user="tg_admin", action="nl_cancel_action",
                   risk_level="low", status="cancelled",
                   result_summary=f"action_id={action_id}")
        await send_chat_reply(
            chat_id,
            f"🚫 Đã hủy <code>{action_id}</code> "
            f"({rec.get('action','?')}).",
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
    if action == "agent_progress":
        return await handle_agent_progress()
    if action == "autorun_stop":
        return handle_agent_autorun_stop()
    if action == "autorun_status":
        return handle_agent_autorun_status()
    if action == "claude_probe":
        return await handle_claude_probe()
    if action == "claude_status":
        return _cq.format_claude_status_vi()
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
    if cmd == "/send_photo":   return await handle_send_photo(arg, chat_id)
    if cmd == "/ocr":          return await handle_ocr(arg, chat_id)
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
    if cmd == "/code_worker_run_once": return await handle_code_run_once(chat_id)
    if cmd == "/code_worker_run_batch": return await handle_code_run_batch(arg, chat_id)
    if cmd == "/code_worker_status":   return handle_code_worker_status()
    if cmd == "/code_worker_pause":
        code_pause(); _cwb.pause()
        return "⏸ Code worker + bridge paused."
    if cmd == "/code_worker_resume":
        code_resume(); _cwb.resume()
        return "▶ Code worker + bridge resumed."
    # ── Claude quota scheduler ────────────────────────────────────────────
    if cmd == "/claude_status":        return _cq.format_claude_status_vi()
    if cmd == "/claude_probe":         return await handle_claude_probe()
    # ── Brain Evolution Loop ──────────────────────────────────────────────
    if cmd == "/brain_evolve_start":   return await handle_brain_evolve_start(arg)
    if cmd == "/brain_evolve_stop":    return handle_brain_evolve_stop()
    if cmd == "/brain_evolve_status":  return handle_brain_evolve_status()
    # ── Agent Autorun (10–12h owner work session) ────────────────────────
    if cmd == "/agent_autorun_start":  return await handle_agent_autorun_start(arg, chat_id=chat_id)
    if cmd == "/agent_autorun_stop":   return handle_agent_autorun_stop()
    if cmd == "/agent_autorun_status": return handle_agent_autorun_status()
    if cmd == "/agent_progress":       return await handle_agent_progress()
    # ── EsimAccess shortcuts ─────────────────────────────────────────────
    if cmd == "/esim_docs":            return handle_esim_docs()
    if cmd == "/esim_curl_dry":        return handle_esim_curl_dry(arg)
    if cmd == "/agent_diag":           return await handle_agent_diag()
    if cmd == "/whoami":               return await handle_whoami()
    if cmd == "/whoami_model":         return await handle_whoami_model()
    if cmd == "/whoami_runtime":       return await handle_whoami_runtime()
    if cmd == "/recent":               return await handle_recent_activity()
    if cmd == "/next_mission":         return await handle_next_mission()
    if cmd == "/roadmap":              return await handle_next_mission()
    # ── Remote workers ───────────────────────────────────────────────────
    if cmd == "/workers_remote":   return handle_workers_remote()
    if cmd == "/worker_add":       return handle_worker_add(arg)
    if cmd == "/worker_info":      return handle_worker_info(arg)
    if cmd == "/worker_test":      return await handle_worker_test(arg)
    if cmd == "/worker_health":    return await handle_worker_test(arg)  # alias
    if cmd == "/ssh_exec":         return await handle_ssh_exec(arg)
    if cmd == "/claude_quota_reset":   return handle_claude_quota_reset(arg)
    if cmd == "/claude_quota_in":      return handle_claude_quota_in(arg)
    if cmd == "/claude_limited":       _cq.set_limited(True);  return _cq.status_summary()
    if cmd == "/claude_available":     _cq.set_limited(False); return _cq.status_summary()
    if cmd == "/claude_autorun_on":    return handle_claude_autorun_on(arg)
    if cmd == "/claude_autorun_off":   _cq.set_autorun(False); return _cq.status_summary()
    # ── Autonomy + file-hub additions ─────────────────────────────────────
    if cmd == "/agent_autonomy_status": return await handle_agent_autonomy_status()
    if cmd == "/code_task_from_file":   return handle_code_task_from_file(arg)
    if cmd == "/run_task_with_file":    return await handle_run_task_with_file(arg)
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
            "/agent_progress — current task progress\n"
            "/memory_search &lt;q&gt; /memory_add /memory_forget /memory_compact /memory_context\n"
            "/products /product_add /product_update /consult &lt;q&gt; "
            "/leads /lead &lt;id&gt; /lead_add /followups\n"
            "/start /help /menu /cancel"
        )

    # Smart routing for plain text
    # 1) Vietnamese natural-language file send: "gửi ROADMAP cho t".
    #    Deterministic match — no LLM round-trip; falls through to
    #    chat if no pattern hits.
    # ── Vietnamese natural-language router ──────────────────────────────
    # Step 1: Legacy file-send picker (high-precision for "gửi file X").
    try:
        from bot.agent.nl_router import detect_intent, handle_send_file_intent
        nl = detect_intent(text)
        if nl and nl.get("intent") == "send_file":
            return await handle_send_file_intent(nl, chat_id)
    except Exception as e:
        log(f"nl_router send-file error: {e}")

    # Step 2: Full classifier (run/create/quota/permissions/status/list/...)
    try:
        from bot.agent.nl_router import classify
        intent = classify(text)
        log(f"nl_intent={intent.name} conf={intent.confidence:.2f} "
            f"risk={intent.risk_level} req_confirm={intent.requires_confirm}")
        if intent.name not in ("chat", "unknown"):
            handled = await _handle_nl_intent(intent, chat_id, text)
            if handled is not None:
                return handled
    except Exception as e:
        import traceback as _tb
        log(f"nl_router classify error: {e}\n{_tb.format_exc()[:300]}")

    # Step 3: legacy task-shaped trigger (long imperative sentences)
    if _looks_like_task(text):
        return await handle_run_task(text)

    # Step 4: default — backend chat
    return await handle_message_backend(text)


async def _handle_nl_intent(intent, chat_id, raw_text: str):
    """Dispatch a v2 nl_router.Intent into the right action.

    Returns a Vietnamese reply string, or None to fall through to chat.
    All replies go via send_chat_reply (NEW message), never edit the
    menu panel.
    """
    name = intent.name

    # ── Run / batch via the bridge — NON-BLOCKING ─────────────────────────
    # Bridge runs (Claude CLI 1-3 min) used to block the dispatch loop and
    # silence the bot. Now we ack immediately and run in background so
    # the polling loop continues to handle new admin messages.
    if name == "run_next_code_task":
        try:
            nx = code_next_task()
            if nx:
                ack = (f"🛠 Đã nhận lệnh. Bridge bắt đầu chạy task "
                       f"<code>{_esc(str(nx['id']))}</code>:\n"
                       f"<i>{_esc((nx.get('title') or '')[:80])}</i>\n\n"
                       f"Em báo khi xong. Trong lúc đó bro cứ chat — "
                       f"bot không bị block.")
            else:
                ack = "💤 Không có task nào queued."
        except Exception:
            ack = "🛠 Bridge bắt đầu… em báo khi xong."
        await send_chat_reply(chat_id, ack)
        asyncio.create_task(
            _bridge_run_in_background(chat_id, n=1,
                                       formatter=_vi_format_run_result),
        )
        return _REPLY_HANDLED

    if name == "run_code_batch":
        n = int(intent.args.get("n") or 1)
        await send_chat_reply(
            chat_id,
            f"🛠 Bridge bắt đầu batch <b>{n}</b> task code. "
            f"Em báo khi xong.",
        )
        asyncio.create_task(
            _bridge_run_in_background(chat_id, n=n,
                                       formatter=_vi_format_run_result),
        )
        return _REPLY_HANDLED

    # ── Create code task from a Vietnamese description ────────────────────
    if name == "create_code_task":
        desc = (intent.args.get("description") or raw_text)[:1000]
        if intent.requires_confirm:
            # High-risk → ask confirm. Carry the full execution payload so
            # _execute_after_confirm can enqueue the task once approved.
            return await _ask_confirm_action(
                chat_id,
                action="create_code_task_high_risk",
                goal=desc,
                pretty=("Task có yếu tố rủi ro cao "
                        "(.env / storage_state / public action). "
                        "Cho phép tạo code task không?"),
                payload={
                    "create_code_task": True,
                    "description":      desc,
                    "risk_level":       intent.risk_level or "high",
                    "priority":         5,
                    "auto_run":         False,
                },
            )
        title = desc.split("\n", 1)[0][:80] or "Coding task"
        tid = code_add_task(title=title, description=desc,
                            risk_level=intent.risk_level,
                            priority=5, created_by="tg_admin_nl")
        # 1. Build deterministic prompt synchronously (so worker has
        #    something ready immediately).
        # 2. Schedule async LLM refinement via 9Router/GPT-5.5+ in the
        #    background — overwrites the saved prompt on success, leaves
        #    the deterministic prompt intact on failure.
        prompt_status = "deterministic"
        try:
            from bot.agent.prompt_builder import (
                build_coding_prompt, save_prompt_for_task,
                refine_prompt_via_llm,
            )
            t = code_get_task(tid)
            if t:
                prompt = build_coding_prompt(t)
                save_prompt_for_task(tid, prompt)

                async def _refine_in_bg(tid_: str, det_prompt: str) -> None:
                    try:
                        refined, status = await refine_prompt_via_llm(
                            det_prompt, timeout_s=30.0,
                        )
                        if status == "refined":
                            save_prompt_for_task(tid_, refined)
                            log(f"prompt refined task={tid_} "
                                f"len={len(refined)}")
                            try:
                                await send_chat_reply(
                                    chat_id,
                                    f"🪄 Prompt task <code>{tid_}</code> "
                                    f"đã được refine qua 9Router. "
                                    f"Bridge sẽ dùng bản refined.",
                                )
                            except Exception:
                                pass
                        else:
                            log(f"prompt refine fallback task={tid_} "
                                f"status={status}")
                    except Exception as e:
                        log(f"prompt refine error task={tid_}: {e}")

                # Fire-and-forget
                asyncio.create_task(_refine_in_bg(tid, prompt))
                prompt_status = "deterministic+refining"
        except Exception as e:
            log(f"prompt build warning: {e}")
        return (f"✅ Đã tạo task code <code>{tid}</code> "
                f"(risk={intent.risk_level}, prompt={prompt_status})\n"
                f"<b>{_esc(title)}</b>\n\n"
                f"Em đang gọi <b>GPT-5.5 qua 9Router</b> để refine "
                f"prompt thành tiếng Anh sạch — sẽ báo khi xong.\n"
                f"Bridge sẽ chạy khi mình ra lệnh "
                f"<i>“làm tiếp task code tiếp theo”</i>, hoặc gõ "
                f"<code>/code_worker_run_once</code>.")

    # ── Remote workers / SSH ─────────────────────────────────────────────
    if name == "remote_worker_list":
        return handle_workers_remote()
    if name == "remote_worker_health":
        wid = (intent.args.get("worker_id") or "").strip()
        if not wid:
            return ("Cú pháp: nói rõ id worker. Ví dụ: "
                    "<i>“worker2 còn sống không”</i>.")
        return await handle_worker_test(wid)
    if name == "ssh_exec":
        wid = (intent.args.get("worker_id") or "").strip()
        cmd = (intent.args.get("command")   or "").strip()
        if not wid or not cmd:
            return "Cú pháp: <i>“ssh worker2 uptime”</i>"
        return await handle_ssh_exec(f"{wid} {cmd}")
    if name == "build_missing_tool":
        return handle_build_missing_tool(
            intent.args.get("description") or raw_text,
        )

    # ── OCR ──────────────────────────────────────────────────────────────
    if name == "ocr_image":
        target = (intent.args.get("target") or "").strip()
        if not target:
            return ("Cú pháp: <code>/ocr &lt;file_id|path&gt;</code> hoặc "
                    "<i>“ocr &lt;file_id&gt;”</i>. Lấy file_id từ "
                    "<code>/files</code>.")
        return await handle_ocr(target, chat_id)

    # ── Brain Evolution Loop ─────────────────────────────────────────────
    if name == "brain_evolve_start":
        n = int(intent.args.get("max_tasks") or 1)
        return await handle_brain_evolve_start(str(n))
    if name == "brain_evolve_stop":
        return handle_brain_evolve_stop()
    if name == "brain_evolve_status":
        return handle_brain_evolve_status()

    # ── Agent Autorun (10–12h owner work session) ────────────────────────
    if name == "agent_autorun_start":
        hrs       = float(intent.args.get("hours")     or 12.0)
        max_tasks = int(intent.args.get("max_tasks")   or 20)
        objective = (intent.args.get("objective") or raw_text)[:300]
        is_self_improve = bool(intent.args.get("auto_self_improve"))
        return await handle_agent_autorun_start(
            f"{hrs} {max_tasks} {objective}".strip(),
            auto_self_improve=is_self_improve,
            chat_id=chat_id,
        )
    if name == "agent_autorun_stop":
        return handle_agent_autorun_stop()
    if name == "agent_autorun_status":
        return handle_agent_autorun_status()
    if name == "agent_progress":
        return await handle_agent_progress()

    # ── EsimAccess intents ───────────────────────────────────────────────
    if name == "esim_docs":
        return handle_esim_docs()
    if name == "esim_curl_dry":
        return handle_esim_curl_dry(raw_text)
    if name == "esim_order_real":
        spec = handle_esim_order_real_pending(raw_text, chat_id)
        return await _ask_confirm_action(
            chat_id,
            action=spec["action"],
            goal=spec["goal"],
            pretty=spec["pretty"],
            payload=spec["payload"],
        )

    # ── Web search / browser ─────────────────────────────────────────────
    if name == "web_search_nl":
        return await handle_web_search_nl(intent.args.get("query") or raw_text)
    if name == "browser_task":
        return handle_browser_task(intent.args.get("raw") or raw_text)

    # ── System action intents (apt/restart/git/secret/network) ───────────
    if name == "system_network_action":
        # Always confirm — owner must approve public/network change.
        return await _ask_confirm_action(
            chat_id,
            action="system_network_action",
            goal=(intent.args.get("raw") or raw_text)[:300],
            pretty=("Việc này thuộc <b>high-risk public/network</b> "
                    "(mở port / firewall / expose internet). "
                    "Em chỉ chạy sau khi anh bấm ✅ Đồng ý."),
            payload={"system_action": True,
                     "raw": intent.args.get("raw") or raw_text},
        )
    if name == "system_secret_dump":
        return handle_system_secret_dump(
            intent.args.get("raw") or raw_text,
        )
    if name == "system_pkg_install":
        return handle_system_pkg_install(
            intent.args.get("raw") or raw_text,
        )
    if name == "system_restart":
        return handle_system_restart(
            intent.args.get("raw") or raw_text,
        )
    if name == "system_git_push":
        return handle_system_git_push(
            intent.args.get("raw") or raw_text,
        )

    # ── Memory NL ────────────────────────────────────────────────────────
    if name == "memory_add":
        content = intent.args.get("content") or raw_text
        # Reuse the existing handler; expects "title | content | tags" or
        # plain content. Pass plain content — it will use first 60 chars
        # as title.
        return await handle_memory_add(content)
    if name == "memory_search":
        return await handle_memory_search(intent.args.get("query") or raw_text)
    if name == "memory_forget":
        target = (intent.args.get("target") or "").strip()
        if target.isdigit():
            return handle_memory_forget(target)
        return ("Để xóa memory, gõ <code>/memory_forget &lt;id&gt;</code> "
                "(lấy id từ <i>tìm trong memory ...</i>).")

    # ── Self-improve once ────────────────────────────────────────────────
    if name == "self_improve":
        return await handle_self_improve_once()

    # ── Show files ────────────────────────────────────────────────────────
    if name == "show_files":
        return handle_files_list()

    # ── Quota schedule ───────────────────────────────────────────────────
    if name == "quota_schedule":
        mins      = int(intent.args.get("minutes") or 0)
        max_tasks = int(intent.args.get("max_tasks") or 0)
        replies = []
        if mins > 0:
            try:
                _cq.set_reset_in(f"{mins}m")
                replies.append(f"🕒 Đã hẹn Claude reset sau <b>{mins} phút</b>.")
            except Exception as e:
                replies.append(f"❌ Không hẹn được: {e}")
        else:
            try:
                _cq.set_limited(True)
                replies.append("🚫 Đã đánh dấu Claude đang hết quota "
                               "(chưa biết lúc reset — gửi /claude_quota_in nếu biết).")
            except Exception as e:
                replies.append(f"❌ {e}")
        if max_tasks > 0:
            try:
                _cq.set_autorun(True, max_tasks=max_tasks)
                replies.append(f"▶ Bật autorun: chạy tối đa "
                               f"<b>{max_tasks}</b> task khi quota về.")
            except Exception as e:
                replies.append(f"❌ autorun: {e}")
        replies.append("")
        replies.append(_cq.status_summary())
        return "\n".join(replies)

    if name == "quota_clear":
        try:
            _cq.set_limited(False)
            # Force a fresh probe so /claude_status reflects reality.
            try:
                if _cq.should_probe_now():
                    await asyncio.to_thread(
                        _cq.probe_claude_available, True,
                    )
            except Exception:
                pass
            return ("✅ Đã gỡ flag <b>limited</b>. Claude sẽ probe "
                    "lại — kết quả thật:\n\n"
                    + _cq.format_claude_status_vi())
        except Exception as e:
            return f"⚠ Không gỡ được: {_esc(str(e))[:120]}"

    if name == "quota_status":
        # Force a fresh probe so the answer reflects reality. Run the
        # blocking subprocess in a thread to keep Telegram responsive.
        try:
            if _cq.should_probe_now():
                await asyncio.to_thread(_cq.probe_claude_available, False)
        except Exception:
            pass

        # Yes/no preface — when the admin asked a yes/no question
        # ("claude đã limit chưa", "claude còn xài được không"), lead
        # with a one-line direct answer in Vietnamese before the panel.
        rt = (raw_text or "").lower()
        is_yesno = any(k in rt for k in
                       ("chưa", "không", "available", "ok không", "ổn không"))
        prefix = ""
        if is_yesno:
            try:
                st = (_cq.get_quota_state() or {}).get("status", "unknown")
            except Exception:
                st = "unknown"
            limited_q = ("limit" in rt or "hết" in rt or "quota" in rt)
            available_q = ("xài" in rt or "dùng" in rt or "chạy" in rt
                           or "available" in rt or "ok" in rt
                           or "ổn" in rt or "rảnh" in rt or "free" in rt
                           or "work" in rt or "hoạt" in rt)
            if st == "limited":
                if limited_q:
                    prefix = "🔴 <b>Đã limit rồi.</b> "
                elif available_q:
                    prefix = "🔴 <b>Chưa xài được — Claude đang limit.</b> "
            elif st == "available":
                if limited_q:
                    prefix = "🟢 <b>Chưa limit — Claude đang xài bình thường.</b> "
                elif available_q:
                    prefix = "🟢 <b>Còn xài được — Claude available.</b> "
            elif st == "auth_required":
                prefix = ("🔒 <b>Cần đăng nhập lại Claude</b> "
                          "(<code>claude login</code>). ")
            elif st in ("error", "unknown"):
                prefix = ("❓ <b>Chưa biết chắc</b> — chạy "
                          "<code>/claude_probe</code> để probe ngay. ")
        return prefix + ("\n\n" if prefix else "") + _cq.format_claude_status_vi()

    # ── Permission grant / revoke ────────────────────────────────────────
    if name == "grant_permission":
        scope = intent.args.get("scope") or "low_medium"
        mins  = int(intent.args.get("minutes") or 30)
        return handle_grant_session(f"{scope} {mins}")

    if name == "revoke_permission":
        return handle_revoke_session()

    # ── Confirm / cancel via plain Vietnamese ────────────────────────────
    if name == "confirm_action":
        return await _confirm_latest_pending(chat_id)
    if name == "cancel_action":
        return await _cancel_latest_pending(chat_id)

    # ── Status / list / search / receive_file_context ────────────────────
    if name == "agent_diag":
        return await handle_agent_diag()
    if name == "whoami_model":
        return await handle_whoami_model()
    if name == "whoami_runtime":
        return await handle_whoami_runtime()
    if name == "recent_activity":
        return await handle_recent_activity()
    if name == "next_mission":
        return await handle_next_mission()
    if name == "whoami":
        return await handle_whoami()
    if name == "status":
        return await handle_agent_status()
    if name == "list_tasks":
        return code_format_list(limit=15)
    if name == "search":
        # Reuse run_task search routing
        return await handle_run_task(intent.args.get("query") or raw_text)

    return None


def _vi_format_run_result(r: dict) -> str:
    """Vietnamese version of coding_worker_bridge.format_run_result."""
    icon = {
        "done":             "✅",
        "pending_action":   "⏸",
        "no_tool":          "❌",
        "interactive_only": "⚠",
        "paused":           "⏯",
        "rejected":         "🚫",
        "worker_failed":    "💥",
        "smoke_failed":     "🚫",
        "evals_failed":     "🚫",
        "no_changes":       "💤",
        "noop":             "💤",
        "exec_error":       "💥",
        "commit_failed":    "💥",
        "push_failed":      "💥",
        "no_push_token":    "🔒",
        "blocked_staged":   "🚫",
        "dry_run":          "🔍",
    }.get(r.get("status", ""), "•")
    status_vi = {
        "done":             "Xong",
        "pending_action":   "Cần xác nhận",
        "no_tool":          "Chưa cài Claude/Codex CLI",
        "interactive_only": "CLI cần TTY (chạy thủ công)",
        "paused":           "Bridge/worker đang pause",
        "rejected":         "Bị từ chối",
        "worker_failed":    "Worker lỗi",
        "smoke_failed":     "Smoke test fail",
        "evals_failed":     "Evals fail",
        "no_changes":       "Worker không thay đổi gì",
        "noop":             "Không có task nào để chạy",
        "exec_error":       "Lỗi exec",
        "commit_failed":    "Commit fail",
        "push_failed":      "Push fail",
        "no_push_token":    "Thiếu GITHUB_TOKEN",
        "blocked_staged":   "Có file cấm staged",
        "dry_run":          "Dry run",
    }.get(r.get("status", ""), r.get("status", "?"))
    parts = [f"{icon} <b>{status_vi}</b>"]
    if r.get("task_id"):
        parts.append(f"task: <code>{r['task_id']}</code>")
    if r.get("tool"):
        parts.append(f"tool: <code>{r['tool']}</code>")
    if r.get("commit"):
        parts.append(f"📦 commit: <code>{r['commit']}</code> đẩy lên dev-agent")
    if r.get("pending_action_id"):
        parts.append(f"⏸ pending: <code>{r['pending_action_id']}</code> "
                     "<i>(gõ “đồng ý” để duyệt)</i>")
    if r.get("log_path"):
        from pathlib import Path as _P
        parts.append(f"log: <code>{_P(r['log_path']).name}</code>")
    if r.get("duration_sec") is not None:
        parts.append(f"thời gian: {r['duration_sec']}s")
    if r.get("summary"):
        parts.append("")
        parts.append(_esc(r["summary"]))
    return "\n".join(parts)


# ── Vietnamese inline-confirm helper ──────────────────────────────────────────

# Sentinel: paths that have already sent the final reply to the user
# return this string. bot_loop checks for it and skips the duplicate
# send_chat_reply / "empty reply fallback".
_REPLY_HANDLED = "\x00__REPLY_HANDLED__\x00"


async def _ask_confirm_action(chat_id, *, action: str, goal: str,
                                pretty: str,
                                payload: Optional[dict] = None) -> str:
    """Create a pending_action and send Yes/No buttons in Vietnamese.

    `payload` is stored under metadata["after_confirm"] and read back by
    `_execute_after_confirm` when the admin taps ✅ Đồng ý / sends "đồng ý".
    Without a payload, confirm only marks the action approved — no side
    effect.
    """
    from bot.agent.permissions import create_pending
    metadata: dict = {"source": "nl_router"}
    if payload:
        metadata["after_confirm"] = payload
    pid = create_pending(action=action, goal=goal[:1000],
                          risk_level="high", user="tg_admin",
                          metadata=metadata)
    kb = make_keyboard([
        [("✅ Đồng ý", f"confirm:{pid}"),
         ("❌ Hủy",     f"cancel:{pid}")],
    ])
    text_html = (f"⚠️ <b>Việc này thuộc high-risk nên cần anh bấm "
                 f"Đồng ý trước khi chạy.</b>\n"
                 f"{_esc(pretty)}\n\n"
                 f"<i>id: <code>{pid}</code></i>")
    await send_chat_reply(chat_id, text_html, kb)
    return _REPLY_HANDLED   # signal: do NOT send a duplicate / fallback


# ── Execute payload after a pending_action is approved ──────────────────────

async def _execute_after_confirm(rec: dict, chat_id) -> str:
    """Run the side-effect of an approved pending_action.

    Returns a Vietnamese reply describing what happened. Always returns
    a non-empty message — never silently empty.
    """
    action  = rec.get("action", "")
    payload = (rec.get("metadata") or {}).get("after_confirm") or {}

    # ── create_code_task_high_risk → enqueue the actual code_task ────────
    if action == "create_code_task_high_risk" or payload.get("create_code_task"):
        desc      = payload.get("description") or rec.get("goal") or ""
        if not desc:
            return ("✅ Đã duyệt nhưng pending_action không có description "
                    "để tạo code_task. Hãy gửi lại task.")
        risk      = payload.get("risk_level") or rec.get("risk_level") or "high"
        priority  = int(payload.get("priority") or 5)
        title     = (desc.split("\n", 1)[0] or "Coding task")[:80]
        try:
            tid = code_add_task(
                title=title, description=desc,
                risk_level=risk, priority=priority,
                created_by="tg_admin_confirmed",
            )
        except Exception as e:
            return (f"❌ Đã duyệt <code>{rec['action_id']}</code> nhưng "
                    f"tạo code_task lỗi: <code>{_esc(str(e))[:200]}</code>")
        # Build prompt best-effort
        try:
            from bot.agent.prompt_builder import (build_coding_prompt,
                                                    save_prompt_for_task)
            t = code_get_task(tid)
            if t:
                save_prompt_for_task(tid, build_coding_prompt(t))
        except Exception as e:
            log(f"after_confirm prompt build warning: {e}")
        log_action(user="tg_admin",
                   action="confirm_create_code_task_high_risk",
                   risk_level="high", status="ok",
                   result_summary=f"task={tid} risk={risk}")
        msg = (f"✅ Đã duyệt <code>{rec['action_id']}</code>.\n"
               f"📦 Đã tạo code task <code>{tid}</code> "
               f"(risk={risk}, priority={priority}).\n"
               f"<b>{_esc(title)}</b>\n\n"
               f"Gõ <i>“làm tiếp task code tiếp theo”</i> để chạy worker, "
               f"hoặc <code>/code_worker_run_once</code>.")
        if payload.get("auto_run"):
            msg += ("\n\n<i>auto_run đã bật, nhưng task high-risk vẫn cần "
                    "xác nhận thêm trước khi worker chạy.</i>")
        return msg

    # ── ssh_exec_high_risk → execute the approved SSH command ────────────
    if action == "ssh_exec_high_risk" or payload.get("ssh_exec"):
        wid = payload.get("worker_id", "")
        cmd = payload.get("command", "")
        if not wid or not cmd:
            return ("✅ Đã duyệt nhưng payload thiếu worker_id/command. "
                    "Hãy thử lại với <code>/ssh_exec</code>.")
        from bot.remote_workers import (ssh_exec, format_ssh_result_vi)
        # Pass risk_override=high so the executor doesn't gate again.
        r = ssh_exec(wid, cmd, user="tg_admin", risk_override="high")
        return ("✅ Đã duyệt — đã chạy:\n\n" +
                format_ssh_result_vi(wid, cmd, r))

    # ── grant_permission with payload ────────────────────────────────────
    if action == "grant_permission" and payload.get("scope"):
        try:
            from bot.agent.sessions import grant_session
            mins = int(payload.get("minutes") or 30)
            grant_session(payload["scope"], mins, user="tg_admin")
            return (f"✅ Đã duyệt <code>{rec['action_id']}</code>.\n"
                    f"Cấp quyền <b>{_esc(payload['scope'])}</b> trong "
                    f"<b>{mins}</b> phút.")
        except Exception as e:
            return (f"❌ Đã duyệt nhưng cấp session lỗi: "
                    f"<code>{_esc(str(e))[:200]}</code>")

    # ── Generic / no payload: just acknowledge ───────────────────────────
    return (f"✅ Đã duyệt <code>{rec['action_id']}</code> "
            f"({_esc(action)}). "
            f"<i>Không có payload tự động — thực thi thủ công nếu cần.</i>")


async def _confirm_latest_pending(chat_id) -> str:
    from bot.agent.permissions import list_pending, confirm_pending, get_pending
    items = list_pending(only_pending=True)
    if not items:
        return "(Không có hành động nào đang chờ xác nhận.)"
    if len(items) > 1:
        ids = ", ".join(f"<code>{p['action_id']}</code>" for p in items[:5])
        return ("Có nhiều hành động đang chờ:\n" + ids +
                "\nGõ <code>/confirm_action &lt;id&gt;</code> cho từng cái.")
    p = items[0]
    # Read fresh record (with metadata.after_confirm) before flipping state
    rec = get_pending(p["action_id"]) or p
    confirm_pending(p["action_id"])
    log_action(user="tg_admin", action="nl_confirm_action",
               risk_level="high", status="confirmed",
               result_summary=f"action_id={p['action_id']}")
    # Execute the stored payload (e.g. enqueue the code_task)
    return await _execute_after_confirm(rec, chat_id)


async def _cancel_latest_pending(chat_id) -> str:
    from bot.agent.permissions import list_pending, cancel_pending
    items = list_pending(only_pending=True)
    if not items:
        return "(Không có hành động nào đang chờ.)"
    if len(items) > 1:
        ids = ", ".join(f"<code>{p['action_id']}</code>" for p in items[:5])
        return ("Có nhiều hành động đang chờ:\n" + ids +
                "\nGõ <code>/cancel_action &lt;id&gt;</code> cho từng cái.")
    p = items[0]
    cancel_pending(p["action_id"])
    log_action(user="tg_admin", action="nl_cancel_action",
               risk_level="low", status="cancelled",
               result_summary=f"action_id={p['action_id']}")
    return (f"🚫 Đã hủy hành động <code>{p['action_id']}</code> "
            f"({p['action']}).")


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
                # Confirm/cancel button paths return _REPLY_HANDLED — the
                # handler already sent the final message; do not duplicate
                # or fall back.
                if reply == _REPLY_HANDLED:
                    log(f"reply=handled (no extra send) chat_id={chat_id}")
                elif reply:
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
                # Owner Command Agent rule: never silent. Reply Vietnamese,
                # redacted summary, point at /agent_diag.
                import traceback as _tb
                tb = _tb.format_exc()
                log(f"error handler=outer text={text[:30]!r} message={e}\n"
                    f"{tb[:500]}")
                # Redact common secret tokens from the summary
                summary = str(e)
                for pat in (r"ghp_[A-Za-z0-9]+",
                             r"sk-[A-Za-z0-9_-]+",
                             r"Bearer [A-Za-z0-9._-]+"):
                    import re as _re
                    summary = _re.sub(pat, "<redacted>", summary)
                try:
                    await send_chat_reply(
                        chat_id,
                        f"⚠️ Agent lỗi khi xử lý lệnh: "
                        f"<code>{_esc(summary[:200])}</code>.\n"
                        f"Dùng /agent_diag để xem trạng thái hoặc "
                        f"<i>“đang làm tới đâu rồi”</i> để xem tiến độ.",
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(bot_loop())
