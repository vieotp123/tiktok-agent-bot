# Agent Framework Study

A focused comparison of four open agent frameworks. The intent is to
extract patterns we should adopt for the muaesim/Chatgibiti business
agent — **not** to clone or run any of these as-is.

We deliberately skip toy LLM-trick demos and stick to architectures that
have been used in production-style settings for sales / support / coding
workflows.

| Framework      | Primary use-case                                | Why we look at it |
|----------------|-------------------------------------------------|-------------------|
| **OpenClaw**   | Anthropic-style multi-agent control plane       | Memory tiers, gateway hub |
| **LangGraph**  | Stateful multi-step graphs over LLM calls       | Durable task state, human-in-loop |
| **OpenHands** (formerly OpenDevin) | Coding agents with shell + git + browser tools | Coding worker loop, sandboxing |
| **CrewAI**     | Role-based agent crews (planner/researcher/exec)| Worker roles, delegation, audit trails |

## OpenClaw

What we keep:
- **Gateway hub model**: a single LLM gateway (we have `bot/llm_client.py`).
- **Multi-tier memory** with namespace isolation:
  - `raw_events`  — append-only, never injected into prompts
  - `memories`    — semantic facts, namespace-scoped
  - `lessons`     — episodic outcomes per skill
  - `task_state`  — durable per-task scratchpad
- **Hard prompt limits** (max-items + max-chars) when building context.

What we discard:
- Plug-in marketplace / arbitrary skill loading. Every skill we ship is
  audited code in `bot/agent/skill_registry.py`. No dynamic plugin
  loading from the internet.

## LangGraph

What we keep:
- **Graph of nodes with persistent state**. Maps to our planner →
  executor pipeline. Each step has an id, status, and output.
- **Human-in-loop interrupt** at risk-gated nodes. Maps to our
  `pending_action` + `/confirm_action` flow.
- **Replayable state**: we persist task_state in SQLite so a restart
  resumes from the last known step.

What we discard:
- Full DAG composition language. We use a simple list-of-steps because
  our goals are short and linear. We can graduate to a proper DAG when
  we add multi-stage SEO/content campaigns.

## OpenHands (OpenDevin)

What we keep:
- **Coding agent loop**: read task → plan → edit → test → commit.
  This is exactly what `bot/code_tasks.py` queues and what
  `docs/CLAUDE_CODE_WORKER.md` instructs the Claude/Codex CLI to do.
- **Sandboxed file ops**: scripts/{backup,smoke,deploy,rollback} make
  every change reversible.
- **Test-before-commit gate**: smoke_test.sh runs before deploy and on
  every commit attempt.

What we discard:
- A standalone container per agent. We run on a single VPS with
  systemd; isolation is via tar.gz backup + rollback.

## CrewAI

What we keep:
- **Worker roles** with explicit job descriptions
  (`bot/agent/worker_roles.py`). Each role has a model_role + risk_level
  + status. No surprise capabilities.
- **Delegation rules**: telegram_admin can queue a code_task; the
  code_worker (Claude/Codex CLI) executes; the planner_executor handles
  free-form goals; the sales_consultant only answers DB-grounded
  product questions.
- **Per-role audit log** (we already log every action via
  `bot/agent/audit_log.py`).

What we discard:
- Agents talking to each other in free-form natural language to
  decide handoffs. We use deterministic dispatch (`bot/agent/planner.py`
  classifies the goal; `executor.py` runs pre-declared step types).
  This is auditable and cheap.

## Patterns we do NOT adopt

- "Auto-import any tool from a registry." Each tool is reviewed code
  in this repo.
- "LLM decides when to push to main." Pushes to `main` are admin-only.
- "Browser/OCR worker can run anything it sees." Future browser/OCR
  workers will only call pre-declared site-specific actions, audited
  per-skill.
- "Plug-and-play marketplace." Adds attack surface; we will gate every
  external integration via a confirmed code_task.

## Decisions for our platform

1. **Memory** stays multi-tier, namespace-scoped, with hard prompt caps.
   Already implemented in `bot/memory_store.py`.
2. **Planner is deterministic** (regex + skill registry). Reasoning-LLM
   plans are an upgrade path, not the default.
3. **Executor is risk-gated**: `low/medium` auto-runs, `high` becomes a
   `pending_action`.
4. **Coding worker** is a separate Claude/Codex CLI session driven by
   `docs/CLAUDE_CODE_WORKER.md`. The Telegram bot only enqueues tasks;
   it does not edit code itself.
5. **Deploy** is gated by `scripts/smoke_test.sh` before and after.
   Failure auto-invokes `rollback_prod.sh`.
6. **No marketplace, no auto-plugin loading, no public actions without
   confirmation.**

References (read public docs only — we do NOT clone or run these):
- OpenClaw: https://github.com/anthropics (concept docs)
- LangGraph: https://langchain-ai.github.io/langgraph/concepts/
- OpenHands: https://docs.all-hands.dev/
- CrewAI: https://docs.crewai.com/
