import json
import os
import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional, Callable
import pytz

REMINDERS_FILE = "/opt/tiktok-bot/data/reminders.json"
TZ = pytz.timezone("Asia/Tokyo")


def _load() -> list:
    if not os.path.exists(REMINDERS_FILE):
        return []
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(data: list) -> None:
    os.makedirs(os.path.dirname(REMINDERS_FILE), exist_ok=True)
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_jst() -> datetime:
    return datetime.now(TZ)


def parse_reminder_time(text: str) -> Optional[datetime]:
    """
    Parse Vietnamese reminder time expressions.
    Returns aware datetime in Asia/Tokyo, or None if ambiguous/unparseable.
    """
    now = now_jst()
    text_lower = text.lower().strip()

    # Pattern: "7h tối", "7h30 tối", "19h", "7:30 tối", "7 giờ tối"
    # Time keywords
    am_kw = ["sáng", "buổi sáng"]
    pm_kw = ["tối", "chiều", "buổi tối", "buổi chiều"]

    # Extract hour and minute
    time_match = re.search(
        r'(\d{1,2})[h:giờ\s]+(\d{0,2})\s*(sáng|tối|chiều|trưa)?', text_lower
    )
    if not time_match:
        time_match = re.search(r'(\d{1,2})\s*(h|giờ)\s*(\d{0,2})', text_lower)

    hour, minute, period = None, 0, None

    if time_match:
        groups = time_match.groups()
        hour = int(groups[0])
        try:
            minute = int(groups[1]) if groups[1] else 0
        except (ValueError, IndexError):
            minute = 0
        period = groups[-1] if groups[-1] in ["sáng", "tối", "chiều", "trưa"] else None

    if hour is None:
        return None

    # Adjust hour for AM/PM
    if period in pm_kw and hour < 12:
        hour += 12
    elif period in am_kw and hour == 12:
        hour = 0
    elif period == "trưa":
        hour = 12

    # Determine date
    target_date = now.date()

    if "ngày mai" in text_lower or "mai" in text_lower:
        target_date = (now + timedelta(days=1)).date()
    elif "ngày kia" in text_lower or "kia" in text_lower:
        target_date = (now + timedelta(days=2)).date()
    else:
        # If time already passed today, schedule for tomorrow
        candidate = TZ.localize(datetime(target_date.year, target_date.month, target_date.day, hour, minute))
        if candidate <= now:
            target_date = (now + timedelta(days=1)).date()

    target_dt = TZ.localize(datetime(target_date.year, target_date.month, target_date.day, hour, minute))
    return target_dt


def add_reminder(username: str, text: str, remind_at: datetime, original_msg: str) -> dict:
    reminders = _load()
    entry = {
        "id": f"{username}_{int(remind_at.timestamp())}",
        "username": username,
        "text": text,
        "remind_at": remind_at.isoformat(),
        "original_msg": original_msg,
        "done": False,
        "created_at": now_jst().isoformat(),
    }
    reminders.append(entry)
    _save(reminders)
    return entry


def get_pending_reminders() -> list:
    reminders = _load()
    now = now_jst()
    pending = []
    for r in reminders:
        if r.get("done"):
            continue
        try:
            remind_at = datetime.fromisoformat(r["remind_at"])
            if remind_at.tzinfo is None:
                remind_at = TZ.localize(remind_at)
            if remind_at <= now:
                pending.append(r)
        except Exception:
            continue
    return pending


def mark_done(reminder_id: str) -> None:
    reminders = _load()
    for r in reminders:
        if r.get("id") == reminder_id:
            r["done"] = True
    _save(reminders)


def get_user_reminders(username: str) -> list:
    reminders = _load()
    now = now_jst()
    result = []
    for r in reminders:
        if r.get("username") != username or r.get("done"):
            continue
        try:
            remind_at = datetime.fromisoformat(r["remind_at"])
            if remind_at.tzinfo is None:
                remind_at = TZ.localize(remind_at)
            if remind_at > now:
                result.append(r)
        except Exception:
            continue
    return sorted(result, key=lambda x: x["remind_at"])


def format_reminders_list(username: str) -> str:
    items = get_user_reminders(username)
    if not items:
        return "mày không có reminder nào đang chờ."
    lines = []
    for i, r in enumerate(items, 1):
        try:
            dt = datetime.fromisoformat(r["remind_at"])
            if dt.tzinfo is None:
                dt = TZ.localize(dt)
            dt_str = dt.strftime("%d/%m %H:%M")
        except Exception:
            dt_str = r.get("remind_at", "?")
        lines.append(f"{i}. [{dt_str}] {r['text']}")
    return "Reminder của mày:\n" + "\n".join(lines)


async def reminder_loop(send_fn: Callable, interval: float = 30.0):
    """Background task: check reminders every `interval` seconds."""
    while True:
        try:
            pending = get_pending_reminders()
            for r in pending:
                msg = f"⏰ Nhắc mày: {r['text']}"
                try:
                    await send_fn(msg)
                except Exception as e:
                    print(f"[reminder] send failed: {e}")
                mark_done(r["id"])
        except Exception as e:
            print(f"[reminder] loop error: {e}")
        await asyncio.sleep(interval)
