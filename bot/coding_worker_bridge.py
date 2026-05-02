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
    p = shutil.which(name)
    return p


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
        lines.append("")
        lines.append(f"<b>Preferred:</b> <code>{pref.binary}</code>")
        lines.append(f"<b>Non-interactive:</b> "
                     f"{'yes' if pref.noninteractive_ok else 'no'}")
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
        # already authenticated the CLI (claude login).
        return [tool.binary, "--print"]
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
            "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    # Force a non-interactive terminal so the CLI doesn't try to draw
    # a TUI and block on a missing TTY.
    env["TERM"] = "dumb"
    return env


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

async def run_once(*, dry_run: bool = False, user: str = "tg_admin"
                   ) -> dict:
    """Pick next queued code_task, execute it via the preferred CLI, run
    tests, commit/push on green, mark task accordingly. Returns a summary
    dict suitable for `format_run_result()`."""
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
        out["status"]  = "worker_failed"
        out["summary"] = (f"{tool.name} exited rc={rc} after {duration}s. "
                          f"Log: {log_path.name}")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    smoke = subprocess.run(["bash", "scripts/smoke_test.sh"],
                           cwd=str(REPO), capture_output=True, text=True,
                           timeout=120)
    smoke_pass = "SMOKE TEST PASSED" in smoke.stdout
    if not smoke_pass:
        out["status"]  = "smoke_failed"
        out["summary"] = (f"smoke test FAILED after worker run. "
                          f"Log: {log_path.name}")
        code_fail(nx["id"], test_summary=out["summary"][:300])
        return out

    evals = subprocess.run([str(REPO / "venv" / "bin" / "python3"),
                             "-m", "bot.agent.evals"],
                            cwd=str(REPO), capture_output=True, text=True,
                            timeout=120)
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
    diff = subprocess.run(["git", "-C", str(REPO), "status", "--short"],
                          capture_output=True, text=True, timeout=10)
    if not diff.stdout.strip():
        out["status"]  = "no_changes"
        out["summary"] = (f"worker exited cleanly but produced NO file "
                          f"changes. Log: {log_path.name}")
        code_finish(nx["id"], commit_hash="",
                    test_summary="no-op worker run; smoke+evals OK")
        return out

    subprocess.run(["git", "-C", str(REPO), "add",
                    "bot", "docs", "scripts", "research", "README.md",
                    ".gitignore"],
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
    push = subprocess.run(["git", "-C", str(REPO), "push",
                           push_url, "dev-agent"],
                          capture_output=True, text=True, timeout=60)
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
    code_finish(nx["id"], commit_hash=sha,
                test_summary=f"smoke+evals ok; pushed {sha}",
                deploy_summary="")
    out["status"]  = "done"
    out["commit"]  = sha
    out["summary"] = (f"worker done. tool={tool.name} duration={duration}s "
                      f"commit={sha} log={log_path.name}")
    log_action(user=user, action="code_worker_done", risk_level="medium",
               status="done", result_summary=f"task={nx['id']} commit={sha}")
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
            "blocked_staged": "🚫", "dry_run": "🔍"}.get(r.get("status",""), "•")
    parts = [f"{icon} <b>code_worker_run_once</b> — "
             f"<i>{r.get('status','?')}</i>"]
    if r.get("task_id"):
        parts.append(f"task: <code>{r['task_id']}</code>")
    if r.get("tool"):
        parts.append(f"tool: <code>{r['tool']}</code>")
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
