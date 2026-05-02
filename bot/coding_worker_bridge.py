"""
Coding Worker Bridge — Telegram-triggered, non-interactive execution of
the queued code_tasks via a local Claude/Codex CLI session.

Design goals:
  - Honest detection: if `claude` / `codex` is missing or interactive-only,
    say so clearly; do NOT silently fail.
  - No surprise: the bridge captures stdout/stderr to a log file, runs
    smoke + evals after the worker exits, commits + pushes ONLY on green
    tests, and never commits forbidden paths.
  - No secrets in logs: env vars carrying `TOKEN`/`KEY`/`AUTH`/`COOKIE`
    are scrubbed from the captured output.
  - Risk gating: high-risk tasks become pending_action; the bridge will
    not start them. ALWAYS_CONFIRM_HINTS from sessions.py override every
    grant.

Public API:
  detect_coding_tools()            -> list[ToolInfo]
  get_preferred_coding_tool()      -> ToolInfo | None
  coding_tool_status()             -> str (HTML for Telegram)
  can_run_noninteractive(tool)     -> tuple[bool, str]
  build_worker_command(tool, prompt_path, task_id) -> list[str]
  run_once(...)                    -> dict (result summary)
  run_batch(n, ...)                -> list[dict]
  is_paused()                       / pause() / resume()
  setup_instructions()             -> str (HTML)
  recent_logs(n=5)                 -> list[Path]
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO   = Path("/opt/tiktok-bot")
LOGDIR = REPO / "data" / "code_worker_logs"
PAUSE_FILE = REPO / "data" / "coding_bridge.paused"

DEFAULT_TIMEOUT_SEC = 45 * 60          # 45 min
MAX_OUTPUT_BYTES    = 4 * 1024 * 1024  # 4 MB log cap

# When set, the claude subprocess is launched via `sudo -u <user> -H` so
# that it picks up that user's ~/.claude/ auth state instead of root's.
# Service still runs as root; only the claude exec drops privileges.
CODING_WORKER_USER = (os.environ.get("CODING_WORKER_USER") or "").strip()
# Absolute path to the claude binary (usually a per-user install). When
# unset we fall back to shutil.which("claude").
CLAUDE_CLI_PATH    = (os.environ.get("CLAUDE_CLI_PATH")    or "").strip()
# Model alias or full id passed to `claude --model`. Aliases ('opus',
# 'sonnet') resolve to the latest of that family inside the CLI; full
# ids ('claude-opus-4-7') pin to a specific version. Default 'opus'
# because we want the strongest available coding model.
_CLAUDE_MODEL_ENV   = (os.environ.get("CLAUDE_CODE_MODEL") or "").strip()
CLAUDE_CODE_MODEL   = _CLAUDE_MODEL_ENV or "opus"
CLAUDE_CODE_MODEL_SOURCE = "env" if _CLAUDE_MODEL_ENV else "default"
# Hardcoded fallback handed to `claude --fallback-model` so the CLI auto-
# switches when the primary is overloaded. Sonnet 4.6 is the
# next-best coding model below Opus 4.7.
CLAUDE_FALLBACK_MODEL = "sonnet"

# Forbidden-name regex applied to staged files before commit. Matches the
# kill-list in bot/agent/sessions.py + obvious credential patterns.
FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"storage_state"),
    re.compile(r"(^|/)data/.*\.db$"),
    re.compile(r"(^|/)data/.*\.jsonl$"),
    re.compile(r"(^|/)data/code_prompts(/|$)"),
    re.compile(r"(^|/)data/code_worker_logs(/|$)"),
    re.compile(r"(^|/)data/telegram/(?!\.gitkeep)"),
    re.compile(r"\.tar\.gz$"),
    re.compile(r"\.log$"),
    re.compile(r"id_rsa"),
    re.compile(r"\.pem$"),
    re.compile(r"\bcookie\b", re.I),
)

# Stripped from captured output (regex on each LINE).
SECRET_LINE_PATTERNS = (
    re.compile(r"(?i)(GITHUB_TOKEN|TELEGRAM_BOT_TOKEN|api[_-]?key|"
               r"authorization|bearer|cookie|set-cookie)"),
    re.compile(r"ghp_[A-Za-z0-9]{6,}"),     # github personal token
    re.compile(r"sk-[A-Za-z0-9]{20,}"),     # openai-style
    re.compile(r"x-access-token:[^@]+@"),   # inline-token push URL
)


@dataclass(frozen=True)
class ToolInfo:
    name:       str           # 'claude' | 'codex'
    binary:     str           # absolute path
    version:    str           # output of --version (truncated)
    noninteractive_ok: bool
    notes:      str = ""

    @property
    def display(self) -> str:
        nt = "✅ non-interactive" if self.noninteractive_ok else "⚠ may need TTY"
        return f"{self.name} ({self.version[:40]}) — {nt}"


# ── Tool detection ────────────────────────────────────────────────────────────

def _which(name: str) -> str | None:
    if name == "claude" and CLAUDE_CLI_PATH:
        # Honour explicit override even if it lives outside PATH (e.g.
        # ~/.local/bin which root's PATH normally omits).
        if Path(CLAUDE_CLI_PATH).exists():
            return CLAUDE_CLI_PATH
    return shutil.which(name)


def _run_as_prefix(tool_name: str) -> list[str]:
    """argv prefix to drop privileges to CODING_WORKER_USER for the
    claude CLI, including an `env` wrapper so we can still pass
    ANTHROPIC_MODEL through (sudo otherwise strips it). Returns []
    when no user is configured or the tool is not claude.
    """
    if tool_name != "claude" or not CODING_WORKER_USER:
        return []
    return [
        "sudo", "-n", "-u", CODING_WORKER_USER, "-H",
        "env", f"ANTHROPIC_MODEL={CLAUDE_CODE_MODEL}",
    ]


def _claude_model_args() -> list[str]:
    """Flags appended after the claude binary so we always pin the
    selected model and have an automatic fallback when it's overloaded."""
    return [
        "--model", CLAUDE_CODE_MODEL,
        "--fallback-model", CLAUDE_FALLBACK_MODEL,
    ]


