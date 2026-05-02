"""
Brain Evolution Loop v1.

A controlled, owner-friendly continuous self-improvement loop for the
agent. NOT an infinite daemon — it runs ONE task at a time, reports
each result, and stops on:

  - admin says stop ("dừng tự cải thiện" / /brain_evolve_stop)
  - high-risk task that needs admin confirmation
  - tests fail twice in a row
  - production unhealthy
  - no safe next task in the queue
  - Claude limited (pause; resume on quota reset via the existing
    claude_quota scheduler when autorun is enabled)

State file: data/brain_evolve.json (gitignored).

Public API:
    state()                                 -> dict
    start(max_tasks=1, *, user="tg_admin")  -> dict
    stop(*, user="tg_admin")                -> dict
    is_enabled()                            -> bool
    status_panel_vi()                       -> str
    on_task_done(result_dict, user)         -> bool   # True if loop continues
    advance_one(user)                       -> dict   # called by scheduler
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("/opt/tiktok-bot/data/brain_evolve.json")

_DEFAULT: dict = {
    "enabled":           False,
    "max_tasks_per_run": 1,
    "started_at":        None,
    "stopped_at":        None,
    "last_run_at":       None,
    "last_task_id":      "",
    "last_status":       "",
    "last_summary":      "",
    "run_count":         0,
    "consecutive_failures": 0,
    "user":              "",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state() -> dict:
    if not STATE_FILE.exists():
        return dict(_DEFAULT)
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        out = dict(_DEFAULT); out.update(d)
        return out
    except Exception:
        return dict(_DEFAULT)


def _save(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


# ── Owner control ─────────────────────────────────────────────────────────────

def start(max_tasks: int = 1, *, user: str = "tg_admin") -> dict:
    n = max(1, min(int(max_tasks), 3))
    d = state()
    d["enabled"]              = True
    d["max_tasks_per_run"]    = n
    d["started_at"]           = _now_iso()
    d["stopped_at"]           = None
    d["consecutive_failures"] = 0
    d["user"]                 = user
    _save(d)
    try:
        from bot.agent.audit_log import log_action
        log_action(user=user, action="brain_evolve_start",
                   risk_level="medium", status="ok",
                   result_summary=f"max_tasks={n}")
    except Exception:
        pass
    return d


def stop(*, user: str = "tg_admin", reason: str = "user") -> dict:
    d = state()
    d["enabled"]    = False
    d["stopped_at"] = _now_iso()
    d["last_status"] = f"stopped:{reason}"
    _save(d)
    try:
        from bot.agent.audit_log import log_action
        log_action(user=user, action="brain_evolve_stop",
                   risk_level="low", status="ok",
                   result_summary=f"reason={reason}")
    except Exception:
        pass
    return d


def is_enabled() -> bool:
    return bool(state().get("enabled"))


def _record_outcome(result: dict) -> None:
    d = state()
    d["last_run_at"]  = _now_iso()
    d["last_task_id"] = result.get("task_id", "")
    d["last_status"]  = result.get("status", "")
    d["last_summary"] = (result.get("summary") or "")[:300]
    d["run_count"]    = int(d.get("run_count") or 0) + 1
    if result.get("status") in ("worker_failed", "smoke_failed",
                                 "evals_failed", "commit_failed",
                                 "push_failed", "exec_error",
                                 "blocked_staged"):
        d["consecutive_failures"] = int(d.get("consecutive_failures") or 0) + 1
    elif result.get("status") in ("done", "no_changes"):
        d["consecutive_failures"] = 0
    _save(d)


def on_task_done(result: dict, user: str = "tg_admin") -> bool:
    """Called after coding_worker_bridge.run_once finishes.

    Updates state, decides whether the loop should auto-continue with
    the next queued task, and returns True if so.

    Stops automatically (sets enabled=False) when:
      - the admin already turned it off (race)
      - status=quota_limited / auth_required (paused; the claude_quota
        scheduler will resume autorun_enabled→run_batch later)
      - status=pending_action (high-risk → wait for admin)
      - consecutive_failures >= 2
    """
    d = state()
    if not d.get("enabled"):
        return False

    _record_outcome(result)
    d = state()  # re-read after write

    status = result.get("status", "")
    if status in ("pending_action",):
        stop(user=user, reason="pending_action")
        return False
    if status in ("quota_limited", "auth_required",
                   "no_tool", "interactive_only"):
        # Pause — keep enabled=True so /brain_evolve_status still reports
        # "active, waiting"; but don't try to immediately re-run. The
        # claude_quota scheduler will re-enter via run_batch when
        # autorun_enabled and quota returns.
        d["last_status"] = f"paused:{status}"
        _save(d)
        return False
    if status in ("worker_failed", "smoke_failed", "evals_failed",
                   "commit_failed", "push_failed", "exec_error",
                   "blocked_staged"):
        if int(d.get("consecutive_failures") or 0) >= 2:
            stop(user=user, reason="two_consecutive_failures")
            return False
        # Single failure: do not auto-continue this turn — let admin decide.
        return False
    if status in ("noop",):
        # No queued task — stop quietly, admin can queue more.
        stop(user=user, reason="queue_empty")
        return False

    # status in {done, no_changes, dry_run} — loop CAN continue.
    return True


async def advance_one(user: str = "tg_admin") -> dict:
    """Run exactly one bridge.run_once cycle and update brain-evolve state.
    Returns the run-result dict from the bridge."""
    from bot import coding_worker_bridge as bridge
    result = await bridge.run_once(user=user)
    on_task_done(result, user=user)
    return result


# ── Vietnamese status ─────────────────────────────────────────────────────────

def status_panel_vi() -> str:
    d = state()
    icon = "🟢" if d.get("enabled") else "⚪"
    lines = [f"{icon} <b>Brain Evolution Loop</b>"]
    lines.append(f"Trạng thái: <b>"
                 f"{'đang chạy' if d.get('enabled') else 'đã dừng'}</b>")
    lines.append(f"Max tasks/run: <b>{d.get('max_tasks_per_run', 1)}</b>")
    if d.get("started_at"):
        lines.append(f"Bắt đầu: <i>{d['started_at']}</i>")
    if d.get("stopped_at"):
        lines.append(f"Dừng: <i>{d['stopped_at']}</i>")
    if d.get("last_run_at"):
        lines.append(f"Run gần nhất: <i>{d['last_run_at']}</i>")
    if d.get("last_task_id"):
        lines.append(f"Task gần nhất: <code>{d['last_task_id']}</code> "
                     f"(<i>{d.get('last_status','?')}</i>)")
    lines.append(f"Tổng run_count: <b>{d.get('run_count', 0)}</b> · "
                 f"thất bại liên tiếp: <b>"
                 f"{d.get('consecutive_failures', 0)}</b>")
    if d.get("last_summary"):
        s = (d["last_summary"]
             .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        lines.append(f"<i>{s[:200]}</i>")
    return "\n".join(lines)
