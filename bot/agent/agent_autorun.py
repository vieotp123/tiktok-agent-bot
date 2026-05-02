"""
Agent Autorun Loop — owner-directed long-horizon work session.

Different from `brain_evolve` (which is one-task self-improve, cap 3):
this is the "làm việc độc lập 12 tiếng" loop. Owner says how many hours
to work, max tasks, optional objective. Loop:

  1. Pre-tick: check stop_at, paused_reason, claude quota.
  2. Pick next code_task (queued, lowest priority number first).
  3. Refine prompt via 9Router GPT-5.5 (best-effort; fallback to
     deterministic prompt_builder).
  4. Run via coding_worker_bridge.run_once().
  5. Record outcome. Auto-stop on:
       - stop_at reached
       - completed_tasks >= max_tasks
       - claude limited (pause; resume on quota return)
       - 2 consecutive non-quota failures
       - admin says stop
  6. Post-tick: report to Telegram.

State file: data/agent_autorun.json (gitignored).

Public API:
    state()                                     -> dict
    start(hours, max_tasks, objective, user)    -> dict
    stop(user, reason)                          -> dict
    is_enabled()                                -> bool
    is_due_to_stop()                            -> tuple[bool, str]
    record_outcome(result)                      -> None
    pause(reason)                               -> None
    resume()                                    -> None
    status_panel_vi()                           -> str
    advance_one(user)                           -> dict
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("/opt/tiktok-bot/data/agent_autorun.json")

_DEFAULT: dict = {
    "enabled":              False,
    "objective":            "",
    "started_at":           None,
    "stop_at":              None,
    "hours":                0.0,
    "max_tasks":            0,
    "completed_tasks":      0,
    "consecutive_failures": 0,
    "paused_reason":        "",
    "next_probe_at":        None,
    "last_run_at":          None,
    "last_task_id":         "",
    "last_status":          "",
    "last_summary":         "",
    "user":                 "",
    # Self-improve mode: when True, autorun pulls roadmap items via
    # bot.agent.self_improve.run_once() when the code-task queue is
    # empty, so the loop runs "tới khi hết quota" instead of stopping
    # on the first noop.
    "auto_self_improve":    False,
    "self_improve_runs":    0,
    # Roadmap items already attempted this autorun session — used to
    # avoid the infinite-loop where Claude can't complete an item
    # (e.g. "Verify product catalog" requires owner data per Operating
    # Rules §4 "never invent prices") and self_improve keeps re-pulling
    # the same first-unchecked item.
    "attempted_items":      [],
}

_FAIL_STATUSES = {
    "worker_failed", "smoke_failed", "evals_failed",
    "commit_failed", "push_failed", "exec_error", "blocked_staged",
}
_PAUSE_STATUSES = {
    "quota_limited", "auth_required",
    "no_tool", "interactive_only",
}
_OK_STATUSES = {"done", "no_changes", "dry_run"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def state() -> dict:
    if not STATE_FILE.exists():
        return dict(_DEFAULT)
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        out = dict(_DEFAULT)
        out.update(d)
        return out
    except Exception:
        return dict(_DEFAULT)


def _save(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


# ── Owner control ─────────────────────────────────────────────────────────────

def start(
    hours: float = 12.0,
    max_tasks: int = 20,
    objective: str = "",
    *,
    user: str = "tg_admin",
    auto_self_improve: bool = False,
) -> dict:
    h = float(max(0.1, min(hours, 24.0)))
    # Self-improve mode allows up to 999 tasks (effectively unlimited
    # for the hour budget); regular autorun caps at 200.
    cap = 999 if auto_self_improve else 200
    n = int(max(1, min(int(max_tasks), cap)))
    started = _now()
    stop_at = started + timedelta(hours=h)
    d = state()
    d["enabled"]              = True
    d["objective"]            = (objective or "")[:500]
    d["started_at"]           = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["stop_at"]              = stop_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["hours"]                = h
    d["max_tasks"]            = n
    d["completed_tasks"]      = 0
    d["consecutive_failures"] = 0
    d["paused_reason"]        = ""
    d["next_probe_at"]        = None
    d["user"]                 = user
    d["auto_self_improve"]    = bool(auto_self_improve)
    d["self_improve_runs"]    = 0
    d["attempted_items"]      = []
    _save(d)
    try:
        from bot.agent.audit_log import log_action
        log_action(user=user, action="agent_autorun_start",
                   risk_level="medium", status="ok",
                   result_summary=f"hours={h} max_tasks={n} "
                                  f"self_improve={auto_self_improve} "
                                  f"obj={(objective or '')[:60]}")
    except Exception:
        pass
    return d


def stop(*, user: str = "tg_admin", reason: str = "user") -> dict:
    d = state()
    was_enabled = d.get("enabled")
    d["enabled"]   = False
    d["last_status"] = f"stopped:{reason}"
    _save(d)
    try:
        from bot.agent.audit_log import log_action
        log_action(user=user, action="agent_autorun_stop",
                   risk_level="low", status="ok",
                   result_summary=f"reason={reason} "
                                  f"completed={d.get('completed_tasks',0)}")
    except Exception:
        pass
    return d


def is_enabled() -> bool:
    return bool(state().get("enabled"))


def is_due_to_stop() -> tuple[bool, str]:
    """Returns (should_stop, reason). Called by scheduler before each tick."""
    d = state()
    if not d.get("enabled"):
        return True, "not_enabled"
    stop_at = _parse_iso(d.get("stop_at") or "")
    if stop_at and _now() >= stop_at:
        return True, "hours_reached"
    completed  = int(d.get("completed_tasks") or 0)
    max_tasks  = int(d.get("max_tasks") or 0)
    if max_tasks > 0 and completed >= max_tasks:
        return True, "max_tasks_reached"
    if int(d.get("consecutive_failures") or 0) >= 2:
        return True, "two_failures"
    return False, ""


def pause(reason: str, retry_after_seconds: int | None = None) -> None:
    d = state()
    d["paused_reason"] = reason[:80]
    if retry_after_seconds and retry_after_seconds > 0:
        nxt = _now() + timedelta(seconds=int(retry_after_seconds))
        d["next_probe_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
    _save(d)


def resume() -> None:
    d = state()
    d["paused_reason"] = ""
    d["next_probe_at"] = None
    _save(d)


def is_paused() -> bool:
    d = state()
    if not d.get("enabled"):
        return False
    return bool(d.get("paused_reason"))


def can_probe_now() -> bool:
    """Used by scheduler: true if pause has expired (or no pause set)."""
    d = state()
    if not d.get("paused_reason"):
        return True
    nxt = _parse_iso(d.get("next_probe_at") or "")
    if not nxt:
        return True
    return _now() >= nxt


def record_outcome(result: dict) -> None:
    """Update state after one bridge.run_once cycle."""
    d = state()
    if not d.get("enabled"):
        return
    status  = (result or {}).get("status", "")
    task_id = (result or {}).get("task_id", "")
    summary = ((result or {}).get("summary") or "")[:300]

    d["last_run_at"]  = _now_iso()
    d["last_task_id"] = task_id
    d["last_status"]  = status
    d["last_summary"] = summary

    if status in _OK_STATUSES:
        d["completed_tasks"]      = int(d.get("completed_tasks") or 0) + 1
        d["consecutive_failures"] = 0
        d["paused_reason"]        = ""
    elif status in _PAUSE_STATUSES:
        # Pause — keep enabled True. Schedule retry in 1h by default.
        d["paused_reason"] = status
        nxt = _now() + timedelta(hours=1)
        d["next_probe_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif status in _FAIL_STATUSES:
        d["consecutive_failures"] = int(d.get("consecutive_failures") or 0) + 1
    elif status == "pending_action":
        # High-risk task surfaced a confirm; stop autorun to wait for owner.
        d["enabled"] = False
        d["last_status"] = "stopped:pending_action"
    elif status == "noop":
        # Queue empty.
        if d.get("auto_self_improve"):
            # Self-improve mode: don't stop — caller will populate
            # queue from roadmap and try again.
            d["last_status"] = "queue_empty_will_self_improve"
        else:
            d["enabled"] = False
            d["last_status"] = "stopped:queue_empty"

    _save(d)


def populate_queue_from_roadmap() -> dict:
    """When queue is empty in self-improve mode, run self_improve_once
    to pull the next roadmap item into the code_task queue.

    Returns a dict describing what was queued (or noop if roadmap empty).
    Safe to call repeatedly — self_improve.run_once de-dupes by checking
    the queue first.
    """
    try:
        from bot.agent import self_improve as _si
        d = state()
        # Pass attempted-items so self_improve skips them instead of
        # re-queuing the same first unchecked roadmap item forever
        # (the infinite-loop bug owner caught in autorun cycle 2+).
        skip_titles = set(d.get("attempted_items") or [])
        result = _si.self_improve_once(user="autorun_pump",
                                         skip_titles=skip_titles)
        # Record the roadmap item we just attempted so the next pump
        # cycle skips it. Status "queued" / "pending_action" both mean
        # we picked an item; "noop" / "existing_queue" / "error" mean
        # we didn't pick a new one (don't add to attempted).
        if (result.get("status") in ("queued", "pending_action")
                and result.get("roadmap_item")):
            attempted = list(d.get("attempted_items") or [])
            if result["roadmap_item"] not in attempted:
                attempted.append(result["roadmap_item"])
            d["attempted_items"] = attempted[-50:]  # cap memory
        d["self_improve_runs"] = int(d.get("self_improve_runs") or 0) + 1
        _save(d)
        return result if isinstance(result, dict) else {
            "status": "ran",
            "summary": str(result)[:200],
        }
    except Exception as e:
        return {"status": "error", "summary": f"self_improve error: {e}"}


# ── Vietnamese parsing helpers ────────────────────────────────────────────────

_RE_HOURS = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(?:tiếng|giờ|gio|h(?:our|rs)?|hours?)\b",
    re.IGNORECASE,
)


def parse_hours_vi(text: str, default: float = 12.0) -> float:
    """Extract '<N> tiếng/giờ/h' from Vietnamese text. Returns default if none."""
    if not text:
        return default
    m = _RE_HOURS.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except Exception:
            return default
    return default


_RE_MAX_TASKS = re.compile(
    r"\b(?:max(?:imum)?|tối\s*đa|cap|đến|tới)\s*(\d+)\s*task",
    re.IGNORECASE,
)


def parse_max_tasks_vi(text: str, default: int = 20) -> int:
    if not text:
        return default
    m = _RE_MAX_TASKS.search(text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return default
    return default


# ── Vietnamese status panel ────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def status_panel_vi() -> str:
    d = state()
    icon = "🟢" if d.get("enabled") else "⚪"
    lines = [f"{icon} <b>Agent Autorun</b>"]
    lines.append(
        f"Trạng thái: <b>"
        f"{'đang chạy' if d.get('enabled') else 'đã dừng'}</b>")
    if d.get("objective"):
        lines.append(f"Mục tiêu: <i>{_esc(d['objective'][:160])}</i>")
    if d.get("hours"):
        lines.append(f"Thời lượng: <b>{d['hours']}h</b>")
    if d.get("started_at"):
        lines.append(f"Bắt đầu: <code>{d['started_at']}</code>")
    if d.get("stop_at"):
        # Compute remaining
        nxt = _parse_iso(d["stop_at"])
        if nxt:
            remain = nxt - _now()
            mins = int(remain.total_seconds() // 60)
            if mins > 0:
                lines.append(f"Còn lại: <b>~{mins // 60}h {mins % 60}m</b> "
                             f"(stop_at <code>{d['stop_at']}</code>)")
            else:
                lines.append(f"⏰ Đã quá hạn stop_at <code>{d['stop_at']}</code>")
    lines.append(f"Đã xong: <b>{d.get('completed_tasks', 0)}</b> / "
                 f"<b>{d.get('max_tasks', 0)}</b> task · "
                 f"thất bại liên tiếp: <b>"
                 f"{d.get('consecutive_failures', 0)}</b>")
    if d.get("paused_reason"):
        lines.append(f"⏸ <b>Đang pause</b>: {_esc(str(d['paused_reason']))}")
        if d.get("next_probe_at"):
            lines.append(f"  thử lại lúc: <code>{d['next_probe_at']}</code>")
    if d.get("last_run_at"):
        lines.append(f"Run gần nhất: <code>{d['last_run_at']}</code>")
    if d.get("last_task_id"):
        lines.append(
            f"Task gần nhất: <code>{_esc(str(d['last_task_id']))}</code> "
            f"(<i>{_esc(str(d.get('last_status', '?')))}</i>)")
    if d.get("last_summary"):
        lines.append(f"<i>{_esc(d['last_summary'][:200])}</i>")
    return "\n".join(lines)


# ── Scheduler hook ────────────────────────────────────────────────────────────

async def pump_loop(user: str = "tg_admin",
                     report_callback=None,
                     poll_interval_sec: float = 5.0) -> None:
    """Continuously call advance_one() until is_due_to_stop() returns True.

    Designed to be spawned as `asyncio.create_task(pump_loop(...))` from
    the Telegram handler when admin starts autorun. Sends each task
    result to report_callback (typically a wrapper around
    send_chat_reply). Sleeps poll_interval_sec between cycles so we
    don't hammer the bridge.

    Pause behaviour:
      - When advance_one returns a paused status (quota_limited /
        auth_required / no_tool / interactive_only), the autorun state
        records paused_reason + next_probe_at. The pump loop then
        sleeps until next_probe_at and retries.
      - The claude_quota scheduler tick may also pump us in parallel
        when reset_at arrives — record_outcome is idempotent so the
        race is safe.

    Stop conditions (mirrors is_due_to_stop):
      - state.enabled flipped to False (admin /agent_autorun_stop)
      - stop_at reached (hours budget exhausted)
      - completed_tasks >= max_tasks
      - 2 consecutive non-quota failures
    """
    import asyncio as _asy

    while True:
        # Check stop conditions
        should_stop, reason = is_due_to_stop()
        if should_stop:
            stop(user="autorun_pump", reason=reason)
            if report_callback:
                try:
                    await report_callback(
                        f"⏹ <b>Agent Autorun stopped</b> — {_esc(reason)}",
                    )
                except Exception:
                    pass
            return

        # Honor pause window — don't pump if next_probe_at hasn't arrived
        if not can_probe_now():
            d = state()
            nxt = d.get("next_probe_at") or ""
            if report_callback:
                try:
                    await report_callback(
                        f"⏸ Autorun paused (<i>{_esc(d.get('paused_reason',''))}</i>) "
                        f"— probe lại lúc <code>{nxt}</code>",
                    )
                except Exception:
                    pass
            # Sleep in 30s chunks so admin stop is detected promptly
            await _asy.sleep(30)
            continue

        # Run one bridge cycle
        try:
            result = await advance_one(user=user)
        except Exception as e:
            # Defensive — never let one bridge error kill the loop.
            try:
                from bot.agent.audit_log import log_action
                log_action(user=user, action="agent_autorun_advance_error",
                           risk_level="medium", status="ok",
                           result_summary=f"err={str(e)[:140]}")
            except Exception:
                pass
            if report_callback:
                try:
                    await report_callback(
                        f"⚠ Autorun cycle error: <code>{_esc(str(e))[:160]}</code>"
                        f"\nĐang sleep 30s rồi thử lại.",
                    )
                except Exception:
                    pass
            await _asy.sleep(30)
            continue

        # Report result to admin
        if report_callback:
            try:
                rstatus = (result or {}).get("status", "?")
                tid     = (result or {}).get("task_id", "")
                summ    = ((result or {}).get("summary") or "")[:200]
                d = state()
                done   = d.get("completed_tasks", 0)
                cap    = d.get("max_tasks", 0)
                hdr_icon = {
                    "done":           "✅",
                    "no_changes":     "💤",
                    "noop":           "💤",
                    "quota_limited":  "🚫",
                    "auth_required":  "🔒",
                    "no_tool":        "❌",
                    "pending_action": "⏸",
                }.get(rstatus, "•")
                msg = (f"{hdr_icon} <b>Autorun cycle</b> — task "
                       f"<code>{_esc(str(tid))}</code> → <i>{_esc(rstatus)}</i>"
                       f"\n  ✓ {done}/{cap}"
                       + (f"\n  <i>{_esc(summ)}</i>" if summ else ""))
                await report_callback(msg)
            except Exception:
                pass

        # If paused, the next iteration will catch it via can_probe_now().
        # Otherwise short sleep to avoid runaway loop on fast no-changes.
        await _asy.sleep(poll_interval_sec)


async def advance_one(user: str = "tg_admin") -> dict:
    """Run exactly one bridge.run_once cycle and update autorun state.

    Caller (claude_quota scheduler / Telegram NL) must check is_due_to_stop()
    BEFORE invoking this and stop() if True.

    In self-improve mode, when bridge returns noop (queue empty), this
    will try to populate the queue from the roadmap once and retry —
    so a single advance_one call can both grow and execute the queue.
    """
    from bot import coding_worker_bridge as bridge
    result = await bridge.run_once(user=user)
    record_outcome(result)

    # Self-improve auto-populate: if queue is empty AND we're in
    # self-improve mode AND not stopped yet, pull next roadmap item
    # and run once more.
    d = state()
    if (d.get("enabled") and d.get("auto_self_improve")
            and (result or {}).get("status") == "noop"):
        pop = populate_queue_from_roadmap()
        pop_status = (pop or {}).get("status", "")
        if pop_status in ("queued", "existing_queue"):
            # New task in queue — run it.
            result2 = await bridge.run_once(user=user)
            record_outcome(result2)
            return result2
        if pop_status == "pending_action":
            # Next roadmap item is high-risk → wait for owner.
            d["paused_reason"] = "roadmap_high_risk_pending_action"
            from datetime import timedelta
            nxt = (_now() + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            d["next_probe_at"] = nxt
            _save(d)
        elif pop_status == "noop":
            # Roadmap exhausted → stop politely.
            stop(user="autorun_pump", reason="roadmap_exhausted")
        # status="error" or unknown → leave as queue_empty_will_self_improve
        # (the next pump cycle will retry).
    return result
