"""
Worker roles registry — lists the conceptual workers the platform has,
which model role they use, and their current status.

This is descriptive data + a /agent_workers handler. It does NOT spawn or
manage processes.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.worker_manager import format_workers_list


@dataclass(frozen=True)
class WorkerRole:
    name: str
    description: str
    channel: str          # telegram | tiktok | internal | code
    model_role: str       # chat | tiktok_chat | telegram_chat | reasoning | coding ...
    risk_level: str       # low | medium | high
    status: str           # live | placeholder | disabled


_ROLES: tuple[WorkerRole, ...] = (
    WorkerRole("telegram_admin",       "Admin command center (long-poll bot).",
               "telegram", "telegram_chat", "low", "live"),
    WorkerRole("tiktok_chatgibiti",    "TikTok DM responder (Playwright). "
                                       "Read-only on inbox; replies inside active chat.",
               "tiktok",   "tiktok_chat",   "medium", "live"),
    WorkerRole("backend_router",       "FastAPI gateway: BTC, search, sales-consult, "
                                       "reminder, generic LLM via 9Router.",
               "internal", "search_summary", "low", "live"),
    WorkerRole("memory_writer",        "Adds raw_events / memories / lessons; "
                                       "auto-lessons hook in runner.",
               "internal", "reasoning",     "low", "live"),
    WorkerRole("sales_consultant",     "Japan eSIM consultant grounded in product DB. "
                                       "Never invents prices.",
               "internal", "telegram_chat", "low", "live"),
    WorkerRole("code_worker",          "Claude/Codex CLI session that picks queued "
                                       "code_tasks. See docs/CLAUDE_CODE_WORKER.md.",
               "code",     "coding",        "medium", "live"),
    WorkerRole("planner_executor",     "Deterministic planner + risk-gated executor "
                                       "for /agent_run.",
               "internal", "reasoning",     "low", "live"),
    WorkerRole("seo_marketing",        "Keyword research v0 — Google "
                                       "Autocomplete + DDG (free, no key). "
                                       "See bot/seo_research.py.",
               "internal", "reasoning",     "low",    "live"),
    WorkerRole("content_factory",      "Caption writer (cx/gpt-5.5) + image-brief "
                                       "stub. Drafts only — never posts. "
                                       "See bot/content_factory.py.",
               "internal", "chat",          "medium", "live"),
    WorkerRole("browser_ocr_worker",   "Playwright research + OCR on screenshots. "
                                       "Not yet implemented.",
               "internal", "vision",        "medium", "placeholder"),
)


def list_worker_roles() -> tuple[WorkerRole, ...]:
    return _ROLES


def format_worker_roles() -> str:
    icon = {"live": "✅", "placeholder": "⚠", "disabled": "🚫"}
    risk = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    lines = ["<b>👥 Worker Roles</b>"]
    for w in _ROLES:
        lines.append(
            f"{icon.get(w.status,'•')}{risk.get(w.risk_level,'⚪')} "
            f"<b>{w.name}</b> "
            f"<i>[{w.channel}/{w.model_role}]</i>\n"
            f"   {w.description}"
        )
    # Surface runtime worker_manager registry (last-seen heartbeats)
    try:
        runtime_block = format_workers_list()
        if runtime_block:
            lines.append("\n<b>Runtime registry:</b>")
            lines.append(runtime_block[:1200])
    except Exception:
        pass
    return "\n".join(lines)