def effective_run_user(tool_name: str) -> str:
    """The OS user the given tool will execute as."""
    if tool_name == "claude" and CODING_WORKER_USER:
        return CODING_WORKER_USER
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "?")


def _try_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=8)
        return (out.stdout or out.stderr or "").strip().splitlines()[0][:80]
    except Exception:
        return ""


def _try_help_for_print_flag(binary: str) -> tuple[bool, str]:
    """Return (supports_noninteractive, note).

    Both `claude --print` and `codex --no-interactive` (or similar) signal
    a usable non-interactive mode. We stay conservative: if --help fails
    or the help text doesn't mention a non-interactive flag, we report
    interactive-only.
    """
    try:
        out = subprocess.run([binary, "--help"], capture_output=True,
                             text=True, timeout=8)
        text = ((out.stdout or "") + "\n" + (out.stderr or "")).lower()
    except Exception as e:
        return False, f"--help failed: {e}"

    # Anthropic Claude CLI (claude-code): supports --print / -p for non-interactive.
    if "--print" in text or "non-interactive" in text or "-p," in text:
        return True, "supports --print (non-interactive)"
    # OpenAI Codex CLI: uses `codex exec` for non-interactive.
    if "codex exec" in text or "non-interactive" in text:
        return True, "supports `codex exec` (non-interactive)"
    return False, "no non-interactive flag detected in --help"


def detect_coding_tools() -> list[ToolInfo]:
    """Return every coding CLI we know about that is on PATH."""
    out: list[ToolInfo] = []
    for name in ("claude", "codex"):
        path = _which(name)
        if not path:
            continue
        ver = _try_version(path)
        nonint, note = _try_help_for_print_flag(path)
        out.append(ToolInfo(name=name, binary=path, version=ver,
                            noninteractive_ok=nonint, notes=note))
    return out


def get_preferred_coding_tool() -> Optional[ToolInfo]:
    """Prefer claude over codex. Prefer non-interactive variants."""
    tools = detect_coding_tools()
    if not tools:
        return None
    nonint = [t for t in tools if t.noninteractive_ok]
    pool = nonint if nonint else tools
    pool.sort(key=lambda t: 0 if t.name == "claude" else 1)
    return pool[0]


_AUTH_TEST_CACHE: dict[str, tuple[float, dict]] = {}
_AUTH_TEST_TTL_SEC = 300.0  # 5 min — model probe is billable


def quick_auth_test(tool: ToolInfo, *, timeout: float = 30.0) -> dict:
    """Drive a tiny non-interactive completion to confirm the CLI is
    authenticated AND record which model actually answered.

    Returns a dict::
        {"auth": "ok"|"fail: <reason>",
         "actual_model": "claude-opus-4-7"|"" ,
         "fallback_used": bool}

    The prompt is fixed ("ping") so it never carries user content. We
    request --output-format json so the response includes a modelUsage
    map keyed by the actual model id, which is the only honest way to
    detect that --fallback-model fired.

    Cached per (binary, run_as, selected_model) for _AUTH_TEST_TTL_SEC.
    """
    blank = {"auth": "skip: tool not probable",
             "actual_model": "", "fallback_used": False}
    if not tool or not tool.noninteractive_ok:
        return {**blank, "auth": "fail: tool not non-interactive"}
    cache_key = (f"{tool.binary}|{effective_run_user(tool.name)}|"
                 f"{CLAUDE_CODE_MODEL}")
    now = time.time()
    cached = _AUTH_TEST_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _AUTH_TEST_TTL_SEC:
        return cached[1]
    if tool.name != "claude":
        # Only claude has a stable cheap probe; codex would need an
        # actual `codex exec` round-trip. Skip for now.
        result = {**blank, "auth": "skip: only claude probed"}
        _AUTH_TEST_CACHE[cache_key] = (now, result)
        return result

    cmd = (_run_as_prefix("claude")
           + [tool.binary, *_claude_model_args(),
              "--output-format", "json", "--print"])
    try:
        proc = subprocess.run(
            cmd, input="ping\n", capture_output=True, text=True,
            timeout=timeout, cwd=str(REPO), env=_scrub_env(),
        )
    except subprocess.TimeoutExpired:
        result = {**blank, "auth": f"fail: timeout after {timeout:.0f}s"}
        _AUTH_TEST_CACHE[cache_key] = (now, result)
        return result
    except FileNotFoundError as e:
        result = {**blank, "auth": f"fail: {e}"}
        _AUTH_TEST_CACHE[cache_key] = (now, result)
        return result

    out: dict = {"auth": "ok", "actual_model": "", "fallback_used": False}
    combined_lower = (proc.stdout + proc.stderr).lower()
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        head = err[0][:120] if err else f"rc={proc.returncode}"
        out["auth"] = f"fail: {head}"
    elif "not logged in" in combined_lower:
        out["auth"] = "fail: not logged in"
    else:
        # Parse JSON to discover the actual model. Fall back to "ok"
        # without model info if parsing fails — the CLI succeeded.
        try:
            import json as _json
            parsed = _json.loads(proc.stdout.strip().splitlines()[-1])
            mu = parsed.get("modelUsage") or {}
            if mu:
                actual = sorted(mu.keys())[0]
                out["actual_model"]  = actual
                out["fallback_used"] = (
                    CLAUDE_CODE_MODEL.lower() in ("opus",)
                    and not actual.startswith("claude-opus")
                )
        except Exception:
            pass

    _AUTH_TEST_CACHE[cache_key] = (now, out)
    return out


