"""
Brain context builder — used when answering a free-form admin question
via the chat fallback in `backend/server.py`.

Produces a single compact system-prompt block that injects:
  1. 3-5 most relevant admin memories  (search_memory on tg_admin namespace)
  2. Last 10 audit entries              (tail_audit)
  3. Current autorun state              (agent_autorun.state)
  4. Active pending_actions             (permissions.list_pending)

Design rules (mirrors `bot.memory_store.build_memory_context`):
  - Read-only. Never mutates DB / files / autorun state.
  - Hard char cap (default 4000) so we never crowd out the user message.
  - Audit `result_summary` is already truncated to 200 chars upstream;
    we still trim per-line for safety.
  - NEVER emits raw `payload_json` from raw_events (we don't read them).
  - NEVER emits secrets — audit_log strips secret keys at write time;
    we just pass through `action`, `risk_level`, `status`, `result_summary`.
  - Empty sections are skipped silently. If everything is empty,
    returns "" so the caller can drop the system message entirely.
"""
from __future__ import annotations

from typing import Iterable

_HEADER       = "### Brain Context (admin)"
_DEFAULT_CAP  = 4000
_MEM_LIMIT    = 5
_AUDIT_LIMIT  = 10
_PENDING_LIMIT = 5


def _safe_search_memories(query: str, limit: int) -> list[dict]:
    try:
        from bot.memory_store import search_memory
        rows = search_memory(query or "", namespace="tg_admin", limit=limit)
        if rows:
            return rows
        # Fallback to top recent admin memories when keyword match is empty.
        from bot.memory_store import list_memories
        return list_memories(namespace="tg_admin", limit=limit)
    except Exception:
        return []


def _safe_tail_audit(n: int) -> list[dict]:
    try:
        from bot.agent.audit_log import tail_audit
        return tail_audit(n) or []
    except Exception:
        return []


def _safe_autorun_state() -> dict:
    try:
        from bot.agent import agent_autorun
        return agent_autorun.state() or {}
    except Exception:
        return {}


def _safe_pending(limit: int) -> list[dict]:
    try:
        from bot.agent.permissions import list_pending
        return (list_pending(only_pending=True) or [])[:limit]
    except Exception:
        return []


def _fmt_memory(m: dict) -> str:
    title = (m.get("title") or "").strip()
    text  = (m.get("summary") or m.get("content") or "").strip()
    return f"• {title + ': ' if title else ''}{text[:160]}"


def _fmt_audit(e: dict) -> str:
    ts     = (e.get("timestamp") or "")[:16]
    action = (e.get("action") or "?")
    risk   = (e.get("risk_level") or "")
    status = (e.get("status") or "")
    summary = (e.get("result_summary") or "").strip()
    head = f"• {ts} {action} [{risk}/{status}]"
    return f"{head} — {summary[:120]}" if summary else head


def _fmt_pending(p: dict) -> str:
    aid    = p.get("action_id", "?")
    action = p.get("action", "?")
    goal   = (p.get("goal") or "")[:80]
    risk   = p.get("risk_level", "?")
    return f"• [{aid}] {action} ({risk}) — {goal}"


def _fmt_autorun(d: dict) -> list[str]:
    if not d:
        return []
    enabled = bool(d.get("enabled"))
    lines: list[str] = [f"enabled={enabled}"]
    if enabled or d.get("last_status"):
        if d.get("objective"):
            lines.append(f"objective={str(d['objective'])[:80]}")
        if d.get("started_at"):
            lines.append(f"started_at={d['started_at']}")
        if d.get("stop_at"):
            lines.append(f"stop_at={d['stop_at']}")
        if d.get("max_tasks") is not None:
            lines.append(
                f"completed={d.get('completed_tasks', 0)}/"
                f"{d.get('max_tasks', 0)}"
            )
        if d.get("paused_reason"):
            lines.append(f"paused={str(d['paused_reason'])[:60]}")
        if d.get("last_task_id"):
            lines.append(
                f"last_task={d['last_task_id']} "
                f"({str(d.get('last_status', '?'))[:24]})"
            )
    return lines


def _join_capped(parts: Iterable[str], cap: int) -> str:
    out: list[str] = []
    used = 0
    for p in parts:
        if not p:
            continue
        n = len(p) + 1  # newline
        if used + n > cap:
            break
        out.append(p)
        used += n
    return "\n".join(out)


def build_admin_brain_context(
    query: str,
    *,
    max_chars: int = _DEFAULT_CAP,
) -> str:
    """Return a compact, copy-paste-safe context block for admin chat.

    Returns "" if every section is empty (no memories, no audit, no
    autorun state, no pending) so the caller can skip injection.
    """
    cap = max(500, int(max_chars))

    memories = _safe_search_memories(query, _MEM_LIMIT)
    audit    = _safe_tail_audit(_AUDIT_LIMIT)
    autorun  = _safe_autorun_state()
    pending  = _safe_pending(_PENDING_LIMIT)

    if not memories and not audit and not autorun and not pending:
        return ""

    lines: list[str] = [_HEADER]

    if memories:
        lines.append("\n**Relevant memories** "
                     f"({min(len(memories), _MEM_LIMIT)}):")
        for m in memories[:_MEM_LIMIT]:
            lines.append(_fmt_memory(m))

    if audit:
        # Newest last so the model reads chronologically.
        lines.append(f"\n**Recent audit** (last {len(audit)}):")
        for e in audit:
            lines.append(_fmt_audit(e))

    autorun_lines = _fmt_autorun(autorun)
    if autorun_lines:
        lines.append("\n**Autorun state:**")
        lines.append("• " + " | ".join(autorun_lines))

    if pending:
        lines.append(f"\n**Pending actions** (need /confirm_action): "
                     f"{len(pending)}")
        for p in pending:
            lines.append(_fmt_pending(p))

    return _join_capped(lines, cap)
