import json
import os
from datetime import datetime
from typing import Any

MEMORY_FILE = "/opt/tiktok-bot/data/memory.json"


def _load() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_memory(username: str) -> dict:
    data = _load()
    return data.get(username, {
        "facts": [],
        "preferences": [],
        "plans": [],
        "recent_messages": [],
    })


def save_user_memory(username: str, memory: dict) -> None:
    data = _load()
    data[username] = memory
    _save(data)


def add_message(username: str, role: str, content: str, max_recent: int = 20) -> None:
    data = _load()
    user = data.get(username, {
        "facts": [],
        "preferences": [],
        "plans": [],
        "recent_messages": [],
    })
    msgs = user.get("recent_messages", [])
    msgs.append({
        "role": role,
        "content": content,
        "ts": datetime.now().isoformat(),
    })
    user["recent_messages"] = msgs[-max_recent:]
    data[username] = user
    _save(data)


def add_fact(username: str, fact: str) -> None:
    data = _load()
    user = data.get(username, {"facts": [], "preferences": [], "plans": [], "recent_messages": []})
    facts = user.get("facts", [])
    norm = fact.strip().lower()
    if norm and not any(f.strip().lower() == norm for f in facts):
        facts.append(fact.strip())
    user["facts"] = facts[-30:]
    data[username] = user
    _save(data)


def forget_user(username: str) -> None:
    data = _load()
    if username in data:
        del data[username]
        _save(data)


def format_memory_for_prompt(username: str) -> str:
    mem = get_user_memory(username)
    parts = []
    if mem.get("facts"):
        parts.append("Facts: " + "; ".join(mem["facts"][-10:]))
    if mem.get("preferences"):
        parts.append("Preferences: " + "; ".join(mem["preferences"][-5:]))
    if mem.get("plans"):
        parts.append("Plans: " + "; ".join(mem["plans"][-5:]))
    return "\n".join(parts) if parts else ""


def get_recent_messages(username: str, n: int = 10) -> list:
    mem = get_user_memory(username)
    return mem.get("recent_messages", [])[-n:]


def get_all_users() -> list:
    data = _load()
    return list(data.keys())


def get_memory_summary(username: str) -> str:
    mem = get_user_memory(username)
    lines = []
    if mem.get("facts"):
        lines.append(f"Facts ({len(mem['facts'])}): " + "; ".join(mem["facts"][-5:]))
    if mem.get("preferences"):
        lines.append(f"Prefs: " + "; ".join(mem["preferences"][-3:]))
    if mem.get("plans"):
        lines.append(f"Plans: " + "; ".join(mem["plans"][-3:]))
    msgs = mem.get("recent_messages", [])
    lines.append(f"Recent msgs: {len(msgs)}")
    return "\n".join(lines) if lines else "Chưa có gì trong bộ nhớ."
