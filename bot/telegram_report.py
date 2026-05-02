"""
Telegram notifier — fire-and-forget admin reports.

Used by overnight worker scripts and agent loops to send status updates
to the admin chat without going through the long-poll bot. Loads creds
from /opt/tiktok-bot/.env. Never logs the token.

Usage (sync):
    from bot.telegram_report import send_telegram_message
    send_telegram_message("Phase 1 done. Tests pass. Commit b4bcf33.")
"""
from __future__ import annotations

import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT  = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
_TG_BASE  = f"https://api.telegram.org/bot{_TG_TOKEN}"


def send_telegram_message(text: str, *, parse_mode: str = "HTML",
                          retries: int = 2, timeout: int = 10) -> bool:
    """Send a message to TELEGRAM_ADMIN_CHAT_ID. Returns True on success.

    Never raises. Never logs the token. On HTML parse failure, retries
    once as plain text. Safe to call from any synchronous script.
    """
    if not _TG_TOKEN or not _TG_CHAT or not text:
        return False

    payload = {
        "chat_id":    _TG_CHAT,
        "text":       text[:4000],
        "parse_mode": parse_mode,
    }

    for attempt in range(retries + 1):
        try:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{_TG_BASE}/sendMessage", data=data, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            # On parse-mode error, retry as plain text once
            if "parse" in body.lower() and parse_mode:
                payload.pop("parse_mode", None)
                parse_mode = ""
                continue
        except Exception:
            pass
        time.sleep(1)
    return False


def send_phase_report(phase: str, status: str, *,
                      tests: str = "", commit: str = "",
                      next_step: str = "") -> bool:
    """Convenience wrapper for night-mission phase reports."""
    icon = "✅" if status.lower() in ("ok", "pass", "done", "success") else (
        "⚠" if "warn" in status.lower() else "❌"
    )
    parts = [f"{icon} <b>{_html_escape(phase)}</b> — <i>{_html_escape(status)}</i>"]
    if tests:
        parts.append(f"<b>Tests:</b> {_html_escape(tests)[:300]}")
    if commit:
        parts.append(f"<b>Commit:</b> <code>{_html_escape(commit)[:40]}</code>")
    if next_step:
        parts.append(f"<b>Next:</b> {_html_escape(next_step)[:200]}")
    return send_telegram_message("\n".join(parts))


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def _send_multipart(method: str, fields: dict[str, str],
                     file_field: str, path: str, filename: str,
                     mime: str, *, timeout: int = 60) -> bool:
    """Internal: POST multipart/form-data via stdlib only.

    Used for sendDocument / sendPhoto. Returns True on HTTP 2xx + ok=True.
    Never raises; never logs the token.
    """
    if not _TG_TOKEN or not _TG_CHAT:
        return False
    try:
        with open(path, "rb") as f:
            file_bytes = f.read()
    except Exception:
        return False
    boundary = "----brain" + str(int(time.time() * 1000))
    crlf = "\r\n"
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            (f"--{boundary}{crlf}"
             f"Content-Disposition: form-data; name=\"{k}\"{crlf}{crlf}"
             f"{v}{crlf}").encode("utf-8")
        )
    parts.append(
        (f"--{boundary}{crlf}"
         f"Content-Disposition: form-data; name=\"{file_field}\"; "
         f"filename=\"{filename}\"{crlf}"
         f"Content-Type: {mime}{crlf}{crlf}").encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"{crlf}--{boundary}--{crlf}".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{_TG_BASE}/{method}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _safe_to_send(path: str) -> tuple[bool, str]:
    """Reuse the same allow-list / block-list as the bot's /send_file.
    Brain must not be a backdoor that bypasses the path security."""
    try:
        from bot.telegram_files import is_safe_send_path
    except Exception as e:
        return False, f"safety module unavailable: {e}"
    return is_safe_send_path(path)


def send_telegram_file(path: str, caption: str = "") -> bool:
    """Send a file (document) to TELEGRAM_ADMIN_CHAT_ID.

    Path is validated against telegram_files.is_safe_send_path so the
    brain cannot leak .env / storage_state / tokens / cookies.
    Returns True on success. Never raises; never logs the token.
    """
    ok, _reason = _safe_to_send(path)
    if not ok:
        return False
    abs_path = os.path.realpath(path)
    filename = os.path.basename(abs_path) or "file"
    mime     = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    fields   = {"chat_id": _TG_CHAT}
    if caption:
        fields["caption"] = caption[:1024]
    return _send_multipart("sendDocument", fields, "document",
                            abs_path, filename, mime)


def send_telegram_photo(path: str, caption: str = "") -> bool:
    """Send an image to TELEGRAM_ADMIN_CHAT_ID. Same safety guard.
    Returns True on success."""
    ok, _reason = _safe_to_send(path)
    if not ok:
        return False
    abs_path = os.path.realpath(path)
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return False
    filename = os.path.basename(abs_path) or "image"
    mime     = mimetypes.guess_type(filename)[0] or "image/jpeg"
    fields   = {"chat_id": _TG_CHAT}
    if caption:
        fields["caption"] = caption[:1024]
    return _send_multipart("sendPhoto", fields, "photo",
                            abs_path, filename, mime)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--file":
        ok = send_telegram_file(sys.argv[2],
                                caption=" ".join(sys.argv[3:]))
        print("ok" if ok else "fail")
    elif len(sys.argv) >= 3 and sys.argv[1] == "--photo":
        ok = send_telegram_photo(sys.argv[2],
                                 caption=" ".join(sys.argv[3:]))
        print("ok" if ok else "fail")
    else:
        msg = " ".join(sys.argv[1:]) or "ping from bot/telegram_report.py"
        ok = send_telegram_message(msg)
        print("ok" if ok else "fail")
