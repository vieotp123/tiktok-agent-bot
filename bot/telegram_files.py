"""
Telegram file inbox/outbox helpers.

Directory layout:
  data/telegram/inbox/       — files received from admin
  data/telegram/outbox/      — (reserved for generated files to send)
  data/telegram/files.jsonl  — append-only file index

Security:
  - /send_file only allows paths under ALLOWED_SEND_ROOTS
  - BLOCKED_PATTERNS reject .env / storage_state / secret filenames
  - Text summarization capped at MAX_SUMMARIZE_BYTES
"""
import json
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

TELEGRAM_DATA = Path("/opt/tiktok-bot/data/telegram")
INBOX_DIR     = TELEGRAM_DATA / "inbox"
OUTBOX_DIR    = TELEGRAM_DATA / "outbox"
FILES_JSONL   = TELEGRAM_DATA / "files.jsonl"

# Absolute path roots that /send_file is permitted to read from
ALLOWED_SEND_ROOTS = (
    "/opt/tiktok-bot/data",
    "/opt/tiktok-bot/reports",
    "/opt/tiktok-bot/screenshots",
)

# Substring patterns in *filenames* that block sending
BLOCKED_PATTERNS = (
    ".env", "storage_state", "tiktok_storage_state",
    "cookies", "private_key", "secret", "token", ".log",
)

# Extensions readable for auto-summarisation
SUMMARIZABLE_EXTS = frozenset({".txt", ".md", ".json", ".csv"})
MAX_SUMMARIZE_BYTES = 50_000  # 50 KB


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_filename(name: str) -> str:
    """Strip path-traversal / shell chars from a filename."""
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe[:120].strip() or "file"


def _short_id() -> str:
    return str(uuid.uuid4())[:8]


def _ensure_dirs() -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)


# ── File record management ────────────────────────────────────────────────────

def record_file(record: dict) -> None:
    """Append one file record to the JSONL index."""
    TELEGRAM_DATA.mkdir(parents=True, exist_ok=True)
    with open(FILES_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_files(n: int = 10) -> list[dict]:
    """Return the last *n* file records, newest first."""
    if not FILES_JSONL.exists():
        return []
    lines = FILES_JSONL.read_text(encoding="utf-8").strip().splitlines()
    records: list[dict] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return list(reversed(records[-n:]))


def get_file_record(file_id: str) -> Optional[dict]:
    """Find a file record by our internal file_id."""
    if not FILES_JSONL.exists():
        return None
    for line in FILES_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            if r.get("file_id") == file_id:
                return r
        except Exception:
            pass
    return None


# ── Download ──────────────────────────────────────────────────────────────────

async def download_telegram_file(
    tg_token: str,
    telegram_file_id: str,
    filename: str,
    file_type: str,
    caption: str = "",
    mime_type: str = "",
    size: int = 0,
    uploaded_by: str = "tg_admin",
) -> Optional[dict]:
    """
    Download a file from Telegram and save to inbox.
    Returns the file record dict, or None on failure.
    """
    _ensure_dirs()
    try:
        # Step 1: resolve Telegram file path
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"https://api.telegram.org/bot{tg_token}/getFile",
                params={"file_id": telegram_file_id},
            )
            d = r.json()
        if not d.get("ok"):
            return None
        tg_path    = d["result"]["file_path"]
        real_size  = d["result"].get("file_size", size)

        # Step 2: build local path
        ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name  = _safe_filename(filename or f"file_{ts}")
        local_name = f"{ts}_{safe_name}"
        local_path = INBOX_DIR / local_name

        # Step 3: download bytes
        async with httpx.AsyncClient(timeout=120) as c:
            resp = await c.get(
                f"https://api.telegram.org/file/bot{tg_token}/{tg_path}"
            )
            local_path.write_bytes(resp.content)

        file_id = _short_id()
        record = {
            "file_id":          file_id,
            "telegram_file_id": telegram_file_id,
            "type":             file_type,
            "filename":         safe_name,
            "local_path":       str(local_path),
            "mime_type":        mime_type or (mimetypes.guess_type(safe_name)[0] or ""),
            "size":             real_size,
            "caption":          caption[:500],
            "uploaded_by":      uploaded_by,
            "created_at":       _now_iso(),
        }
        record_file(record)
        return record

    except Exception:
        return None


# ── Security validation ───────────────────────────────────────────────────────

def is_safe_send_path(path: str) -> tuple[bool, str]:
    """
    Validate a path for /send_file.
    Returns (ok, reason_if_blocked).
    """
    try:
        abs_path = os.path.realpath(path)
    except Exception as e:
        return False, f"❌ Lỗi xử lý path: {e}"

    if not any(abs_path.startswith(root) for root in ALLOWED_SEND_ROOTS):
        return False, (
            "❌ Path nằm ngoài vùng cho phép.\n"
            f"Allowed: {', '.join(ALLOWED_SEND_ROOTS)}"
        )

    name_lower = os.path.basename(abs_path).lower()
    for pat in BLOCKED_PATTERNS:
        if pat in name_lower:
            return False, f"❌ Tên file chứa pattern bị chặn: <code>{pat}</code>"

    if not os.path.isfile(abs_path):
        return False, "❌ File không tồn tại hoặc là thư mục."

    return True, ""


# ── Text file reading for summarisation ──────────────────────────────────────

def read_text_file(path: str) -> tuple[bool, str]:
    """
    Read a text file for LLM summarisation.
    Returns (ok, content_or_error_message).
    """
    abs_path = os.path.realpath(path)
    ext = Path(abs_path).suffix.lower()
    if ext not in SUMMARIZABLE_EXTS:
        return False, (
            f"⚠️ Loại file <code>{ext}</code> chưa hỗ trợ phân tích tự động. "
            f"File đã được lưu, phân tích bằng tool phù hợp sau.\n"
            f"Hỗ trợ hiện tại: {', '.join(sorted(SUMMARIZABLE_EXTS))}"
        )
    sz = os.path.getsize(abs_path)
    if sz > MAX_SUMMARIZE_BYTES:
        return False, f"⚠️ File quá lớn ({sz:,} bytes, tối đa {MAX_SUMMARIZE_BYTES:,} bytes) để auto-tóm tắt."
    try:
        content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        return True, content
    except Exception as e:
        return False, f"❌ Đọc file lỗi: {e}"
