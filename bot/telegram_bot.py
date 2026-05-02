"""
Telegram admin-control bot — long-polling, no external library.
Only responds to TELEGRAM_ADMIN_CHAT_ID.

Commands:
  /status              — systemctl status + last log lines
  /health              — backend health check
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
  /files               — list 10 most recent received files
  /file <file_id>      — show file metadata
  /send_file <path>    — send a file from allowed paths back to admin
  /logs                — last 20 lines of tiktok-bot journal
  /tiktok_chat_info    — show current TikTok chat metadata

Plain text:
  - Short / conversational  → backend /message (9Router chat)
  - Starts with task intent → auto run_task (create + execute)

Files (photo / document / audio / video / voice):
  - Downloaded to data/telegram/inbox/
  - Indexed in data/telegram/files.jsonl
  - If caption contains 'tóm tắt' / 'summarize' / 'đọc file' → auto-summarise
  - If caption contains 'ocr' / 'phân tích ảnh' → warn (not yet supported)
  - Otherwise → store only, reply with file_id
"""
import asyncio
import json
import os
import subprocess
import sys
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")
sys.path.insert(0, "/opt/tiktok-bot")

from bot.agent.task_queue import list_tasks, get_task, cancel_task, create_task
from bot.agent.skill_registry import list_skills, get_skill
from bot.agent.permissions import list_pending, confirm_pending, cancel_pending
from bot.agent.audit_log import log_action
from bot.agent.runner import run_task
from bot.telegram_files import (
    download_telegram_file, list_files, get_file_record,
    is_safe_send_path, read_text_file,
)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ADMIN   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
BACKEND    = os.getenv("BACKEND_URL", "http://localhost:8000")
TG_BASE    = f"https://api.telegram.org/bot{TG_TOKEN}"

POLL_TIMEOUT  = 30
MAX_REPLY_LEN = 4000

# Intent patterns for auto-run_task routing
_TASK_PATTERNS = [
    re.compile(r'^(tìm|search|kiểm tra|tóm tắt|phân tích|tạo báo cáo|generate)\s+\S', re.I),
    re.compile(r'^(find|check|summarize|analyze|create report|write report)\s+\S', re.I),
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][tg] {msg}", flush=True)


def _looks_like_task(text: str) -> bool:
    """True if the message looks like an explicit task goal rather than casual chat."""
    if len(text) < 25:
        return False
    for pat in _TASK_PATTERNS:
        if pat.match(text):
            return True
    return False


# ── Low-level Telegram API ────────────────────────────────────────────────────

async def tg_call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{TG_BASE}/{method}", json=payload)
        return r.json()


