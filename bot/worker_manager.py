"""
Worker Manager — tracks all active/known workers (channels, bots, future agents).

A "worker" is any process that executes tasks on behalf of the Brain.
Current workers: telegram_bot, tiktok_bot.
Future workers: browser_worker, coding_worker, ocr_worker, image_gen_worker.

Workers are registered in data/workers.json.
No SSH automation. No remote execution. Placeholder only for now.

WorkerTask / WorkerResult dataclasses define the typed communication contract
between the Brain and workers, inspired by OpenHands action/observation model.
"""
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORKERS_FILE = Path("/opt/tiktok-bot/data/workers.json")


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class WorkerTask:
    """A typed work order dispatched to a worker."""
    task_id:    str
    type:       str            # skill name or action type
    platform:   str            # "telegram" | "tiktok" | "internal"
    input:      dict = field(default_factory=dict)
    limits:     dict = field(default_factory=lambda: {"timeout_s": 30, "max_tokens": 600})
    risk_level: str  = "low"  # "low" | "medium" | "high"


@dataclass
class WorkerResult:
    """Typed result returned by a worker after executing a WorkerTask."""
    task_id:  str
    status:   str              # "done" | "failed" | "partial"
    items:    list = field(default_factory=list)
    files:    list = field(default_factory=list)   # output file paths
    summary:  str  = ""
    error:    str  = ""


@dataclass
class Worker:
    """A registered worker — channel bot, agent process, or future service."""
    id:           str
    type:         str          # "channel" | "agent" | "service"
    platform:     str          # "telegram" | "tiktok" | "internal"
    status:       str          # "active" | "inactive" | "unknown"
    capabilities: list = field(default_factory=list)
    last_seen:    str  = ""
    constraint:   str  = ""    # e.g. "TARGET_CHAT_NAME=Chatgibiti"
    metadata:     dict = field(default_factory=dict)


# ── Persistence helpers ───────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict[str, dict]:
    if WORKERS_FILE.exists():
        try:
            return json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict) -> None:
    WORKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKERS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Core API ──────────────────────────────────────────────────────────────────

def register_worker(
    worker_id: str,
    type_: str,
    platform: str,
    capabilities: list[str],
    constraint: str = "",
    metadata: dict | None = None,
) -> Worker:
    """Register or update a worker. Idempotent — safe to call on every startup."""
    data = _load()
    entry = data.get(worker_id, {})
    entry.update({
        "id":           worker_id,
        "type":         type_,
        "platform":     platform,
        "status":       "active",
        "capabilities": capabilities,
        "last_seen":    _now_utc(),
        "constraint":   constraint,
        "metadata":     metadata or {},
    })
    data[worker_id] = entry
    _save(data)
    return Worker(**{k: entry[k] for k in Worker.__dataclass_fields__})


def update_worker_status(worker_id: str, status: str) -> bool:
    """Set worker status (active/inactive/unknown). Returns False if not found."""
    data = _load()
    if worker_id not in data:
        return False
    data[worker_id]["status"]    = status
    data[worker_id]["last_seen"] = _now_utc()
    _save(data)
    return True


def touch_worker(worker_id: str) -> None:
    """Update last_seen without changing status. Call on each heartbeat."""
    data = _load()
    if worker_id in data:
        data[worker_id]["last_seen"] = _now_utc()
        _save(data)


def get_worker(worker_id: str) -> Optional[Worker]:
    data = _load()
    entry = data.get(worker_id)
    if not entry:
        return None
    return Worker(**{k: entry.get(k, "") for k in Worker.__dataclass_fields__})


def list_workers(status_filter: str | None = None) -> list[Worker]:
    """Return all workers, optionally filtered by status."""
    data = _load()
    workers = []
    for entry in data.values():
        if status_filter and entry.get("status") != status_filter:
            continue
        # Safely build Worker — fill missing fields with defaults
        kwargs = {k: entry.get(k, v.default if hasattr(v, "default") else "")
                  for k, v in Worker.__dataclass_fields__.items()}
        workers.append(Worker(**kwargs))
    return sorted(workers, key=lambda w: w.platform)


def worker_health() -> dict[str, str]:
    """
    Return a health map {worker_id: status_string}.
    Placeholder — in the future, this will ping each worker's heartbeat endpoint.
    """
    data = _load()
    result = {}
    for wid, entry in data.items():
        last = entry.get("last_seen", "")
        status = entry.get("status", "unknown")
        result[wid] = f"{status} (last_seen={last})"
    return result


def format_workers_list() -> str:
    """Human-readable worker list for Telegram /workers command."""
    workers = list_workers()
    if not workers:
        return "No workers registered yet."
    lines = ["<b>Workers</b>"]
    for w in workers:
        icon = "🟢" if w.status == "active" else "🔴"
        caps = ", ".join(w.capabilities[:4]) or "none"
        constraint = f"\n   ⚙️ {w.constraint}" if w.constraint else ""
        lines.append(
            f"{icon} <b>{w.id}</b> [{w.platform}]"
            f"\n   Type: {w.type} | Status: {w.status}"
            f"\n   Caps: {caps}"
            f"\n   Seen: {w.last_seen[:16]}{constraint}"
        )
    return "\n\n".join(lines)


# ── Seed default workers if file is empty ────────────────────────────────────

def _seed_defaults() -> None:
    """Register known workers if workers.json is empty or missing."""
    data = _load()
    if data:
        return

    register_worker(
        worker_id="telegram_bot",
        type_="channel",
        platform="telegram",
        capabilities=["send_message", "receive_message", "file_upload", "file_download",
                      "inline_menu", "callback_query"],
        constraint="ADMIN_CHAT_ID only",
    )
    register_worker(
        worker_id="tiktok_bot",
        type_="channel",
        platform="tiktok",
        capabilities=["read_chat", "send_message"],
        constraint="TARGET_CHAT_NAME=Chatgibiti",
    )


_seed_defaults()
