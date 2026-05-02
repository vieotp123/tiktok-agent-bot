# Agent Framework Study

A focused, opinionated study of four agent frameworks. Goal: extract
patterns we should adopt for the muaesim/Chatgibiti business agent
platform — **not** clone or run any of these as-is.

## TL;DR — what we're stealing, in one paragraph

Treat the platform as **OpenClaw's gateway** (single LLM chokepoint,
channels as adapters) wrapping **LangGraph's durable graph** (typed
states, replayable from checkpoints, human-in-loop interrupt) where
each step routes work to **OpenHands-style coding worker loops**
(action/observation, test-before-commit, sandbox via tar+rollback)
organised as **CrewAI roles** (planner, code worker, sales consultant,
research worker — each role declares its model, risk, and skills).

We do **not** adopt: dynamic plug-in marketplaces, free-form
agent-to-agent natural-language delegation, auto-merge to main, or any
LLM-decided destructive action.

---

## OpenClaw — what we keep

| Pattern                    | How we apply it                                              |
|----------------------------|--------------------------------------------------------------|
| Chat gateway / control plane | `bot/llm_client.py` — single chokepoint for every LLM call. ENV overrides validated against `/models`.                                                                          |
| Channels / adapters        | Telegram (`bot/telegram_bot.py`), TikTok (`bot/tiktok_bot.py`), backend (`backend/server.py`). Each channel has its own role mapping but uses the same gateway.                          |
| Skills / actions           | `bot/agent/skill_registry.py` — risk-tagged, enabled bool, examples. Not loaded dynamically.                                                                                   |
| Device / local worker      | The Code Worker is a Claude/Codex CLI session co-located on the VPS. It sees `/opt/tiktok-bot` directly; no remote sandbox.                                                              |
| Permission boundary        | `bot/agent/risk.py::classify_risk` + `bot/agent/permissions.py::create_pending`. Hard rules, not LLM judgement.                                                                          |
| Personal assistant UX      | Telegram inline menu with edit-in-place, stable anchor, rebuild-at-bottom on `/menu`.                                                                                                  |

What we discard: a plugin marketplace and arbitrary tool-loading from
the internet. Every skill is reviewed code in this repo.

## LangGraph — what we keep

| Pattern                | How we apply it                                                  |
|------------------------|------------------------------------------------------------------|
| Durable execution      | `data/code_tasks.db` (code tasks) + `data/tasks.db` (general tasks) + `data/agent_memory.db::task_state`. State survives restarts.                            |
| Resumable workflow     | `bot/agent/task_lifecycle.py` — explicit state machine. Each transition is logged.                                                                            |
| State machines         | `queued → planning → running → testing → deploying → done` with valid-transition table.                                                                       |
| Interrupt / human approval | `permissions.create_pending` + `/confirm_action`. High-risk steps interrupt the executor.                                                                |
| Memory separation      | `raw_events / memories / lessons / task_state / memory_usage` in `agent_memory.db`. Hard caps when building prompt context.                                   |
| Checkpointing          | `data/telegram/getupdates_offset.txt`, `data/telegram/menu_state.json`, task `updated_at` columns. Restart resumes cleanly.                                    |

What we discard: full DAG composition language. Goals here are short
and linear. We can graduate to a proper DAG when SEO/content
campaigns need parallel research.

## OpenHands — what we keep

| Pattern                  | How we apply it                                                |
|--------------------------|----------------------------------------------------------------|
| Coding worker            | `docs/CLAUDE_CODE_WORKER.md` — operating manual for the Claude/Codex CLI session.                                                                              |
| Workspace / sandbox      | tar.gz backups + `scripts/rollback_prod.sh`. The "sandbox" is reversibility, not isolation.                                                                  |
| Action / observation     | Edit → smoke_test → audit_log entry → next action. Smoke test is the observation.                                                                              |
| Test / commit loop       | `scripts/smoke_test.sh` is the gate. No commit without it. No deploy without pre+post smoke.                                                                  |
| GitHub workflow          | `dev-agent` is the worker branch. `main` is admin-only. Token used inline; never persisted in `git config`.                                                  |
| Task execution discipline| `bot/code_tasks.py` + the worker manual: pick highest-priority queued, set running, edit minimally, test, commit, finish/fail.                                 |

What we discard: per-agent containers. We rely on backup+rollback
because the deploy target is a single VPS.

## CrewAI — what we keep

| Pattern                | How we apply it                                                  |
|------------------------|------------------------------------------------------------------|
| Role-based agents      | `bot/agent/worker_roles.py` — 9 roles, each with channel + model_role + risk_level + status (live/placeholder/disabled).                                       |
| Tasks / crews / flows  | Tasks in `data/code_tasks.db` and `data/tasks.db`. Crews are implicit: planner → executor → code_worker.                                                       |
| Guardrails             | `risk.py` regex rules + `permissions.create_pending` + `requires_confirm`. Plus the smoke-test gate in deploy.                                                |
| Memory / knowledge     | `bot/memory_store.py` — namespaced, importance-scored, hard prompt caps.                                                                                       |
| Observability          | `bot/agent/audit_log.py` JSONL + structured Telegram logs `[telegram] update type=… auth ok=…`.                                                              |

What we discard: free-form agent-to-agent natural-language delegation
to decide handoffs. We use deterministic dispatch (`planner.py`
classifies, `executor.py` runs declared step types). Auditable, cheap,
fast.

---

## What we explicitly do NOT copy

- **Plug-in marketplaces** — adds remote attack surface. Every
  capability is reviewed code or a `pending_action`-gated external
  call.
