"""
Prompt Builder — turn a coding-task description into a high-quality
Claude/Codex prompt + test plan + final-report template.

The runtime chat model NEVER edits source code. Coding work is done by
a separate Claude/Codex CLI session that picks up queued code_tasks.
This module produces the prompt that session reads first.

Public API:
  classify_coding_task(description)        -> "feature"|"bug"|"refactor"|...
  estimate_code_task_risk(description)     -> "low"|"medium"|"high"
  select_files_to_inspect(description)     -> list[str]
  build_coding_prompt(task)                -> str (markdown)
  build_test_plan(task)                    -> str
  build_final_report_template(task)        -> str
  save_prompt_for_task(task_id, prompt)    -> Path  (data/code_prompts/<id>.md)

Storage path:
  /opt/tiktok-bot/data/code_prompts/<task_id>.md   (gitignored)
"""
from __future__ import annotations

import re
from pathlib import Path

from bot.agent.risk import classify_risk

PROMPTS_DIR = Path("/opt/tiktok-bot/data/code_prompts")

# ── File-pattern hints — keyword → likely files to inspect ────────────────────

_FILE_HINTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\btelegram\b|\bmenu\b|\b/cancel\b|\binline\b", re.I),
        ("bot/telegram_bot.py", "data/telegram/menu_state.json (gitignored)")),
    (re.compile(r"\btiktok\b|\bplaywright\b|\bdm\b|\bsender_key\b", re.I),
        ("bot/tiktok_bot.py",  "data/chat_info.json (gitignored)")),
    (re.compile(r"\bbackend\b|\b/message\b|\b/router_status\b|\b/health\b", re.I),
        ("backend/server.py",)),
    (re.compile(r"\b9router\b|\bllm\b|\bmodel\b|\bcomplete\b", re.I),
        ("bot/llm_client.py",)),
    (re.compile(r"\bcrm\b|\bproduct\b|\blead\b|\bconsult\b|\bsales\b|\besim\b", re.I),
        ("bot/business_store.py", "data/business.db (gitignored)")),
    (re.compile(r"\bmemory\b|\blesson\b|\braw_event\b", re.I),
        ("bot/memory_store.py", "data/agent_memory.db (gitignored)")),
    (re.compile(r"\bcode[_\s]task\b|\bcoding\s+queue\b|\bworker\b", re.I),
        ("bot/code_tasks.py", "docs/CLAUDE_CODE_WORKER.md")),
    (re.compile(r"\bplanner\b|\bexecutor\b|\brisk\b|\bagent\b", re.I),
        ("bot/agent/planner.py", "bot/agent/executor.py",
         "bot/agent/risk.py", "bot/agent/task_lifecycle.py")),
    (re.compile(r"\bocr\b|\bvision\b|\bimage\b|\bphoto\b", re.I),
        ("bot/agent/skill_registry.py", "bot/telegram_files.py")),
    (re.compile(r"\bsearch\b|\bduckduckgo\b|\bcrawl\b", re.I),
        ("bot/tools.py",)),
    (re.compile(r"\bdeploy\b|\bbackup\b|\brollback\b|\bsmoke\b", re.I),
        ("scripts/deploy_prod.sh", "scripts/smoke_test.sh",
         "scripts/backup_prod.sh", "scripts/rollback_prod.sh")),
    (re.compile(r"\bskill\b", re.I),
        ("bot/agent/skill_registry.py",)),
    (re.compile(r"\bsystemd\b|\b\.service\b|\bunit\b", re.I),
        ("systemd/", "manage.sh")),
)


# ── Classification ────────────────────────────────────────────────────────────

def classify_coding_task(description: str) -> str:
    """Return one of: feature | bug | refactor | docs | infra | test."""
    d = (description or "").lower()
    if re.search(r"\bbug\b|\bfix\b|\bregression\b|\bbroken\b|\bcrash\b", d):
        return "bug"
    if re.search(r"\brefactor\b|\bclean\s*up\b|\brename\b|\bdedup\b|\bsimplif", d):
        return "refactor"
    if re.search(r"\bdocs?\b|\breadme\b|\bdocumentation\b|\bcomment", d):
        return "docs"
    if re.search(r"\bdeploy\b|\bsystemd\b|\bnginx\b|\binfra\b|\bbackup\b|"
                 r"\brollback\b|\bsmoke\b|\bcron\b", d):
        return "infra"
    if re.search(r"\btest\b|\beval\b|\bspec\b|\bharness\b", d):
        return "test"
    return "feature"


