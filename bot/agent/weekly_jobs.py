"""
Weekly-jobs scheduler — minimal v0.

First (and currently only) job:
  - **nl_cohort_drift**: replays every phrase in
    `data/nl_eval_cohort.jsonl` through `bot.agent.nl_router.classify`
    and produces a Telegram-ready digest of any phrase whose intent has
    drifted from the cohort's recorded expected intent. Roadmap item:
    "NL eval suite — automated weekly eval that runs 200+ admin phrases
    and reports classifier drift. Output → Telegram digest."

The scheduler is **idempotent per ISO week**: `run_weekly_jobs()` checks
`data/weekly_jobs_state.json` (gitignored runtime state) and skips a job
that has already run this ISO week (UTC). Each run is audit-logged via
`bot.agent.audit_log.log_action`, and the digest is shipped to the admin
chat via `bot.telegram_report.send_telegram_message` unless the caller
opts out (`send_telegram=False`).

This module never sends DMs, never edits .env / storage_state, and is
read-only against the cohort + classifier. Risk: low.

Public API:
  nl_cohort_drift_check(cases_in=None,
                        classify_fn=None)         -> dict
  format_nl_cohort_drift_html(result)             -> str
  iso_week(now)                                   -> str   ("YYYY-W##")
  should_run_this_week(job_name, now=None)        -> bool
  mark_ran_this_week(job_name, now=None)          -> None
  run_weekly_jobs(now=None, force=False,
                   send_telegram=True)            -> dict

Ties in to:
  - docs/SELF_OPERATING_AGENT.md §11 (eval harness — nl_cohort)
  - docs/SELF_OPERATING_AGENT.md §13 (run-once loops, never daemons)
  - docs/OPERATING_RULES.md §1 (no token logging — telegram_report
    handles credentials and never echoes them)
  - docs/CLAUDE_CODE_WORKER.md §3 (no public action from a code task —
    this digest goes to the admin chat only)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from bot.agent.audit_log import log_action
from bot.agent.evals import NL_COHORT_PATH, _load_nl_cohort

STATE_PATH = Path("/opt/tiktok-bot/data/weekly_jobs_state.json")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_week(now: Optional[datetime] = None) -> str:
    now = now or _utcnow()
    iso = now.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


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


def should_run_this_week(job_name: str,
                          now: Optional[datetime] = None) -> bool:
    """True when `job_name` has not run yet in the current ISO week."""
    now = now or _utcnow()
    state = _load_state()
    last = (state.get(job_name) or {}).get("last_run_week", "")
    return last != iso_week(now)


def mark_ran_this_week(job_name: str,
                        now: Optional[datetime] = None) -> None:
    now = now or _utcnow()
    state = _load_state()
    state[job_name] = {
        "last_run_week": iso_week(now),
        "last_run_at":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_state(state)


# ── NL cohort drift ───────────────────────────────────────────────────────────

def nl_cohort_drift_check(
    cases_in: Optional[list[dict]] = None,
    classify_fn: Optional[Callable[[str], object]] = None,
) -> dict:
    """Replay every cohort phrase through the classifier and structure
    the drift result. `cases_in` / `classify_fn` let evals inject
    synthetic data without touching the live cohort or classifier.

    Shape:
      {
        "job":          "nl_cohort_drift",
        "run_at":       "<UTC ISO>",
        "cohort_path":  "<absolute path>",
        "total":        int,
        "drift_count":  int,
        "drift_rate":   float,            # 0.0..1.0
        "drift_rows":   [
            {"text": str, "want": str, "got": str,
             "confidence": float, "tag": str},
            ...
        ],
      }
    """
    if classify_fn is None:
        from bot.agent.nl_router import classify as classify_fn  # type: ignore

    if cases_in is None:
        cases = _load_nl_cohort()
    else:
        cases = list(cases_in)

    drift_rows: list[dict] = []
    for c in cases:
        text = (c.get("text") or "").strip()
        want = (c.get("intent") or "").strip()
        if not text or not want:
            continue
        got = classify_fn(text)
        got_name = getattr(got, "name", "")
        got_conf = float(getattr(got, "confidence", 0.0) or 0.0)
        if got_name != want:
            drift_rows.append({
                "text":       text,
                "want":       want,
                "got":        got_name,
                "confidence": got_conf,
                "tag":        (c.get("tag") or ""),
            })

    total = len(cases)
    return {
        "job":         "nl_cohort_drift",
        "run_at":      _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cohort_path": str(NL_COHORT_PATH),
        "total":       total,
        "drift_count": len(drift_rows),
        "drift_rate":  (len(drift_rows) / total) if total else 0.0,
        "drift_rows":  drift_rows,
    }


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_nl_cohort_drift_html(result: dict) -> str:
    """Telegram-ready digest. Caps the body so a fully-broken classifier
    cannot exceed Telegram's 4 KB sendMessage limit."""
    total = int(result.get("total") or 0)
    drift = list(result.get("drift_rows") or [])
    rate  = float(result.get("drift_rate") or 0.0) * 100.0
    icon  = "🟢" if not drift else ("🟡" if rate < 5.0 else "🔴")
    head  = (f"{icon} <b>NL cohort drift</b> — {len(drift)}/{total} "
             f"phrase(s) drifted ({rate:.1f}%)")
    if total == 0:
        return (f"{icon} <b>NL cohort drift</b> — cohort empty; "
                f"check <code>{_esc(str(NL_COHORT_PATH))}</code>.")
    if not drift:
        return head + "\n<i>Classifier matches cohort 100%.</i>"
    lines = [head]
    for row in drift[:25]:
        text = _esc((row.get("text") or "")[:60])
        want = _esc(row.get("want") or "")
        got  = _esc(row.get("got") or "")
        lines.append(f"• <code>{text}</code> — got <b>{got}</b> "
                     f"want <b>{want}</b>")
    if len(drift) > 25:
        lines.append(f"<i>… và {len(drift) - 25} phrase khác.</i>")
    return "\n".join(lines)