def coding_tool_status() -> str:
    tools = detect_coding_tools()
    lines = ["<b>🛠 Coding tool detection</b>"]
    if not tools:
        lines.append("❌ No supported coding CLI found on PATH "
                     "(checked: claude, codex).")
        lines.append("")
        lines.append(setup_instructions())
        return "\n".join(lines)
    pref = get_preferred_coding_tool()
    for t in tools:
        mark = "▶" if pref and pref.binary == t.binary else "•"
        lines.append(f"{mark} {t.display}")
        if t.notes:
            lines.append(f"   <i>{t.notes}</i>")
    if pref:
        run_as = effective_run_user(pref.name)
        nonint = "ok" if pref.noninteractive_ok else "fail"
        if pref.name == "claude":
            probe = quick_auth_test(pref)
        else:
            probe = {"auth": "skip", "actual_model": "",
                     "fallback_used": False}
        lines.append("")
        lines.append("<b>Effective config</b>")
        lines.append(f"  tool=<code>{pref.name}</code>")
        lines.append(f"  path=<code>{pref.binary}</code>")
        lines.append(f"  run_as=<code>{run_as}</code>")
        lines.append(f"  selected_model=<code>{CLAUDE_CODE_MODEL}</code>")
        lines.append(f"  model_source=<code>{CLAUDE_CODE_MODEL_SOURCE}</code>")
        if probe.get("actual_model"):
            lines.append(
                f"  actual_model=<code>{probe['actual_model']}</code>"
            )
        if probe.get("fallback_used"):
            lines.append(
                f"  ⚠ <i>Opus unavailable, using "
                f"{CLAUDE_FALLBACK_MODEL} fallback</i>"
            )
        lines.append(f"  fallback_model=<code>{CLAUDE_FALLBACK_MODEL}</code>")
        lines.append(f"  auth_test=<code>{probe['auth']}</code>")
        lines.append(f"  non_interactive=<code>{nonint}</code>")
        snap = dirty_tree_snapshot()
        lines.append(f"  dirty_tree=<code>{'true' if snap['dirty'] else 'false'}</code>")
        lines.append(f"  dirty_files=<code>{len(snap['files'])}</code>")
        if snap["dirty"]:
            sample = ", ".join(snap["files"][:3])
            more   = max(0, len(snap["files"]) - 3)
            extra  = f" (+{more} more)" if more else ""
            lines.append(f"  ⚠ <i>worker will refuse run_once unless "
                         f"allow_dirty=True. Sample: {sample}{extra}</i>")
    return "\n".join(lines)


def can_run_noninteractive(tool: ToolInfo) -> tuple[bool, str]:
    if not tool:
        return False, "no tool"
    if tool.noninteractive_ok:
        return True, "ok"
    return False, ("interactive-only — open a terminal and run the worker "
                   "manually with the saved prompt")


def build_worker_command(tool: ToolInfo, prompt_path: Path, task_id: str
                          ) -> list[str]:
    """Return argv for the chosen tool to run the prompt non-interactively.

    For the Anthropic Claude CLI:
        claude --print < prompt.md
    For OpenAI Codex:
        codex exec --workdir /opt/tiktok-bot < prompt.md
    Caller is responsible for piping the file via stdin (we do that in
    run_once() so the binary stays vendored to PATH only).
    """
    if tool.name == "claude":
        # `claude --print` reads instructions from stdin and prints
        # the assistant's reply; combined with the prompt template it
        # is enough to drive a code_task end-to-end IF the user has
        # already authenticated the CLI (claude login). --model pins
        # the family (default 'opus'); --fallback-model lets the CLI
        # auto-switch to sonnet if Opus is overloaded.
        #
        # --permission-mode bypassPermissions: required for autonomous
        # coding via --print. Without it, the CLI's permission system
        # blocks Edit / Bash / Write tools and the worker exits with
        # "edit was denied" → no_changes. The bridge has its own safety
        # gates AFTER the run (forbidden-path filter, smoke + evals,
        # dirty-tree gate, commit only on green) so this is acceptable.
        # Owner Authority Policy explicitly allows the agent to edit
        # repo files. Aligns with /opt/tiktok-bot/docs/OPERATING_RULES
        # §10 (owner tooling doctrine — never refuse generically).
        # --add-dir pins the working directory for tool sandbox
        # discovery. We do NOT use --dangerously-skip-permissions
        # since the bridge runs with internet access (9Router, github,
        # etc); bypassPermissions still applies the CLI's intrinsic
        # safety nets while skipping per-edit prompts.
        return (_run_as_prefix("claude")
                + [tool.binary, *_claude_model_args(),
                   "--permission-mode", "bypassPermissions",
                   "--add-dir", str(REPO),
                   "--print"])
    if tool.name == "codex":
        return [tool.binary, "exec", "--cd", str(REPO)]
    # Unknown tool — refuse to invent flags
    return [tool.binary]


