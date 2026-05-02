"""
Audit log — append-only JSONL at data/audit/actions.jsonl.
Logs every task/action. Never logs secrets.
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
    **extra,
) -> None:
    """Append one audit entry. Silently drops keys that look like secrets."""
    entry: dict = {
        "timestamp":      _now(),
        "user":           user,
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
