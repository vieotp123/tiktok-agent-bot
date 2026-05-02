"""
Planner — turn a free-form goal into a structured execution plan.

The planner is deterministic (regex + skill registry). It does NOT call
an LLM by default. The structured plan v2 carries enough metadata that
the executor can fail-fast on the first failed step and the eval suite
can lock the planner against regressions.

Plan v2 schema:
{
  "plan_id":   "plan_<uuid>",
  "goal":      "...",
  "risk_level":"low|medium|high",
  "model_role":"chat|reasoning|coding|search_summary|...",
  "rationale": "...",
  "steps": [
     {
       "id":               "step_1",
       "title":            "...",
       "skill":            "search_web|btc_price|chat|sales_consult|...",
       "args":             "...",
       "risk_level":       "low|medium|high",
       "requires_confirm": false,
       "expected_output":  "short text",
       "test":             "deterministic check or skill name to verify"
     }, ...
  ],
  "stop_conditions":  [...],
  "success_criteria": [...]
}
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from bot.agent.risk import classify_risk
from bot.tools import is_btc_query, is_search_query, extract_search_query


# ── Public API ────────────────────────────────────────────────────────────────

def plan_goal(goal: str) -> dict[str, Any]:
    """Return a structured plan for the goal (deterministic, no LLM)."""
    plan = _plan_goal_inner(goal)
    plan = _finalise(plan)
    # Plan-level risk = max of (goal-level risk, step risks).
    rank = {"low": 0, "medium": 1, "high": 2}
    inv  = {0: "low", 1: "medium", 2: "high"}
    cur  = rank.get(plan.get("risk_level", "low"), 0)
    for s in plan.get("steps", []):
        cur = max(cur, rank.get(s.get("risk_level", "low"), 0))
    plan["risk_level"] = inv[cur]
    return plan


def _plan_goal_inner(goal: str) -> dict[str, Any]:
    if not goal:
        return _empty_plan()
    g = goal.strip()
    risk = classify_risk(g)

    plan = _empty_plan(goal=g, risk=risk)

    # ── HIGH RISK: do not plan execution; emit a single confirm step
    if risk == "high":
        plan["rationale"] = (
            "High-risk goal — public action / outreach / infrastructure / "
            "main-branch git / .env edit. No auto-execution; admin must "
            "/confirm_action."
        )
        plan["model_role"] = "reasoning"
        plan["steps"].append(_step(
            "request_confirm", g[:200], risk="high",
            title="Wait for admin confirmation",
            requires_confirm=True,
            expected_output="pending_action_id",
            test="permissions.get_pending(id) is not None",
        ))
        plan["stop_conditions"].append("admin /cancel_action")
        plan["success_criteria"].append("admin /confirm_action and step run")
        return plan

    low = g.lower()

    # ── BTC realtime
    if is_btc_query(g):
        plan["rationale"]  = "BTC price → CoinGecko (no LLM)."
        plan["model_role"] = "search_summary"
        plan["steps"].append(_step(
            "btc_price", "", risk="low",
            title="Fetch BTC USD/JPY/VND",
            expected_output="Bitcoin hiện tại: $X (+Y%) ¥…JPY ₫…VND",
            test="reply contains 'Bitcoin'",
        ))
        plan["success_criteria"].append("reply has $/¥/₫")
        return plan

    # ── eSIM sales intent
    try:
        from bot.business_store import detect_esim_intent
    except Exception:
        detect_esim_intent = lambda _t: False  # type: ignore

    if detect_esim_intent(g):
        plan["rationale"]  = "eSIM intent → DB-grounded sales consult."
        plan["model_role"] = "telegram_chat"
        plan["steps"].extend([
            _step(
                "sales_consult", g, risk="low",
                title="Consult product DB",
                expected_output="grounded reply with active product ids",
                test="reply does not invent prices",
            ),
            _step(
                "log_consulting_log", "auto", risk="low",
                title="Persist consulting_log + raw_event (audit-only)",
                expected_output="row written to consulting_logs",
                test="row count increases by 1",
            ),
        ])
        plan["success_criteria"].extend([
            "no needs_update product quoted as confirmed",
            "consulting_logs row written",
        ])
        return plan

    # ── Web search
    if is_search_query(g):
        q = extract_search_query(g)
        plan["rationale"]  = "Search intent → DuckDuckGo + LLM summary."
        plan["model_role"] = "search_summary"
        plan["steps"].extend([
            _step(
                "search_web", q, risk="low",
                title="DuckDuckGo top-K results",
                expected_output="result list",
                test="≥1 result returned",
            ),
            _step(
                "chat_summary", "summarise the search results", risk="low",
                title="Summarise via cx/gpt-5.5",
                expected_output="3-6 bullet summary in Vietnamese",
                test="reply length 100..3000 chars",
            ),
        ])
        plan["stop_conditions"].append("DDG returns 0 hits")
        return plan

    # ── Memory query
    if any(k in low for k in ("memory_search", "memory ", "ghi nhớ",
                                "lessons", "context preview")):
        plan["rationale"]  = "Memory query → semantic search."
        plan["model_role"] = "reasoning"
        plan["steps"].append(_step(
            "memory_search", g, risk="low",
            title="Search semantic memory",
            expected_output="0..5 memory matches",
            test="returns list[dict]",
        ))
        return plan

    # ── File analysis
    if re.search(r"\b(tóm\s+tắt|summari[sz]e|phân\s+tích)\s+file\b", low):
        plan["rationale"]  = "File analysis intent."
        plan["model_role"] = "reasoning"
        plan["steps"].append(_step(
            "file_summary", g, risk="medium",
            title="Summarise uploaded file via backend",
            expected_output="LLM summary",
            test="reply length > 50 chars",
        ))
        return plan

    # ── Coding task — runtime model NEVER edits source. Queue for the
    # Claude/Codex worker.
    if re.search(r"\b(refactor|fix bug|implement|sửa code|viết code|"
                 r"add feature|tạo file|update.*\.py|edit.*\.py|"
                 r"thêm worker|skeleton|prototype)\b", low):
        plan["rationale"]  = "Coding goal — queue as code_task."
        plan["model_role"] = "coding"
        plan["steps"].extend([
            _step(
                "build_coding_prompt", g, risk="low",
                title="Generate Claude/Codex prompt",
                expected_output="markdown prompt saved at data/code_prompts/<id>.md",
                test="file exists and >500 chars",
            ),
            _step(
                "queue_code_task", g, risk="medium",
                title="Insert into code_tasks queue",
                expected_output="ctk_<id>",
                test="code_tasks row created with status=queued",
            ),
        ])
        plan["success_criteria"].append("code_tasks row exists with prompt linked")
        return plan

    # ── Reporting / status
    if re.search(r"\b(tạo\s+report|create\s+report|báo\s+cáo|"
                 r"status|tình\s+hình)\b", low):
        plan["rationale"]  = "Reporting goal → status digest."
        plan["model_role"] = "reasoning"
        plan["steps"].extend([
            _step(
                "agent_health", "", risk="low",
                title="Probe services + router + git",
                expected_output="Agent Health markdown",
                test="non-empty",
            ),
            _step(
                "memory_search", "lessons", risk="low",
                title="Pull recent lessons",
                expected_output="0..3 lessons",
                test="returns list[dict]",
            ),
        ])
        return plan

    # ── Default: chat
    plan["rationale"]  = "Generic goal → 9Router chat (cx/gpt-5.5)."
    plan["model_role"] = "telegram_chat"
    plan["steps"].append(_step(
        "chat", g, risk="low",
        title="Forward to backend /message source=telegram",
        expected_output="LLM reply",
        test="reply non-empty",
    ))
    return plan


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_plan(goal: str = "", risk: str = "low") -> dict[str, Any]:
    return {
        "plan_id":          f"plan_{uuid.uuid4().hex[:10]}",
        "goal":             goal[:300],
        "risk_level":       risk,
        "model_role":       "telegram_chat",
        "rationale":        "",
        "steps":            [],
        "stop_conditions":  [],
        "success_criteria": [],
    }


def _step(skill: str, args: str, *, risk: str, title: str,
          requires_confirm: bool = False,
          expected_output: str = "", test: str = "") -> dict[str, Any]:
    """Step IDs are assigned by _finalise() after the plan is built."""
    return {
        "id":               "",
        "title":            title[:200],
        "skill":            skill,
        "args":             args[:500],
        "risk_level":       risk,
        "requires_confirm": bool(requires_confirm),
        "expected_output":  expected_output[:200],
        "test":             test[:200],
    }


def _finalise(plan: dict[str, Any]) -> dict[str, Any]:
    """Assign step IDs sequentially — called by plan_goal_v2 wrapper."""
    for i, s in enumerate(plan.get("steps", []), 1):
        s["id"] = f"step_{i}"
    return plan
