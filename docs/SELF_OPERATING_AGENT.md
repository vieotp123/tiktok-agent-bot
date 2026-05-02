# Self-Operating Agent — Blueprint

This document is the source of truth for how the agent platform plans
work, gates risk, executes, and reports.  Worker sessions
(Claude/Codex CLI, future SEO/content workers) MUST read this before
making changes.

## 1. Mission

Run the muaesim.vn / Chatgibiti Japan eSIM business with minimal human
operations:

- Reply to TikTok DMs with verified product info.
- Manage product catalog + leads via Telegram command center.
- Queue and execute coding tasks safely.
- Eventually: SEO research, content drafts, ad campaign briefs,
  daily-digest follow-ups.

Hard non-goals:

- Posting publicly without admin confirmation.
- Auto-DMing customers we have not been DM'd by.
- Editing TikTok reader / `.env` / `storage_state` automatically.
- Touching `main` branch without admin confirmation.

## 2. Core loop

```
                  ┌─────────────────────┐
                  │    Telegram /menu   │  ← admin
                  │  /agent_run <goal>  │
                  │  /agent_plan <goal> │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │ planner.plan_goal() │  deterministic, no LLM by default
                  └──────────┬──────────┘
                             │
                  classify_risk(goal)
                             │
       ┌─────────────────────┼─────────────────────┐
       ▼                     ▼                     ▼
   ┌────────┐         ┌────────────┐         ┌────────────┐
   │ low    │         │ medium     │         │ high       │
   │ auto   │         │ auto + log │         │ pending_   │
   │ exec   │         │ + audit    │         │ action     │
   └────┬───┘         └────┬───────┘         └────┬───────┘
        │                  │                      │
        ▼                  ▼                      ▼
   executor._run_step()              waits for /confirm_action
                                     before becoming low/medium task
```

## 3. Task lifecycle

Two separate task systems coexist:

- `bot/agent/task_queue.py` — generic durable tasks (search, BTC, chat).
  Used by `/run_task` and the agent runner.
- `bot/code_tasks.py` — coding work for the Claude/Codex worker.
  Used by `/code_task` and the worker CLI.

Both follow the same state machine:

```
queued → running → testing → deploying → done
                          → failed
                          → waiting_confirm → done | cancelled
                          → cancelled
```

## 4. Risk policy

Single source of truth: `bot/agent/risk.py::classify_risk(goal)`.

| Risk     | Examples                                                           | Behaviour                                |
|----------|--------------------------------------------------------------------|------------------------------------------|
| 🟢 low   | search, chat, product DB read, memory read                         | auto-run, audit-log entry                |
| 🟡 medium| product CRUD, lead update, multi-step task, file summarise         | auto-run, audit-log entry, surfaced in `/audit_recent` |
| 🔴 high  | TikTok DM, public posting, restart, deploy, `.env` edit, push main | becomes `pending_action`; admin must `/confirm_action` |

The classifier is regex-only and deterministic. **Goals are not
re-classified at runtime by an LLM** — the human-readable rule set in
`risk.py` is the contract.

## 5. Worker roles

See `bot/agent/worker_roles.py::list_worker_roles()`. Eight roles
declared today:

| Role               | Channel  | Model           | Status      |
|--------------------|----------|-----------------|-------------|
| telegram_admin     | telegram | telegram_chat   | live        |
| tiktok_chatgibiti  | tiktok   | tiktok_chat     | live        |
| backend_router     | internal | search_summary  | live        |
| memory_writer      | internal | reasoning       | live        |
| sales_consultant   | internal | telegram_chat   | live        |
| code_worker        | code     | coding          | live        |
| planner_executor   | internal | reasoning       | live        |
| seo_marketing      | internal | reasoning       | placeholder |
| content_factory    | internal | coding          | placeholder |
| browser_ocr_worker | internal | vision          | placeholder |

## 6. Model policy

All LLM calls go through `bot/llm_client.complete(role=…)`. Roles map
to 9Router:

| Role                                                | Model                |
|-----------------------------------------------------|----------------------|
| chat / tiktok_chat / telegram_chat / search_summary | `cx/gpt-5.5`         |
| reasoning / coding | best available Claude — currently `cc/claude-sonnet-4-6`, auto-upgrades to `cc/claude-sonnet-4-7` when listed |
| critic                                              | `cx/gpt-5.3-codex`   |
| vision                                              | `openai/gpt-4o`      |
| cheap / fallback                                    | `openai/gpt-4o-mini` |