async def tg_call_multipart(method: str, data: dict, files: dict) -> dict:
    """POST multipart/form-data (for sending files)."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{TG_BASE}/{method}", data=data, files=files)
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


async def send_document(chat_id: str | int, path: str, caption: str = "") -> bool:
    """Send a file as a Telegram document."""
    try:
        abs_path = os.path.realpath(path)
        filename = os.path.basename(abs_path)
        with open(abs_path, "rb") as f:
            data_bytes = f.read()
        res = await tg_call_multipart(
            "sendDocument",
            {"chat_id": str(chat_id), "caption": caption[:1000]},
            {"document": (filename, data_bytes)},
        )
        return res.get("ok", False)
    except Exception as e:
        log(f"send_document error: {e}")
        return False


async def send_photo(chat_id: str | int, path: str, caption: str = "") -> bool:
    """Send an image as a Telegram photo."""
    try:
        abs_path = os.path.realpath(path)
        filename = os.path.basename(abs_path)
        with open(abs_path, "rb") as f:
            data_bytes = f.read()
        res = await tg_call_multipart(
            "sendPhoto",
            {"chat_id": str(chat_id), "caption": caption[:1000]},
            {"photo": (filename, data_bytes)},
        )
        return res.get("ok", False)
    except Exception as e:
        log(f"send_photo error: {e}")
        return False


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
        ts = t["created_at"][11:16]
        short = t["goal"][:40]
        in_files = json.loads(t.get("input_files") or "[]")
        file_tag = f" 📎{len(in_files)}" if in_files else ""
        lines.append(f"{icon} <code>{t['task_id']}</code> [{t['type']}]{file_tag} {short} <i>({ts})</i>")
    lines.append("\nUse /task <id> for details.")
    return "\n".join(lines)


def handle_task_detail(task_id: str) -> str:
    t = get_task(task_id.strip())
    if not t:
        return f"Task <code>{task_id}</code> not found."
    in_files  = json.loads(t.get("input_files")  or "[]")
    out_files = json.loads(t.get("output_files") or "[]")
    lines = [
        f"<b>Task {t['task_id']}</b>",
        f"Type: {t['type']}",
        f"Status: <b>{t['status']}</b>",
        f"Goal: {t['goal'][:100]}",
        f"Created: {t['created_at']}",
    ]
    if in_files:
        lines.append(f"Input files: {', '.join(str(f) for f in in_files)}")
    if out_files:
        lines.append(f"Output files: {', '.join(str(f) for f in out_files)}")
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


def handle_files_list() -> str:
    files = list_files(10)
    if not files:
        return "Không có file nào. Gửi file/ảnh cho bot để lưu vào inbox."
    TYPE_ICON = {"photo": "🖼", "document": "📄", "audio": "🎵", "video": "🎬", "voice": "🎤"}
    lines = ["<b>Files gần nhất (inbox)</b>"]
    for f in files:
        icon = TYPE_ICON.get(f.get("type", ""), "📎")
        ts   = f.get("created_at", "")[-21:-6] or f.get("created_at", "")[:16]
        sz   = f.get("size", 0)
        kb   = f"{sz // 1024}KB" if sz else "?"
        cap  = f" — {f['caption'][:30]!r}" if f.get("caption") else ""
        lines.append(f"{icon} <code>{f['file_id']}</code> <b>{f['filename'][:30]}</b> ({kb}){cap}")
    lines.append("\nUse /file <id> for details.")
    return "\n".join(lines)


def handle_file_detail(file_id: str) -> str:
    r = get_file_record(file_id.strip())
    if not r:
        return f"❌ File <code>{file_id}</code> không tìm thấy."
    lines = [
        f"<b>File {r['file_id']}</b>",
        f"Type: {r.get('type', '?')}",
        f"Filename: <code>{r['filename']}</code>",
        f"Local path: <code>{r['local_path']}</code>",
        f"Size: {r.get('size', 0):,} bytes",
        f"MIME: {r.get('mime_type', '?')}",
        f"Caption: {r.get('caption', '') or '(none)'}",
        f"Created: {r.get('created_at', '?')}",
    ]
    return "\n".join(lines)


async def handle_send_file(path: str, chat_id: str | int) -> str:
    """Send a file from an allowed path back to admin."""
    path = path.strip()
    ok, reason = is_safe_send_path(path)
    if not ok:
        return reason
    ext = Path(path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        sent = await send_photo(chat_id, path, caption=f"📤 {os.path.basename(path)}")
    else:
        sent = await send_document(chat_id, path, caption=f"📤 {os.path.basename(path)}")
    if sent:
        return f"✅ Đã gửi: <code>{path}</code>"
    return f"❌ Gửi file thất bại: <code>{path}</code>"


async def handle_logs() -> str:
    """Return last 20 lines of tiktok-bot journal."""
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
    path = Path("/opt/tiktok-bot/data/chat_info.json")
    if not path.exists():
        return "❌ Chưa có thông tin chat — bot chưa khởi động hoặc chưa vào chat."
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"❌ Đọc chat_info lỗi: {e}"
    title   = info.get("chatTitle") or info.get("chat_name") or "?"
    members = info.get("memberCount", 0)
    visible = info.get("membersVisible", 0)
    updated = info.get("updated_at", "?")[:19]
    return "\n".join([
        "<b>TikTok Chat Info</b>",
        f"Chat: <b>{title}</b>",
        f"Members (DOM): {members}",
        f"Avatar imgs in header: {visible}",
        f"Updated: <i>{updated}</i>",
    ])


async def handle_run_task(goal: str) -> str:
    if not goal:
        return "Usage: /run_task <goal>\nExample: /run_task tìm thông tin mới nhất về eSIM Nhật"
    log(f"run_task goal={goal[:80]!r}")
    result = await run_task(goal=goal, user="tg_admin")
    task_id    = result["task_id"]
    status     = result["status"]
    task_result = result.get("result", "")
    icon = "✅" if status == "done" else "❌"
    lines = [
        f"{icon} Task <code>{task_id}</code> [{result['type']}] {status}",
        f"Goal: {goal[:80]}",
    ]
    if task_result:
        lines.append(f"\nResult:\n{task_result[:1200]}")
    return "\n".join(lines)


async def handle_message(text: str) -> str:
    """Forward to backend /message (9Router chat)."""
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


# ── File inbox ────────────────────────────────────────────────────────────────

_SUMMARIZE_KW = ("tóm tắt", "summarize", "đọc file", "analyze", "phân tích file")
_OCR_KW       = ("ocr", "phân tích ảnh", "xem ảnh", "read image")


async def _summarize_via_backend(content: str, caption: str, file_id: str) -> str:
    """Send file content to backend for LLM summarization."""
    try:
        truncated = content[:4000]
        tail_note = f"\n\n[... còn {len(content)-4000} ký tự không hiển thị]" if len(content) > 4000 else ""
        prompt = (
            f"Admin gửi file (file_id={file_id}) kèm yêu cầu: \"{caption}\"\n\n"
            f"Nội dung file:\n{truncated}{tail_note}\n\n"
            "Tóm tắt/phân tích theo yêu cầu, ngắn gọn."
        )
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                f"{BACKEND}/message",
                json={"username": "tg_admin_file", "content": prompt},
            )
        if r.status_code == 200:
            return r.json().get("reply", "(empty reply)")
        return f"Backend error {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"Lỗi tóm tắt: {e}"


async def handle_file_message(msg: dict, chat_id: str | int) -> str:
    """Handle an incoming file/photo/audio/video/voice from admin."""
    caption = (msg.get("caption") or "").strip()

    # Detect file type and extract fields
    if "photo" in msg:
        photos    = msg["photo"]
        photo     = max(photos, key=lambda x: x.get("file_size", 0))
        tg_fid    = photo["file_id"]
        ftype     = "photo"
        filename  = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        mime      = "image/jpeg"
        size      = photo.get("file_size", 0)
    elif "document" in msg:
        doc       = msg["document"]
        tg_fid    = doc["file_id"]
        ftype     = "document"
        filename  = doc.get("file_name", f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        mime      = doc.get("mime_type", "")
        size      = doc.get("file_size", 0)
    elif "audio" in msg:
        audio     = msg["audio"]
        tg_fid    = audio["file_id"]
        ftype     = "audio"
        filename  = audio.get("file_name", f"audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3")
        mime      = audio.get("mime_type", "audio/mpeg")
        size      = audio.get("file_size", 0)
    elif "video" in msg:
        video     = msg["video"]
        tg_fid    = video["file_id"]
        ftype     = "video"
        filename  = video.get("file_name", f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        mime      = video.get("mime_type", "video/mp4")
        size      = video.get("file_size", 0)
    elif "voice" in msg:
        voice     = msg["voice"]
        tg_fid    = voice["file_id"]
        ftype     = "voice"
        filename  = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
        mime      = voice.get("mime_type", "audio/ogg")
        size      = voice.get("file_size", 0)
    else:
        return "⚠️ Loại file không được nhận dạng."

    log(f"file_recv type={ftype} filename={filename!r} size={size}")

    record = await download_telegram_file(
        tg_token=TG_TOKEN,
        telegram_file_id=tg_fid,
        filename=filename,
        file_type=ftype,
        caption=caption,
        mime_type=mime,
        size=size,
        uploaded_by="tg_admin",
    )
    if not record:
        return f"❌ Download file thất bại. Có thể file quá lớn (Telegram API giới hạn 20MB)."

    file_id   = record["file_id"]
    local_path = record["local_path"]
    kb         = size // 1024

    reply = (
        f"✅ Đã nhận file: <code>{file_id}</code>\n"
        f"📁 <b>{filename}</b> ({kb}KB, {ftype})\n"
        f"💾 <code>{local_path}</code>"
    )
    if caption:
        reply += f"\n📝 Caption: <i>{caption[:100]}</i>"

    # Caption-based auto-processing
    cap_low = caption.lower()
    if any(kw in cap_low for kw in _SUMMARIZE_KW):
        ok, content = read_text_file(local_path)
        if ok:
            summary = await _summarize_via_backend(content, caption, file_id)
            reply += f"\n\n<b>Tóm tắt:</b>\n{summary[:2500]}"
            # Record as a task
            task_id = create_task(
                type_="summarize_file",
                goal=f"Tóm tắt file {filename}",
                status="done",
                input_files=[local_path],
            )
            reply += f"\n\n📋 Task: <code>{task_id}</code>"
        else:
            reply += f"\n\n{content}"
    elif any(kw in cap_low for kw in _OCR_KW):
        reply += "\n\n⚠️ OCR / vision chưa hỗ trợ tự động. File đã lưu để xử lý sau."
    elif caption and ftype in ("document",):
        # Unknown caption intent on a document — hint
        reply += "\n\n💡 Tip: thêm 'tóm tắt' vào caption để auto-tóm tắt file text."

    return reply


# ── Route command ─────────────────────────────────────────────────────────────

async def dispatch(text: str, chat_id: str | int = "") -> str:
    """Route text or command to the correct handler."""
    low   = text.lower().strip()
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/status",):
        return await handle_status()
    if cmd in ("/health",):
        return await handle_health()
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
    if cmd in ("/files",):
        return handle_files_list()
    if cmd in ("/file",):
        return handle_file_detail(arg)
    if cmd in ("/send_file",):
        return await handle_send_file(arg, chat_id)
    if cmd in ("/logs",):
        return await handle_logs()
    if cmd in ("/tiktok_chat_info",):
        return handle_tiktok_chat_info()
    if low.startswith("/"):
        return (
            "Commands:\n"
            "/status · /health · /router_status · /models\n"
            "/skills · /skill <name>\n"
            "/tasks · /task <id> · /run_task <goal> · /cancel_task <id>\n"
            "/pending_actions · /confirm_action <id> · /cancel_action <id>\n"
            "/files · /file <id> · /send_file <path> · /logs\n"
            "/tiktok_chat_info\n"
            "\nOr send plain text to chat / run a task automatically."
        )

    # ── Plain text: smart routing ─────────────────────────────────────────────
    if _looks_like_task(text):
        log(f"auto run_task text={text[:60]!r}")
        return await handle_run_task(text)

    # Default: chat via backend
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

    # Drain stale updates on startup
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
            msg    = update.get("message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text    = (msg.get("text") or "").strip()

            # ── Auth check ────────────────────────────────────────────────────
            if chat_id != str(TG_ADMIN):
                log(f"unauthorized chat_id={chat_id}")
                try:
                    await tg_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Not authorized.",
                    })
                except Exception:
                    pass
                continue

            # ── File message ──────────────────────────────────────────────────
            has_file = any(k in msg for k in ("photo", "document", "audio", "video", "voice"))
            if not text and has_file:
                log(f"file_recv type={'photo' if 'photo' in msg else 'doc/audio/video'}")
                try:
                    reply = await handle_file_message(msg, chat_id)
                    await send(chat_id, reply)
                    log_action(
                        user="tg_admin",
                        action="file_upload",
                        risk_level="low",
                        status="ok",
                        result_summary=reply[:100],
                    )
                except Exception as e:
                    log(f"file handle error: {e}")
                    try:
                        await send(chat_id, f"❌ File error: {e}")
                    except Exception:
                        pass
                continue

            # Text message with optional file (caption handled above if no text)
            if has_file and text:
                # text is the caption in this branch (shouldn't normally happen but handle it)
                pass

            if not text:
                continue

            log(f"recv: {text[:80]!r}")

            try:
                reply = await dispatch(text, chat_id)
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
