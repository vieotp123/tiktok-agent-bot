"""
Audit log — append-only JSONL at data/audit/actions.jsonl.
Logs every task/action. Never logs secrets.

Schema (one JSON object per line):
  timestamp      UTC ISO 8601
  user           actor (tg_admin, tiktok_chatgibiti, task_<id>, ...)
  channel        source channel (telegram | tiktok | internal | task)
  action         skill or command name (run_task:search_web, /status, ...)
  risk_level     low | medium | high
  status         ok | done | failed | pending | cancelled
  result_summary truncated to 200 chars, no secrets
  task_id        task_queue row id if applicable
  goal           user intent, truncated to 120 chars
"""
import json
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DIR  = Path("/opt/tiktok-bot/data/audit")
AUDIT_FILE = AUDIT_DIR / "actions.jsonl"

_SECRET_KEYS = frozenset({"token", "password", "secret", "key", "api_key", "auth"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_action(
    user: str,
    action: str,
    risk_level: str = "low",
    status: str = "ok",
    result_summary: str = "",
    task_id: str = "",
    channel: str = "",
    **extra,
) -> None:
    """
    Append one audit entry. Silently drops keys that look like secrets.

    channel: "telegram" | "tiktok" | "internal" | "task" (auto-inferred if empty)
    """
    if not channel:
        u = user.lower()
        if u.startswith("tg_"):
            channel = "telegram"
        elif u.startswith("task_"):
            channel = "task"
        else:
            channel = "tiktok"

    entry: dict = {
        "timestamp":      _now(),
        "user":           user,
        "channel":        channel,
        "action":         action,
        "risk_level":     risk_level,
        "status":         status,
        "result_summary": result_summary[:200],
        "task_id":        task_id,
    }
    # Merge extra fields, skipping secret-looking keys
    for k, v in extra.items():
        if k.lower() not in _SECRET_KEYS:
            entry[k] = str(v)[:200] if isinstance(v, str) else v

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def tail_audit(n: int = 20) -> list[dict]:
    """Return last N audit entries (newest last)."""
    if not AUDIT_FILE.exists():
        return []
    lines = AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
    entries: list[dict] = []
    for line in lines[-n:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


# Alias used by Telegram /audit_recent command
tail_recent = tail_audit


def format_audit_recent(n: int = 10) -> str:
    """Human-readable audit entries for Telegram /audit_recent command."""
    entries = tail_audit(n)
    if not entries:
        return "No audit entries yet."
    icon_map = {"low": "🔵", "medium": "🟡", "high": "🔴"}
    lines = [f"<b>Audit log</b> (last {len(entries)})"]
    for e in entries:
        icon = icon_map.get(e.get("risk_level", "low"), "⚪")
        status = e.get("status", "")
        status_icon = "✅" if status in ("ok", "done") else ("❌" if status == "failed" else "⏳")
        ts = e.get("timestamp", "")[:16]
        action = e.get("action", "")
        summary = e.get("result_summary", "")[:80]
        channel = e.get("channel", "")
        lines.append(
            f"{icon}{status_icon} <b>{action}</b> [{channel}]"
            + (f"\n   {summary}" if summary else "")
            + f"\n   <i>{ts}</i>"
        )
    return "\n\n".join(lines)