- **LLM-decided destructive actions** — pushes to `main`, public posts,
  customer DM outreach, `.env` edits, systemd restarts: all require a
  human-confirmed `pending_action`.
- **Auto-merge to main** — only the admin merges.
- **Scraping with stored credentials** — out of scope until secret
  vault exists.
- **Free-form tool execution from chat** — the runtime chat model
  CANNOT edit code. Coding goes through `code_tasks` + the Claude/Codex
  CLI worker.

---

## Security risks of plugin / skill marketplaces

(Why we deliberately stay closed.)

1. **Supply-chain compromise.** A trusted plugin gets hijacked; one
   `git pull` later, the agent has new privileges. We mitigate by
   refusing dynamic loading altogether.
2. **Prompt-injected tool descriptions.** A web page can carry text
   that looks like a tool spec. With dynamic registries, that becomes
   executable. We register skills by hand only.
3. **Capability creep.** Each new plugin widens the action surface
   beyond what risk policy was written for. Our `risk.py` lists
   high-risk patterns explicitly; we'd have to re-audit every plugin.
4. **Credential exposure.** Plugins demand API keys and store them
   loosely. We keep all credentials in a single `.env` and inject only
   to the gateway (`bot/llm_client.py`).
5. **Side-channel exfiltration.** Plugins that read filesystem can
   leak `storage_state`, cookies, or backup tarballs. The bot worker
   itself has filesystem access; we keep that one component small and
   reviewed instead of multiplying it.

---

## Gap analysis — current project vs target self-operating agent

| Capability                                | Today           | Gap → mitigation                                                                                       |
|-------------------------------------------|-----------------|-------------------------------------------------------------------------------------------------------|
| LLM gateway                               | ✅ live         | —                                                                                                     |
| Telegram command center                   | ✅ live         | —                                                                                                     |
| TikTok DM worker                          | ✅ live         | —                                                                                                     |
| Multi-tier memory                         | ✅ live         | Add eval that the prompt-context builder respects max-items / max-chars (Phase 5).                    |
| Risk classifier                           | ✅ live         | Add eval table to lock the rule set against regressions (Phase 5).                                    |
| Planner                                   | ✅ basic        | Upgrade to **structured plan** (Phase 3): JSON, per-step risk + skill + test + expected_output.       |
| Executor                                  | ✅ basic        | Drive off the structured plan; fail-fast on first failed step.                                        |
| Code task queue                           | ✅ live         | Wire to **task_lifecycle** (Phase 2) so transitions are validated and logged.                         |
| **Task lifecycle state machine**          | ❌ implicit     | **NEW Phase 2**: `bot/agent/task_lifecycle.py` with valid transitions and reasons.                    |
| **Coding-prompt builder**                 | ❌              | **NEW Phase 4**: `bot/agent/prompt_builder.py` that turns a task description into a Claude prompt + test plan + final-report template. |
| **Eval harness**                          | ❌              | **NEW Phase 5**: `bot/agent/evals.py` covers router, risk, planner, menu, memory, lifecycle.          |
| **Observability (`/agent_status`)**       | ⚠ partial       | **Phase 6**: extend `self_check` with pending counts, last error, last deploy.                        |
| **Permission session grants**             | ❌              | **NEW Phase 7**: `grant_session(scope, minutes)` — temporary low/medium auto-approval window.         |
| **Self-improve loop**                     | ❌              | **NEW Phase 8**: `/self_improve_once` runs ONCE, no daemon, never edits source from runtime model.    |
| Audit log                                 | ✅ live         | —                                                                                                     |
| Smoke test / deploy / rollback            | ✅ live         | Already wired; `deploy_prod.sh` auto-rolls back on post-deploy failure.                               |
| Worker roles                              | ✅ declarative  | Plug `evals.py` into the worker_roles dashboard (Phase 6).                                            |

---

## Specific implementation priorities for this repo

1. **Durable workflow state** — `task_lifecycle.py` (Phase 2). Single
   table-driven transition validator used by both `code_tasks` and the
   general `task_queue`.
2. **Prompt / task generation** — `prompt_builder.py` (Phase 4).
   Generate the Claude/Codex prompt automatically when admin queues a
   coding task. Save long prompts to `data/code_prompts/<task_id>.md`.
3. **Memory hygiene** — already enforced; add an eval (Phase 5) that
   asserts `build_memory_context` truncates to ≤8 items and ≤6000
   chars and never includes `payload_json`.
4. **Permission / risk engine** — `risk.py` + `permissions.py` already
   solid; add session-grant scopes (Phase 7) so the admin can grant
   "auto-approve low/medium for 30 min" while supervising overnight.
5. **Code worker loop** — already documented; add `prompt_builder.py`
   so the worker session has a high-quality starting prompt.
6. **Eval / test harness** — `evals.py` (Phase 5). Runs in <30s. Hard
   blocks future regressions in router policy, risk classifier,
   planner, menu, memory, lifecycle.
7. **Observability** — `/agent_status` (Phase 6) merges service
   health, router roles, branch/commit, pending counts, memory counts,
   last 3 audit lines, last deploy result.
8. **Rollback / deploy discipline** — already wired; add a post-deploy
   `/agent_status` ping in `deploy_prod.sh` to send a Telegram report.

---

## References (public docs only — we do NOT clone or run)

- OpenClaw concept docs (Anthropic-adjacent)
- LangGraph: <https://langchain-ai.github.io/langgraph/concepts/>
- OpenHands: <https://docs.all-hands.dev/>
- CrewAI: <https://docs.crewai.com/>