ENV overrides for any role MUST be validated against `/models` before
use. Chat models never run coding workloads.

## 7. Memory policy

- `raw_events` — append-only, never inlined into LLM prompts.
- `memories`   — semantic facts; max 8 items / 6000 chars per prompt.
- `lessons`    — injected only when the same skill re-runs.
- `payload_json` from raw events is **never** in prompt context.
- Customer message content is logged with a 60-char summary; never the
  full content.
- Tokens / cookies / `storage_state` are **never** logged.

## 8. Git / deploy policy

- Active branch: `dev-agent`. Worker sessions push here only.
- `main` is admin-only — workers never push to main.
- Tokens used inline in the push URL only; never written to
  `git config` or any committed file.
- After every push: `git remote set-url origin
  https://github.com/vieotp123/tiktok-agent-bot.git` so no token leaks
  in the remote URL.
- Deploy is gated by `scripts/smoke_test.sh` before and after, with
  `scripts/rollback_prod.sh` as auto-fallback.

## 9. Telemetry & audit

- `bot/agent/audit_log.py` — append-only JSONL at
  `data/audit/actions.jsonl`. Every skill / command emits an entry.
- `bot/memory_store.py::add_raw_event` — secondary append-only event
  log inside `agent_memory.db`.
- Telegram bot logs every callback / message route with structured
  `[telegram] ...` lines.

## 10. Self-check

`bot/agent/self_check.py::run_self_check()` — used by `/agent_health`.
Probes: systemd services, backend `/health` and `/router_status`,
active model roles, git state, process count, state-file existence.

## 11. Eval harness

`bot/agent/evals.py` runs in <10 s and locks invariants across:

- **router** — chat = `cx/gpt-5.5`, coding = Claude.
- **risk**   — 13 representative goals classified correctly.
- **planner** — 6 plans each return correct plan-level risk and step IDs.
- **lifecycle** — valid + invalid transitions match the table.
- **menu** — every menu builder returns valid keyboard.
- **memory** — `build_memory_context` respects 6000-char cap and never
  leaks `payload_json`.
- **files** — `is_safe_send_path` blocks `.env` / `storage_state` /
  path traversal.
- **tasks** — code_task lifecycle on the live DB.
- **prompt** — prompt_builder produces a complete 10-section markdown.

Wired to `/agent_evals`. Currently **72/72 pass in ~5s**.

## 12. Permission sessions

`bot/agent/sessions.py` lets the admin grant a time-boxed wider scope
without touching individual `/confirm_action`s. Scopes:

| Scope             | Auto-approves                                   |
|-------------------|-------------------------------------------------|
| `low_only`        | low (default)                                   |
| `low_medium`      | low + medium                                    |
| `code_low_medium` | low + medium specifically for code_task work    |
| `admin_readonly`  | low read-only (no writes)                       |

A hard kill-list (`ALWAYS_CONFIRM_HINTS`) overrides any granted scope:
`post`, `dm khách`, `.env`, `storage_state`, `tiktok_bot.py`, `git push
to main`, `merge main`, `restart`, `systemctl`, `deploy`, `rollback`,
`drop table`, `delete from`, `rm -rf`. These ALWAYS require explicit
`/confirm_action`.

Cap: 240 minutes per grant; default scope is restored on revoke or
expiry.

## 13. Self-improvement loop v1

`bot/agent/self_improve.py::self_improve_once()` — **run-once**, NOT a
daemon:

1. Read `docs/CURRENT_STATUS.md` + `docs/ROADMAP.md`.
2. If a queued low/medium-risk code_task already exists, surface it
   (and ensure its prompt is saved to `data/code_prompts/<id>.md`).
3. Else, take the first unchecked roadmap item:
   - high-risk → create `pending_action` (admin must `/confirm_action`).
   - low/medium → queue a code_task and generate the prompt.
4. Send a Telegram report.

This function NEVER edits source code. Coding work is performed by a
separate Claude/Codex CLI session that picks up queued code_tasks and
follows `docs/CLAUDE_CODE_WORKER.md`.

## 14. Failure handling

If a phase or test fails twice in a row, **stop that phase** and:
1. Preserve the current stable state (no partial deploy).
2. Mark the related code_task `failed` with a root-cause summary.
3. Send a Telegram report with the failure reason.

Never loop indefinitely on errors. Never auto-roll-forward through a
broken state.
