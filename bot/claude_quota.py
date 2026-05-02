"""
Claude quota scheduler.

Honest design: this module does NOT try to detect quota by API probing
(false positives, wastes the actual quota). Instead, the admin records
when their Claude quota resets, and the scheduler:

  1. Notifies once when the reset time arrives.
  2. If autorun is enabled AND the local coding CLI is non-interactive,
     triggers `bot.coding_worker_bridge.run_once()` (or a small batch).
  3. If the CLI is unavailable, sends the admin the exact manual command
     they should run instead.

State lives at data/claude_quota.json (gitignored):

    {
      "reset_at":         "2026-05-02T14:30:00Z" | null,
      "limited":          true | false,
      "autorun":          true | false,
      "max_tasks":        1,
      "last_notified_at": "...",   # when we last DM'd "quota reset"
      "last_run_at":      "...",   # when we last actually invoked the
                                    # bridge
    }

Public API:
    set_reset_at(iso_or_human)     -> dict
    set_reset_in(human_duration)   -> dict      # "30m" / "2h" / "3h30m"
    set_limited(flag: bool)        -> dict
    set_autorun(flag, max_tasks=1) -> dict
    status_summary()               -> str (HTML)
    state()                        -> dict

Background loop:
    start_scheduler_thread()       — fire-and-forget; safe to call once
                                       at telegram_bot import time.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

QUOTA_FILE = Path("/opt/tiktok-bot/data/claude_quota.json")
CHECK_INTERVAL_SEC = 60


# ── State helpers ─────────────────────────────────────────────────────────────

_DEFAULT: dict = {
    # Legacy keys (kept for backward compat with existing callers/tests)
    "reset_at":            None,    # ISO 8601 UTC, when limit ends
    "limited":             False,   # True when we believe Claude is rate-limited
    "autorun":             False,   # True → fire run_batch on reset
    "max_tasks":           1,       # batch size for autorun
    "last_notified_at":    None,    # legacy "due-time" notifier dedup
    "last_run_at":          None,
    # v2 — honest availability tracking
    "status":              "unknown",  # unknown|available|limited|auth_required|error
    "last_probe_at":       None,
    "last_success_at":     None,
    "last_limited_at":     None,
    "retry_after_seconds": 0,
    "next_probe_at":       None,
    "autorun_enabled":     False,    # mirrors `autorun`; kept for new spec readability
    "autorun_max_tasks":   1,
    "last_error_summary":  "",
    "last_notified_key":   "",       # dedup key per state transition
    "probe_count":         0,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def state() -> dict:
    if not QUOTA_FILE.exists():
        return dict(_DEFAULT)
    try:
        d = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        merged = dict(_DEFAULT); merged.update(d)
        return merged
    except Exception:
        return dict(_DEFAULT)


def _save(d: dict) -> None:
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

_ISO_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?Z?$")
_HUMAN_RE = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*$",
                       re.IGNORECASE)


def _parse_reset(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    if _ISO_RE.match(text):
        norm = text.replace(" ", "T")
        if not norm.endswith("Z"):
            norm += "Z"
        try:
            return datetime.strptime(norm, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc,
            )
        except Exception:
            try:
                return datetime.strptime(norm, "%Y-%m-%dT%H:%MZ").replace(
                    tzinfo=timezone.utc,
                )
            except Exception:
                return None
    return None


def _parse_human_duration(text: str) -> Optional[timedelta]:
    text = (text or "").strip()
    m = _HUMAN_RE.match(text)
    if not m:
        return None
    h, mi = m.group(1), m.group(2)
    if not h and not mi:
        return None
    return timedelta(hours=int(h or 0), minutes=int(mi or 0))


def set_reset_at(text: str) -> dict:
    """Set reset time as ISO 8601 UTC: 'YYYY-MM-DD HH:MM' or with seconds."""
    when = _parse_reset(text)
    if not when:
        raise ValueError(f"unrecognised reset time {text!r} — "
                         "expected 'YYYY-MM-DD HH:MM' UTC")
    d = state()
    d["reset_at"]         = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["last_notified_at"] = None
    d["limited"]          = True
    _save(d)
    return d


def set_reset_in(text: str) -> dict:
    """Set reset 'X h Y m' from now (UTC)."""
    delta = _parse_human_duration(text)
    if not delta:
        raise ValueError(f"unrecognised duration {text!r} — "
                         "use e.g. '30m', '2h', '3h30m'")
    d = state()
    when = _now() + delta
    d["reset_at"]         = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["last_notified_at"] = None
    d["limited"]          = True
    _save(d)
    return d


def set_limited(flag: bool) -> dict:
    d = state()
    d["limited"] = bool(flag)
    if not flag:
        d["reset_at"]         = None
        d["last_notified_at"] = None
    _save(d)
    return d


def set_autorun(flag: bool, max_tasks: int = 1) -> dict:
    d = state()
    d["autorun"]   = bool(flag)
    d["max_tasks"] = max(1, min(int(max_tasks), 3))
    _save(d)
    return d


def status_summary() -> str:
    d = state()
    lines = ["<b>📅 Claude quota</b>"]
    lines.append(f"limited: <b>{'yes' if d['limited'] else 'no'}</b>")
    if d["reset_at"]:
        try:
            t   = datetime.strptime(d["reset_at"], "%Y-%m-%dT%H:%M:%SZ"
                                    ).replace(tzinfo=timezone.utc)
            now = _now()
            delta = t - now
            sec = int(delta.total_seconds())
            if sec > 0:
                h, m = divmod(sec // 60, 60)
                eta = f"{h}h{m:02d}m"
                lines.append(f"reset_at: <code>{d['reset_at']}</code> "
                             f"(in {eta})")
            else:
                lines.append(f"reset_at: <code>{d['reset_at']}</code> "
                             f"(due, will fire next check)")
        except Exception:
            lines.append(f"reset_at: <code>{d['reset_at']}</code>")
    else:
        lines.append("reset_at: <i>not set</i>")
    lines.append(f"autorun: <b>{'on' if d['autorun'] else 'off'}</b> "
                 f"(max_tasks={d['max_tasks']})")
    if d["last_notified_at"]:
        lines.append(f"last_notified_at: {d['last_notified_at']}")
    if d["last_run_at"]:
        lines.append(f"last_run_at: {d['last_run_at']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# v2 — honest availability tracking
# ─────────────────────────────────────────────────────────────────────────────
#
# Reality: the Claude Code CLI does NOT expose remaining quota %. Pretending
# would mislead. Instead we track:
#   - probes (cheap "Reply only: CLAUDE_PROBE_OK" calls, cached 5 min)
#   - error parsing on actual worker output
#   - exponential backoff when limited without an exact reset time
#
# The brain pauses tasks rather than failing them, schedules retries, and
# notifies the admin once per state transition.
# ─────────────────────────────────────────────────────────────────────────────

import os
import subprocess
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

PROBE_CACHE_SUCCESS_SEC  = 5 * 60          # success cached 5 min
PROBE_TIMEOUT_SEC        = 60
PROBE_PROMPT             = "Reply only: CLAUDE_PROBE_OK"
PROBE_OK_MARKER          = "CLAUDE_PROBE_OK"

# Backoff schedule for "limited but no exact reset time"
_BACKOFF_SCHEDULE_MIN    = (30, 60, 120, 240)  # cap 4h


# ── Public state writers ──────────────────────────────────────────────────────

def get_quota_state() -> dict:
    """Alias for state() — v2 callers should use this."""
    return state()


def save_quota_state(d: dict) -> None:
    """Direct merge-and-save for v2 callers."""
    cur = state(); cur.update(d or {}); _save(cur)


def _set_next_probe_with_backoff(d: dict) -> None:
    """Pick next_probe_at when no exact reset is known."""
    n = int(d.get("probe_count") or 0)
    idx = min(n, len(_BACKOFF_SCHEDULE_MIN) - 1)
    delay_min = _BACKOFF_SCHEDULE_MIN[idx]
    d["next_probe_at"] = (_now() + _td(minutes=delay_min)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def mark_available(*, note: str = "", actual_model: str = "") -> dict:
    d = state()
    iso = _now_iso()
    d.update(
        status              = "available",
        last_probe_at       = iso,
        last_success_at     = iso,
        retry_after_seconds = 0,
        next_probe_at       = (_now() + _td(seconds=PROBE_CACHE_SUCCESS_SEC)
                               ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_error_summary  = note[:300],
        limited             = False,
    )
    if actual_model:
        d["actual_model"] = actual_model[:80]
    _save(d)
    return d


def mark_limited(*, reset_at: str | None = None,
                  retry_after_seconds: int | None = None,
                  note: str = "") -> dict:
    d = state()
    iso = _now_iso()
    d["status"]              = "limited"
    d["last_probe_at"]       = iso
    d["last_limited_at"]     = iso
    d["last_error_summary"]  = note[:300]
    d["limited"]             = True
    d["probe_count"]         = int(d.get("probe_count") or 0) + 1

    if reset_at:
        d["reset_at"]            = reset_at
        d["retry_after_seconds"] = 0
        d["next_probe_at"]       = reset_at
    elif retry_after_seconds and retry_after_seconds > 0:
        d["retry_after_seconds"] = int(retry_after_seconds)
        d["reset_at"]            = (_now()
            + _td(seconds=int(retry_after_seconds))).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        d["next_probe_at"]       = d["reset_at"]
    else:
        # Unknown — fall back to exponential backoff
        d["retry_after_seconds"] = 0
        d["reset_at"]            = None
        _set_next_probe_with_backoff(d)
    _save(d)
    return d


def mark_auth_required(*, note: str = "") -> dict:
    d = state()
    iso = _now_iso()
    d.update(
        status              = "auth_required",
        last_probe_at       = iso,
        last_error_summary  = note[:300],
        limited             = False,        # not the same as quota
        # Don't auto-probe again; admin must run `claude login`.
        next_probe_at       = (_now() + _td(hours=4)).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"),
    )
    _save(d)
    return d


def mark_error(*, note: str = "") -> dict:
    d = state()
    iso = _now_iso()
    d.update(
        status              = "error",
        last_probe_at       = iso,
        last_error_summary  = note[:300],
        # Retry after 5 min on ambiguous errors (network etc.)
        next_probe_at       = (_now() + _td(minutes=5)).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"),
    )
    _save(d)
    return d


# ── Probe gating ──────────────────────────────────────────────────────────────

def should_probe_now() -> bool:
    """Return True if we should consume a fresh probe right now."""
    d = state()
    if d.get("status") in (None, "", "unknown", "error"):
        return True
    nxt = d.get("next_probe_at")
    if not nxt:
        return True
    try:
        t = _dt.strptime(nxt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
    except Exception:
        return True
    return _now() >= t


def next_retry_time() -> Optional[_dt]:
    d = state()
    nxt = d.get("next_probe_at") or d.get("reset_at")
    if not nxt:
        return None
    try:
        return _dt.strptime(nxt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
    except Exception:
        return None


# ── Error parser (CLI stdout / stderr → state hint) ──────────────────────────

# Keep the patterns broad — Claude's user-facing wording shifts. Each tuple
# is (regex, "key" used by callers).
_LIMIT_PATTERNS = (
    re.compile(r"usage\s+limit\s+reached", re.I),
    re.compile(r"quota\s+exceeded",        re.I),
    re.compile(r"\brate\s+limit",          re.I),
    re.compile(r"too\s+many\s+requests",   re.I),
    re.compile(r"\blimit\s+reached",       re.I),
    re.compile(r"try\s+again\s+in",        re.I),
    re.compile(r"please\s+try\s+again",    re.I),
)
_AUTH_PATTERNS = (
    re.compile(r"login\s+required",        re.I),
    re.compile(r"please\s+login",          re.I),
    re.compile(r"authenticat(?:ion|ed)",   re.I),
    re.compile(r"not\s+authenticated",     re.I),
    re.compile(r"unauthorized",            re.I),
    re.compile(r"missing\s+api\s+key",     re.I),
)
_RESET_AT_PATTERNS = (
    re.compile(r"resets?\s+at\s+([0-9T:\- ]+ ?(?:UTC|Z|[+\-]\d{2}:?\d{2})?)", re.I),
    re.compile(r"available\s+at\s+([0-9T:\- ]+ ?(?:UTC|Z|[+\-]\d{2}:?\d{2})?)", re.I),
)
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[- ]after[:\s]+(\d+)\s*(?:s|sec|seconds)?", re.I),
    re.compile(r"try\s+again\s+in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?(?:\s*(\d+)\s*s)?", re.I),
    re.compile(r"in\s+(\d+)\s*(?:m|min|minute)", re.I),
    re.compile(r"in\s+(\d+)\s*(?:h|hour)", re.I),
)


def parse_claude_error(text: str) -> dict:
    """Best-effort parser for Claude CLI error output.

    Returns:
        {"kind": "limited" | "auth_required" | "error" | "ok",
         "reset_at":            "ISO8601" | None,
         "retry_after_seconds": int | None,
         "summary":             "<≤200 char snippet>"}

    `kind="ok"` means we couldn't classify it as limit/auth — caller
    should treat the run as a generic worker failure.
    """
    if not text:
        return {"kind": "ok", "reset_at": None,
                "retry_after_seconds": None, "summary": ""}
    snippet = text.strip()[:400]

    is_auth = any(p.search(text) for p in _AUTH_PATTERNS)
    is_limit = any(p.search(text) for p in _LIMIT_PATTERNS)

    reset_at: Optional[str] = None
    for p in _RESET_AT_PATTERNS:
        m = p.search(text)
        if m:
            raw = m.group(1).strip().rstrip(".,;:")
            # Try a few common formats
            for fmt in ("%Y-%m-%dT%H:%M:%SZ",
                         "%Y-%m-%d %H:%M:%S",
                         "%Y-%m-%dT%H:%M:%S",
                         "%Y-%m-%d %H:%M",
                         "%Y-%m-%dT%H:%M",
                         "%Y-%m-%d %H:%M UTC"):
                try:
                    t = _dt.strptime(raw, fmt).replace(tzinfo=_tz.utc)
                    reset_at = t.strftime("%Y-%m-%dT%H:%M:%SZ")
                    break
                except Exception:
                    continue
            if reset_at:
                break

    retry_sec: Optional[int] = None
    # "retry-after: 123" pattern
    m = _RETRY_AFTER_PATTERNS[0].search(text)
    if m:
        try:
            retry_sec = int(m.group(1))
        except Exception:
            pass
    # "try again in 2h 30m 5s"
    if retry_sec is None:
        m = _RETRY_AFTER_PATTERNS[1].search(text)
        if m and any(g for g in m.groups()):
            h = int(m.group(1) or 0)
            mm = int(m.group(2) or 0)
            ss = int(m.group(3) or 0)
            tot = h * 3600 + mm * 60 + ss
            if tot > 0:
                retry_sec = tot
    # "in 2 minutes" / "in 2 hours" — only if we still have nothing
    if retry_sec is None:
        m = _RETRY_AFTER_PATTERNS[2].search(text)
        if m:
            try:
                retry_sec = int(m.group(1)) * 60
            except Exception:
                pass
    if retry_sec is None:
        m = _RETRY_AFTER_PATTERNS[3].search(text)
        if m:
            try:
                retry_sec = int(m.group(1)) * 3600
            except Exception:
                pass

    if is_auth and not is_limit:
        return {"kind": "auth_required", "reset_at": None,
                "retry_after_seconds": None, "summary": snippet}
    if is_limit:
        return {"kind": "limited", "reset_at": reset_at,
                "retry_after_seconds": retry_sec, "summary": snippet}
    return {"kind": "ok", "reset_at": None,
            "retry_after_seconds": None, "summary": snippet}


# ── Probe ────────────────────────────────────────────────────────────────────

def _build_probe_command() -> Optional[list[str]]:
    """Return argv for a non-interactive Claude probe, or None if the
    bridge can't supply one."""
    try:
        from bot import coding_worker_bridge as _cwb
    except Exception:
        return None
    tool = _cwb.get_preferred_coding_tool()
    if not tool or tool.name != "claude" or not tool.noninteractive_ok:
        return None

    cli_path = (os.environ.get("CLAUDE_CLI_PATH") or "").strip() or tool.binary
    user     = (os.environ.get("CODING_WORKER_USER") or "").strip()
    model    = (os.environ.get("CLAUDE_CODE_MODEL") or "opus").strip()

    cmd: list[str] = []
    if user:
        cmd += ["sudo", "-n", "-u", user, "-H",
                "env", f"ANTHROPIC_MODEL={model}"]
    cmd += [cli_path,
            "--model",          model,
            "--fallback-model", "sonnet",
            "--print"]
    return cmd


