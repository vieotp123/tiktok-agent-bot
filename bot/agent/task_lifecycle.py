"""
Task lifecycle — explicit state machine for code_tasks and the general
task queue.

States:
  queued | planning | running | waiting_confirm | testing | deploying
  done | failed | cancelled | paused

Transitions are *table-driven*. Anything not listed is rejected. Every
attempted transition is audit-logged regardless of outcome.

Used by:
  - bot/code_tasks.py     (code worker queue)
  - bot/agent/task_queue  (general agent task queue)
  - bot/agent/executor.py (drives transitions for /agent_run)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from bot.agent.audit_log import log_action

# ── State set ─────────────────────────────────────────────────────────────────

STATES: tuple[str, ...] = (
    "queued",
    "planning",
    "running",
    "waiting_confirm",
    "testing",
    "deploying",
    "done",
    "failed",
    "cancelled",
    "paused",
)

TERMINAL: frozenset[str] = frozenset({"done", "failed", "cancelled"})

# ── Transition table ──────────────────────────────────────────────────────────
#   old_state -> set of valid new_states.
# Rationale comments below explain each edge.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued":          frozenset({
        "planning",        # planner picks the task
        "running",         # legacy: skip planning for trivial work
        "waiting_confirm", # high-risk task, gate at start
        "cancelled",       # admin cancels before pickup
        "paused",          # global pause
    }),
    "planning":        frozenset({
        "running",
        "waiting_confirm",
        "failed",          # planning itself failed (e.g. no plan generated)
        "cancelled",
        "paused",
    }),
    "running":         frozenset({
        "testing",         # task ran, ready to verify
        "waiting_confirm", # mid-run hit a high-risk step
        "done",            # trivially done with no test step
        "failed",          # exception during run
        "cancelled",
        "paused",
    }),
    "waiting_confirm": frozenset({
        "running",         # /confirm_action -> resume
        "cancelled",       # /cancel_action
        "failed",          # confirm timed out / explicitly rejected
        "paused",
    }),
    "testing":         frozenset({
        "deploying",
        "done",            # tests pass, no deploy needed
        "failed",
        "cancelled",
        "paused",
    }),
    "deploying":       frozenset({
        "done",
        "failed",          # post-deploy smoke failed; rollback handles repair
        "cancelled",
        "paused",
    }),
    # Paused can resume to whichever state was previous; we accept paused→queued
    # as the safe re-entry point (pickup again from scratch).
    "paused":          frozenset({"queued", "cancelled"}),
    # Terminal states cannot transition unless explicitly reopened.
    "done":            frozenset(),
    "failed":          frozenset({"queued"}),  # admin can reopen a failure
    "cancelled":       frozenset(),            # never auto-resumes
}


# ── API ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TransitionResult:
    ok: bool
    old_state: str
    new_state: str
    reason: str = ""
    error: str = ""


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def valid_next_states(old_state: str) -> tuple[str, ...]:
    return tuple(sorted(_TRANSITIONS.get(old_state, frozenset())))


def validate_transition(old_state: str, new_state: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False explains why the transition is invalid."""
    if old_state not in STATES:
        return False, f"unknown old_state {old_state!r}"
    if new_state not in STATES:
        return False, f"unknown new_state {new_state!r}"
    if old_state == new_state:
        return True, "no-op"
    allowed = _TRANSITIONS.get(old_state, frozenset())
    if new_state not in allowed:
        return False, (f"{old_state} → {new_state} not allowed "
                       f"(valid: {sorted(allowed)})")
    return True, ""


def transition_task(
    task_id: str,
    old_state: str,
    new_state: str,
    *,
    reason: str = "",
    user: str = "agent",
    audit_action: str = "task_transition",
) -> TransitionResult:
    """
    Validate a transition and emit an audit-log entry.

    This function does NOT write to any task store — caller is
    responsible for the actual UPDATE on its table. Validation is
    pure-functional so callers can pre-check before touching DB.
    """
    ok, why = validate_transition(old_state, new_state)
    status_word = "ok" if ok else "rejected"
    risk = (
        "high"   if new_state in ("waiting_confirm", "deploying") else
        "medium" if new_state in ("testing", "running")           else
        "low"
    )
    try:
        log_action(
            user=user, action=audit_action, risk_level=risk,
            status=status_word,
            result_summary=(f"{task_id} {old_state}->{new_state} "
                            f"reason={reason[:80]!r}")[:200],
            task_id=task_id,
        )
    except Exception:
        # Audit failure must not block transition validation
        pass
    return TransitionResult(ok=ok, old_state=old_state, new_state=new_state,
                            reason=reason, error=("" if ok else why))


def can_auto_execute(task: dict) -> tuple[bool, str]:
    """Return (ok, reason) — whether the executor may auto-run this task
    without further human approval right now.

    Rules:
      - High-risk tasks NEVER auto-execute. They must be in waiting_confirm
        and explicitly confirmed before going to running.
      - Cancelled / failed / done: terminal; cannot run.
      - Paused: globally halted; not eligible.
      - Otherwise: queued or planning is eligible if risk_level in
        {low, medium}.
    """
    risk  = (task.get("risk_level") or "low").lower()
    state = (task.get("status") or "").lower()

    if state in TERMINAL:
        return False, f"task is terminal ({state})"
    if state == "paused":
        return False, "task paused"
    if state == "waiting_confirm":
        return False, "awaits confirm_action"
    if risk == "high":
        return False, "high-risk requires explicit confirm"

    if state in ("queued", "planning", "running", "testing", "deploying"):
        return True, "ok"
    return False, f"unknown state {state!r}"


def task_state_summary(task: dict) -> str:
    """Compact one-liner suitable for Telegram/CLI output."""
    if not task:
        return "(no task)"
    risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
        (task.get("risk_level") or "low").lower(), "⚪")
    state_icon = {
        "queued": "🕐", "planning": "🧭", "running": "⏳",
        "waiting_confirm": "⏸", "testing": "🧪", "deploying": "🚀",
        "done": "✅", "failed": "❌", "cancelled": "🚫", "paused": "⏯",
    }.get(task.get("status", ""), "•")
    title = (task.get("title") or task.get("goal") or "")[:60]
    nxt = valid_next_states(task.get("status", "queued"))
    return (f"{state_icon}{risk_icon} {task.get('id','?')} — "
            f"{title}\n  state={task.get('status','?')} "
            f"next∈{list(nxt)}")


# ── Convenience ──────────────────────────────────────────────────────────────

def all_states() -> Iterable[str]:
    return STATES


def transition_table() -> dict[str, list[str]]:
    """Pretty-printable copy of the table — useful for /agent_policy."""
    return {k: sorted(v) for k, v in _TRANSITIONS.items()}
