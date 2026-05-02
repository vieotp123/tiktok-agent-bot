# Agent Architecture Research Notes

> Studied: LangGraph, OpenHands, CrewAI, OpenClaw  
> Purpose: Inform our agent platform evolution  
> Date: 2026-05-02  
> Rule: No blind copying. Only borrow patterns that fit our scale.

---

## 1. LangGraph — Durable State Machines

**Core concept**  
Workflows as directed graphs where each node is an agent function and edges carry typed state.
A `Checkpointer` snapshots state at every "superstep" so long-running jobs survive crashes and
can be inspected or resumed at any point.

**Patterns worth borrowing**
- **Thread IDs + checkpoint state**: Every task gets a unique thread ID. State accumulates across
  invocations. Enables resume-from-failure without replaying everything from scratch.
- **`interrupt_before` edges**: A node can pause and wait for human approval before executing a
  transition. Maps directly to our `pending_action` / `confirm_action` pattern.
- **Short-term vs long-term memory separation**: Working state lives in graph channels (in-memory,
  fast). Long-term facts live in external storage, retrieved on demand. Never dump raw logs into
  LLM prompts — retrieve only top-k relevant entries.
- **Streaming**: Yield partial state updates while the graph runs. Our Telegram bot can show
  progress messages for long tasks instead of a single reply after 30s.

**What NOT to copy**
- Pregel superstep engine: overkill for linear pipelines. Added latency and ops complexity.
- PostgresCheckpointer in production: requires schema migrations, backups, connection pooling.
  Start with SQLite checkpoints or simple JSON state.
- Multi-tenant shared checkpoint storage needs access control — not free.

**Our mapping**
```
LangGraph thread_id   →  our task_id
Checkpointer          →  task_queue.db task row (status, result_summary, updated_at)
interrupt_before      →  permissions.requires_confirm → pending_actions.json
Memory channels       →  memory_store.py (raw_events / memories tables)
```

---

## 2. OpenHands — Event-Stream + Action/Observation Loop

**Core concept**  
Agent = stateless LLM function. State = append-only `EventLog` (list of past Actions +
Observations). Agent reads the full log, emits one atomic `Action`. Runtime executes it in
a sandboxed `Workspace`, returns an `Observation`. Repeat.

**Patterns worth borrowing**
- **Event-stream as canonical state**: All actions and their results are logged in order.
  Replay = re-read the log. This is deterministic and trivially serializable.
- **Action/Observation types**: Structured output instead of freeform text.
  `CmdRunAction`, `WriteFileAction`, `BrowseURLAction` each have typed fields.
  Our `WorkerTask` / `WorkerResult` structs should follow this pattern.
- **AgentDelegate pattern**: An agent can delegate a subtask to a specialist sub-agent and
  get the result back as an Observation. Maps to our future multi-worker model.
- **Explicit sandbox boundary**: Code execution goes through a `Runtime` that enforces the
  workspace perimeter. Nothing runs on the host by accident.

**What NOT to copy**
- Docker-per-session is heavy. For trusted internal tools, in-process execution with an
  explicit capability allowlist is simpler and faster.
- Event log grows unbounded — need compaction strategy for long sessions.
- CodeAct (LLM generates Python/bash to run) is powerful but increases attack surface.
  We should NOT let the LLM generate arbitrary shell commands without confirmation.

**Our mapping**
```
EventLog              →  audit_log.py (JSONL append-only)
Action types          →  WorkerTask.type field  
Observation types     →  WorkerResult.status / items / error
Workspace             →  future: sandboxed worker (Docker or subprocess with limits)
AgentDelegate         →  future: worker_manager.dispatch(task) → result
```

---

## 3. CrewAI — Role-Based Teams + Task Dependency Graphs

**Core concept**  
Define agents by role/goal/backstory (prompt-level), wire them into Tasks with explicit
dependencies, run as a Crew. Each task's output becomes context for the next. Flows add
event-driven branching logic around Crews.

**Patterns worth borrowing**
- **YAML/config-first agent definitions**: Agents declared as data, not code. Easy to version,
  review, and reason about. Maps to our `skill_registry.py` Skill dataclass approach.
- **Task context chaining**: Each skill invocation can receive the result of a prior skill.
  Our `runner.py` should support chaining: `search_web → summarize → draft_reply`.
- **Guardrails on task output**: Validate LLM output before accepting it. E.g. assert JSON
  structure, check for banned phrases, verify URL format. We already have `_strip_banned()`.
- **Observability hooks**: `on_task_start`, `on_task_end`, `on_agent_action` callbacks.
  Our audit_log already does this; make it structured enough to query.
- **Memory types** to model:
  - `raw_events` — chronological log (we have this in audit_log)
  - `short_term_task_state` — current task context (task_queue row)
  - `semantic_memory` — searchable facts about users/topics
  - `episodic_lessons` — what worked / what failed in past runs
  - `procedural_skill_notes` — per-skill tuning notes

**What NOT to copy**
- YAML scaffolding gets unwieldy for dynamic agent creation (per-user skills).
- Task context chaining with no limits causes token bloat. Be explicit about what context
  each task needs; don't inject everything.