def setup_instructions() -> str:
    return (
        "<b>Manual setup required</b>\n\n"
        "To enable Telegram-driven autonomous coding, install one of:\n\n"
        "<b>1) Anthropic Claude CLI</b> (preferred):\n"
        "<pre>npm install -g @anthropic-ai/claude-code\n"
        "claude login   # interactive once</pre>\n"
        "Verify: <code>claude --version</code> and "
        "<code>echo 'ping' | claude --print</code>.\n\n"
        "<b>2) OpenAI Codex CLI</b> (fallback):\n"
        "<pre>npm install -g @openai/codex\n"
        "codex login   # interactive once</pre>\n"
        "Verify: <code>codex exec --cd /opt/tiktok-bot 'list files'</code>.\n\n"
        "<i>The bridge never stores or logs CLI auth tokens — "
        "authentication is a one-time interactive step done by the admin.</i>"
    )


# ── Pause / resume / state ────────────────────────────────────────────────────

def is_paused() -> bool:
    return PAUSE_FILE.exists()


def pause() -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(_now_iso())


def resume() -> None:
    try:
        PAUSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recent_logs(n: int = 5) -> list[Path]:
    if not LOGDIR.exists():
        return []
    files = sorted(LOGDIR.glob("*.log"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return files[:n]


# ── Output scrubbing ──────────────────────────────────────────────────────────

def _scrub_line(line: str) -> str:
    for pat in SECRET_LINE_PATTERNS:
        if pat.search(line):
            return "[REDACTED line — looked like a secret]\n"
    return line


def _scrub_env() -> dict[str, str]:
    """Build a sanitised env for the worker subprocess.

    We KEEP what the CLI needs to find its config (HOME, PATH, USER,
    LANG, TERM=dumb, ANTHROPIC_API_KEY/etc IF they were already in env)
    but we DO NOT add token-bearing headers ourselves.
    """
    keep = {"HOME", "PATH", "USER", "LANG", "LC_ALL", "TERM",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
            "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_MODEL", "ANTHROPIC_MODEL"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    # Force a non-interactive terminal so the CLI doesn't try to draw
    # a TUI and block on a missing TTY.
    env["TERM"] = "dumb"
    return env


# ── Dirty-tree snapshot + delta ───────────────────────────────────────────────

def _working_tree_state() -> set[str]:
    """Return the set of file paths that differ from HEAD right now.

    Includes both tracked-modified (`git diff --name-only HEAD`) and
    untracked-but-not-gitignored files (`git ls-files -o --exclude-standard`).
    Pure-ignored noise like `data/` is intentionally excluded so that
    runtime state never trips the dirty-tree gate.
    """
    paths: set[str] = set()
    a = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", "HEAD"],
                       capture_output=True, text=True, timeout=10)
    for ln in a.stdout.splitlines():
        ln = ln.strip()
        if ln:
            paths.add(ln)
    b = subprocess.run(["git", "-C", str(REPO), "ls-files",
                        "-o", "--exclude-standard"],
                       capture_output=True, text=True, timeout=10)
    for ln in b.stdout.splitlines():
        ln = ln.strip()
        if ln:
            paths.add(ln)
    return paths


def dirty_tree_snapshot() -> dict:
    """Capture working-tree state for use as a pre-run baseline.

    Returns {"dirty": bool, "files": list[str], "head": str}.
    """
    files = sorted(_working_tree_state())
    head  = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5
                           ).stdout.strip()
    return {"dirty": bool(files), "files": files, "head": head}


def _files_changed_since(snapshot: dict) -> list[str]:
    """Files that differ from HEAD now but did NOT differ in `snapshot`.
    These are exactly the paths the worker is responsible for."""
    before = set(snapshot.get("files") or [])
    after  = _working_tree_state()
    return sorted(after - before)


# ── Forbidden-path enforcement before commit ──────────────────────────────────

def _staged_paths() -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), "diff", "--cached",
                          "--name-only"], capture_output=True, text=True,
                         timeout=10)
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _filter_forbidden_staged() -> list[str]:
    """Unstage anything whose path matches FORBIDDEN_PATH_PATTERNS.
    Returns the list of paths that were UNSTAGED."""
    bad: list[str] = []
    for p in _staged_paths():
        if any(rx.search(p) for rx in FORBIDDEN_PATH_PATTERNS):
            bad.append(p)
    if bad:
        subprocess.run(["git", "-C", str(REPO), "reset", "HEAD", "--"] + bad,
                       capture_output=True, text=True, timeout=10)
    return bad