def estimate_code_task_risk(description: str) -> str:
    """High-confidence risk for a coding task. Conservative: when in doubt,
    promote to medium. Reuses bot.agent.risk for explicit high-risk markers."""
    base = classify_risk(description)
    if base == "high":
        return "high"

    d = (description or "").lower()
    # Coding tasks that touch production-fragile components are inherently
    # higher than a simple regex check would suggest.
    if re.search(r"\btiktok_bot\.py\b|\btiktok\s+reader\b|\bplaywright\b", d):
        return "high"
    if re.search(r"\b\.env\b|\bstorage[_\s]state\b|\bsystemd\b|"
                 r"\bnginx\b|\bmain\s+branch\b", d):
        return "high"
    if re.search(r"\bdb\s+migration\b|\bdrop\s+table\b|\bschema\b", d):
        return "high"

    cls = classify_coding_task(description)
    if cls in ("docs", "test"):
        return "low"
    if cls == "refactor":
        return "medium"
    if cls == "infra":
        return "medium"
    return "medium"  # default for "feature" / "bug": medium


def select_files_to_inspect(description: str) -> list[str]:
    """Return a list of file paths the worker should read first."""
    d = description or ""
    matched: list[str] = []
    for pat, files in _FILE_HINTS:
        if pat.search(d):
            matched.extend(files)
    if not matched:
        # Defaults — every coding task should at least look at these
        matched = [
            "docs/CLAUDE_CODE_WORKER.md",
            "docs/SELF_OPERATING_AGENT.md",
            "docs/OPERATING_RULES.md",
        ]
    # Always include the worker manual
    must = ["docs/CLAUDE_CODE_WORKER.md"]
    out = list(dict.fromkeys(must + matched))
    return out[:10]


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_coding_prompt(task: dict) -> str:
    """Return the full markdown prompt for a Claude/Codex CLI session."""
    title       = task.get("title", "(no title)")
    description = task.get("description") or task.get("title", "")
    task_id     = task.get("id", "(unknown)")
    risk        = task.get("risk_level") or estimate_code_task_risk(description)
    branch      = task.get("branch") or "dev-agent"
    cls         = classify_coding_task(description)
    files       = select_files_to_inspect(description)
    test_plan   = build_test_plan(task)
    final       = build_final_report_template(task)

    files_block = "\n".join(f"- `{p}`" for p in files)

    prompt = f"""# Code Task — {title}

**Task ID:** `{task_id}`
**Branch:** `{branch}`
**Risk:** **{risk}**
**Class:** {cls}

## 1. Project context

- Repo: `/opt/tiktok-bot` on the production VPS.
- Python: `/opt/tiktok-bot/venv/bin/python3`.
- Services (systemd): `tiktok-bot`, `tiktok-backend`, `tiktok-telegram`.
- Telegram is the admin command center.
- TikTok is a Chatgibiti-only DM worker.
- 9Router is the LLM gateway. Chat = `cx/gpt-5.5`. Coding = best-available
  Claude (`cc/claude-sonnet-4-7` if listed, else `cc/claude-sonnet-4-6`).
  Critic = `cx/gpt-5.3-codex`. Fallback = `openai/gpt-4o-mini`.

## 2. Mission for this task

```
{description}
```

## 3. Mandatory pre-reads

You MUST read these before any edit:

1. `docs/CLAUDE_CODE_WORKER.md` — the worker operating manual.
2. `docs/SELF_OPERATING_AGENT.md` — risk policy + state machine.
3. `docs/OPERATING_RULES.md` — secrets / channels / sales / git.

## 4. Files likely to inspect

{files_block}

(Read these before changing anything else. Stop if a needed file is
missing — do NOT guess.)

## 5. Risk classification

Risk for this task: **{risk}**.

- `low`     — auto-deploy after tests pass, audit-logged.
- `medium`  — auto-deploy after tests pass, surfaced in `/audit_recent`.
- `high`    — DO NOT push or deploy. Stop and create a `pending_action`
              via `bot.agent.permissions.create_pending`. Wait for admin
              `/confirm_action`.

If your edits cross into a higher risk band than declared, STOP and
escalate — never silently widen scope.

## 6. Implementation phases (suggested)

1. Re-read the mandatory docs in §3.
2. Read each file in §4 once. Note the exact lines you intend to change.
3. Make the **minimum** safe change. One logical edit per commit.
4. Run the test plan in §7. **Tests must pass before any commit.**
5. Stage explicit files only — never `git add -A`.
6. Reset any accidentally-staged secret/runtime file (see §9).
7. Commit with a descriptive message.
8. Push using inline token (see §9). Reset remote URL after.
9. Update task status: `python -m bot.code_tasks finish {task_id} \\
     --commit <hash> --summary "..."`.
10. Send Telegram report: `python -m bot.telegram_report \\
      "✅ code_task {task_id} done — <hash> — <summary>"`.

## 7. Test plan

{test_plan}

## 8. Stop conditions

- Tests fail twice in a row → `python -m bot.code_tasks fail {task_id} \\
  --summary "<root cause>"` and stop.
- A required file is missing or the change can't be made minimally → stop
  and report.
- The change drifts toward a high-risk action you weren't authorised
  for → stop and create a `pending_action`.
- The smoke test fails after a commit (pre-deploy) → revert the commit,
  do not push, report.

## 9. Git instructions

```
TOKEN=$(grep -E '^GITHUB_TOKEN=' /opt/tiktok-bot/.env | cut -d= -f2-)

# Stage explicitly. Never `git add -A`.
git add bot docs research scripts README.md .gitignore

# Reset any accidentally-staged secret/runtime file.
git reset .env "*.env" tiktok_storage_state.json storage_state.json \\
          backups "*.tar.gz" "*.log" \\
          data/*.db data/*.jsonl data/code_prompts \\
          data/telegram/files.jsonl \\
          data/telegram/session_state.json \\
          data/telegram/menu_state.json \\
          data/telegram/getupdates_offset.txt 2>/dev/null || true

git commit -m "<verb in lowercase, ≤72 chars>"

# Push using inline token. Then RESET the remote URL.
git push https://x-access-token:${{TOKEN}}@github.com/vieotp123/tiktok-agent-bot.git {branch}
git remote set-url origin https://github.com/vieotp123/tiktok-agent-bot.git

# Verify no token leak.
git remote -v                                       # must be clean
git config --get-regexp 'http\\..*\\.extraheader'   # must be empty
```

Never log the token. Never paste it into chat. Never commit it.

## 10. Final report format

{final}

---

*Generated by `bot.agent.prompt_builder.build_coding_prompt` —
deterministic, no LLM call.*
"""
    return prompt


