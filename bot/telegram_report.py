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

import os
import time
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


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "ping from bot/telegram_report.py"
    ok = send_telegram_message(msg)
    print("ok" if ok else "fail")
