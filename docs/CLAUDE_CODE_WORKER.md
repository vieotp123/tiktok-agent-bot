# Claude / Codex Code Worker — Operating Manual

This document is the contract for any Claude/Codex CLI session that
picks up coding tasks queued by the Telegram admin via `/code_task`.

**You MUST read this entire file before editing any code.**

## Two ways the worker runs

1. **Manual** — admin runs `claude` / `codex` in a terminal at
   `/opt/tiktok-bot`, pastes the saved prompt from
   `data/code_prompts/<task_id>.md`, lets the CLI work, then completes
   the loop with `python -m bot.code_tasks finish <id> --commit <h>`.
   This was validated by commit `c5b0e2b`.
2. **Automated via the bridge** — admin sends `/code_worker_run_once`
   (or `/code_worker_run_batch <n>`) in Telegram. The bridge
   (`bot/coding_worker_bridge.py`) detects the CLI, runs it
   non-interactively with the saved prompt as stdin, captures a
   sanitised log to `data/code_worker_logs/`, runs smoke + evals,
   commits + pushes only on green tests, and marks the task done. If
   the CLI is missing or interactive-only, the bridge returns the
   manual setup instructions and leaves the task untouched.

The contract below applies in BOTH modes — the bridge enforces the
hard rules in §3 by unstaging any forbidden path before commit.

## 0. Where you are

- Branch: `dev-agent` (always — never push to `main`).
- Repo:   `/opt/tiktok-bot` on the production VPS.
- Python: `/opt/tiktok-bot/venv/bin/python3`.
- Services: `tiktok-bot`, `tiktok-backend`, `tiktok-telegram` (systemd).

## 1. Mandatory pre-reads

Before touching any code:

1. `docs/SELF_OPERATING_AGENT.md` — risk policy, model policy, audit policy.
2. `docs/OPERATING_RULES.md` — secrets, channels, sales replies, git.
3. `docs/CURRENT_STATUS.md` — what's working today and known limits.
4. The full description of the queued task (`python -m bot.code_tasks info <id>`).

## 2. Your loop

```
while not paused and queue not empty:
    1. nx = python -m bot.code_tasks next        # next queued id
    2. python -m bot.code_tasks info $nx         # read full description
    3. python -m bot.code_tasks start $nx        # status → running

    4. Make MINIMAL safe changes:
       - dev-agent only
       - one logical change per task
       - never touch .env / storage_state / TikTok reader unless task is
         explicitly tagged risk=high AND has been confirmed by admin
       - prefer Edit over Write to keep diffs reviewable

    5. Run tests:
       - python -m py_compile <changed files>
       - bash scripts/smoke_test.sh      ← MUST pass
       - any task-specific test the user mentioned

    6. If tests PASS:
       - git add <only the files you intended>
       - git reset .env "*.env" tiktok_storage_state.json storage_state.json
            backups "*.tar.gz" "*.log" data/*.db data/*.jsonl
            data/telegram/files.jsonl data/telegram/session_state.json
            data/telegram/menu_state.json data/telegram/getupdates_offset.txt
       - git commit -m "<short message starting with action>"
       - PUSH using inline token (see §4):
            TOKEN=$(grep -E '^GITHUB_TOKEN=' /opt/tiktok-bot/.env | cut -d= -f2-)
            git push https://x-access-token:${TOKEN}@github.com/vieotp123/tiktok-agent-bot.git dev-agent
       - Reset remote so the token is NOT stored:
            git remote set-url origin https://github.com/vieotp123/tiktok-agent-bot.git
            git config --get-regexp 'http\..*\.extraheader' || true   # MUST be empty
       - python -m bot.code_tasks finish $nx --commit <full hash> \
                                            --summary "<≤200 chars>"
       - python -m bot.telegram_report "✅ code_task <id> done — <commit short> — <summary>"

    7. If tests FAIL:
       - python -m bot.code_tasks fail $nx --summary "<root cause ≤200 chars>"
       - python -m bot.telegram_report "❌ code_task <id> failed — <root cause>"
       - DO NOT commit. Leave the working tree dirty for the human to inspect.
       - If you can fix it cheaply (one retry), retry ONCE.
         If second attempt also fails: stop the loop and report.
```

## 3. Hard rules

- **Never push to `main`.** Only `dev-agent`.
- **Never** commit `.env`, `*.env`, `tiktok_storage_state.json`,
  `storage_state.json`, backups, logs, `data/*.db`, `data/*.jsonl`,
  `data/telegram/menu_state.json`, `data/telegram/session_state.json`,
  `data/telegram/getupdates_offset.txt`.
- **Never log** the GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, or any cookie.
  Don't `echo $TOKEN`. Don't `git config user.password`. Use the inline
  push URL pattern in §4.
