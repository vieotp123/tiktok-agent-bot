"""
Claude quota scheduler.

Honest design: this module does NOT try to detect quota by API probing
(false positives, wastes the actual quota). Instead, the admin records
when their Claude quota resets, and the scheduler:

  1. Notifies once when the reset time arrives.
  2. If autorun is enabled AND the local coding CLI is non-interactive,
     triggers `bot.coding_worker_bridge.run_once()` (or a small batch).
  3. If the CLI is unavailable, sends the admin the exact manual command
     they should run instead.

State lives at data/claude_quota.json (gitignored):

    {
      "reset_at":         "2026-05-02T14:30:00Z" | null,
      "limited":          true | false,
      "autorun":          true | false,
      "max_tasks":        1,
      "last_notified_at": "...",   # when we last DM'd "quota reset"
      "last_run_at":      "...",   # when we last actually invoked the
                                    # bridge
    }

Public API:
    set_reset_at(iso_or_human)     -> dict
    set_reset_in(human_duration)   -> dict      # "30m" / "2h" / "3h30m"
    set_limited(flag: bool)        -> dict
    set_autorun(flag, max_tasks=1) -> dict
    status_summary()               -> str (HTML)
    state()                        -> dict

Background loop:
    start_scheduler_thread()       — fire-and-forget; safe to call once
                                       at telegram_bot import time.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

QUOTA_FILE = Path("/opt/tiktok-bot/data/claude_quota.json")
CHECK_INTERVAL_SEC = 60


# ── State helpers ─────────────────────────────────────────────────────────────

_DEFAULT: dict = {
    "reset_at":         None,
    "limited":          False,
    "autorun":          False,
    "max_tasks":        1,
    "last_notified_at": None,
    "last_run_at":      None,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def state() -> dict:
    if not QUOTA_FILE.exists():
        return dict(_DEFAULT)
    try:
        d = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        merged = dict(_DEFAULT); merged.update(d)
        return merged
    except Exception:
        return dict(_DEFAULT)


def _save(d: dict) -> None:
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

_ISO_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?Z?$")
_HUMAN_RE = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*$",
                       re.IGNORECASE)


def _parse_reset(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    if _ISO_RE.match(text):
        norm = text.replace(" ", "T")
        if not norm.endswith("Z"):
            norm += "Z"
        try:
            return datetime.strptime(norm, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc,
            )
        except Exception:
            try:
                return datetime.strptime(norm, "%Y-%m-%dT%H:%MZ").replace(
                    tzinfo=timezone.utc,
                )
            except Exception:
                return None
    return None


def _parse_human_duration(text: str) -> Optional[timedelta]:
    text = (text or "").strip()
    m = _HUMAN_RE.match(text)
    if not m:
        return None
    h, mi = m.group(1), m.group(2)
    if not h and not mi:
        return None
    return timedelta(hours=int(h or 0), minutes=int(mi or 0))


def set_reset_at(text: str) -> dict:
    """Set reset time as ISO 8601 UTC: 'YYYY-MM-DD HH:MM' or with seconds."""
    when = _parse_reset(text)
    if not when:
        raise ValueError(f"unrecognised reset time {text!r} — "
                         "expected 'YYYY-MM-DD HH:MM' UTC")
    d = state()
    d["reset_at"]         = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["last_notified_at"] = None
    d["limited"]          = True
    _save(d)
    return d


def set_reset_in(text: str) -> dict:
    """Set reset 'X h Y m' from now (UTC)."""
    delta = _parse_human_duration(text)
    if not delta:
        raise ValueError(f"unrecognised duration {text!r} — "
                         "use e.g. '30m', '2h', '3h30m'")
    d = state()
    when = _now() + delta
    d["reset_at"]         = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["last_notified_at"] = None
    d["limited"]          = True
    _save(d)
    return d


def set_limited(flag: bool) -> dict:
    d = state()
    d["limited"] = bool(flag)
    if not flag:
        d["reset_at"]         = None
        d["last_notified_at"] = None
    _save(d)
    return d


def set_autorun(flag: bool, max_tasks: int = 1) -> dict:
    d = state()
    d["autorun"]   = bool(flag)
    d["max_tasks"] = max(1, min(int(max_tasks), 3))
    _save(d)
    return d


def status_summary() -> str:
    d = state()
    lines = ["<b>📅 Claude quota</b>"]
    lines.append(f"limited: <b>{'yes' if d['limited'] else 'no'}</b>")
    if d["reset_at"]:
        try:
            t   = datetime.strptime(d["reset_at"], "%Y-%m-%dT%H:%M:%SZ"
                                    ).replace(tzinfo=timezone.utc)
            now = _now()
            delta = t - now
            sec = int(delta.total_seconds())
            if sec > 0:
                h, m = divmod(sec // 60, 60)
                eta = f"{h}h{m:02d}m"
                lines.append(f"reset_at: <code>{d['reset_at']}</code> "
                             f"(in {eta})")
            else:
                lines.append(f"reset_at: <code>{d['reset_at']}</code> "
                             f"(due, will fire next check)")
        except Exception:
            lines.append(f"reset_at: <code>{d['reset_at']}</code>")
    else:
        lines.append("reset_at: <i>not set</i>")
    lines.append(f"autorun: <b>{'on' if d['autorun'] else 'off'}</b> "
                 f"(max_tasks={d['max_tasks']})")
    if d["last_notified_at"]:
        lines.append(f"last_notified_at: {d['last_notified_at']}")
    if d["last_run_at"]:
        lines.append(f"last_run_at: {d['last_run_at']}")
    return "\n".join(lines)


# ── Scheduler thread ──────────────────────────────────────────────────────────

_thread_started = False
_thread_lock = threading.Lock()


def _due(d: dict) -> bool:
    if not d.get("reset_at"):
        return False
    if d.get("last_notified_at") == d["reset_at"]:
        return False
    try:
        t = datetime.strptime(d["reset_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return False
    return _now() >= t


def _on_due() -> None:
    """Called once when the reset time first arrives."""
    from bot.telegram_report import send_telegram_message
    d = state()
    msg_lines = [f"⏰ <b>Claude quota reset reached</b> "
                 f"({d.get('reset_at')})"]

    autorun_ok = bool(d.get("autorun"))
    max_tasks  = int(d.get("max_tasks") or 1)

    # Try the bridge if autorun is enabled
    if autorun_ok:
        try:
            from bot import coding_worker_bridge as bridge
            tool = bridge.get_preferred_coding_tool()
        except Exception as e:
            tool = None
            msg_lines.append(f"bridge import failed: {e}")
        if tool and tool.noninteractive_ok:
            msg_lines.append(f"Autorun: invoking {tool.name} for up to "
                             f"{max_tasks} task(s).")
            try:
                import asyncio
                results = asyncio.run(bridge.run_batch(max_tasks))
                for r in results:
                    msg_lines.append("• " + bridge.format_run_result(r)
                                       .replace("\n", " ")[:160])
                d["last_run_at"] = _now_iso()
            except Exception as e:
                msg_lines.append(f"bridge run_batch error: {e}")
        elif tool:
            msg_lines.append(f"⚠ {tool.name} is interactive-only — "
                             "manual run required: open a terminal and "
                             "run the worker per docs/CLAUDE_CODE_WORKER.md.")
        else:
            msg_lines.append("⚠ No coding CLI on PATH — install per "
                             "docs/CLAUDE_CODE_WORKER.md or use a manual "
                             "Claude/Codex session.")
    else:
        msg_lines.append("Autorun is OFF — set "
                         "<code>/claude_autorun_on 1</code> if you want "
                         "the bridge to fire next time.")

    d["last_notified_at"] = d.get("reset_at")
    d["limited"]          = False
    _save(d)
    send_telegram_message("\n".join(msg_lines))


def _scheduler_loop() -> None:
    while True:
        try:
            d = state()
            if _due(d):
                _on_due()
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL_SEC)


def start_scheduler_thread() -> bool:
    """Idempotent: launch the scheduler thread once per process."""
    global _thread_started
    with _thread_lock:
        if _thread_started:
            return False
        t = threading.Thread(target=_scheduler_loop, daemon=True,
                              name="claude-quota-scheduler")
        t.start()
        _thread_started = True
        return True
