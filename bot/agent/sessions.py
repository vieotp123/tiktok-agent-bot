"""
Time-boxed permission sessions.

A session GRANT temporarily widens what the agent can auto-execute
without per-action confirm. It NEVER bypasses high-risk public actions
(post / DM / .env / storage_state / merge main / delete / restart) —
those always require an explicit `/confirm_action`.

Scopes:
  low_only          — auto-approve low-risk (default behaviour)
  low_medium        — auto-approve low + medium risk (e.g. product CRUD)
  code_low_medium   — auto-approve queueing + executing low/medium
                      code_tasks (the Claude/Codex worker still drives;
                      this just removes the per-task admin tap)
  admin_readonly    — read-only commands only; explicit deny on any write

Storage:
  data/telegram/permission_session.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bot.agent.audit_log import log_action

SESSION_FILE = Path("/opt/tiktok-bot/data/telegram/permission_session.json")

VALID_SCOPES = ("low_only", "low_medium", "code_low_medium", "admin_readonly")
DEFAULT_SCOPE = "low_only"

# Hard rules — patterns that ALWAYS require explicit confirm regardless
# of granted session scope. Mirrors bot/agent/risk.py high-risk markers.
ALWAYS_CONFIRM_HINTS = (
    "post", "dm khách", "gửi dm", "publish",
    ".env", "storage_state", "tiktok_bot.py", "playwright",
    "git push.*main", "merge.*main",
    "restart", "systemctl", "deploy", "rollback",
    "drop table", "delete from", "rm -rf",
)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    if not SESSION_FILE.exists():
        return {}
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def grant_session(scope: str, minutes: int, *, user: str = "tg_admin") -> dict:
    """Grant a time-boxed scope. Returns the active grant record."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"invalid scope {scope!r}. valid={VALID_SCOPES}")
    minutes = max(1, min(int(minutes), 240))   # cap at 4 hours
    expires_at = _now_ts() + minutes * 60
    record = {
        "scope":      scope,
        "user":       user,
        "granted_at": _now_iso(),
        "expires_at": expires_at,
    }
    _save(record)
    log_action(user=user, action="session_grant", risk_level="medium",
               status="ok",
               result_summary=f"scope={scope} minutes={minutes}")
    return record


def revoke_session(*, user: str = "tg_admin") -> bool:
    if not SESSION_FILE.exists():
        return False
    try:
        SESSION_FILE.unlink()
    except Exception:
        return False
    log_action(user=user, action="session_revoke", risk_level="low",
               status="ok", result_summary="cleared")
    return True


def current_session() -> dict | None:
    """Return active session record or None if none / expired."""
    rec = _load()
    if not rec:
        return None
    if _now_ts() > float(rec.get("expires_at", 0)):
        # Auto-clean expired
        try:
            SESSION_FILE.unlink()
        except Exception:
            pass
        return None
    return rec


def current_scope() -> str:
    rec = current_session()
    return rec["scope"] if rec else DEFAULT_SCOPE


def remaining_minutes() -> int:
    rec = current_session()
    if not rec:
        return 0
    delta = float(rec.get("expires_at", 0)) - _now_ts()
    return max(0, int(delta // 60))


def can_auto_approve(risk_level: str, goal: str = "") -> tuple[bool, str]:
    """Decide whether the active session permits auto-approving an
    action of given risk_level (and optionally goal text).

    Returns (ok, reason).

    Hard rules:
      - Always-confirm hints (DM/post/.env/storage_state/restart/deploy/
        merge main/drop table) → never auto-approve, regardless of scope.
      - admin_readonly → only low-risk read-only allowed.
    """
    g = (goal or "").lower()
    for hint in ALWAYS_CONFIRM_HINTS:
        # Treat each hint as a literal substring (cheap; risk.py already
        # owns the regex authority).
        if hint.replace(".*", " ") in g.replace(" main ", " main "):
            return False, f"goal contains always-confirm hint: {hint!r}"

    scope = current_scope()
    risk  = (risk_level or "low").lower()

    if scope == "admin_readonly":
        return (risk == "low"), "admin_readonly only allows low"
    if scope == "low_only":
        return (risk == "low"), "low_only only auto-approves low"
    if scope == "low_medium":
        return (risk in ("low", "medium")), \
               "low_medium auto-approves low+medium"
    if scope == "code_low_medium":
        return (risk in ("low", "medium")), \
               "code_low_medium auto-approves low+medium"
    return False, f"unknown scope {scope!r}"


def format_session_panel() -> str:
    """Telegram /permissions render."""
    rec  = current_session()
    if not rec:
        return ("<b>📜 Permissions</b>\n"
                f"Active scope: <b>{DEFAULT_SCOPE}</b>  "
                "<i>(default — no time-boxed grant)</i>\n\n"
                "Use /grant_session &lt;scope&gt; &lt;minutes&gt; to widen.\n"
                f"Valid scopes: {', '.join(VALID_SCOPES)}")
    return ("<b>📜 Permissions</b>\n"
            f"Active scope: <b>{rec['scope']}</b>\n"
            f"Granted: {rec['granted_at']}\n"
            f"Remaining: <b>{remaining_minutes()}</b> min\n\n"
            "High-risk public actions still require /confirm_action.")
