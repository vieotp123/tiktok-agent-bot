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
    search_memory_simple, format_search_results,
    list_lessons, format_lessons_list,
)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ADMIN   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
BACKEND    = os.getenv("BACKEND_URL", "http://localhost:8000")
TG_BASE    = f"https://api.telegram.org/bot{TG_TOKEN}"

POLL_TIMEOUT  = 30
MAX_REPLY_LEN = 4000

SESSION_FILE = Path("/opt/tiktok-bot/data/telegram/session_state.json")
SESSION_TTL  = 600  # 10 minutes

# Task intent patterns for smart routing
_TASK_PATTERNS = [
    re.compile(r'^(tìm|search|kiểm tra|tóm tắt|phân tích|tạo báo cáo|generate)\s+\S', re.I),
    re.compile(r'^(find|check|summarize|analyze|create report|write report)\s+\S', re.I),
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][tg] {msg}", flush=True)


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


# ── Low-level Telegram API ────────────────────────────────────────────────────

async def tg_call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{TG_BASE}/{method}", json=payload)
        return r.json()


async def tg_call_multipart(method: str, data: dict, files: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{TG_BASE}/{method}", data=data, files=files)
        return r.json()


async def send(chat_id: str | int, text: str,
               reply_markup: Optional[dict] = None) -> Optional[int]:
    """Send message, return message_id or None."""
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
    return (r.get("result") or {}).get("message_id")


async def edit_msg(chat_id: str | int, message_id: int, text: str,
                   reply_markup: Optional[dict] = None) -> None:
    payload: dict = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text[:MAX_REPLY_LEN],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg_call("editMessageText", payload)


async def answer_cb(callback_query_id: str, text: str = "") -> None:
    await tg_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200],
    })


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


BACK_ROW = [("◀ Back", "menu:main")]


def menu_main() -> tuple[str, dict]:
    text = (
        "<b>🤖 Bot Control Panel</b>\n"
        "Choose a category:"
    )
    kb = make_keyboard([
        [("📊 Status", "nav:status"), ("🤖 Router/Models", "nav:router")],
        [("🧠 Tasks",  "nav:tasks"),  ("🔎 Search",        "nav:search")],
        [("📁 Files",  "nav:files"),  ("🧩 Skills",        "nav:skills")],
        [("⚙️ Admin", "nav:admin")],
    ])
    return text, kb


def menu_status() -> tuple[str, dict]:
    text = "<b>📊 Status</b>"
    kb = make_keyboard([
        [("🏥 Health", "do:health"), ("📋 Logs", "do:logs")],
        [("🎮 TikTok Chat Info", "do:tiktok_chat_info")],
        BACK_ROW,
    ])
    return text, kb


def menu_router() -> tuple[str, dict]:
    text = "<b>🤖 Router / Models</b>"
    kb = make_keyboard([
        [("📡 Router Status", "do:router_status"), ("📑 Models", "do:models")],
        [("🗂 Model Policy", "do:model_policy")],
        BACK_ROW,
    ])
    return text, kb


def menu_tasks() -> tuple[str, dict]:
    text = "<b>🧠 Tasks</b>"
    kb = make_keyboard([
        [("📋 Task List", "do:tasks"), ("▶ Run Task", "input:run_task")],
        [("⏳ Pending Actions", "do:pending_actions")],
        BACK_ROW,
    ])
    return text, kb


def menu_search() -> tuple[str, dict]:
    text = "<b>🔎 Search</b>"
    kb = make_keyboard([
        [("🌐 Search Web", "input:search_web"), ("🔬 Deep Research", "input:deep_research")],
        BACK_ROW,
    ])
    return text, kb


def menu_files() -> tuple[str, dict]:
    text = "<b>📁 Files</b>"
    kb = make_keyboard([
        [("📂 Recent Files", "do:files"), ("📤 Send File", "input:send_file")],
        [("📖 Upload Guide", "do:upload_guide")],
        BACK_ROW,
    ])
    return text, kb


