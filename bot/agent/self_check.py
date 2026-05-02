"""
self_check — quick agent platform health probe.

Returns an HTML-formatted summary suitable for /agent_health Telegram reply.
Checks:
  - systemd service states
  - backend /health and /router_status
  - active model roles
  - git repo state
  - duplicate process count
  - existence of menu_state, code_tasks, business, agent_memory DBs
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import httpx

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")


def _systemctl_active(svc: str) -> str:
    try:
        out = subprocess.check_output(["systemctl", "is-active", svc],
                                      text=True, stderr=subprocess.STDOUT).strip()
        return out
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip() or "unknown"
    except Exception:
        return "error"


def _git_summary() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", "/opt/tiktok-bot", "log", "--oneline", "-1"],
            text=True, stderr=subprocess.DEVNULL).strip()
        branch = subprocess.check_output(
            ["git", "-C", "/opt/tiktok-bot", "branch", "--show-current"],
            text=True, stderr=subprocess.DEVNULL).strip()
        ahead = subprocess.check_output(
            ["git", "-C", "/opt/tiktok-bot", "rev-list", "--count",
             f"origin/{branch}..{branch}"],
            text=True, stderr=subprocess.DEVNULL).strip()
        return f"{branch} @ {out} (ahead={ahead})"
    except Exception as e:
        return f"git error: {e}"


def _proc_count() -> int:
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "tiktok|uvicorn"],
            text=True, stderr=subprocess.DEVNULL)
        return len([ln for ln in out.splitlines() if "grep" not in ln])
    except Exception:
        return 0


async def run_self_check() -> str:
    lines = ["<b>🩺 Agent Health</b>"]

    # ── services
    services = {svc: _systemctl_active(svc) for svc in
                ("tiktok-bot", "tiktok-backend", "tiktok-telegram")}
    icons = {"active": "✅", "inactive": "❌", "failed": "❌"}
    lines.append("<b>Services:</b>")
    for svc, st in services.items():
        lines.append(f"  {icons.get(st, '⚪')} {svc}: {st}")

    # ── backend health
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r1 = await c.get(f"{BACKEND}/health")
            r2 = await c.get(f"{BACKEND}/router_status")
        h1 = r1.json() if r1.status_code == 200 else {}
        h2 = r2.json() if r2.status_code == 200 else {}
        rms = h2.get("role_models", {})
        lines.append(f"<b>Backend:</b> ✅ {h1.get('status', '?')}")
        lines.append(f"<b>Router:</b> reachable={h2.get('reachable')} "
                     f"models={h2.get('model_count', '?')}")
        for role in ("chat", "tiktok_chat", "telegram_chat", "search_summary",
                     "reasoning", "coding"):
            lines.append(f"  {role}: <code>{rms.get(role, '?')}</code>")
    except Exception as e:
        lines.append(f"<b>Backend:</b> ❌ {e}")

    # ── git + procs + DBs
    lines.append(f"<b>Git:</b> {_git_summary()}")
    pc = _proc_count()
    lines.append(f"<b>Processes:</b> {pc} (expect 4)")

    files = {
        "agent_memory.db":     Path("/opt/tiktok-bot/data/agent_memory.db"),
        "business.db":         Path("/opt/tiktok-bot/data/business.db"),
        "code_tasks.db":       Path("/opt/tiktok-bot/data/code_tasks.db"),
        "menu_state.json":     Path("/opt/tiktok-bot/data/telegram/menu_state.json"),
        "session_state.json":  Path("/opt/tiktok-bot/data/telegram/session_state.json"),
    }
    lines.append("<b>State files:</b>")
    for name, p in files.items():
        lines.append(f"  {'✅' if p.exists() else '⚠'} {name} "
                     f"({p.stat().st_size if p.exists() else 0} bytes)")

    return "\n".join(lines)
