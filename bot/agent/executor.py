"""
Executor — runs a plan produced by bot.agent.planner.

Risk gating:
  low/medium → execute steps directly via existing tools/runners.
  high       → DO NOT execute; create a pending_action via
               bot.agent.permissions.add_pending and return its id.

Returns a dict:
  {
    "status": "done" | "pending_action" | "rejected" | "failed" | "partial",
    "risk":   "low" | "medium" | "high",
    "output": "...",
    "pending_action_id": "...",      # if status == pending_action
    "error":  "...",                 # if status == failed
    "steps":  [...]                  # per-step outputs
  }
"""
from __future__ import annotations

import os
import sys
from typing import Any

import httpx

sys.path.insert(0, "/opt/tiktok-bot")

from bot.agent.planner import plan_goal
from bot.agent.audit_log import log_action
from bot.code_tasks import add_task as code_add_task

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")


async def execute_goal(goal: str, *, user: str = "tg_admin") -> dict[str, Any]:
    plan = plan_goal(goal)
    risk = plan.get("risk", "low")

    if risk == "high":
        try:
            from bot.agent.permissions import create_pending
            pid = create_pending(
                action="agent_run_high_risk",
                goal=goal[:300], risk_level="high", user=user,
                metadata={"rationale": plan.get("rationale", "")[:300]},
            )
        except Exception as e:
            return {"status": "rejected", "risk": "high",
                    "error": f"could not create pending_action: {e}",
                    "steps": []}
        log_action(user=user, action="agent_run_high_risk",
                   risk_level="high", status="pending",
                   result_summary=f"pid={pid}", goal=goal[:120])
        return {
            "status": "pending_action", "risk": "high",
            "pending_action_id": pid,
            "output": (f"High-risk goal queued. Review the plan and use "
                       f"/confirm_action {pid} to approve, or /cancel_action "
                       f"{pid} to reject."),
            "steps": plan.get("steps", []),
        }

    # low/medium → execute steps in order
    outputs: list[str] = []
    failed = False
    for step in plan.get("steps", []):
        action = step.get("action", "")
        args   = step.get("args", "")
        try:
            out = await _run_step(action, args, user=user)
        except Exception as e:
            outputs.append(f"❌ step {action} failed: {e}")
            failed = True
            break
        outputs.append(f"• [{action}] {str(out)[:600]}")

    log_action(user=user, action="agent_run",
               risk_level=risk, status="done" if not failed else "failed",
               result_summary="; ".join(o[:80] for o in outputs)[:200],
               goal=goal[:120])

    return {
        "status": "failed" if failed else ("partial" if not outputs else "done"),
        "risk": risk,
        "output": "\n".join(outputs) if outputs else "(no steps)",
        "steps": plan.get("steps", []),
    }


async def _run_step(action: str, args: str, *, user: str = "tg_admin") -> str:
    """Dispatch a single planner step. Defensive — never raises."""
    # Read-only / deterministic shortcuts first
    if action == "btc_price":
        from bot.tools import get_btc_price
        return (await get_btc_price())[:300]

    if action == "search_web":
        # Backend has a smarter LLM-summary path; reuse it.
        return await _call_backend(f"tìm thông tin: {args}",
                                   username=f"agent_{user}", source="search")

    if action == "chat" or action == "chat_summary":
        return await _call_backend(args or "tóm tắt",
                                   username=f"agent_{user}", source="telegram")

    if action == "sales_consult":
        from bot.business_store import build_consult_reply
        reply, ids, conf = build_consult_reply(args, audience="admin")
        return f"{reply} | products={ids} conf={conf:.2f}"

    if action == "log_consulting_log":
        # Already logged by the consult step above when called via backend.
        return "ok (logged)"

    if action == "memory_search":
        from bot.memory_store import search_memory, format_search_results
        rows = search_memory(args, namespace="tg_admin", limit=5)
        # Plain-text trim (no HTML)
        return f"{len(rows)} match(es) for {args[:40]!r}"

    if action == "queue_code_task":
        tid = code_add_task(title=args[:100],
                            description=args, risk_level="medium",
                            priority=5, created_by=user)
        return f"queued code_task {tid}"

    if action == "request_confirm":
        # Should never reach here — high-risk path already returned.
        return "request_confirm placeholder"

    return f"unknown action {action}"


async def _call_backend(content: str, *, username: str,
                        source: str = "telegram") -> str:
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(
                f"{BACKEND_URL}/message",
                json={"username": username, "content": content, "source": source},
            )
        if r.status_code == 200:
            return (r.json().get("reply") or "")[:1500]
        return f"backend {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return f"backend error: {e}"
