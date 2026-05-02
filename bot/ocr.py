"""
OCR tool — extract text from a saved image via the 9Router vision model.

Design:
  - Reads a single image file from one of `bot.telegram_files.ALLOWED_SEND_ROOTS`
    (defense in depth: rejects .env / storage_state / cookies / log paths even
    if they ever sneak into a permitted root).
  - Calls `bot.llm_client.complete(role="vision")` with an OpenAI-style
    multimodal message (text prompt + base64 image_url). 9Router routes the
    `vision` role to `openai/gpt-4o`.
  - Redacts obvious secret-looking lines (token / password / api_key / sk-...
    / Bearer ...) BEFORE the result leaves this module — both for the audit
    log and for the caller. Telegram replies and the audit_log entry get
    the redacted text only; the raw extraction is never persisted.
  - Hard image-size cap (8 MB) and hard extension allow-list (png/jpg/jpeg
    /webp/gif). No PDF, no SVG, no remote URLs.
  - Audit-logged at risk_level=low (read-only on a file already in the
    inbox). The handler stays low even though the description-level risk
    of "OCR" can be classified medium by the planner.

Public API:
  is_ocr_safe_path(path)              -> (ok: bool, reason: str)
  redact_secrets(text)                -> (redacted: str, count: int)
  async ocr_image(path, prompt=None)  -> dict
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Optional

from bot.agent.audit_log import log_action
from bot.llm_client import complete
from bot.telegram_files import is_safe_send_path

OCR_ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
OCR_MAX_BYTES    = 8 * 1024 * 1024  # 8 MB hard cap

_DEFAULT_PROMPT = (
    "Extract all visible text from this image, preserving line breaks and "
    "approximate layout. If the image contains no text, reply with the "
    "single token NO_TEXT. Do not summarise; do not translate; output only "
    "the raw text."
)

_SECRET_PATTERNS = (
    (re.compile(r"\b(?:Bearer\s+)([A-Za-z0-9_\-\.]{16,})", re.I),
        "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
        "sk-[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"),
        "ghp_[REDACTED]"),
    (re.compile(r"\bgho_[A-Za-z0-9]{16,}\b"),
        "gho_[REDACTED]"),
    (re.compile(r"\b(?:password|passwd|pwd|api[_\-]?key|secret|token|auth"
                r")\s*[:=]\s*\S+", re.I),
        "[REDACTED_SECRET]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END "
                r"[A-Z ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]"),
)


def is_ocr_safe_path(path: str):
    ok, reason = is_safe_send_path(path)
    if not ok:
        return False, reason

    abs_path = os.path.realpath(path)
    ext = Path(abs_path).suffix.lower()
    if ext not in OCR_ALLOWED_EXTS:
        return False, (
            f"Extension {ext or '(none)'} không được hỗ trợ OCR. "
            f"Allowed: {', '.join(sorted(OCR_ALLOWED_EXTS))}."
        )

    sz = os.path.getsize(abs_path)
    if sz > OCR_MAX_BYTES:
        return False, (
            f"File quá lớn ({sz:,} bytes, tối đa {OCR_MAX_BYTES:,} bytes) "
            f"để chạy OCR."
        )
    if sz <= 0:
        return False, "File rỗng."

    return True, ""


def redact_secrets(text: str):
    if not text:
        return "", 0
    out = text
    count = 0
    for pat, replacement in _SECRET_PATTERNS:
        out, n = pat.subn(replacement, out)
        count += n
    return out, count


def _mime_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif":  "image/gif",
    }.get(ext, "application/octet-stream")


async def ocr_image(
    path: str,
    prompt: Optional[str] = None,
    *,
    user: str = "tg_admin",
    timeout: float = 45.0,
    max_tokens: int = 1500,
):
    ok, reason = is_ocr_safe_path(path)
    if not ok:
        log_action(
            user=user, action="ocr_image", risk_level="low",
            status="failed", channel="internal",
            result_summary=f"refused: {reason}"[:200],
        )
        return {"ok": False, "error": reason, "path": path}

    abs_path = os.path.realpath(path)
    try:
        raw = Path(abs_path).read_bytes()
    except Exception as e:
        log_action(
            user=user, action="ocr_image", risk_level="low",
            status="failed", channel="internal",
            result_summary=f"read_error: {e!r}"[:200],
        )
        return {"ok": False, "error": f"Đọc file lỗi: {e}", "path": path}

    b64 = base64.b64encode(raw).decode("ascii")
    mime = _mime_for(abs_path)
    user_text = (prompt or _DEFAULT_PROMPT).strip() or _DEFAULT_PROMPT

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }]

    result = await complete(
        messages, role="vision",
        temperature=0.0, max_tokens=max_tokens,
        timeout=timeout, source="ocr",
    )
    if result.get("error"):
        detail = (result.get("error_detail") or "")[:200]
        log_action(
            user=user, action="ocr_image", risk_level="low",
            status="failed", channel="internal",
            result_summary=f"llm_error: {detail}"[:200],
        )
        return {"ok": False, "error": f"LLM lỗi: {detail}", "path": path}

    text_raw = (result.get("content") or "").strip()
    text, redactions = redact_secrets(text_raw)

    log_action(
        user=user, action="ocr_image", risk_level="low",
        status="ok", channel="internal",
        result_summary=(
            f"chars={len(text)} redactions={redactions} "
            f"file={os.path.basename(abs_path)}"
        )[:200],
    )
    return {
        "ok":         True,
        "text":       text,
        "model":      result.get("model", ""),
        "redactions": redactions,
        "chars":      len(text),
        "path":       abs_path,
    }


def is_ocr_intent(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    keywords = (
        "ocr", "đọc text", "doc text", "đọc chữ", "doc chu",
        "trích text", "trich text", "extract text",
        "phân tích ảnh", "phan tich anh",
        "xem chữ trong ảnh", "xem chu trong anh",
        "đọc ảnh", "doc anh", "read image",
    )
    return any(k in t for k in keywords)
