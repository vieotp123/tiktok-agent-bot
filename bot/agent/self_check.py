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


def _last_audit_lines(n: int = 3) -> list[str]:
    p = Path("/opt/tiktok-bot/data/audit/actions.jsonl")
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _last_deploy_status() -> str:
    """Find the most recent deploy/rollback log line in the journal."""
    try:
        out = subprocess.check_output(
            ["sudo", "journalctl", "-n", "300",
             "--no-pager", "--output=cat"],
            text=True, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return "(journal unavailable)"
    deploy_lines = [ln for ln in out.splitlines()
                    if "deploy_prod" in ln or "rollback_prod" in ln]
    if not deploy_lines:
        return "(no recent deploy)"
    return deploy_lines[-1][-200:]


def _pending_counts() -> dict:
    """Return counts of pending items across the platform."""
    counts = {"pending_actions": 0, "code_tasks_queued": 0,
              "code_tasks_running": 0, "code_tasks_waiting_confirm": 0,
              "tasks_running": 0, "tasks_queued": 0}
    try:
        from bot.agent.permissions import list_pending
        counts["pending_actions"] = len(list_pending(only_pending=True))
    except Exception:
        pass
    try:
        from bot.code_tasks import list_tasks as code_list
        for st in ("queued", "running", "waiting_confirm"):
            counts[f"code_tasks_{st}"] = len(code_list(status=st, limit=200))
    except Exception:
        pass
    try:
        from bot.agent.task_queue import list_tasks as gen_list
        # task_queue uses different schema; tolerate failures
        counts["tasks_queued"]  = len(gen_list(status="queued",  limit=200))
        counts["tasks_running"] = len(gen_list(status="running", limit=200))
    except Exception:
        pass
    return counts


def _memory_counts() -> dict:
    out = {"memories": 0, "lessons": 0, "raw_events": 0}
    try:
        import sqlite3
        with sqlite3.connect("/opt/tiktok-bot/data/agent_memory.db") as c:
            c.row_factory = sqlite3.Row
            for tbl in out:
                try:
                    out[tbl] = c.execute(
                        f"SELECT COUNT(*) c FROM {tbl}"
                    ).fetchone()[0]
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _last_smoke_test_status() -> str:
    """Tail recent journal for the last smoke_test outcome line."""
    try:
        out = subprocess.check_output(
            ["sudo", "journalctl", "-n", "500", "--no-pager",
             "--output=cat"],
            text=True, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return "(journal unavailable)"
    candidates = [ln for ln in out.splitlines()
                  if "SMOKE TEST" in ln]
    if not candidates:
        return "(no smoke_test in last 500 journal lines)"
    last = candidates[-1]
    return last[-160:]


def _top_queued_code_tasks(n: int = 3) -> list[dict]:
    try:
        from bot.code_tasks import list_tasks as code_list
        return code_list(status="queued", limit=n)
    except Exception:
        return []


def _top_unchecked_roadmap(n: int = 3) -> tuple[list[str], int, int]:
    """Return (top-N unchecked items, done_count, pending_count)."""
    p = Path("/opt/tiktok-bot/docs/ROADMAP.md")
    if not p.exists():
        return [], 0, 0
    txt = p.read_text(encoding="utf-8")
    import re as _re
    done    = len(_re.findall(r"^- \[x\] ", txt, _re.MULTILINE))
    pending = _re.findall(r"^- \[ \] (.+)$", txt, _re.MULTILINE)
    return pending[:n], done, len(pending)


async def _live_role_models() -> dict:
    """Pull current 9Router role-model assignments."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{BACKEND}/router_status")
        if r.status_code == 200:
            return r.json().get("role_models", {})
    except Exception:
        pass
    return {}


async def run_agent_status() -> str:
    """Extended dashboard for /agent_status."""
    base = await run_self_check()  # services + backend + git
    pc  = _pending_counts()
    mc  = _memory_counts()
    aud = _last_audit_lines(3)
    dep = _last_deploy_status()
    smoke = _last_smoke_test_status()
    rms = await _live_role_models()

    # Roadmap snapshot
    top, done, pending = _top_unchecked_roadmap(3)
    roadmap_lines = [f"<b>Roadmap:</b> {done} done · {pending} pending"]
    for i, item in enumerate(top, 1):
        # HTML-escape the roadmap text
        safe = (item.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))[:120]
        roadmap_lines.append(f"  {i}. {safe}")

    # Code task snapshot (top 3 queued)
    code_lines = [f"<b>Code Worker:</b> "
                  f"queued={pc['code_tasks_queued']} · "
                  f"running={pc['code_tasks_running']} · "
                  f"waiting={pc['code_tasks_waiting_confirm']}"]
    for t in _top_queued_code_tasks(3):
        title = (t.get("title") or "")[:48]
        title = (title.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;"))
        risk_icon = {"low": "🟢", "medium": "🟡",
                      "high": "🔴"}.get(t.get("risk_level", "low"), "⚪")
        code_lines.append(f"  {risk_icon} <code>{t['id']}</code> "
                          f"p{t['priority']} {title}")

    # Active model roles (live)
    role_lines = ["<b>Live model roles:</b>"]
    if rms:
        for role in ("chat", "tiktok_chat", "telegram_chat",
                     "search_summary", "reasoning", "coding", "critic",
                     "vision", "cheap"):
            v = rms.get(role, "?")
            role_lines.append(f"  {role}: <code>{v}</code>")
    else:
        role_lines.append("  (router_status unreachable)")

    extra = ["",
             *role_lines,
             "",
             "<b>Pending:</b>",
             f"  pending_actions={pc['pending_actions']} · "
             f"tasks_queued={pc['tasks_queued']} · "
             f"tasks_running={pc['tasks_running']}",
             "",
             *code_lines,
             "",
             *roadmap_lines,
             "",
             "<b>Memory:</b>",
             f"  memories={mc['memories']} · lessons={mc['lessons']} · "
             f"raw_events={mc['raw_events']}",
             "",
             f"<b>Last smoke:</b> <code>{smoke[:140]}</code>",
             f"<b>Last deploy:</b> {dep[:160]}"]

    if aud:
        extra.append("")
        extra.append("<b>Last audit (3):</b>")
        for ln in aud:
            extra.append(f"  <code>{ln[:140]}</code>")

    return base + "\n" + "\n".join(extra)


async def run_agent_metrics() -> str:
    """Compact numeric snapshot — useful for /agent_metrics."""
    pc = _pending_counts()
    mc = _memory_counts()
    proc = _proc_count()
    git  = _git_summary()

    # Audit count last 24h
    audit24 = 0
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
        for ln in _last_audit_lines(500):
            if ln and ln > cutoff:
                audit24 += 1
    except Exception:
        pass

    return ("<b>📈 Agent Metrics</b>\n"
            f"git: {git}\n"
            f"procs: {proc}\n"
            f"pending: actions={pc['pending_actions']} "
            f"code_queued={pc['code_tasks_queued']} "
            f"code_running={pc['code_tasks_running']} "
            f"waiting={pc['code_tasks_waiting_confirm']}\n"
            f"memory: memories={mc['memories']} lessons={mc['lessons']} "
            f"raw_events={mc['raw_events']}\n"
            f"audit24h: {audit24}")


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
