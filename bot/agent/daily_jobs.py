"""
Daily-jobs scheduler — minimal v0.

First (and currently only) job:
  - **catalog_freshness_check**: which `active` products in business.db
    haven't been touched (`updated_at`) in N days. The roadmap also
    lists daily lead-followup digest + daily SEO crawl as future sub-jobs;
    they are intentionally NOT implemented here (Operating Rules: minimal
    safe change per task).

The scheduler is **idempotent per-day**: `run_daily_jobs()` checks
`data/daily_jobs_state.json` (gitignored runtime state) and skips a job
that has already run today (UTC). Each run is audit-logged via
`bot.agent.audit_log.log_action`.

This module never sends DMs, never edits .env / storage_state, and is
read-only against `business.db`. Risk: low. The Telegram dispatcher /
agent_autorun loop is the intended caller.

Public API:
  find_stale_active_products(stale_after_days=14, now=None,
                             products_in=None)   -> list[dict]
  catalog_freshness_check(stale_after_days=None, now=None,
                          products_in=None)      -> dict
  format_catalog_freshness_html(result)          -> str
  should_run_today(job_name, now=None)           -> bool
  mark_ran_today(job_name, now=None)             -> None
  run_daily_jobs(now=None, force=False)          -> dict

Ties in to:
  - docs/SELF_OPERATING_AGENT.md §13 (run-once loops, never daemons)
  - docs/OPERATING_RULES.md §4 (catalog freshness drives the
    `needs_update` warning surface; this job only flags, never quotes)
  - docs/CLAUDE_CODE_WORKER.md §3 (no public action from a code task —
    this module reports to admin only)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.agent.audit_log import log_action

STATE_PATH = Path("/opt/tiktok-bot/data/daily_jobs_state.json")

DEFAULT_STALE_DAYS = int(os.getenv("DAILY_JOBS_STALE_DAYS", "14") or 14)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


def should_run_today(job_name: str, now: Optional[datetime] = None) -> bool:
    """True when `job_name` has not run yet on the current UTC date."""
    now = now or _utcnow()
    state = _load_state()
    last = _parse_iso((state.get(job_name) or {}).get("last_run_at", ""))
    if last is None:
        return True
    return last.date() < now.date()


def mark_ran_today(job_name: str, now: Optional[datetime] = None) -> None:
    now = now or _utcnow()
    state = _load_state()
    state[job_name] = {
        "last_run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_state(state)


# ── Catalog freshness ─────────────────────────────────────────────────────────

def find_stale_active_products(
    stale_after_days: int = DEFAULT_STALE_DAYS,
    now: Optional[datetime] = None,
    products_in: Optional[list[dict]] = None,
) -> list[dict]:
    """Return active products whose `updated_at` is older than N days.

    `products_in` lets callers (and evals) inject a synthetic catalog so
    the live business.db is not touched. When None, reads via
    `bot.business_store.list_products(status="active")`.
    """
    now = now or _utcnow()
    if products_in is None:
        from bot.business_store import list_products
        rows = list_products(status="active", limit=500)
    else:
        rows = [p for p in products_in if (p.get("status") or "") == "active"]

    out: list[dict] = []
    for p in rows:
        ts = _parse_iso(p.get("updated_at") or "")
        if ts is None:
            days = stale_after_days + 1
        else:
            days = max(0, (now - ts).days)
        if days >= stale_after_days:
            out.append({
                "id":   p.get("id", ""),
                "name": p.get("name", ""),
                "network":     p.get("network", ""),
                "country":     p.get("country", ""),
                "updated_at":  p.get("updated_at", ""),
                "days_since_update": days,
            })
    out.sort(key=lambda x: x["days_since_update"], reverse=True)
    return out


def catalog_freshness_check(
    stale_after_days: Optional[int] = None,
    now: Optional[datetime] = None,
    products_in: Optional[list[dict]] = None,
) -> dict:
    """Run the freshness check and return a structured result dict.

    Shape:
      {
        "job":              "catalog_freshness",
        "run_at":           "<UTC ISO>",
        "stale_after_days": int,
        "total_active":     int,
        "stale_count":      int,
        "stale_products":   [ {id, name, days_since_update, ...} ],
      }
    """
    n = int(stale_after_days if stale_after_days is not None
            else DEFAULT_STALE_DAYS)
    now = now or _utcnow()

    if products_in is None:
        from bot.business_store import list_products
        active = list_products(status="active", limit=500)
    else:
        active = [p for p in products_in if (p.get("status") or "") == "active"]

    stale = find_stale_active_products(
        stale_after_days=n, now=now, products_in=active,
    )
    return {
        "job":              "catalog_freshness",
        "run_at":           now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stale_after_days": n,
        "total_active":     len(active),
        "stale_count":      len(stale),
        "stale_products":   stale,
    }


def format_catalog_freshness_html(result: dict) -> str:
    """Telegram-ready summary. Caller is responsible for HTML escaping
    only of fields it injects on top of this — the product names are
    already truncated and run through a minimal escape here."""
    n      = int(result.get("stale_after_days") or 0)
    total  = int(result.get("total_active") or 0)
    stale  = result.get("stale_products") or []
    icon   = "🟢" if not stale else "🟡"
    head   = (f"{icon} <b>Catalog freshness</b> — {len(stale)}/{total} "
              f"active product(s) chưa update &gt; {n} ngày")
    if not stale:
        return head + "\n<i>Tất cả active products còn tươi.</i>"
    lines = [head]
    for p in stale[:10]:
        name = (p.get("name") or "")[:60].replace("<", "&lt;").replace(">", "&gt;")
        pid  = (p.get("id") or "")[:40]
        days = int(p.get("days_since_update") or 0)
        lines.append(f"• <code>{pid}</code> — {name} ({days}d)")
    if len(stale) > 10:
        lines.append(f"<i>… và {len(stale) - 10} product khác.</i>")
    return "\n".join(lines)


# ── Scheduler entry point ─────────────────────────────────────────────────────

def run_daily_jobs(now: Optional[datetime] = None,
                    force: bool = False) -> dict:
    """Run every daily job that is due today.

    Idempotent per UTC date unless `force=True`. Each job's success or
    skip is audit-logged. Returns:
      {
        "ran_at":  "<UTC ISO>",
        "jobs":    {<job_name>: <result-dict|"skipped"|"error: ...">},
      }
    """
    now = now or _utcnow()
    out: dict = {
        "ran_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs":   {},
    }

    job = "catalog_freshness"
    if not force and not should_run_today(job, now=now):
        out["jobs"][job] = "skipped"
        log_action(
            user="daily_jobs", action=f"daily_jobs:{job}",
            risk_level="low", status="ok",
            result_summary="skipped (already ran today)",
            channel="internal",
        )
    else:
        try:
            result = catalog_freshness_check(now=now)
            out["jobs"][job] = result
            mark_ran_today(job, now=now)
            log_action(
                user="daily_jobs", action=f"daily_jobs:{job}",
                risk_level="low", status="ok",
                result_summary=(f"stale={result['stale_count']}/"
                                f"{result['total_active']} "
                                f"thr={result['stale_after_days']}d"),
                channel="internal",
            )
        except Exception as e:
            out["jobs"][job] = f"error: {e}"
            log_action(
                user="daily_jobs", action=f"daily_jobs:{job}",
                risk_level="low", status="failed",
                result_summary=f"error: {e}"[:200],
                channel="internal",
            )
    return out
