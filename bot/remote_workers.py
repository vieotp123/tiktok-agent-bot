"""
Remote Worker / SSH control foundation.

Goals:
  - Honest: does NOT install paramiko or any extra dep. Uses the
    system `ssh` binary, key-only auth.
  - Safe: tight host allowlist, key-path allowlist, command risk
    classifier (low/medium/high/blocked), output redaction, audit log.
  - No password storage.
  - Output truncated.
  - Audit every exec via bot.agent.audit_log.

Storage: data/remote_workers.json (gitignored). Keys live under
/opt/tiktok-bot/keys/ (gitignored).

Worker schema:
  {
    "id":         "worker2",
    "host":       "1.2.3.4",
    "port":       22,
    "username":   "ubuntu",
    "auth_type":  "key",
    "key_path":   "/opt/tiktok-bot/keys/worker2_id_ed25519",
    "tags":       ["ocr"],
    "enabled":    true,
    "created_at": "...",
    "notes":      ""
  }

Risk classifier (classify_ssh_command):
  blocked  → never run, even with confirm
  high     → admin must /confirm_action
  medium   → session grant or confirm
  low      → run with audit
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO       = Path("/opt/tiktok-bot")
WORKERS_FILE = REPO / "data" / "remote_workers.json"
KEYS_DIR     = REPO / "keys"

# Filesystem path roots a worker key may live under. Anything outside
# is rejected to prevent reading arbitrary identity files.
SAFE_KEY_ROOTS = (str(KEYS_DIR), "/opt/tiktok-bot/keys")

# Output / safety bounds
SSH_DEFAULT_TIMEOUT_SEC  = 30
SSH_MAX_OUTPUT_BYTES     = 64 * 1024     # 64KB per command
SSH_MAX_CMD_LEN          = 1024

# Hosts that are explicitly allowed. We require admin to add the host
# explicitly via /worker_add — there is no implicit allowlist.
def host_allowlist() -> set[str]:
    return {w["host"] for w in list_workers() if w.get("enabled")}


# ── Risk classifier ──────────────────────────────────────────────────────────

# BLOCKED — no confirm can override
_BLOCKED_PATTERNS = (
    re.compile(r"\bmkfs\b|\bdd\s+if=", re.I),
    re.compile(r"\bshred\b", re.I),
    re.compile(r"\bcurl\s+\S+\s*\|\s*(bash|sh)\b", re.I),
    re.compile(r"\bwget\s+\S+\s*\|\s*(bash|sh)\b", re.I),
    re.compile(r"\bnc\s+.*-e\b", re.I),
    re.compile(r"\bufw\s+disable\b", re.I),
    re.compile(r"\biptables\s+-F\b", re.I),
    re.compile(r"setenforce\s+0", re.I),
    re.compile(r"\bsystemctl\s+(disable|mask)\s+(ufw|firewalld|fail2ban)", re.I),
    re.compile(r"cat\s+.*(\.env|storage_state|id_rsa|\.pem|cookie|"
               r"\.ssh/.*key|secret|token)", re.I),
    re.compile(r"\bscp\b|\brsync\s+.*@", re.I),  # blocked → exfil potential
)

# HIGH — explicit confirm required
_HIGH_PATTERNS = (
    re.compile(r"\bapt\s+install\b", re.I),
    re.compile(r"\bapt\s+remove\b", re.I),
    re.compile(r"\bapt\s+purge\b", re.I),
    re.compile(r"\byum\s+(install|remove)\b", re.I),
    re.compile(r"\bdpkg\s+-i\b"),
    re.compile(r"\bnpm\s+install\b", re.I),
    re.compile(r"\bpip\s+install\b", re.I),
    re.compile(r"\breboot\b", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\buseradd\b|\busermod\b|\buserdel\b", re.I),
    re.compile(r"\bpasswd\b", re.I),
    re.compile(r"\bvisudo\b|/etc/sudoers", re.I),
    re.compile(r"\b(ufw|iptables|firewalld)\s+", re.I),
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\brm\s+-f\b", re.I),
    re.compile(r"\bchmod\s+-?R?\s*0?[67]77\b", re.I),
    re.compile(r"\bchown\b\s+\S+\s*:\S*\s*/", re.I),
    re.compile(r">\s*/etc/", re.I),
    re.compile(r"\bdrop\s+(table|database)\b", re.I),
    re.compile(r"\btruncate\s+table\b", re.I),
    re.compile(r"\bdelete\s+from\b", re.I),
    re.compile(r"\bsystemctl\s+(disable|mask)\b", re.I),
)

# MEDIUM — session grant or confirm acceptable
_MEDIUM_PATTERNS = (
    re.compile(r"\bsystemctl\s+restart\b", re.I),
    re.compile(r"\bsystemctl\s+(start|stop)\b", re.I),
    re.compile(r"\bdocker\s+(restart|start|stop)\b", re.I),
    re.compile(r"\bgit\s+(pull|fetch|reset)\b", re.I),
    re.compile(r"\bmkdir\b", re.I),
    re.compile(r"\bapt\s+update\b", re.I),
    re.compile(r"\bapt-get\s+update\b", re.I),
    re.compile(r"\btail\s+-f\b", re.I),
)

# LOW — read-only / harmless
_LOW_PATTERNS = (
    re.compile(r"^\s*(uptime|whoami|hostname|pwd|date|id)\s*$", re.I),
    re.compile(r"^\s*df\s+(-h|-i|-T|-Th)?\s*$", re.I),
    re.compile(r"^\s*free\s+(-m|-h)?\s*$", re.I),
    re.compile(r"^\s*ls\b", re.I),
    re.compile(r"^\s*cat\s+/proc/(loadavg|cpuinfo|meminfo)", re.I),
    re.compile(r"^\s*ps\s+(aux|-ef|-A)\s*$", re.I),
    re.compile(r"^\s*systemctl\s+(status|is-active)\b", re.I),
    re.compile(r"^\s*journalctl\s+", re.I),
    re.compile(r"^\s*docker\s+(ps|images|version)\s*$", re.I),
    re.compile(r"^\s*git\s+(status|log|diff|branch|remote\s+-v)\s*$", re.I),
    re.compile(r"^\s*echo\s+", re.I),
)


def classify_ssh_command(cmd: str) -> tuple[str, str]:
    """Return (risk, reason). risk ∈ {blocked, high, medium, low}.

    Rule precedence: blocked > high > medium > low. If nothing matches,
    treat as 'high' by default — unknown commands need explicit approval.
    """
    if not cmd or not cmd.strip():
        return "blocked", "empty command"
    if len(cmd) > SSH_MAX_CMD_LEN:
        return "blocked", f"command too long ({len(cmd)} > {SSH_MAX_CMD_LEN})"

    for p in _BLOCKED_PATTERNS:
        if p.search(cmd):
            return "blocked", f"matches BLOCKED pattern: {p.pattern[:60]}"
    for p in _HIGH_PATTERNS:
        if p.search(cmd):
            return "high", f"matches HIGH pattern: {p.pattern[:60]}"
    for p in _MEDIUM_PATTERNS:
        if p.search(cmd):
            return "medium", f"matches MEDIUM pattern: {p.pattern[:60]}"
    for p in _LOW_PATTERNS:
        if p.search(cmd):
            return "low", f"matches LOW pattern: {p.pattern[:60]}"
    return "high", "unknown command — defaulting to HIGH (admin must confirm)"


# ── Output redaction ─────────────────────────────────────────────────────────

_SECRET_LINE_PATTERNS = (
    re.compile(r"(?i)(GITHUB_TOKEN|TELEGRAM_BOT_TOKEN|api[_-]?key|"
               r"authorization|bearer|cookie|set-cookie|password)"),
    re.compile(r"ghp_[A-Za-z0-9]{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"x-access-token:[^@]+@"),
)


def redact(text: str) -> str:
    """Line-level scrub for SSH output. Returns the safe string."""
    if not text:
        return ""
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if any(p.search(line) for p in _SECRET_LINE_PATTERNS):
            out_lines.append("[REDACTED — secret-like content]\n")
        else:
            out_lines.append(line)
    out = "".join(out_lines)
    if len(out) > SSH_MAX_OUTPUT_BYTES:
        out = out[:SSH_MAX_OUTPUT_BYTES] + "\n[truncated]"
    return out


# ── Worker registry ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    if not WORKERS_FILE.exists():
        return {"workers": []}
    try:
        return json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"workers": []}


def _save(d: dict) -> None:
    WORKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKERS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def list_workers() -> list[dict]:
    return list(_load().get("workers", []))


def get_worker(worker_id: str) -> Optional[dict]:
    for w in list_workers():
        if w.get("id") == worker_id:
            return w
    return None


def _validate_key_path(path: str) -> tuple[bool, str]:
    """Reject keys outside SAFE_KEY_ROOTS. Reject world-readable keys."""
    try:
        abs_p = os.path.realpath(path)
    except Exception as e:
        return False, f"path error: {e}"
    if not any(abs_p.startswith(root) for root in SAFE_KEY_ROOTS):
        return False, ("key_path must live under "
                       + " | ".join(SAFE_KEY_ROOTS))
    if not os.path.isfile(abs_p):
        return False, "key_path does not exist"
    try:
        mode = os.stat(abs_p).st_mode & 0o777
        if mode & 0o077:
            return False, (f"key file mode {oct(mode)} too permissive — "
                           f"chmod 600 first")
    except Exception:
        pass
    return True, ""


def add_worker(*, worker_id: str, host: str,
                username: str, key_path: str,
                port: int = 22, tags: Optional[list[str]] = None,
                notes: str = "") -> tuple[bool, str]:
    """Register a new worker. Returns (ok, reason)."""
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", worker_id):
        return False, "worker_id must be alphanumeric / _ / - (≤32 chars)"
    if get_worker(worker_id):
        return False, f"worker {worker_id!r} already exists"
    # Lightweight host sanity — no spaces, length cap
    if not host or len(host) > 253 or " " in host:
        return False, "invalid host"
    if not re.match(r"^[a-zA-Z0-9_.-]{1,64}$", username or ""):
        return False, "invalid username"
    if not (1 <= int(port) <= 65535):
        return False, "invalid port"
    ok, why = _validate_key_path(key_path)
    if not ok:
        return False, why

    d = _load()
    d.setdefault("workers", []).append({
        "id":         worker_id,
        "host":       host,
        "port":       int(port),
        "username":   username,
        "auth_type":  "key",
        "key_path":   os.path.realpath(key_path),
        "tags":       list(tags or []),
        "enabled":    True,
        "created_at": _now_iso(),
        "notes":      notes[:300],
    })
    _save(d)
    return True, "ok"


def remove_worker(worker_id: str) -> bool:
    d = _load()
    before = len(d.get("workers", []))
    d["workers"] = [w for w in d.get("workers", []) if w.get("id") != worker_id]
    _save(d)
    return len(d["workers"]) < before


def disable_worker(worker_id: str) -> bool:
    d = _load()
    for w in d.get("workers", []):
        if w.get("id") == worker_id:
            w["enabled"] = False
            _save(d)
            return True
    return False


# ── SSH executor (key-only) ───────────────────────────────────────────────────

def _ssh_argv(worker: dict, cmd: str) -> list[str]:
    """Build a strict ssh argv. Key-only, no password fallback, batch
    mode (no interactive prompt), strict timeout."""
    return [
        "ssh",
        "-i", worker["key_path"],
        "-p", str(worker.get("port", 22)),
        "-o", "BatchMode=yes",                # never prompt for password
        "-o", "PasswordAuthentication=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/opt/tiktok-bot/keys/known_hosts",
        "-o", f"ConnectTimeout={SSH_DEFAULT_TIMEOUT_SEC}",
        f"{worker['username']}@{worker['host']}",
        "--",
        cmd,
    ]


def ssh_exec(worker_id: str, cmd: str, *,
              timeout: int = SSH_DEFAULT_TIMEOUT_SEC,
              user: str = "tg_admin",
              risk_override: Optional[str] = None) -> dict:
    """Run `cmd` on the named worker over SSH.

    risk_override: when provided, bypasses the default classifier
        (used by the inline-confirm callback after admin approves).
        Even with override, BLOCKED commands stay blocked.

    Returns:
        {"status": "ok" | "no_worker" | "disabled" | "blocked" |
                    "needs_confirm" | "error" | "timeout",
         "rc":      int | None,
         "risk":    str,
         "reason":  str,
         "stdout":  str (redacted),
         "stderr":  str (redacted),
         "duration_sec": float}
    """
    out: dict = {"status": "ok", "rc": None,
                 "risk": "low", "reason": "", "stdout": "", "stderr": ""}

    worker = get_worker(worker_id)
    if not worker:
        out["status"] = "no_worker"
        out["reason"] = f"worker {worker_id!r} not registered"
        return out
    if not worker.get("enabled", True):
        out["status"] = "disabled"
        out["reason"] = f"worker {worker_id!r} is disabled"
        return out

    risk, reason = classify_ssh_command(cmd)
    out["risk"]   = risk
    out["reason"] = reason

    if risk == "blocked":
        out["status"] = "blocked"
        return out

    # Risk gating happens at the caller (Telegram) — we only refuse
    # the actual exec when caller didn't explicitly approve via override.
    if risk in ("high", "medium") and risk_override is None:
        out["status"] = "needs_confirm"
        return out

    # Audit BEFORE running
    try:
        from bot.agent.audit_log import log_action
        log_action(user=user, action="ssh_exec",
                   risk_level={"low": "low", "medium": "medium",
                                "high": "high"}[risk],
                   status="started",
                   result_summary=(f"{worker_id}: {cmd[:120]}"))
    except Exception:
        pass

    argv = _ssh_argv(worker, cmd)
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        out["status"]       = "timeout"
        out["duration_sec"] = float(timeout)
        out["reason"]       = f"ssh timeout after {timeout}s"
        return out
    except FileNotFoundError as e:
        out["status"] = "error"
        out["reason"] = f"ssh binary missing: {e}"
        return out
    except Exception as e:
        out["status"] = "error"
        out["reason"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        out["duration_sec"] = round(elapsed, 2)

    out["rc"]     = proc.returncode
    out["stdout"] = redact(proc.stdout or "")
    out["stderr"] = redact(proc.stderr or "")
    out["status"] = "ok" if proc.returncode == 0 else "error"

    try:
        from bot.agent.audit_log import log_action
        log_action(user=user, action="ssh_exec",
                   risk_level={"low": "low", "medium": "medium",
                                "high": "high"}[risk],
                   status="done" if out["status"] == "ok" else "failed",
                   result_summary=(f"{worker_id}: rc={proc.returncode} "
                                    f"cmd={cmd[:80]}"))
    except Exception:
        pass
    return out


# ── Telegram-friendly formatters ──────────────────────────────────────────────

def format_workers_list_vi() -> str:
    items = list_workers()
    if not items:
        return ("<b>🌐 Remote workers</b>\n"
                "<i>(chưa có worker nào — dùng "
                "<code>/worker_add &lt;id&gt; &lt;host&gt; &lt;user&gt; "
                "&lt;key_path&gt;</code> để thêm)</i>")
    icon = {"True": "✅", "False": "🚫"}
    lines = ["<b>🌐 Remote workers</b>"]
    for w in items:
        on = icon.get(str(bool(w.get("enabled", True))), "•")
        tags = " ".join(f"#{t}" for t in (w.get("tags") or [])[:3])
        lines.append(f"{on} <code>{w['id']}</code>  "
                     f"{w['username']}@{w['host']}:{w.get('port',22)}  {tags}")
    return "\n".join(lines)


def format_worker_info_vi(worker_id: str) -> str:
    w = get_worker(worker_id)
    if not w:
        return f"❌ Worker <code>{worker_id}</code> chưa đăng ký."
    return ("\n".join([
        f"<b>🌐 Worker {w['id']}</b>",
        f"Host: <code>{w['host']}:{w.get('port',22)}</code>",
        f"User: <code>{w['username']}</code>",
        f"Auth: {w.get('auth_type','key')}",
        f"Key: <code>{w.get('key_path','?')}</code>",
        f"Enabled: <b>{'yes' if w.get('enabled', True) else 'no'}</b>",
        f"Tags: {', '.join(w.get('tags') or []) or '—'}",
        f"Created: {w.get('created_at','?')}",
        f"Notes: {w.get('notes','') or '—'}",
    ]))


def format_ssh_result_vi(worker_id: str, cmd: str, r: dict) -> str:
    icon = {"ok": "✅", "no_worker": "❓", "disabled": "🚫",
            "blocked": "🛑", "needs_confirm": "⏸",
            "error": "❌", "timeout": "⏱"}.get(r.get("status",""), "•")
    parts = [f"{icon} <b>ssh {worker_id}</b>"]
    parts.append(f"cmd: <code>{cmd[:120].replace('<','&lt;').replace('>','&gt;')}</code>")
    parts.append(f"risk: <b>{r.get('risk','?')}</b> · "
                 f"status: <b>{r.get('status','?')}</b>")
    if r.get("reason"):
        parts.append(f"<i>{r['reason'][:160]}</i>")
    if r.get("rc") is not None:
        parts.append(f"rc={r['rc']} · {r.get('duration_sec','?')}s")
    if r.get("stdout"):
        parts.append("<b>stdout:</b>")
        parts.append("<pre>" + r["stdout"][:1500]
                     .replace("<", "&lt;").replace(">", "&gt;") + "</pre>")
    if r.get("stderr"):
        parts.append("<b>stderr:</b>")
        parts.append("<pre>" + r["stderr"][:600]
                     .replace("<", "&lt;").replace(">", "&gt;") + "</pre>")
    return "\n".join(parts)