def probe_claude_available(force: bool = False) -> dict:
    """Run a tiny non-interactive Claude call to verify availability.

    Caches success for PROBE_CACHE_SUCCESS_SEC; respects next_probe_at
    when limited (unless `force=True`).

    Returns the new state dict.
    """
    d = state()
    if not force and not should_probe_now():
        return d

    cmd = _build_probe_command()
    if cmd is None:
        return mark_error(note="no preferred Claude CLI on PATH for probe")

    log_cmd = " ".join(cmd[-3:])  # only the last 3 args; never the full env
    d["probe_count"] = int(d.get("probe_count") or 0) + 1

    try:
        proc = subprocess.run(
            cmd, input=PROBE_PROMPT,
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return mark_error(note=f"probe timeout after {PROBE_TIMEOUT_SEC}s")
    except FileNotFoundError as e:
        return mark_error(note=f"probe binary missing: {e}")
    except Exception as e:
        return mark_error(note=f"probe error: {type(e).__name__}: {e}")

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    out = out.strip()
    rc  = proc.returncode

    # Available?
    if rc == 0 and PROBE_OK_MARKER in out:
        return mark_available(note=f"probe rc=0 cmd={log_cmd}")

    # Try to classify the failure
    parsed = parse_claude_error(out)
    if parsed["kind"] == "limited":
        return mark_limited(reset_at=parsed["reset_at"],
                            retry_after_seconds=parsed["retry_after_seconds"],
                            note=parsed["summary"])
    if parsed["kind"] == "auth_required":
        return mark_auth_required(note=parsed["summary"])

    # Unclassified non-zero exit → generic error
    return mark_error(note=(out[:300] or f"probe rc={rc}"))


# ── Vietnamese status formatter ───────────────────────────────────────────────

def format_claude_status_vi() -> str:
    d = state()
    icon = {"available": "✅", "limited": "⏸", "auth_required": "🔒",
            "error": "💥", "unknown": "❓"}.get(d.get("status", "unknown"),
                                                "•")
    lines = [f"<b>{icon} Claude Worker</b>"]
    model = d.get("actual_model") or os.environ.get("CLAUDE_CODE_MODEL", "opus")
    lines.append(f"Model: <code>{model}</code>")
    lines.append(f"Status: <b>{d.get('status', 'unknown')}</b>")
    if d.get("last_probe_at"):
        lines.append(f"Probe gần nhất: <i>{d['last_probe_at']}</i>")
    if d.get("last_success_at"):
        lines.append(f"Thành công gần nhất: <i>{d['last_success_at']}</i>")
    if d.get("last_limited_at"):
        lines.append(f"Bị limit gần nhất: <i>{d['last_limited_at']}</i>")
    if d.get("reset_at"):
        try:
            t   = _dt.strptime(d["reset_at"],
                                "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
            sec = int((t - _now()).total_seconds())
            if sec > 0:
                h, m = divmod(sec // 60, 60)
                lines.append(f"Reset/retry: <code>{d['reset_at']}</code> "
                             f"(còn {h}h{m:02d}m)")
            else:
                lines.append(f"Reset/retry: <code>{d['reset_at']}</code> "
                             "(đã đến hạn)")
        except Exception:
            lines.append(f"Reset/retry: <code>{d['reset_at']}</code>")
    elif d.get("next_probe_at"):
        lines.append(f"Probe kế: <code>{d['next_probe_at']}</code>")
    lines.append(f"Autorun: <b>{'on' if d.get('autorun') else 'off'}</b> "
                 f"(max_tasks={d.get('max_tasks', 1)})")
    if d.get("last_error_summary"):
        # Strip HTML risk
        s = d["last_error_summary"][:160]
        s = s.replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<i>{s}</i>")
    # Queue snapshot
    try:
        from bot.code_tasks import list_tasks
        q = list_tasks(status="queued", limit=200)
        lines.append(f"Task đang queue: <b>{len(q)}</b>")
    except Exception:
        pass
    return "\n".join(lines)


# ── Scheduler thread ──────────────────────────────────────────────────────────

_thread_started = False
_thread_lock = threading.Lock()


def _due(d: dict) -> bool:
    if not d.get("reset_at"):
        return False
    if d.get("last_notified_at") == d["reset_at"]:
        return False
    try:
        t = datetime.strptime(d["reset_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return False
    return _now() >= t


def _on_due() -> None:
    """Called once when the reset time first arrives.

    v2: probe Claude FIRST, then run the bridge only if available.
    Notifies admin once per state transition (last_notified_key dedup).
    """
    from bot.telegram_report import send_telegram_message
    d = state()
    msg_lines = [f"⏰ <b>Claude quota reset reached</b> "
                 f"({d.get('reset_at')})"]

    autorun_ok = bool(d.get("autorun"))
    max_tasks  = int(d.get("max_tasks") or 1)

    # Probe to confirm Claude really came back
    probed = probe_claude_available(force=True)
    msg_lines.append(f"Probe result: <b>{probed.get('status', '?')}</b>")
    if probed.get("status") != "available":
        if probed.get("last_error_summary"):
            msg_lines.append(f"<i>{probed['last_error_summary'][:160]}</i>")
        if probed.get("status") == "limited":
            msg_lines.append("Claude vẫn chưa hồi quota; sẽ thử lại sau "
                             f"<code>{probed.get('next_probe_at')}</code>.")
        elif probed.get("status") == "auth_required":
            msg_lines.append("⚠ Cần đăng nhập lại: chạy "
                             "<code>claude login</code> trên VPS.")
        else:
            msg_lines.append("⚠ Probe không thành công, sẽ thử lại theo "
                             "lịch backoff.")
        d["last_notified_key"] = f"probe_failed:{probed.get('status')}:{probed.get('next_probe_at')}"
        d["last_notified_at"]  = d.get("reset_at")
        _save(d)
        send_telegram_message("\n".join(msg_lines))
        return

    # Available — fire bridge if autorun is on
    if autorun_ok:
        try:
            from bot import coding_worker_bridge as bridge
            msg_lines.append(f"Autorun: gọi bridge cho tối đa "
                             f"<b>{max_tasks}</b> task.")
            import asyncio
            results = asyncio.run(bridge.run_batch(max_tasks))
            for r in results:
                msg_lines.append("• " + bridge.format_run_result(r)
                                   .replace("\n", " ")[:160])
            # Re-probe after run (catches mid-run quota hit)
            probe_claude_available(force=True)
            d2 = state()
            d2["last_run_at"]      = _now_iso()
            d2["last_notified_at"] = d.get("reset_at")
            d2["last_notified_key"] = f"available_run:{_now_iso()[:13]}"
            _save(d2)
        except Exception as e:
            msg_lines.append(f"bridge run_batch error: {e}")
    else:
        msg_lines.append("Autorun đang OFF — bật bằng "
                         "<code>/claude_autorun_on 1</code> nếu muốn agent "
                         "tự chạy lần sau.")
        d["last_notified_at"]   = d.get("reset_at")
        d["last_notified_key"]  = f"available_no_autorun:{_now_iso()[:13]}"
        _save(d)

    send_telegram_message("\n".join(msg_lines))


def _scheduler_loop() -> None:
    while True:
        try:
            d = state()
            if _due(d):
                _on_due()
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL_SEC)


def start_scheduler_thread() -> bool:
    """Idempotent: launch the scheduler thread once per process."""
    global _thread_started
    with _thread_lock:
        if _thread_started:
            return False
        t = threading.Thread(target=_scheduler_loop, daemon=True,
                              name="claude-quota-scheduler")
        t.start()
        _thread_started = True
        return True