# ── Scheduler entry point ─────────────────────────────────────────────────────

def run_weekly_jobs(now: Optional[datetime] = None,
                    force: bool = False,
                    send_telegram: bool = True) -> dict:
    """Run every weekly job that is due this ISO week.

    Idempotent per ISO week unless `force=True`. Each job's success or
    skip is audit-logged. The NL cohort digest is sent to the admin chat
    via `bot.telegram_report.send_telegram_message` when `send_telegram`
    is true and the job actually ran.

    Returns:
      {
        "ran_at":  "<UTC ISO>",
        "iso_week": "<YYYY-W##>",
        "jobs":    {<job_name>: <result-dict|"skipped"|"error: ...">},
      }
    """
    now = now or _utcnow()
    out: dict = {
        "ran_at":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iso_week": iso_week(now),
        "jobs":     {},
    }

    job = "nl_cohort_drift"
    if not force and not should_run_this_week(job, now=now):
        out["jobs"][job] = "skipped"
        log_action(
            user="weekly_jobs", action=f"weekly_jobs:{job}",
            risk_level="low", status="ok",
            result_summary=f"skipped (already ran in {iso_week(now)})",
            channel="internal",
        )
        return out

    try:
        result = nl_cohort_drift_check()
        out["jobs"][job] = result
        mark_ran_this_week(job, now=now)
        log_action(
            user="weekly_jobs", action=f"weekly_jobs:{job}",
            risk_level="low", status="ok",
            result_summary=(f"drift={result['drift_count']}/"
                            f"{result['total']} "
                            f"week={iso_week(now)}"),
            channel="internal",
        )
        if send_telegram:
            try:
                from bot.telegram_report import send_telegram_message
                send_telegram_message(format_nl_cohort_drift_html(result))
            except Exception as e:
                log_action(
                    user="weekly_jobs",
                    action=f"weekly_jobs:{job}:telegram",
                    risk_level="low", status="failed",
                    result_summary=f"telegram send failed: {e}"[:200],
                    channel="internal",
                )
    except Exception as e:
        out["jobs"][job] = f"error: {e}"
        log_action(
            user="weekly_jobs", action=f"weekly_jobs:{job}",
            risk_level="low", status="failed",
            result_summary=f"error: {e}"[:200],
            channel="internal",
        )
    return out


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="bot.agent.weekly_jobs")
    p.add_argument("--force", action="store_true",
                   help="Re-run even if already ran this ISO week.")
    p.add_argument("--no-telegram", action="store_true",
                   help="Skip the Telegram digest send.")
    args = p.parse_args()
    out = run_weekly_jobs(force=args.force,
                          send_telegram=not args.no_telegram)
    print(json.dumps(out, indent=2, default=str))
    job = out.get("jobs", {}).get("nl_cohort_drift")
    if isinstance(job, str) and job.startswith("error:"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
