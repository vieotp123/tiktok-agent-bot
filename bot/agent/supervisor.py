"""
Lightweight Supervisor — drift detection for long autorun sessions.

Owner directive (2026-05-03):
  "đặt giới hạn autorun là 1/6/2026" — keep autorun running ~29 days.
  "nó chỉ dừng khi claude bị limit, hết limit lại chạy tiếp" — never
  hard-stop on quota; pause + resume.

This module decides — after each cycle — whether autorun should:
  • CONTINUE — work is on track or merely paused on quota.
  • PAUSE   — quota / auth issue; defer to autorun's existing pause
              logic (next_probe_at + 1h backoff).
  • STOP    — drift threshold exceeded → halt the long session.

Drift threshold rules (deterministic, no LLM call by default):

  CRITICAL_STATUSES — count toward drift:
      worker_failed, smoke_failed, evals_failed,
      commit_failed, push_failed, exec_error, blocked_staged

  NEUTRAL_STATUSES — never count as failure:
      done, no_changes, noop, dry_run

  PAUSE_STATUSES — never count as failure (resume happens):
      quota_limited, auth_required, no_tool, interactive_only

Threshold: > 1 critical failure in last 6 cycles → STOP.
Quota / pause cycles are skipped (don't fill the window).

Public API:
    record_cycle(history, result_dict) -> None  (in-place append)
    check_drift(history, *, window=6, max_failures=1) -> dict
    explain_via_llm(history) -> str  (optional richer summary)
"""
from __future__ import annotations

from typing import Optional

CRITICAL_STATUSES = frozenset({
    "worker_failed", "smoke_failed", "evals_failed",
    "commit_failed", "push_failed", "exec_error", "blocked_staged",
})

NEUTRAL_STATUSES = frozenset({
    "done", "no_changes", "noop", "dry_run",
})

PAUSE_STATUSES = frozenset({
    "quota_limited", "auth_required",
    "no_tool", "interactive_only",
})

MAX_HISTORY = 50


def _classify(status: str) -> str:
    if status in CRITICAL_STATUSES:
        return "critical"
    if status in PAUSE_STATUSES:
        return "pause"
    if status in NEUTRAL_STATUSES:
        return "neutral"
    return "unknown"


def record_cycle(history: list, result: dict) -> list:
    """Append a cycle outcome to the rolling history (in-place + return).

    Caps the history at MAX_HISTORY entries (FIFO eviction). Each entry:
        {
          "ts":      ISO8601 UTC,
          "task_id": "ctk_…",
          "status":  bridge result status,
          "category": critical|pause|neutral|unknown,
          "summary": ≤200 char snippet,
        }
    """
    from datetime import datetime, timezone
    if not isinstance(history, list):
        history = []
    status = (result or {}).get("status", "unknown")
    entry = {
        "ts":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id":  (result or {}).get("task_id", ""),
        "status":   status,
        "category": _classify(status),
        "summary":  ((result or {}).get("summary") or "")[:200],
    }
    history.append(entry)
    # Cap memory
    if len(history) > MAX_HISTORY:
        del history[: len(history) - MAX_HISTORY]
    return history


def check_drift(history: list, *,
                  window: int = 6,
                  max_failures: int = 1) -> dict:
    """Return {verdict, critical_count, considered, summary}.

    Looks at the last `window` NON-PAUSE cycles. Counts critical
    failures. If > max_failures → verdict=stop.

    Pause cycles (quota_limited / auth_required) are SKIPPED entirely
    so a long quota window doesn't push real failures out of the
    drift view. This is the "chỉ dừng khi limit thật" guarantee.

    Verdicts:
        continue — < threshold critical fails OR not enough cycles
        stop     — drift threshold exceeded
    """
    if not history:
        return {"verdict": "continue", "critical_count": 0,
                "considered": 0,
                "summary": "no cycle history yet"}

    # Skip pause cycles
    non_pause = [h for h in history if h.get("category") != "pause"]
    recent    = non_pause[-window:]
    if len(recent) < window:
        return {"verdict": "continue",
                "critical_count": sum(1 for h in recent
                                       if h.get("category") == "critical"),
                "considered": len(recent),
                "summary": (f"only {len(recent)}/{window} non-pause "
                            f"cycles in history; need more data")}

    crit = [h for h in recent if h.get("category") == "critical"]
    crit_count = len(crit)

    if crit_count > max_failures:
        # Build a short summary of which task failures triggered drift
        tasks = ", ".join(f"{h.get('task_id','?')}({h.get('status','?')})"
                           for h in crit[-3:])
        return {"verdict": "stop",
                "critical_count": crit_count,
                "considered": len(recent),
                "summary": (f"drift: {crit_count} critical fail(s) "
                            f"in last {len(recent)} non-pause cycles "
                            f"(>{max_failures} allowed). "
                            f"recent: {tasks}")}

    return {"verdict": "continue",
            "critical_count": crit_count,
            "considered": len(recent),
            "summary": (f"on track: {crit_count}/{len(recent)} critical "
                        f"in window")}


def explain_via_llm(history: list) -> Optional[str]:
    """Optional: ask GPT-5.5 for a richer summary of recent cycles.

    Called only when drift is suspected (saves LLM tokens). Returns
    a short Vietnamese paragraph or None on error. Never raises.
    """
    if not history:
        return None
    try:
        from bot.llm_client import complete as _complete
    except Exception:
        return None
    recent = history[-12:]
    bullets = []
    for h in recent:
        bullets.append(
            f"  - [{h.get('ts','?')[:19]}] "
            f"{h.get('task_id','?')} → {h.get('status','?')} "
            f"({h.get('category','?')})"
        )
    sys_msg = (
        "Bạn là technical supervisor cho 1 autonomous coding agent. "
        "Đọc cycle history dưới đây và trả lời ngắn gọn (<= 60 từ "
        "tiếng Việt): agent đang đi đúng hướng hay đang drift? "
        "Nếu drift, đề xuất đổi hướng nào. KHÔNG bịa, không khuyến "
        "nghị nếu data không đủ."
    )
    user_msg = "Cycle history (mới nhất ở dưới):\n" + "\n".join(bullets)
    try:
        import asyncio
        resp = asyncio.run(_complete(
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user",   "content": user_msg},
            ],
            role="reasoning",
            timeout=15,
            max_tokens=200,
        ))
    except Exception:
        return None
    if isinstance(resp, dict) and not resp.get("error"):
        return (resp.get("content") or "").strip()[:400] or None
    return None


# ── Vietnamese status panel ───────────────────────────────────────────────────

def format_drift_report(check: dict, history: list) -> str:
    """Pretty-print a drift check result for Telegram."""
    icon = "🟢" if check.get("verdict") == "continue" else "🛑"
    lines = [f"{icon} <b>Supervisor</b>"]
    lines.append(f"Verdict: <b>{check.get('verdict','?')}</b>")
    lines.append(
        f"Critical: <b>{check.get('critical_count', 0)}</b> "
        f"/ {check.get('considered', 0)} cycles considered")
    lines.append(f"<i>{(check.get('summary') or '')[:200]}</i>")
    if history:
        recent = history[-5:]
        lines.append("")
        lines.append("<b>Last 5 cycles:</b>")
        for h in recent:
            cat_icon = {
                "critical": "💥", "pause": "⏸",
                "neutral":  "✓",  "unknown": "?",
            }.get(h.get("category"), "•")
            lines.append(
                f"  {cat_icon} <code>{h.get('task_id','?')}</code> "
                f"{h.get('status','?')}")
    return "\n".join(lines)
