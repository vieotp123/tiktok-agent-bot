"""
Risk classifier — maps a free-form goal into one of:
    low | medium | high

Risk policy (single source of truth):

low:
  - read-only research / web search
  - chat reply via 9Router
  - product DB read-only lookup
  - memory read

medium:
  - write to product DB (active/needs_update toggle, add/update product)
  - write to leads/conversations/consulting_logs
  - add semantic memory
  - run a task that calls 9Router multiple times
  - file summarisation

high:  (must become pending_action; never auto-executes)
  - sending a TikTok DM
  - public posting / commenting / following
  - editing TikTok reader / Playwright / storage_state
  - editing .env / systemd / nginx
  - git push to main
  - deploying production
  - shutting down / restarting services
  - any "outreach" or "notify customer" that opens new conversations
  - bulk import that touches >50 rows

Heuristic-only — no LLM call required (deterministic, fast, auditable).
"""
from __future__ import annotations

import re

# Hard rules — substring matches → high risk regardless of phrasing.
_HIGH_RISK_PATTERNS = [
    r"\bdm\s+khách\b", r"\bgửi\s+dm\b", r"\bsend.*dm\b",
    r"\bauto[\s_-]?dm\b", r"\bfollow[\s_-]?up.*\blead",
    r"\bđăng\s+(bài|post|video)\b", r"\bpublish\b", r"\bpost\b",
    r"\bcomment\s+công\s+khai\b", r"\bauto[\s_-]?follow\b",
    r"\bmass[\s_-]?dm\b", r"\boutreach\b",
    r"\brestart\s+(bot|service|tiktok)\b", r"\bsystemctl\b",
    r"\bdeploy\b", r"\brollback\b",
    r"\bgit\s+push.*main\b", r"\bmerge.*main\b",
    r"\bedit\s+\.env\b", r"\bstorage[_\s]state\b",
    r"\btiktok[_\s]bot\.py\b", r"\bplaywright\b.*(reader|extract|selector)",
    r"\bnginx\b", r"\biptables\b", r"\bsudo\b",
    # Explicit "(high risk)" annotation in a roadmap item or goal — honour it.
    r"\(\s*high[\s_-]?risk\b",
]

# Medium markers
_MEDIUM_RISK_PATTERNS = [
    r"\bcập\s+nhật\b", r"\bupdate\b", r"\bsửa\b", r"\bthêm\b", r"\binsert\b",
    r"\bverify\b", r"\bdisable\b",
    r"\btạo\s+(lead|task|memory)\b", r"\badd\s+(lead|task|memory|product)\b",
    r"\bsummariz", r"\btóm\s+tắt\s+file\b",
    r"\brun.*task\b", r"\bschedule\b",
    r"\btìm\s+nhiều\b", r"\bdeep\s+research\b", r"\bcrawl\b",
]


def classify_risk(goal: str) -> str:
    """Return 'low' | 'medium' | 'high' for a goal description."""
    if not goal:
        return "low"
    g = goal.lower().strip()

    for pat in _HIGH_RISK_PATTERNS:
        if re.search(pat, g):
            return "high"
    for pat in _MEDIUM_RISK_PATTERNS:
        if re.search(pat, g):
            return "medium"
    return "low"


POLICY_SUMMARY = (
    "<b>📜 Agent Risk Policy</b>\n\n"
    "<b>🟢 low</b> — read-only research, chat, product DB lookup, memory read.\n"
    "<i>Auto-executes via /agent_run.</i>\n\n"
    "<b>🟡 medium</b> — DB writes (products, leads, consulting), file summarise, "
    "multi-step tasks.\n"
    "<i>Auto-executes via /agent_run, fully audit-logged.</i>\n\n"
    "<b>🔴 high</b> — TikTok DM, public posting, restart, deploy, .env edit, "
    "TikTok reader/storage_state changes, git push to main.\n"
    "<i>Becomes a pending_action — admin must /confirm_action &lt;id&gt;.</i>\n\n"
    "All LLM calls go through 9Router/llm_client. Coding/reasoning uses "
    "<code>cc/claude-sonnet-4-6</code>; chat uses <code>cx/gpt-5.5</code>."
)
