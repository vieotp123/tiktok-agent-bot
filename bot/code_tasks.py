"""
Code Task Queue — durable queue of coding work the Claude/Codex worker
should pick up.

Storage: SQLite at data/code_tasks.db (gitignored).

Lifecycle:
  queued → running → testing → deploying → done
                              → failed
                              → waiting_confirm → confirmed → ...
                              → cancelled

Risk levels (set by the *creator*):
  low    — refactor/docs/single-file fix; auto-deploy after tests pass
  medium — multi-file feature; auto-deploy after tests pass + audit
  high   — touches TikTok reader / .env / storage_state / systemd /
           main branch / external public action — must be confirmed by
           admin via /confirm_action before deploy

CLI (used by Claude/Codex worker sessions):
  python -m bot.code_tasks list
  python -m bot.code_tasks add "<title>" [--priority N] [--risk low|medium|high]
                                          [--description "..."]
  python -m bot.code_tasks info <id>
  python -m bot.code_tasks start <id>
  python -m bot.code_tasks finish <id> --commit <hash> --summary "..."
  python -m bot.code_tasks fail <id> --summary "..."
  python -m bot.code_tasks cancel <id>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/tiktok-bot/data/code_tasks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS code_tasks (
    id              TEXT    PRIMARY KEY,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    priority        INTEGER NOT NULL DEFAULT 5,
    risk_level      TEXT    NOT NULL DEFAULT 'low',  -- low | medium | high
    status          TEXT    NOT NULL DEFAULT 'queued',
    branch          TEXT    NOT NULL DEFAULT 'dev-agent',
    commit_hash     TEXT    NOT NULL DEFAULT '',
    deployed_commit TEXT    NOT NULL DEFAULT '',
    test_summary    TEXT    NOT NULL DEFAULT '',
    deploy_summary  TEXT    NOT NULL DEFAULT '',
    rollback_summary TEXT   NOT NULL DEFAULT '',
    created_by      TEXT    NOT NULL DEFAULT 'tg_admin',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_code_tasks_status   ON code_tasks(status);
CREATE INDEX IF NOT EXISTS idx_code_tasks_priority ON code_tasks(priority);
"""

_VALID_RISK   = {"low", "medium", "high"}
_VALID_STATUS = {
    "queued", "running", "testing", "deploying",
    "done", "failed", "cancelled", "waiting_confirm",
}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return f"ctk_{uuid.uuid4().hex[:10]}"


# ── Public API ────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn():
        pass


def add_task(title: str, *,
             description: str = "",
             priority: int = 5,
             risk_level: str = "low",
             branch: str = "dev-agent",
             created_by: str = "tg_admin") -> str:
    if risk_level not in _VALID_RISK:
        raise ValueError(f"risk_level must be one of {_VALID_RISK}")
    tid = _new_id()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO code_tasks "
            "(id, title, description, priority, risk_level, status, branch, "
            " created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (tid, title[:200], description[:2000], int(priority),
             risk_level, branch, created_by, now, now),
        )
    return tid


def update_task(task_id: str, **fields) -> bool:
    if not fields:
        return False
    allowed = {
        "title", "description", "priority", "risk_level", "status",
        "branch", "commit_hash", "deployed_commit",
        "test_summary", "deploy_summary", "rollback_summary",
    }
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status" and v not in _VALID_STATUS:
            continue
        if k == "risk_level" and v not in _VALID_RISK:
            continue
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return False
    sets.append("updated_at=?"); params.append(_now())
    params.append(task_id)
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE code_tasks SET {', '.join(sets)} WHERE id=?", params,
        )
    return cur.rowcount > 0


def get_task(task_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM code_tasks WHERE id=?",
                         (task_id,)).fetchone()
    return dict(r) if r else None


