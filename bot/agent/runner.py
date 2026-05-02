"""
Task runner — executes skills, updates task queue, logs to audit.
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

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")


def detect_task_type(goal: str) -> str:
    """Classify goal into a skill name."""
    if is_btc_query(goal):
        return "btc_price"
    if is_search_query(goal):
        return "search_web"
    return "chat"


async def _call_backend(username: str, content: str) -> str:
    """POST to /message and return reply string."""
    async with httpx.AsyncClient(timeout=35) as c:
        r = await c.post(
            f"{BACKEND}/message",
            json={"username": username, "content": content},
        )
    if r.status_code == 200:
        return r.json().get("reply", "")
    return f"backend error {r.status_code}: {r.text[:150]}"


async def run_task(goal: str, user: str = "tg_admin") -> dict:
    """
    Create task → execute → update queue → audit log.
    Returns: {task_id, type, status, result}
    """
    task_type = detect_task_type(goal)
    task_id = create_task(type_=task_type, goal=goal, status="running")

    try:
        if task_type == "btc_price":
            result = await get_btc_price()

        elif task_type == "search_web":
            # Route through backend /message so LLM summarises results
            result = await _call_backend(f"task_{task_id}", goal)

        else:  # chat
            result = await _call_backend(f"task_{task_id}", goal)

        update_task(
            task_id,
            status="done",
            progress="100%",
            result_summary=result[:500],
        )
        log_action(
            user=user,
            action=f"run_task:{task_type}",
            risk_level="low",
            status="done",
            result_summary=result[:200],
            task_id=task_id,
            goal=goal[:120],
        )
        return {"task_id": task_id, "type": task_type, "status": "done", "result": result}

    except Exception as e:
        err = str(e)[:200]
        update_task(task_id, status="failed", error=err)
        log_action(
            user=user,
            action=f"run_task:{task_type}",
            risk_level="low",
            status="failed",
            result_summary=err,
            task_id=task_id,
            goal=goal[:120],
        )
        return {"task_id": task_id, "type": task_type, "status": "failed", "result": err}
