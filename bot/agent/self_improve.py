"""
Self-improvement loop — RUN ONCE flow (no daemon).

`/self_improve_once`:

  1. Read docs/CURRENT_STATUS.md + docs/ROADMAP.md.
  2. Check the code_tasks queue. If a queued low/medium-risk coding
     task exists, surface it (do NOT run the model — that's the
     Claude/Codex CLI worker's job).
  3. Else, propose the next ROADMAP item as a coding task and queue it
     (medium-risk by default).
  4. Generate a Claude/Codex prompt via prompt_builder and save to
     data/code_prompts/<task_id>.md.
  5. Send a Telegram report.

Hard rules:
  - This function does NOT edit source code.
  - It does NOT call an LLM.
  - It does NOT run an infinite loop.
  - High-risk roadmap items become pending_action, not code_tasks.
"""
from __future__ import annotations

import re
from pathlib import Path

from bot.code_tasks import (
    add_task as code_add_task,
    list_tasks as code_list_tasks,
    next_queued_task,
)
from bot.agent.prompt_builder import (
    build_coding_prompt,
    estimate_code_task_risk,
    save_prompt_for_task,
)
from bot.agent.risk import classify_risk
from bot.agent.permissions import create_pending


ROADMAP = Path("/opt/tiktok-bot/docs/ROADMAP.md")
STATUS  = Path("/opt/tiktok-bot/docs/CURRENT_STATUS.md")


def _first_unchecked_roadmap_item(skip_titles: set[str] | None = None
                                    ) -> str | None:
    """Return the first `- [ ]` item in ROADMAP whose first 80 chars
    are NOT in skip_titles. Used by autorun to avoid infinite-loop
    on items that Claude can't complete autonomously (e.g. tasks
    that need real owner data — "Verify product catalog" requires
    actual prices the agent must NOT invent per Operating Rules §4)."""
    if not ROADMAP.exists():
        return None
    txt = ROADMAP.read_text(encoding="utf-8")
    skip = skip_titles or set()
    for line in txt.splitlines():
        m = re.match(r"^- \[ \] (.+)$", line)
        if not m:
            continue
        item = m.group(1).strip()
        title = _short_title(item, 80)
        if title not in skip:
            return item
    return None


def _short_title(text: str, n: int = 60) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s[:n]


def self_improve_once(*, user: str = "tg_admin",
                        skip_titles: set[str] | None = None) -> dict:
    """Run-once flow. Returns a summary dict for the Telegram report.

    `skip_titles` lets autorun pass a set of roadmap-item titles
    that have already been attempted this session — so the loop
    doesn't keep queuing the same item Claude can't complete.
    """
    out: dict = {
        "status":            "noop",
        "queued_task_id":    "",
        "created_task_id":   "",
        "pending_action_id": "",
        "prompt_path":       "",
        "summary":           "",
        "roadmap_item":      "",
    }

    # Step 1: existing queued coding work?
    nx = next_queued_task()
    if nx:
        out["status"]         = "existing_queue"
        out["queued_task_id"] = nx["id"]
        out["summary"] = (f"Existing code_task in queue: {nx['id']} — "
                          f"{nx['title'][:60]} (risk={nx['risk_level']}). "
                          "Open a Claude/Codex CLI session to run it.")
        # Make sure we have a prompt saved for that task
        prompt = build_coding_prompt(nx)
        path   = save_prompt_for_task(nx["id"], prompt)
        out["prompt_path"] = str(path)
        return out

    # Step 2: pull the next roadmap item (skipping already-attempted titles)
    item = _first_unchecked_roadmap_item(skip_titles=skip_titles)
    if not item:
        out["status"]  = "noop"
        if skip_titles:
            out["summary"] = (
                f"Nothing queued. All {len(skip_titles)} unchecked "
                f"roadmap items have already been attempted this "
                f"session. Update docs/ROADMAP.md or clear skip-set."
            )
        else:
            out["summary"] = ("Nothing queued and ROADMAP has no unchecked "
                              "items. Update docs/ROADMAP.md.")
        return out
    out["roadmap_item"] = _short_title(item, 80)

    risk = classify_risk(item)
    coding_risk = estimate_code_task_risk(item)
    final_risk = "high" if "high" in (risk, coding_risk) else coding_risk

    # Step 3a: HIGH risk — never queue a code_task automatically.
    # Create a pending_action for explicit admin review.
    if final_risk == "high":
        try:
            pid = create_pending(
                action="self_improve_high_risk",
                goal=item[:300],
                risk_level="high",
                user=user,
                metadata={"source": "self_improve_once",
                          "roadmap_item": item[:300]},
            )
            out["status"]            = "pending_action"
            out["pending_action_id"] = pid
            out["summary"] = (f"High-risk roadmap item — pending_action "
                              f"{pid} created. Use /confirm_action {pid} "
                              "to approve.")
        except Exception as e:
            out["status"]  = "error"
            out["summary"] = f"failed to create pending_action: {e}"
        return out

    # Step 3b: low/medium → queue a code_task
    title = _short_title(item, 80)
    description = (
        f"From ROADMAP.md: {item}\n\n"
        "Implement minimally. Re-read docs/CLAUDE_CODE_WORKER.md, "
        "docs/SELF_OPERATING_AGENT.md, docs/OPERATING_RULES.md before any "
        "edit. Run scripts/smoke_test.sh before commit. Push to dev-agent "
        "with inline-token push pattern; reset remote URL after."
    )
    tid = code_add_task(
        title=title, description=description,
        risk_level=final_risk, priority=6,
        created_by="self_improve",
    )
    out["created_task_id"] = tid

    # Step 4: build + persist prompt
    task_dict = {
        "id":          tid,
        "title":       title,
        "description": description,
        "risk_level":  final_risk,
        "branch":      "dev-agent",
    }
    prompt = build_coding_prompt(task_dict)
    path = save_prompt_for_task(tid, prompt)
    out["prompt_path"] = str(path)

    out["status"] = "queued"
    out["summary"] = (f"Queued code_task {tid} (risk={final_risk}) for "
                      f"the Claude/Codex worker. Prompt saved to "
                      f"{path.name}.")
    return out


def format_self_improve_report(result: dict) -> str:
    icons = {"queued": "🛠", "existing_queue": "📋",
             "pending_action": "⏸", "noop": "💤", "error": "❌"}
    icon = icons.get(result.get("status", ""), "•")
    lines = [f"{icon} <b>self_improve_once</b> — "
             f"<i>{result.get('status','?')}</i>"]
    if result.get("created_task_id"):
        lines.append(f"Created: <code>{result['created_task_id']}</code>")
    if result.get("queued_task_id"):
        lines.append(f"Existing queued: <code>{result['queued_task_id']}</code>")
    if result.get("pending_action_id"):
        lines.append(f"Pending action: <code>{result['pending_action_id']}</code>")
        lines.append("<i>Use /confirm_action to approve, "
                     "/cancel_action to reject.</i>")
    if result.get("prompt_path"):
        lines.append(f"Prompt: <code>{result['prompt_path']}</code>")
    if result.get("summary"):
        lines.append("")
        lines.append(result["summary"])
    return "\n".join(lines)