def build_test_plan(task: dict) -> str:
    """Return a markdown test plan tailored to the task class."""
    description = task.get("description") or task.get("title", "")
    cls         = classify_coding_task(description)

    base = [
        "1. `python -m py_compile <changed files>` — must succeed.",
        "2. `bash scripts/smoke_test.sh` — must end with `🎉 SMOKE TEST PASSED`.",
        "3. `python -m bot.code_tasks list` — your task should still appear "
        "in queued/running until you finish/fail it.",
    ]

    extras: dict[str, list[str]] = {
        "bug": [
            "4. Add or extend a test that exercises the bug. The test "
            "must FAIL on the buggy code and PASS on your fix.",
            "5. Reproduce the bug end-to-end via the Telegram menu or "
            "backend curl, then verify the fix end-to-end the same way.",
        ],
        "feature": [
            "4. Run `python -m bot.agent.evals` (when present) — no new "
            "regressions.",
            "5. Add at least one direct test invocation in the test plan "
            "of the form `python -c 'from bot... import X; assert X(...)'`.",
        ],
        "refactor": [
            "4. Run the eval suite. Behaviour must be byte-identical "
            "wherever feasible.",
            "5. Diff is structural, not behavioural — prefer renames + "
            "extracts to logic changes.",
        ],
        "docs": [
            "4. Verify the markdown renders sanely (no broken HTML).",
            "5. Check that any code blocks copy-paste-runnable.",
        ],
        "infra": [
            "4. Run the script locally with `--dry-run` if it supports it.",
            "5. Manually verify the post-restart smoke test passes if the "
            "change touches systemd or service definitions.",
        ],
        "test": [
            "4. Run the new test once with the bug present (must fail), "
            "then with the fix (must pass).",
            "5. Time-box the new test under 30s total.",
        ],
    }

    block = "\n".join(base + extras.get(cls, []))
    return block


def build_final_report_template(task: dict) -> str:
    return (
        "Send this to admin via `python -m bot.telegram_report ...`:\n\n"
        "```\n"
        f"✅ code_task {task.get('id', '<id>')} done — <commit-short> — "
        "<≤200 char summary>\n"
        "```\n\n"
        "If the task fails, send:\n\n"
        "```\n"
        f"❌ code_task {task.get('id', '<id>')} failed — <root cause "
        "≤200 chars>\n"
        "```"
    )


# ── Storage helpers ───────────────────────────────────────────────────────────

def save_prompt_for_task(task_id: str, prompt: str) -> Path:
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    p = PROMPTS_DIR / f"{task_id}.md"
    p.write_text(prompt, encoding="utf-8")
    return p


def load_prompt_for_task(task_id: str) -> str | None:
    p = PROMPTS_DIR / f"{task_id}.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")