# ── Main: run_once ────────────────────────────────────────────────────────────

async def run_once(*, dry_run: bool = False, user: str = "tg_admin",
                   allow_dirty: bool = False) -> dict:
    """Pick next queued code_task, execute it via the preferred CLI, run
    tests, commit/push on green, mark task accordingly. Returns a summary
    dict suitable for `format_run_result()`.

    When the working tree already has uncommitted edits, run is refused
    with status=dirty_tree unless `allow_dirty=True`. The task remains
    queued so the admin can stash/commit and retry without losing it.
    Only files the worker actually changed are staged at commit time —
    pre-existing dirty files never get swept into the worker commit.
    """
    from bot.code_tasks import (next_queued_task, get_task,
                                 update_task as code_update,
                                 finish_task as code_finish,
                                 fail_task   as code_fail,
                                 is_paused   as code_is_paused)
    from bot.agent.permissions import create_pending
    from bot.agent.audit_log    import log_action

    out: dict = {
        "status": "noop", "task_id": "", "tool": "",
        "log_path": "", "summary": "",
    }

    if is_paused():
        out["status"], out["summary"] = "paused", "coding bridge is paused"
        return out
    if code_is_paused():
        out["status"], out["summary"] = "paused", "code_tasks worker is paused"
        return out

    nx = next_queued_task()
    if not nx:
        out["status"], out["summary"] = "noop", "no queued code_task"
        return out

    out["task_id"] = nx["id"]

    # ── 1. Risk gate ────────────────────────────────────────────────────
    if (nx.get("risk_level") or "low").lower() == "high":
        try:
            pid = create_pending(
                action="code_worker_run_once_high_risk",
                goal=nx.get("title", "")[:300],
                risk_level="high", user=user,
                metadata={"task_id": nx["id"]},
            )
        except Exception as e:
            out["status"] = "rejected"
            out["summary"] = f"could not create pending_action: {e}"
            return out
        log_action(user=user, action="code_worker_high_risk",
                   risk_level="high", status="pending",
                   result_summary=f"task={nx['id']} pid={pid}")
        out["status"]            = "pending_action"
        out["pending_action_id"] = pid
        out["summary"] = (f"high-risk task — pending_action {pid} created. "
                          f"Use /confirm_action {pid} to approve.")
        return out

    # ── 2. Tool detection ────────────────────────────────────────────────
    tool = get_preferred_coding_tool()
    if not tool:
        out["status"]  = "no_tool"
        out["summary"] = ("No coding CLI on PATH (claude/codex). "
                          "Manual worker session required.")
        return out
    out["tool"] = tool.name

    ok, why = can_run_noninteractive(tool)
    if not ok:
        out["status"]  = "interactive_only"
        out["summary"] = (f"{tool.name} is installed but interactive-only "
                          f"({why}). Open a terminal and run the worker "
                          f"manually with the saved prompt.")
        return out

    # ── 2.4 Quota gate (Claude only) ─────────────────────────────────────
    # Probe before running. If limited / auth_required / error, leave the
    # task queued and surface the schedule. The task is NOT marked failed.
    if tool.name == "claude":
        try:
            from bot import claude_quota as _cq
            if _cq.should_probe_now():
                # The probe spawns the Claude CLI synchronously (up to
                # 60s). Run it in a worker thread so the asyncio event
                # loop — and Telegram polling — stays responsive.
                await asyncio.to_thread(_cq.probe_claude_available, False)
            qstate = _cq.get_quota_state()
        except Exception as e:
            qstate = {"status": "unknown",
                      "last_error_summary": f"quota probe error: {e}"}
        st = qstate.get("status", "unknown")
        if st == "limited":
            nxt = qstate.get("reset_at") or qstate.get("next_probe_at") or "?"
            out["status"]  = "quota_limited"
            out["summary"] = (
                f"⏸ Claude bị giới hạn — sẽ thử lại lúc {nxt}. "
                f"Task <code>{nx['id']}</code> giữ nguyên trạng thái queued."
            )
            # Do NOT mark task failed — keep queued for retry.
            log_action(user=user, action="code_worker_quota_limited",
                       risk_level="low", status="ok",
                       result_summary=f"task={nx['id']} retry_at={nxt}")
            return out
        if st == "auth_required":
            out["status"]  = "auth_required"
            out["summary"] = (
                "🔒 Claude CLI yêu cầu đăng nhập lại. Chạy "
                "<code>claude login</code> trên VPS rồi thử lại. "
                f"Task <code>{nx['id']}</code> giữ queued."
            )
            log_action(user=user, action="code_worker_auth_required",
                       risk_level="low", status="ok",
                       result_summary=f"task={nx['id']}")
            return out
        # status="unknown"/"error" → proceed (probe may have failed for
        # transient reasons; let the actual run reveal the issue and
        # parse_claude_error will catch it post-hoc).

    # ── 2.5 Dirty-tree gate ──────────────────────────────────────────────
    # If the working tree is already dirty, refuse to run by default.
    # Otherwise the bridge would sweep unrelated in-flight edits into
    # the worker's commit (root cause of the bb48c37/55e9579 incidents).
    pre_snapshot = dirty_tree_snapshot()
    out["dirty_tree_before"] = pre_snapshot["dirty"]
    out["dirty_files_before"] = list(pre_snapshot["files"])
    if pre_snapshot["dirty"] and not allow_dirty:
        # Leave the task queued so admin can stash/commit and retry.
        # Don't mark it failed.
        sample = pre_snapshot["files"][:5]
        more   = max(0, len(pre_snapshot["files"]) - 5)
        out["status"]  = "dirty_tree"
        out["summary"] = (
            f"refused: working tree has {len(pre_snapshot['files'])} "
            f"uncommitted file(s). "
            f"Sample: {sample}{f' (+{more} more)' if more else ''}. "
            f"Commit/stash them or call run_once(allow_dirty=True)."
        )
        log_action(user=user, action="code_worker_dirty_tree_refused",
                   risk_level="low", status="ok",
                   result_summary=f"task={nx['id']} dirty={len(pre_snapshot['files'])}")
        return out

    # ── 3. Ensure prompt exists ──────────────────────────────────────────
    from bot.agent.prompt_builder import (build_coding_prompt,
                                            save_prompt_for_task,
                                            load_prompt_for_task)
    prompt = load_prompt_for_task(nx["id"])
    if not prompt:
        prompt = build_coding_prompt(nx)
        save_prompt_for_task(nx["id"], prompt)
    prompt_path = Path("/opt/tiktok-bot/data/code_prompts") / f"{nx['id']}.md"

    if dry_run:
        out["status"]  = "dry_run"
        out["summary"] = (f"would run: {' '.join(build_worker_command(tool, prompt_path, nx['id']))} "
                          f"< {prompt_path}")
        return out

    # ── 4. Mark running ──────────────────────────────────────────────────
    code_update(nx["id"], status="running")
    log_action(user=user, action="code_worker_run_once",
               risk_level="low", status="ok",
               result_summary=f"task={nx['id']} tool={tool.name}")

    # ── 5. Execute ───────────────────────────────────────────────────────
    LOGDIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOGDIR / f"{nx['id']}_{ts}.log"
    out["log_path"] = str(log_path)

    cmd = build_worker_command(tool, prompt_path, nx["id"])
    started = time.time()
    rc = -1
    try:
        with open(prompt_path, "rb") as stdin_fh, \
             open(log_path,    "wb") as log_fh:
            log_fh.write(f"# coding_worker_bridge log\n# task_id={nx['id']}\n"
                         f"# tool={tool.name} cmd={' '.join(cmd)}\n"
                         f"# started_at={_now_iso()}\n\n".encode())
            log_fh.flush()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_fh,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(REPO),
                env=_scrub_env(),
            )
            try:
                # Stream-scrub. Write up to MAX_OUTPUT_BYTES.
                bytes_written = 0
                while True:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(8192),
                        timeout=DEFAULT_TIMEOUT_SEC,
                    )
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    scrubbed = "".join(_scrub_line(ln + "\n") if "\n" not in ln
                                        else _scrub_line(ln)
                                        for ln in text.splitlines(keepends=True))
                    payload = scrubbed.encode("utf-8", errors="replace")
                    if bytes_written + len(payload) > MAX_OUTPUT_BYTES:
                        log_fh.write(
                            "\n[truncated -- exceeded MAX_OUTPUT_BYTES]\n"
                            .encode("utf-8"),
                        )
                        proc.kill()
                        break
                    log_fh.write(payload)
                    bytes_written += len(payload)
                rc = await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                log_fh.write(
                    "\n[killed -- exceeded DEFAULT_TIMEOUT_SEC]\n"
                    .encode("utf-8"),
                )
                rc = -9
    except FileNotFoundError as e:
        out["status"]  = "no_tool"
        out["summary"] = f"binary missing at runtime: {e}"
        code_fail(nx["id"], test_summary=out["summary"])
        return out
    except Exception as e:
        out["status"]  = "exec_error"
        out["summary"] = f"exec error: {e}"
        code_fail(nx["id"], test_summary=out["summary"])
        return out

    duration = int(time.time() - started)
    out["duration_sec"] = duration

    # ── 6. Post-run checks ───────────────────────────────────────────────
    if rc != 0:
        # Was it a quota / auth issue? Parse the captured log so the
        # task is paused (queued for retry) instead of marked failed.
        log_tail = ""
        try:
            log_tail = log_path.read_text(encoding="utf-8",
                                           errors="replace")[-4000:]
        except Exception:
            pass
        if tool.name == "claude" and log_tail:
            try:
                from bot import claude_quota as _cq
                parsed = _cq.parse_claude_error(log_tail)
            except Exception:
                parsed = {"kind": "ok"}
            if parsed["kind"] == "limited":
                _cq.mark_limited(reset_at=parsed["reset_at"],
                                 retry_after_seconds=parsed["retry_after_seconds"],
                                 note=parsed["summary"])
                # Re-queue the task — flip back to status='queued'.
                from bot.code_tasks import update_task as _ct_update
                _ct_update(nx["id"], status="queued")
                out["status"]  = "quota_limited"
                out["summary"] = (
                    f"⏸ Claude bị giới hạn giữa task. "
                    f"Đã re-queue <code>{nx['id']}</code>. "
                    f"Sẽ thử lại lúc "
                    f"{parsed.get('reset_at') or 'theo lịch backoff'}."
                )
                code_state = _cq.get_quota_state()
                out["claude_state"] = code_state.get("status")
                out["next_probe_at"] = code_state.get("next_probe_at")
                log_action(user=user,
                           action="code_worker_paused_quota_midrun",
                           risk_level="low", status="ok",
                           result_summary=f"task={nx['id']}")
                return out
            if parsed["kind"] == "auth_required":
                _cq.mark_auth_required(note=parsed["summary"])
                from bot.code_tasks import update_task as _ct_update
                _ct_update(nx["id"], status="queued")
                out["status"]  = "auth_required"
                out["summary"] = (
                    "🔒 Claude yêu cầu đăng nhập lại giữa task. "
                    f"Đã re-queue <code>{nx['id']}</code>. Chạy "
                    "<code>claude login</code> trên VPS."
                )
                return out
        out["status"]  = "worker_failed"
        out["summary"] = (f"{tool.name} exited rc={rc} after {duration}s. "
                          f"Log: {log_path.name}")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    # smoke + evals are sync subprocess calls (10-30s each). Owner asked
    # for "fix cho nó làm việc nhanh hơn, vì model 4.7 mạnh lắm" — speed
    # up the post-run gate by running smoke + evals CONCURRENTLY in
    # worker threads. Total wall time = max(smoke, evals) instead of
    # smoke + evals. Typical saving 8-15s per task.
    def _run_blocking_check(argv: list[str], timeout_sec: int = 120):
        return subprocess.run(argv, cwd=str(REPO), capture_output=True,
                               text=True, timeout=timeout_sec)
    smoke_task = asyncio.to_thread(
        _run_blocking_check, ["bash", "scripts/smoke_test.sh"], 120,
    )
    evals_task = asyncio.to_thread(
        _run_blocking_check,
        [str(REPO / "venv" / "bin" / "python3"), "-m", "bot.agent.evals"],
        120,
    )
    smoke, evals = await asyncio.gather(smoke_task, evals_task)

    smoke_pass = "SMOKE TEST PASSED" in smoke.stdout
    if not smoke_pass:
        out["status"]  = "smoke_failed"
        out["summary"] = (f"smoke test FAILED after worker run. "
                          f"Log: {log_path.name}")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    evals_ok = "passed" in evals.stdout and "0 eval(s) failed" not in evals.stdout
    # Stricter check: count "passed" line
    m = re.search(r"Agent Evals — (\d+)/(\d+) passed", evals.stdout)
    if m and m.group(1) == m.group(2):
        evals_ok = True
    if not evals_ok:
        out["status"]  = "evals_failed"
        out["summary"] = (f"evals FAILED. Log: {log_path.name}")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    # ── 7. Commit + push ─────────────────────────────────────────────────
    # Stage only the paths the worker itself changed (delta vs the
    # pre-run snapshot). When allow_dirty=True the snapshot may have
    # contained unrelated dirty files; those are still excluded so the
    # commit stays scoped to this task.
    worker_changed = _files_changed_since(pre_snapshot)
    out["worker_changed"] = list(worker_changed)
    if not worker_changed:
        out["status"]  = "no_changes"
        out["summary"] = (f"worker exited cleanly but produced NO file "
                          f"changes vs pre-run snapshot. "
                          f"Log: {log_path.name}")
        code_finish(nx["id"], commit_hash="",
                    test_summary="no-op worker run; smoke+evals OK")
        return out

    subprocess.run(["git", "-C", str(REPO), "add", "--"] + worker_changed,
                   capture_output=True, text=True, timeout=10)
    bad = _filter_forbidden_staged()
    if bad:
        out["status"] = "blocked_staged"
        out["summary"] = (f"unstaged forbidden paths: {bad[:5]}. "
                          f"Worker output staged secrets — refused commit.")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    msg = f"code_worker_run_once: {nx['id']} — {nx.get('title', '')[:60]}"
    commit = subprocess.run(["git", "-C", str(REPO), "commit", "-m", msg],
                            capture_output=True, text=True, timeout=15)
    if commit.returncode != 0:
        out["status"]  = "commit_failed"
        out["summary"] = (f"git commit failed: "
                          f"{(commit.stderr or commit.stdout)[:200]}")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    # Read token strictly from .env, never from log
    env_path = REPO / ".env"
    token = ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("GITHUB_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass
    if not token:
        out["status"]  = "no_push_token"
        out["summary"] = "GITHUB_TOKEN not in .env; commit kept locally."
        code_finish(nx["id"], commit_hash=_short_sha(),
                    test_summary="commit ok, no push (no token)")
        return out

    push_url = (f"https://x-access-token:{token}"
                f"@github.com/vieotp123/tiktok-agent-bot.git")
    # git push talks to the network — run in worker thread so the
    # asyncio event loop (Telegram polling) stays responsive.
    push = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(REPO), "push", push_url, "dev-agent"],
        capture_output=True, text=True, timeout=60,
    )
    # Always reset remote URL to the clean form, regardless of push outcome
    subprocess.run(["git", "-C", str(REPO), "remote", "set-url", "origin",
                    "https://github.com/vieotp123/tiktok-agent-bot.git"],
                   capture_output=True, text=True, timeout=5)

    if push.returncode != 0:
        out["status"]  = "push_failed"
        out["summary"] = f"push failed: {(push.stderr or push.stdout)[:200]}"
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    sha = _short_sha()

    # ── Auto-deploy: hot-reload services if bot/ source code was edited ──
    # Owner asked: "ủa hiện tại nó chỉ commit thôi chứ ko tự sửa
    # production à?" — yes the bot was running stale RAM code while
    # disk had latest. After every successful commit, if any path under
    # bot/ (or backend/) was committed, schedule a deferred restart of
    # the relevant systemd services. Use `nohup … &` so the restart
    # survives the bot process being killed.
    deploy_summary = ""
    needs_telegram_restart = any(
        f.startswith("bot/") for f in worker_changed
    )
    needs_backend_restart = any(
        f.startswith("backend/") for f in worker_changed
    )
    services: list[str] = []
    if needs_telegram_restart:
        services.append("tiktok-telegram")
    if needs_backend_restart:
        services.append("tiktok-backend")
    if services:
        try:
            # 6-second delay so this Telegram message + the autorun
            # cycle report can be sent BEFORE we kill the process.
            # Bot runs as root (systemd User=root) so no sudo needed.
            cmd = (f"sleep 6 && systemctl restart "
                   f"{' '.join(services)} >/dev/null 2>&1")
            subprocess.Popen(["nohup", "bash", "-c", cmd],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
            deploy_summary = (
                f"auto-deploy scheduled: restart {', '.join(services)} "
                f"in 6s. boot-resume will pick up next autorun cycle."
            )
        except Exception as e:
            deploy_summary = f"auto-deploy schedule failed: {e}"

    code_finish(nx["id"], commit_hash=sha,
                test_summary=f"smoke+evals ok; pushed {sha}",
                deploy_summary=deploy_summary)
    probe = quick_auth_test(tool) if tool.name == "claude" else {}
    actual_model = probe.get("actual_model", "")
    fallback     = probe.get("fallback_used", False)
    out["selected_model"] = CLAUDE_CODE_MODEL
    out["actual_model"]   = actual_model
    out["fallback_used"]  = fallback
    out["status"]  = "done"
    out["commit"]  = sha
    out["deploy_summary"] = deploy_summary
    model_part = (f" model={actual_model or CLAUDE_CODE_MODEL}"
                  + (" (fallback)" if fallback else ""))
    out["summary"] = (f"worker done. tool={tool.name}{model_part} "
                      f"duration={duration}s commit={sha} log={log_path.name}"
                      + (f" · {deploy_summary}" if deploy_summary else ""))
    log_action(user=user, action="code_worker_done", risk_level="medium",
               status="done",
               result_summary=f"task={nx['id']} commit={sha} "
                              f"deploy={'yes' if services else 'no'}")
    return out


def _short_sha() -> str:
    out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short",
                          "HEAD"], capture_output=True, text=True, timeout=5)
    return out.stdout.strip()


