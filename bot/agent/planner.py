"""
Planner — turn a free-form goal into a structured execution plan.

The planner uses simple, deterministic rules based on the existing skill
registry. It does NOT call an LLM by default (cheap, fast, predictable).
For richer plans the agent can later call 9Router with role=reasoning,
but the deterministic baseline is enough to start.

Output:
  {
    "goal":      "...",
    "risk":      "low" | "medium" | "high",
    "rationale": "...",
    "steps": [
       {"action": "search_web", "args": "...", "risk": "low",  "note": "..."},
       {"action": "chat_summary", "args": "...", "risk": "low"},
       ...
    ],
  }
"""
from __future__ import annotations

import re
from typing import Any

from bot.agent.risk import classify_risk
from bot.tools import is_btc_query, is_search_query, extract_search_query


def plan_goal(goal: str) -> dict[str, Any]:
    """Return a deterministic plan for the goal."""
    if not goal:
        return {"goal": "", "risk": "low", "rationale": "empty goal",
                "steps": []}
    g = goal.strip()
    risk = classify_risk(g)
    plan: dict[str, Any] = {
        "goal": g[:300],
        "risk": risk,
        "rationale": "",
        "steps": [],
    }
    low = g.lower()

    # ── High-risk: do not plan execution; emit a single "needs confirm" step
    if risk == "high":
        plan["rationale"] = (
            "High-risk goal — public action / outreach / infrastructure. "
            "No auto-execution; admin must confirm via pending_action."
        )
        plan["steps"].append({
            "action": "request_confirm",
            "args":   g[:200],
            "risk":   "high",
            "note":   "Will create a pending_action and wait for /confirm_action.",
        })
        return plan

    # ── BTC realtime
    if is_btc_query(g):
        plan["rationale"] = "BTC price query → CoinGecko lookup."
        plan["steps"].append({
            "action": "btc_price", "args": "", "risk": "low",
            "note":   "Realtime BTC USD/JPY/VND from CoinGecko.",
        })
        return plan

    # ── eSIM sales intent
    try:
        from bot.business_store import detect_esim_intent
    except Exception:
        detect_esim_intent = lambda _t: False  # type: ignore
    if detect_esim_intent(g):
        plan["rationale"] = "eSIM intent → DB-grounded sales consult."
        plan["steps"].extend([
            {"action": "sales_consult", "args": g, "risk": "low",
             "note":   "Lookup product DB; never invents price."},
            {"action": "log_consulting_log", "args": "auto", "risk": "medium",
             "note":   "Write consulting_logs + raw_event."},
        ])
        return plan

    # ── Web search
    if is_search_query(g):
        q = extract_search_query(g)
        plan["rationale"] = "Search intent → DuckDuckGo + LLM summary."
        plan["steps"].extend([
            {"action": "search_web", "args": q, "risk": "low",
             "note":   "DuckDuckGo top-K results."},
            {"action": "chat_summary", "args": "summarise", "risk": "low",
             "note":   "9Router cx/gpt-5.5 summarisation."},
        ])
        return plan

    # ── Memory query / explicit /memory_search
    if any(k in low for k in ("memory", "ghi nhớ", "lessons")):
        plan["rationale"] = "Memory query → semantic search."
        plan["steps"].append({
            "action": "memory_search", "args": g, "risk": "low",
        })
        return plan

    # ── Coding / refactor — needs Claude reasoning model, but the planner
    # itself is read-only. We emit a code_task creation step (medium-risk).
    if re.search(r"\b(refactor|fix bug|implement|sửa code|viết code|"
                 r"add feature|tạo file|update.*\.py|edit.*\.py)\b", low):
        plan["rationale"] = "Coding goal — queue as code_task for the worker."
        plan["steps"].append({
            "action": "queue_code_task", "args": g, "risk": "medium",
            "note":   "Worker session reads docs/CLAUDE_CODE_WORKER.md.",
        })
        return plan

    # ── Default: chat with 9Router
    plan["rationale"] = "Generic goal → 9Router chat (cx/gpt-5.5)."
    plan["steps"].append({
        "action": "chat", "args": g, "risk": "low",
        "note":   "Routed to backend /message source=telegram.",
    })
    return plan