- **Never edit** `bot/tiktok_bot.py` (the TikTok reader / Playwright)
  unless the task is explicitly risk=high AND confirmed via
  `/confirm_action` first. The TikTok reader is fragile and a regression
  silently breaks customer DMs.
- **Never auto-merge to main.**  If a task says "merge to main", create a
  pending_action instead and stop.
- **Never run a public action** (TikTok DM, post, comment, follow) from
  a code task. Code tasks are for refactoring / docs / new tools — not
  for live customer outreach.
- **Never install random packages** outside the existing `venv`.  If
  you need a new dependency, add it to `requirements.txt` only and
  flag it in your task summary.

## 4. Token handling

The push token lives in `/opt/tiktok-bot/.env` as `GITHUB_TOKEN=…`.
Read it ONCE per push and use it inline:

```bash
TOKEN=$(grep -E '^GITHUB_TOKEN=' /opt/tiktok-bot/.env | cut -d= -f2-)
git push https://x-access-token:${TOKEN}@github.com/vieotp123/tiktok-agent-bot.git dev-agent
git remote set-url origin https://github.com/vieotp123/tiktok-agent-bot.git
```

Then verify:

```bash
git remote -v                                      # must show clean URL
git config --get-regexp 'http\..*\.extraheader'    # must be empty
```

If you ever leave a token in `git config` or in any file, rotate it
immediately and ping the admin.

## 5. Reporting

Every completed/failed task SHOULD send a Telegram report:

```bash
python -m bot.telegram_report "✅ code_task ctk_abc done — d4e5f6 — short summary"
```

The helper escapes its argument and never logs the token.

## 6. CLI cheat-sheet

```bash
python -m bot.code_tasks list                            # all tasks
python -m bot.code_tasks list --status queued
python -m bot.code_tasks add "title" --priority 7 --risk low \
                                     --description "longer description"
python -m bot.code_tasks info <id>
python -m bot.code_tasks start <id>
python -m bot.code_tasks finish <id> --commit <hash> --summary "..."
python -m bot.code_tasks fail <id> --summary "root cause"
python -m bot.code_tasks cancel <id>
python -m bot.code_tasks status                          # worker dashboard
python -m bot.code_tasks next                            # prints next queued id
python -m bot.code_tasks pause                           # halt auto-run
python -m bot.code_tasks resume

bash scripts/smoke_test.sh                               # MUST pass before commit
bash scripts/backup_prod.sh                              # creates backups/prod_<ts>.tar.gz
bash scripts/deploy_prod.sh                              # smoke + backup + restart + smoke
bash scripts/rollback_prod.sh [path]                     # restore from backup
```

## 7. Failure escalation

If a phase or test fails twice in a row, **stop**:

1. Mark the code_task `failed` with a one-paragraph root-cause summary.
2. Send a Telegram report.
3. Leave the working tree clean (`git status` should be tidy or have
   only intentional WIP that the admin can review).
4. Do NOT loop indefinitely on errors.
5. Do NOT auto-roll-forward through a broken state.

## 8. Style

- Edit existing files; create new ones only when necessary.
- Keep diffs small. One logical change per commit.
- Never add markdown noise to source files. Comments only when they
  explain *why*, not *what*.
- Prefer `bot/llm_client.complete(role=…)` over any direct LLM SDK
  call. Chat models never run coding workloads.
- Never wrap a tool call in dynamic `eval()` / `exec()`.

## 9. Model selection

- `coding_worker_bridge` pins the model passed to the Claude CLI via
  `--model $CLAUDE_CODE_MODEL` and provides automatic CLI-level
  fallback via `--fallback-model sonnet`. Both are also exposed
  through `ANTHROPIC_MODEL` (set inside the sudo `env` wrapper) so any
  Claude SDK reads pick the same model.
- Default: `opus` → `claude-opus-4-7`. To pin Sonnet instead, set
  `CLAUDE_CODE_MODEL=sonnet` in `.env` and restart `tiktok-telegram`.
- `/code_worker_status` reports `selected_model`, `model_source`
  (`env` vs `default`), and `actual_model` — the model that actually
  answered a probe ping, which is the only honest fallback indicator.
- `/code_worker_run_once` summary echoes the actual model used and
  appends `⚠ fallback` if `--fallback-model` fired during the probe.

## 10. Worth re-reading every loop

- Risk policy → `bot/agent/risk.py`
- Model policy → `bot/llm_client.py`
- Audit log → `bot/agent/audit_log.py`
- Worker roles → `bot/agent/worker_roles.py`
- Skill registry → `bot/agent/skill_registry.py`
- Smoke test → `scripts/smoke_test.sh`

When in doubt, prefer "stop and report" over "guess and push".