def menu_skills() -> tuple[str, dict]:
    text = "<b>🧩 Skills</b>"
    kb = make_keyboard([
        [("📋 List Skills", "do:skills"), ("🔍 Skill Detail", "input:skill_detail")],
        BACK_ROW,
    ])
    return text, kb


def menu_admin() -> tuple[str, dict]:
    text = "<b>⚙️ Admin</b>"
    kb = make_keyboard([
        [("📊 Git Status", "do:git_status"), ("💾 Backup", "do:backup")],
        [("🔄 Restart Bot", "confirm:restart_bot")],
        BACK_ROW,
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
        lines = [
            "✅ <b>9Router reachable</b>",
            f"Models available: {d.get('model_count', '?')}",
            "",
            "<b>Active model policy:</b>",
            f"  chat/fast     : <code>{role_models.get('chat', d.get('chat_model','?'))}</code>",
            f"  tiktok_chat   : <code>{role_models.get('tiktok_chat','?')}</code>",
            f"  telegram_chat : <code>{role_models.get('telegram_chat','?')}</code>",
            f"  search_summary: <code>{role_models.get('search_summary','?')}</code>",
            f"  reasoning     : <code>{role_models.get('reasoning','?')}</code>",
            f"  coding        : <code>{role_models.get('coding','?')}</code>",
            f"  critic        : <code>{role_models.get('critic','?')}</code>",
            f"  cheap/fallback: <code>{role_models.get('cheap','openai/gpt-4o-mini')}</code>",
            "",
            f"Test ({role_models.get('chat','?')}): "
            f"{'✅ ' + d.get('test_reply','')[:40] if d.get('test_pass') else '❌ failed'}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


async def handle_model_policy() -> str:
    """Show current resolved model for each role."""
    try:
        from bot.llm_client import resolve_model, ROLE_MODEL_DEFAULT
        lines = ["<b>Model Policy (resolved)</b>"]
        for role in ("chat", "fast", "tiktok_chat", "telegram_chat", "search_summary",
                     "reasoning", "coding", "critic", "vision", "cheap"):
            model = await resolve_model(role)
            default = ROLE_MODEL_DEFAULT.get(role, "?")
            tag = "" if model == default else " ⚠️"
            lines.append(f"  <b>{role}</b>: <code>{model}</code>{tag}")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


async def handle_models() -> str:
    try:
        from bot.llm_client import list_models_top, resolve_model, ROLE_MODEL_DEFAULT
        top = await list_models_top(20)
        # Mark roles
        role_map: dict[str, list[str]] = {}
        for role in ROLE_MODEL_DEFAULT:
            m = await resolve_model(role)
            role_map.setdefault(m, []).append(role)

        lines = ["<b>Top models (9Router)</b>"]
        for m in top:
            tag = ""
            if m in role_map:
                tag = " <i>[" + ", ".join(role_map[m]) + "]</i>"
            lines.append(f"  <code>{m}</code>{tag}")
        lines.append(f"\n({len(top)} shown)")
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
        lines.append(f"{'✅' if s.enabled else '❌'} {RISK.get(s.risk_level,'⚪')} "
                     f"<b>{s.name}</b> — {s.description[:60]}")
    lines.append("\nUse /skill <name> for details.")
    return "\n".join(lines)


def handle_skill_detail(name: str) -> str:
    s = get_skill(name.strip())
    if not s:
        return f"Skill <b>{name}</b> not found."
    RISK = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    return "\n".join([
        f"<b>{s.name}</b> {RISK.get(s.risk_level,'⚪')}",
        f"Description: {s.description}",
        f"Risk: <b>{s.risk_level}</b>",
        f"Enabled: {'yes' if s.enabled else 'no'}",
        f"Handler: <code>{s.handler}</code>",
        ("Examples: " + " | ".join(s.examples[:3])) if s.examples else "",
    ])


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
        lines.append(
            f"{ICON.get(t['status'],'•')} <code>{t['task_id']}</code> "
            f"[{t['type']}]{ftag} {t['goal'][:40]} <i>({t['created_at'][11:16]})</i>"
        )
    lines.append("\nUse /task <id> for details.")
    return "\n".join(lines)


def handle_task_detail(task_id: str) -> str:
    t = get_task(task_id.strip())
    if not t:
        return f"Task <code>{task_id}</code> not found."
    in_f  = json.loads(t.get("input_files")  or "[]")
    out_f = json.loads(t.get("output_files") or "[]")
    lines = [
        f"<b>Task {t['task_id']}</b>",
        f"Type: {t['type']} | Status: <b>{t['status']}</b>",
        f"Goal: {t['goal'][:100]}",
        f"Created: {t['created_at']}",
    ]
    if in_f:
        lines.append(f"Input files: {', '.join(str(f) for f in in_f)}")
    if out_f:
        lines.append(f"Output files: {', '.join(str(f) for f in out_f)}")
    if t.get("result_summary"):
        lines.append(f"Result:\n<pre>{t['result_summary'][:400]}</pre>")
    if t.get("error"):
        lines.append(f"Error: {t['error'][:200]}")
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


async def handle_memory_search(query: str) -> str:
    """Search semantic memory for the admin user."""
    if not query.strip():
        return "Usage: /memory_search <query>"
    results = search_memory_simple("tg_admin", query, limit=5)
    return format_search_results(results, query)


def handle_lessons(arg: str = "") -> str:
    """Return episodic lessons list, optionally filtered by skill name."""
    skill = arg.strip() or None
    return format_lessons_list(skill=skill, limit=10)


def handle_audit_recent() -> str:
    """Return last 10 audit log entries."""
    return format_audit_recent(n=10)


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
    """Handle an inline keyboard button press."""
    cb_id   = cb["id"]
    data    = cb.get("data", "")
    msg_id  = (cb.get("message") or {}).get("message_id")

    await answer_cb(cb_id)   # always ACK

    parts = data.split(":", 1)
    kind  = parts[0]
    val   = parts[1] if len(parts) > 1 else ""

    # ── Navigation: show sub-menu ─────────────────────────────────────────────
    if kind == "nav":
        menu_fn = {
            "status": menu_status, "router": menu_router, "tasks": menu_tasks,
            "search": menu_search, "files":  menu_files,  "skills": menu_skills,
            "admin":  menu_admin,
        }.get(val)
        if menu_fn:
            text, kb = menu_fn()
        else:
            text, kb = menu_main()
        if msg_id:
            await edit_msg(chat_id, msg_id, text, kb)
        else:
            await send(chat_id, text, kb)
        return

    if kind == "menu" and val == "main":
        text, kb = menu_main()
        if msg_id:
            await edit_msg(chat_id, msg_id, text, kb)
        else:
            await send(chat_id, text, kb)
        return

    # ── Immediate action ──────────────────────────────────────────────────────
    if kind == "do":
        result = await _execute_action(val, chat_id)
        await send(chat_id, result or "(no result)")
        return

    # ── Input required: set session state ─────────────────────────────────────
    if kind == "input":
        prompts = {
            "run_task":     "✏️ Enter the task goal:",
            "search_web":   "🔎 Enter keyword to search:",
            "deep_research":"🔬 Enter research topic:",
            "send_file":    "📤 Enter file path or file_id:",
            "skill_detail": "🧩 Enter skill name:",
        }
        prompt = prompts.get(val, "✏️ Enter input:")
        _session_save(val, prompt)
        if msg_id:
            await edit_msg(chat_id, msg_id,
                           f"{prompt}\n\n<i>Send /cancel to abort.</i>",
                           make_keyboard([[("❌ Cancel", "menu:main")]]))
        else:
            await send(chat_id, f"{prompt}\n\n<i>Send /cancel to abort.</i>")
        return

    # ── Confirm dangerous actions ─────────────────────────────────────────────
    if kind == "confirm":
        if val == "restart_bot":
            confirm_kb = make_keyboard([
                [("✅ Yes, restart", "do:restart_bot"), ("❌ Cancel", "menu:main")],
            ])
            if msg_id:
                await edit_msg(chat_id, msg_id,
                               "⚠️ <b>Restart tiktok-bot?</b>\nThis will briefly drop the TikTok session.",
                               confirm_kb)
            else:
                await send(chat_id, "⚠️ Confirm restart?", confirm_kb)
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
    return f"Unknown action: {action}"


# ── Main command dispatcher ───────────────────────────────────────────────────

async def dispatch(text: str, chat_id: str | int = "") -> str:
    low   = text.lower().strip()
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        _, kb = menu_main()
        await send(
            chat_id,
            "👋 <b>Bot Control Panel</b>\n"
            "Use /menu for the full inline menu.\n\n"
            "<b>Agent commands:</b>\n"
            "/agent_blueprint — architecture overview\n"
            "/workers — active workers\n"
            "/memory_search &lt;q&gt; — search memory\n"
            "/lessons [skill] — past lessons\n"
            "/audit_recent — last audit entries",
            kb,
        )
        return ""
    if cmd == "/menu":
        text_m, kb = menu_main()
        await send(chat_id, text_m, kb)
        return ""
    if cmd == "/cancel":
        _session_clear()
        return "✅ Cancelled. Session cleared."

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
    if cmd == "/memory_search":    return await handle_memory_search(arg)
    if cmd == "/lessons":          return handle_lessons(arg)
    if cmd == "/audit_recent":     return handle_audit_recent()

    if low.startswith("/"):
        return (
            "Commands: /menu /status /health /router_status /models /model_policy\n"
            "/skills /skill <name> /tasks /task <id> /run_task <goal> /cancel_task <id>\n"
            "/pending_actions /confirm_action <id> /cancel_action <id>\n"
            "/files /file <id> /send_file <path> /logs /tiktok_chat_info\n"
            "/agent_blueprint /workers /memory_search <q> /lessons [skill] /audit_recent\n"
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

    # Drain stale updates
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
                "offset":          offset,
                "timeout":         POLL_TIMEOUT,
                "allowed_updates": ["message", "callback_query"],
            })
        except Exception as e:
            log(f"getUpdates error: {e}")
            await asyncio.sleep(5)
            continue

        for update in r.get("result", []):
            offset = update["update_id"] + 1

            # ── Callback query (inline button press) ──────────────────────────
            cb = update.get("callback_query")
            if cb:
                chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", TG_ADMIN))
                if chat_id != str(TG_ADMIN):
                    await tg_call("answerCallbackQuery", {
                        "callback_query_id": cb["id"],
                        "text": "Not authorized.",
                    })
                    continue
                try:
                    await dispatch_callback(cb, chat_id)
                except Exception as e:
                    log(f"callback error: {e}")
                    try:
                        await answer_cb(cb["id"])
                        await send(chat_id, f"Error: {e}")
                    except Exception:
                        pass
                continue

            # ── Text / file message ───────────────────────────────────────────
            msg = update.get("message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text    = (msg.get("text") or "").strip()

            if chat_id != str(TG_ADMIN):
                log(f"unauthorized chat_id={chat_id}")
                try:
                    await tg_call("sendMessage", {"chat_id": chat_id, "text": "Not authorized."})
                except Exception:
                    pass
                continue

            # File message (no text)
            has_file = any(k in msg for k in ("photo","document","audio","video","voice"))
            if not text and has_file:
                log(f"file_recv")
                try:
                    reply = await handle_file_message(msg, chat_id)
                    await send(chat_id, reply)
                    log_action(user="tg_admin", action="file_upload",
                               risk_level="low", status="ok", result_summary=reply[:100])
                except Exception as e:
                    log(f"file error: {e}")
                    try:
                        await send(chat_id, f"❌ File error: {e}")
                    except Exception:
                        pass
                continue

            if not text:
                continue

            log(f"recv: {text[:80]!r}")

            # Check pending session state
            session = _session_get()
            if session and not text.startswith("/"):
                try:
                    reply = await handle_pending_input(session["action"], text, chat_id)
                    await send(chat_id, reply)
                    log_action(user="tg_admin", action=f"input:{session['action']}",
                               risk_level="low", status="ok", result_summary=reply[:100])
                except Exception as e:
                    log(f"pending_input error: {e}")
                    await send(chat_id, f"Error: {e}")
                continue

            # Normal dispatch
            try:
                reply = await dispatch(text, chat_id)
                if reply:
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
