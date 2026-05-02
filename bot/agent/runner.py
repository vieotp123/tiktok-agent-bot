"""
Task runner — executes skills, updates task queue, logs to audit.
Auto-lesson hook: records episodic lessons after task completion/failure.
"""
import asyncio
import os
import sys

import httpx

sys.path.insert(0, "/opt/tiktok-bot")

from bot.tools import (
    get_btc_price,
    search_web, format_search_results,
    is_btc_query, is_search_query, extract_search_query,
)
from bot.agent.task_queue import create_task, update_task
from bot.agent.audit_log import log_action
from bot.memory_store import (
    add_raw_event, add_lesson, update_task_state, lessons_for_retry,
)

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")


def detect_task_type(goal: str) -> str:
    """Classify goal into a skill name."""
    if is_btc_query(goal):
        return "btc_price"
    if is_search_query(goal):
        return "search_web"
    return "chat"


async def _call_backend(username: str, content: str, source: str = "task") -> str:
    """POST to /message and return reply string."""
    async with httpx.AsyncClient(timeout=35) as c:
        r = await c.post(
            f"{BACKEND}/message",
            json={"username": username, "content": content, "source": source},
        )
    if r.status_code == 200:
        return r.json().get("reply", "")
    return f"backend error {r.status_code}: {r.text[:150]}"


async def run_task(goal: str, user: str = "tg_admin") -> dict:
    """
    Create task → execute → update queue → audit log → auto-lesson hook.
    Returns: {task_id, type, status, result}
    """
    task_type = detect_task_type(goal)
    task_id   = create_task(type_=task_type, goal=goal, status="running")

    # ── Track task state in memory store ──────────────────────────────────────
    update_task_state(
        str(task_id),
        status="running",
        goal=goal,
        current_step="start",
    )

    # ── Log task start as raw event ───────────────────────────────────────────
    add_raw_event(
        source=user,
        action=f"run_task:{task_type}",
        summary=f"Starting {task_type}: {goal[:100]}",
        actor=user,
        event_type="task_start",
        task_id=str(task_id),
    )

    # ── Retry detection: surface prior failure lessons for this skill ────────
    prior_failures = lessons_for_retry(task_type, limit=3)
    if prior_failures:
        add_raw_event(
            source=user,
            action=f"run_task:{task_type}",
            summary=f"Retry detected: {len(prior_failures)} prior "
                    f"failure(s) for skill {task_type}",
            actor=user,
            event_type="task_retry",
            task_id=str(task_id),
            metadata={"prior_failure_ids": [l["id"] for l in prior_failures]},
        )

    try:
        if task_type == "btc_price":
            result = await get_btc_price()

        elif task_type == "search_web":
            result = await _call_backend(f"task_{task_id}", goal, source="search")

        else:  # chat
            result = await _call_backend(f"task_{task_id}", goal, source="task")

        update_task(
            task_id,
            status="done",
            progress="100%",
            result_summary=result[:500],
        )
        update_task_state(str(task_id), status="done", current_step="complete")

        log_action(
            user=user,
            action=f"run_task:{task_type}",
            risk_level="low",
            status="done",
            result_summary=result[:200],
            task_id=task_id,
            goal=goal[:120],
        )

        # ── Auto-lesson: log successful task execution ────────────────────────
        add_raw_event(
            source=user,
            action=f"run_task:{task_type}",
            summary=f"Task done: {goal[:80]} → {result[:120]}",
            actor=user,
            event_type="task_done",
            task_id=str(task_id),
        )

        # Record lesson for search/btc tasks (these have deterministic outcomes)
        if task_type in ("search_web", "btc_price"):
            add_lesson(
                skill=task_type,
                outcome="success",
                lesson_text=f"Successfully executed '{goal[:60]}'. Result length: {len(result)} chars.",
                task_id=str(task_id),
                importance=3,  # routine success — low importance
            )

        return {"task_id": task_id, "type": task_type, "status": "done", "result": result}

    except Exception as e:
        err = str(e)[:200]
        update_task(task_id, status="failed", error=err)
        update_task_state(str(task_id), status="failed", current_step="error")

        log_action(
            user=user,
            action=f"run_task:{task_type}",
            risk_level="low",
            status="failed",
            result_summary=err,
            task_id=task_id,
            goal=goal[:120],
        )

        # ── Auto-lesson: record failure with root cause ───────────────────────
        add_raw_event(
            source=user,
            action=f"run_task:{task_type}",
            summary=f"Task failed: {goal[:80]} → error: {err[:100]}",
            actor=user,
            event_type="task_failed",
            task_id=str(task_id),
        )

        add_lesson(
            skill=task_type,
            outcome="failure",
            lesson_text=f"Failed: '{goal[:60]}'. Error: {err[:120]}",
            task_id=str(task_id),
            root_cause=err[:200],
            importance=6,  # failures are more important to remember
        )

        return {"task_id": task_id, "type": task_type, "status": "failed", "result": err}