- CrewAI AMP (external observability SaaS) — use local logging instead.
- Vector DB integrations — don't add until simple keyword search proves insufficient.

**Our mapping**
```
Agent(role, goal)     →  Skill(name, description, handler)
Task(context=[prev])  →  runner.run_task chain (future)
Guardrail             →  _strip_banned() + output validation
Crew                  →  worker_manager.run_pipeline() (future)
memory_manager        →  memory_store.py (add_memory, search_memory_simple)
```

---

## 4. OpenClaw — Gateway as Control Plane

**Core concept**  
Single long-lived daemon (`Gateway`) owns all messaging surfaces. Channel plugins emit
normalized message events. Sessions `(user, channel, agent)` own state and memory. Skills
are config-driven with explicit sandbox and permission flags.

**Patterns worth borrowing**
- **Gateway hub**: One process owns all I/O channels (Telegram, TikTok, future channels).
  Agents do NOT directly touch channels. This is already how our backend/server.py works.
- **Channel plugins emit normalized events**: `{type, from, channel, text, attachments}`.
  Our `source=` parameter in backend calls is the start of this.
- **Session isolation `(user, channel, agent)`**: Different channels don't share context
  by default. Our username prefix convention (`tg_`, `tiktok_`) partially does this.
- **Skill config with explicit `allow`/`sandbox` flags**: Skills declare their own risk level.
  Our `Skill(risk_level=...)` in `skill_registry.py` follows this.
- **Device pairing / admin auth**: Only the Telegram admin chat ID can send commands.
  Already enforced in `telegram_bot.py` via `ADMIN_CHAT_ID`.

**What NOT to copy**
- WebSocket daemon architecture: our FastAPI backend is simpler and sufficient.
- Multi-channel state sync: keep Telegram and TikTok context separate. Don't blend them.
- Plugin auto-download: never auto-install plugins. All skills must be code-reviewed.
- Bail-if-config-broken logic: our permissioning is already Python-native; no TOML needed.

**Our mapping**
```
Gateway daemon        →  backend/server.py (FastAPI)
Channel plugins       →  bot/tiktok_bot.py, bot/telegram_bot.py (push to /message)
Session              →  (username, source) pair in memory.py
Skill config         →  skill_registry.py Skill dataclass
device auth          →  ADMIN_CHAT_ID check in telegram_bot.py
```

---

## 5. What We Should NOT Copy (Security Risks)

### Plugin/Skill auto-installation
- Never auto-download and run skill plugins from external registries.
- All skills must be code-reviewed and committed to this repo.
- Reference: supply chain attacks on npm/PyPI can run arbitrary code on install.

### Arbitrary shell execution
- LLM-generated bash/Python commands must NEVER execute without human confirmation.
- High-risk action → `pending_actions.json` → `/confirm_action <id>` required.
- Only pre-approved, typed actions (from `skill_registry.py`) can run automatically.

### Prompt injection via tool output
- Web search results, file contents, and API responses can contain injected instructions.
- Always wrap external content in a `<search_result>` / `<file_content>` delimiter.
- Validate LLM output for banned phrases before sending.

### Credential leakage in logs
- Audit log already strips `token`, `password`, `secret`, `key`, `api_key`, `auth` keys.
- Never log full LLM prompts (they may contain user context with secrets).
- `.env` and `storage_state.json` are in `.gitignore` — keep them there.

### Permission creep
- Skill permissions must be declared at registration time, not dynamically granted.
- High-risk skills must require `confirm_action` every time — no "remember my choice".
- Medium-risk skills log but don't block — always auditable.

### Sandbox escape
- Future browser/code workers MUST run in a subprocess or container.
- Never let a worker write to `/opt/tiktok-bot/.env` or `storage_state.json`.
- Restrict worker filesystem access to `data/workers/<task_id>/` only.

---

## 6. Summary: Patterns We Adopt

| Pattern | Source | Status |
|---------|--------|--------|
| `task_id` as durable unit of work | LangGraph threads | ✅ `task_queue.py` |
| Checkpoint task state in SQLite | LangGraph checkpointer | ✅ `task_queue.db` |
| `interrupt_before` = pending_action | LangGraph human-in-loop | ✅ `permissions.py` |
| Append-only audit EventLog | OpenHands event stream | ✅ `audit_log.py` |
| Typed `WorkerTask` / `WorkerResult` | OpenHands actions | 🔧 `worker_manager.py` |
| Skills as config (name/desc/risk) | CrewAI + OpenClaw | ✅ `skill_registry.py` |
| Guardrails (`_strip_banned`) | CrewAI guardrails | ✅ `server.py` |
| Memory separation (short/long/episodic) | CrewAI + LangGraph | 🔧 `memory_store.py` |
| Gateway owns all channels | OpenClaw gateway | ✅ `backend/server.py` |
| Channel context isolation | OpenClaw sessions | ✅ `source=` param |
| Admin auth gate | OpenClaw device pairing | ✅ `ADMIN_CHAT_ID` |
| No arbitrary plugin install | Security rule | ✅ not implemented |
| No LLM-generated shell exec | Security rule | ✅ not implemented |

Legend: ✅ done | 🔧 skeleton needed | ❌ not planned yet
