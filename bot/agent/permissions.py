"""
Permission layer — risk-based approval for agent actions.

low    → execute immediately, log only
medium → execute immediately, log with medium flag
high   → create pending_action in data/pending_actions.json,
         wait for /confirm_action <id> or /cancel_action <id>
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

PENDING_PATH = Path("/opt/tiktok-bot/data/pending_actions.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    if PENDING_PATH.exists():
        try:
            return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def requires_confirm(risk_level: str) -> bool:
    """True if this risk_level requires explicit /confirm_action."""
    return risk_level == "high"


def create_pending(
    action: str,
    goal: str,
    risk_level: str,
    user: str,
    metadata: dict | None = None,
) -> str:
    """
    Create a pending_action entry.
    Returns the action_id (short UUID).
    """
    action_id = str(uuid.uuid4())[:8]
    data = _load()
    data[action_id] = {
        "action_id": action_id,
        "action": action,
        "goal": goal,
        "risk_level": risk_level,
        "user": user,
        "status": "pending",
        "metadata": metadata or {},
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save(data)
    return action_id


def get_pending(action_id: str) -> dict | None:
    return _load().get(action_id)


def confirm_pending(action_id: str) -> bool:
    data = _load()
    if action_id not in data:
        return False
    data[action_id]["status"] = "confirmed"
    data[action_id]["updated_at"] = _now()
    _save(data)
    return True


def cancel_pending(action_id: str) -> bool:
    data = _load()
    if action_id not in data:
        return False
    data[action_id]["status"] = "cancelled"
    data[action_id]["updated_at"] = _now()
    _save(data)
    return True


def list_pending(only_pending: bool = True) -> list[dict]:
    data = _load()
    items = list(data.values())
    if only_pending:
        items = [i for i in items if i["status"] == "pending"]
    return sorted(items, key=lambda x: x["created_at"], reverse=True)