# ── Batch ─────────────────────────────────────────────────────────────────────

async def run_batch(n: int, *, user: str = "tg_admin") -> list[dict]:
    """Run up to n tasks. Stops early on failure / pending_action /
    no_tool / paused. Cap n at 3 to avoid surprise."""
    n = max(1, min(int(n), 3))
    results: list[dict] = []
    for i in range(n):
        r = await run_once(user=user)
        results.append(r)
        if r["status"] in ("done",):
            continue
        # any non-success → stop the batch so the admin can investigate
        break
    return results


# ── Telegram-facing summaries ─────────────────────────────────────────────────

def format_run_result(r: dict) -> str:
    icon = {"done": "✅", "pending_action": "⏸", "no_tool": "❌",
            "interactive_only": "⚠", "paused": "⏯",
            "rejected": "🚫", "worker_failed": "💥", "smoke_failed": "🚫",
            "evals_failed": "🚫", "no_changes": "💤", "noop": "💤",
            "exec_error": "💥", "commit_failed": "💥",
            "push_failed": "💥", "no_push_token": "🔒",
            "blocked_staged": "🚫", "dry_run": "🔍",
            "dirty_tree": "🧹"}.get(r.get("status",""), "•")
    parts = [f"{icon} <b>code_worker_run_once</b> — "
             f"<i>{r.get('status','?')}</i>"]
    if r.get("task_id"):
        parts.append(f"task: <code>{r['task_id']}</code>")
    if r.get("tool"):
        parts.append(f"tool: <code>{r['tool']}</code>")
    if r.get("actual_model") or r.get("selected_model"):
        m = r.get("actual_model") or r.get("selected_model")
        parts.append(f"model: <code>{m}</code>"
                     + (" ⚠ fallback" if r.get("fallback_used") else ""))
    if r.get("commit"):
        parts.append(f"commit: <code>{r['commit']}</code>")
    if r.get("pending_action_id"):
        parts.append(f"pending: <code>{r['pending_action_id']}</code> "
                     "<i>(/confirm_action to approve)</i>")
    if r.get("log_path"):
        parts.append(f"log: <code>{Path(r['log_path']).name}</code>")
    if r.get("duration_sec") is not None:
        parts.append(f"duration: {r['duration_sec']}s")
    if r.get("summary"):
        parts.append("")
        parts.append(r["summary"])
    return "\n".join(parts)