def list_tasks(status: str | None = None, limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM code_tasks"
    params: list = []
    if status:
        sql += " WHERE status=?"; params.append(status)
    sql += " ORDER BY (status='queued') DESC, priority DESC, created_at ASC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def next_queued_task() -> dict | None:
    """Return the highest-priority queued task (FIFO within same priority)."""
    items = list_tasks(status="queued", limit=1)
    return items[0] if items else None


def cancel_task(task_id: str) -> bool:
    return update_task(task_id, status="cancelled")


# ── Convenience for the worker loop ───────────────────────────────────────────

def start_task(task_id: str) -> bool:
    return update_task(task_id, status="running")


def finish_task(task_id: str, *, commit_hash: str = "",
                test_summary: str = "", deploy_summary: str = "") -> bool:
    return update_task(
        task_id, status="done",
        commit_hash=commit_hash, test_summary=test_summary,
        deploy_summary=deploy_summary,
    )


def fail_task(task_id: str, *, test_summary: str = "") -> bool:
    return update_task(task_id, status="failed",
                       test_summary=test_summary)


# ── Formatting (Telegram) ─────────────────────────────────────────────────────

def format_tasks_list(status: str | None = None, limit: int = 15) -> str:
    items = list_tasks(status=status, limit=limit)
    if not items:
        target = f" — status={status}" if status else ""
        return f"<b>Code tasks</b>{target}\n(no tasks)"
    icon = {
        "queued":          "🕐",
        "running":         "⏳",
        "testing":         "🧪",
        "deploying":       "🚀",
        "done":            "✅",
        "failed":          "❌",
        "cancelled":       "🚫",
        "waiting_confirm": "⏸",
    }
    risk = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    lines = [f"<b>Code tasks</b>" + (f" — {status}" if status else "")]
    for t in items:
        title = (t.get("title") or "")[:48]
        title = (title.replace("&", "&amp;")
                       .replace("<", "&lt;")
                       .replace(">", "&gt;"))
        lines.append(
            f"{icon.get(t['status'], '•')}{risk.get(t['risk_level'], '⚪')} "
            f"<code>{t['id']}</code> "
            f"<b>{title}</b> "
            f"<i>p{t['priority']} · {t['created_at'][11:16]}</i>"
        )
    return "\n".join(lines)


def format_task_detail(task_id: str) -> str:
    t = get_task(task_id)
    if not t:
        return f"Task <code>{task_id}</code> không tồn tại."

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    risk = {"low": "🟢 low", "medium": "🟡 medium", "high": "🔴 high"}
    lines = [
        f"<b>Code Task {t['id']}</b>",
        f"Title: {esc(t['title'])}",
        f"Risk: {risk.get(t['risk_level'], t['risk_level'])} "
        f"| Priority: {t['priority']} | Status: <b>{t['status']}</b>",
        f"Branch: {esc(t.get('branch', ''))}",
    ]
    if t.get("description"):
        lines.append(f"Description:\n<pre>{esc(t['description'][:1000])}</pre>")
    if t.get("commit_hash"):
        lines.append(f"Commit: <code>{esc(t['commit_hash'][:14])}</code>")
    if t.get("test_summary"):
        lines.append(f"Test: {esc(t['test_summary'][:300])}")
    if t.get("deploy_summary"):
        lines.append(f"Deploy: {esc(t['deploy_summary'][:300])}")
    if t.get("rollback_summary"):
        lines.append(f"Rollback: {esc(t['rollback_summary'][:300])}")
    lines.append(f"Created: {t['created_at']} by {esc(t.get('created_by',''))}")
    lines.append(f"Updated: {t['updated_at']}")
    return "\n".join(lines)


def format_status_summary() -> str:
    """High-level worker status: counts by status."""
    counts: dict[str, int] = {}
    for s in _VALID_STATUS:
        counts[s] = 0
    with _conn() as conn:
        for r in conn.execute(
            "SELECT status, COUNT(*) c FROM code_tasks GROUP BY status"
        ).fetchall():
            counts[r["status"]] = r["c"]
    paused = (Path("/opt/tiktok-bot/data/code_tasks.paused")).exists()
    lines = [
        "<b>🛠 Code Worker Status</b>",
        f"Worker: {'⏸ paused' if paused else '▶ active'}",
        f"queued={counts.get('queued',0)}  running={counts.get('running',0)}  "
        f"testing={counts.get('testing',0)}  deploying={counts.get('deploying',0)}",
        f"done={counts.get('done',0)}  failed={counts.get('failed',0)}  "
        f"cancelled={counts.get('cancelled',0)}  "
        f"waiting_confirm={counts.get('waiting_confirm',0)}",
    ]
    nx = next_queued_task()
    if nx:
        lines.append(f"\nNext: <code>{nx['id']}</code> — {nx['title'][:60]}")
    return "\n".join(lines)


# ── Pause/Resume control file ─────────────────────────────────────────────────

_PAUSE_FILE = Path("/opt/tiktok-bot/data/code_tasks.paused")


def is_paused() -> bool:
    return _PAUSE_FILE.exists()


def pause() -> None:
    _PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PAUSE_FILE.write_text(_now())


def resume() -> None:
    try:
        _PAUSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _cli() -> int:
    p = argparse.ArgumentParser(prog="bot.code_tasks")
    sub = p.add_subparsers(dest="cmd", required=True)

    sl = sub.add_parser("list")
    sl.add_argument("--status", default=None)

    sa = sub.add_parser("add")
    sa.add_argument("title")
    sa.add_argument("--description", default="")
    sa.add_argument("--priority", type=int, default=5)
    sa.add_argument("--risk", default="low", choices=sorted(_VALID_RISK))

    si = sub.add_parser("info"); si.add_argument("id")
    sst = sub.add_parser("start"); sst.add_argument("id")
    sf = sub.add_parser("finish"); sf.add_argument("id")
    sf.add_argument("--commit", default=""); sf.add_argument("--summary", default="")
    sfx = sub.add_parser("fail"); sfx.add_argument("id"); sfx.add_argument("--summary", default="")
    sc = sub.add_parser("cancel"); sc.add_argument("id")
    sub.add_parser("status")
    sub.add_parser("next")
    sub.add_parser("pause")
    sub.add_parser("resume")

    args = p.parse_args()
    init_db()

    if args.cmd == "list":
        for t in list_tasks(status=args.status, limit=50):
            print(f"{t['status']:<10} {t['risk_level']:<6} p{t['priority']:>2} "
                  f"{t['id']}  {t['title'][:60]}")
        return 0
    if args.cmd == "add":
        tid = add_task(args.title, description=args.description,
                       priority=args.priority, risk_level=args.risk,
                       created_by="cli")
        print(tid); return 0
    if args.cmd == "info":
        t = get_task(args.id)
        if not t: print("not found", file=sys.stderr); return 1
        print(json.dumps(t, indent=2)); return 0
    if args.cmd == "start":
        return 0 if start_task(args.id) else 1
    if args.cmd == "finish":
        return 0 if finish_task(args.id, commit_hash=args.commit,
                                  test_summary=args.summary) else 1
    if args.cmd == "fail":
        return 0 if fail_task(args.id, test_summary=args.summary) else 1
    if args.cmd == "cancel":
        return 0 if cancel_task(args.id) else 1
    if args.cmd == "status":
        # Plain text version for CLI
        t = format_status_summary()
        # strip HTML for CLI
        import re; print(re.sub(r"<[^>]+>", "", t)); return 0
    if args.cmd == "next":
        nx = next_queued_task()
        if not nx: print(""); return 1
        print(nx["id"]); return 0
    if args.cmd == "pause":
        pause(); print("paused"); return 0
    if args.cmd == "resume":
        resume(); print("resumed"); return 0
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
