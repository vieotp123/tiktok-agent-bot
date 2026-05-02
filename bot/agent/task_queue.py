"""
SQLite-backed task queue.

Task fields:
  task_id        TEXT PRIMARY KEY
  type           TEXT   -- search_web / btc_price / chat / ...
  goal           TEXT   -- human-readable objective
  status         TEXT   -- queued / running / done / failed / cancelled / waiting_confirm
  progress       TEXT
  result_summary TEXT
  result_path    TEXT
  error          TEXT
  created_at     TEXT   (ISO-8601 UTC)
  updated_at     TEXT
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/tiktok-bot/data/tasks.db")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id        TEXT PRIMARY KEY,
                type           TEXT NOT NULL DEFAULT '',
                goal           TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'queued',
                progress       TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                result_path    TEXT NOT NULL DEFAULT '',
                error          TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            )
        """)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_task(type_: str, goal: str, status: str = "queued") -> str:
    init_db()
    task_id = str(uuid.uuid4())[:8]
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO tasks"
            " (task_id, type, goal, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (task_id, type_, goal, status, now, now),
        )
    return task_id


def update_task(task_id: str, **kwargs) -> None:
    init_db()
    kwargs["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", vals)


def get_task(task_id: str) -> dict | None:
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 15) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_task(task_id: str) -> bool:
    task = get_task(task_id)
    if not task:
        return False
    if task["status"] in ("done", "failed", "cancelled"):
        return False
    update_task(task_id, status="cancelled")
    return True
