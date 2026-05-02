# Agent Platform Blueprint

> Project: tiktok-bot → AI Agent Platform  
> Branch: dev-agent  
> Last updated: 2026-05-02  
> Status: Foundation phase — skeleton implemented, workers/memory/vision not yet live

---

## 1. Brain (Control Plane)

The "Brain" is the central intelligence layer that owns routing, memory, decisions, and audit.
It does NOT directly touch any social platform — channels do that.

```
┌─────────────────────────────────────────────────────────────┐
│                         BRAIN                               │
│                                                             │
│  Telegram (command)  ──►  backend/server.py  ◄──  TikTok   │
│                              │                              │
│              ┌───────────────┼───────────────┐             │
│              │               │               │             │
│         9Router LLM     task_queue       skill_registry    │
│         (llm_client)    (SQLite)         (Skill dataclass) │
│              │               │               │             │
│         memory_store    worker_manager   permissions        │
│         (JSONL/SQLite)  (workers.json)   (pending_actions) │
│              │               │               │             │
│              └───────────────┼───────────────┘             │
│                          audit_log                          │
│                         (JSONL append-only)                 │
└─────────────────────────────────────────────────────────────┘
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| LLM Gateway | `bot/llm_client.py` | 9Router with role→model mapping, fallback chain |
| Task Queue | `bot/agent/task_queue.py` | SQLite-backed durable task state |
| Skill Registry | `bot/agent/skill_registry.py` | All skills declared with risk_level |
| Worker Manager | `bot/worker_manager.py` | Workers list, health, dispatch routing |
| Memory Store | `bot/memory_store.py` | raw_events / memories / lessons / skill_notes |
| Permissions | `bot/agent/permissions.py` | Risk gating, pending_actions, confirm flow |
| Audit Log | `bot/agent/audit_log.py` | Append-only action log, never secrets |
| Reports | (future) `bot/reports.py` | Daily digest, task stats, error summary |

---

## 2. Channels

Channels are **workers** that touch external platforms. They push normalized messages to
the Brain (`/message` endpoint) and receive reply text back. They do NOT make LLM calls directly.

### Active Channels

| Channel | File | Role | Scope |
|---------|------|------|-------|
| Telegram | `bot/telegram_bot.py` | Command center, file hub, confirmation UI | Full control — admin only |
| TikTok | `bot/tiktok_bot.py` | Social worker | Chatgibiti only, read/reply |

### Future Channels (not yet implemented)

| Channel | Purpose | Notes |
|---------|---------|-------|
| Facebook/Page | Social worker | Same pattern as TikTok |
| Browser worker | Web automation | Must run in subprocess sandbox |
| OCR worker | Image/screenshot text extraction | Tesseract or vision LLM |
| Image-gen worker | Create product/social images | Stable Diffusion or API |
| Coding worker | GitHub PR, code review, patch | Sandbox + human confirm required |

### Channel Contract

Every channel communicates with the Brain via:
```
POST /message
{
  "username": "<channel>_<user_id>",   // tg_admin, tiktok_chatgibiti
  "content":  "<user message>",
  "source":   "telegram|tiktok|task|file_summary|search"
}
→ { "reply": "...", "messages": [...], "error": false }
```

Source tag drives LLM role selection in `llm_client.py`:
- `telegram` → `telegram_chat` role → `cx/gpt-5.5`
- `tiktok`   → `tiktok_chat` role → `cx/gpt-5.5`
- `search`   → `search_summary` role → `cx/gpt-5.5`
- `task`     → `chat` role → `cx/gpt-5.5`
- `file_summary` → `chat` role → `cx/gpt-5.5`

---

## 3. Skills

Skills are the atomic capabilities the Brain can invoke. Each skill has:
- **name**: unique identifier
- **description**: human-readable, shown in /skills output
- **risk_level**: `low | medium | high`
- **handler**: function/tool identifier string
- **enabled**: bool (can disable without removing)

### Current Skills

| Skill | Risk | Status | Notes |
|-------|------|--------|-------|
| `chat` | low | ✅ live | General LLM via 9Router |
| `btc_price` | low | ✅ live | CoinGecko realtime |
| `search_web` | low | ✅ live | DuckDuckGo + LLM summary |
| `file_summary` | low | ✅ live | Summarize uploaded file via LLM |
| `router_status` | low | ✅ live | 9Router health + role models |
| `status` | low | ✅ live | systemd service status |
| `models` | low | ✅ live | Show active LLM model per role |
| `task_runner` | medium | ✅ live | Run task via agent/runner.py |
| `tiktok_chat_info` | medium | ✅ live | Read chat context from TikTok |
| `memory_search` | low | ✅ live | Search/retrieve semantic memory |
| `telegram_file_hub` | low | ✅ live | Receive + summarize files via Telegram |
| `product_lookup` | low | ✅ live | Search Product DB (`bot/business_store.py`) |
| `sales_consult` | low | ✅ live | DB-grounded eSIM consult; never invents prices |
| `send_tiktok_dm` | high | ⚠️ guarded | Requires /confirm_action |
| `restart_service` | high | ⚠️ guarded | Requires /confirm_action |

### Future Skills (placeholders only, disabled in registry)

| Skill | Risk | Notes |
|-------|------|-------|
| `seo_research` | medium | Keyword + competitor research |
| `content_factory` | medium | Draft post/caption/content |
| `browser_search` | medium | Playwright-based web automation |
| `ocr_remote` | medium | Extract text from image |
| `image_generate` | medium | Generate image from prompt |
| `git_commit` | high | Commit code to GitHub |
| `deploy` | high | Trigger deployment |

---

## 4. Task Lifecycle

```
queued
  │
  ▼
running ──(needs human input)──► waiting_confirm
  │                                     │
  │                              /confirm_action
  │                                     │
  ▼                                     ▼
done / failed / cancelled ◄─────── confirmed
```

### Task States

| State | Description |
|-------|-------------|
| `queued` | Created, not yet picked up by runner |
| `running` | Runner has started execution |
| `waiting_confirm` | Blocked on `/confirm_action <id>` |
| `done` | Completed successfully |
| `failed` | Exception or error |
| `cancelled` | Cancelled by user |

### Task Record (task_queue.db)

```sql
CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,
  type          TEXT,
  goal          TEXT,
  status        TEXT,
  progress      TEXT,
  result_summary TEXT,
  error         TEXT,
  created_at    TEXT,
  updated_at    TEXT,
  input_files   TEXT,   -- JSON list of file paths
  output_files  TEXT    -- JSON list of output file paths
);
```

---

## 5. Risk Levels

### Low risk — execute immediately, log only
- `search_web`
- `btc_price`
- `chat`
- `file_summary`
- `router_status`
- `status`
- `models`
- Read from `data/` safe folders

### Medium risk — execute + log with medium flag, no blocking
- `task_runner` (run a new task)
- `tiktok_chat_info`
- Draft content
- Create reports
- Browse web (future)
- Create images (future)

### High risk — MUST create pending_action, MUST wait for `/confirm_action <id>`
- `send_tiktok_dm` — send DM to external user
- `post_public` — publish content publicly
- `publish_website` — deploy to live site
- `restart_service` — systemctl restart in production
- `edit_env` — modify .env file
- `delete_data` — delete records or files
- `run_shell` — arbitrary shell command
- `git_merge_deploy` — merge branch + deploy

```
High-risk flow:
  User request
    → detect risk_level == "high"
    → permissions.create_pending(action, goal, user)
    → Bot: "⚠️ High-risk action: <goal>\nAction ID: <id>\nConfirm: /confirm_action <id>"
    → User sends /confirm_action <id>
    → permissions.confirm_pending(id)
    → runner resumes execution
    → audit_log.log_action(status="done")
```

---

## 6. Worker Model

### WorkerTask

```python
@dataclass
class WorkerTask:
    task_id:    str
    type:       str        # skill name or action type
    platform:   str        # "telegram" | "tiktok" | "internal"
    input:      dict       # arbitrary skill parameters
    limits:     dict       # {"timeout_s": 30, "max_tokens": 600}
    risk_level: str        # "low" | "medium" | "high"
```

### WorkerResult

```python
@dataclass
class WorkerResult:
    task_id:  str
    status:   str          # "done" | "failed" | "partial"
    items:    list         # result items (messages, records, etc.)
    files:    list[str]    # output file paths
    summary:  str          # human-readable summary
    error:    str          # error message if failed
```

### Worker Registry (data/workers.json)

```json
{
  "telegram_bot": {
    "id": "telegram_bot",
    "type": "channel",
    "platform": "telegram",
    "status": "active",
    "capabilities": ["send_message", "receive_message", "file_upload", "file_download"],
    "last_seen": "2026-05-02T06:54:44Z"
  },
  "tiktok_bot": {
    "id": "tiktok_bot",
    "type": "channel",
    "platform": "tiktok",
    "status": "active",
    "capabilities": ["read_chat", "send_message"],
    "last_seen": "2026-05-02T06:40:00Z",
    "constraint": "TARGET_CHAT_NAME=Chatgibiti"
  }
}
```

---

## 7. Memory Model

### Memory Tiers

| Tier | Storage | TTL | Purpose | Don't do |
|------|---------|-----|---------|----------|
| `raw_events` | JSONL | 7 days | Full chronological log of all actions | Don't inject raw into prompts |
| `short_term_task_state` | SQLite (task_queue) | Until task done | Current task context | Don't keep after task finishes |
| `semantic_memory` | SQLite / JSONL | Indefinite | User facts, preferences, topics | Don't store secrets |
| `episodic_lessons` | SQLite / JSONL | Indefinite | What worked / what failed per skill | Source of skill tuning |
| `procedural_skill_notes` | SQLite / JSONL | Indefinite | Per-skill prompt tuning notes | Don't let LLM write these directly |

### Retrieval Rules
1. Never dump raw_events into LLM prompts.
2. Retrieve only top-K relevant `semantic_memory` entries (keyword match or recency).
3. Inject `episodic_lessons` as few-shot examples when the same skill is reused.
4. `procedural_skill_notes` are reviewed by humans before activating.

### Memory Store API (`bot/memory_store.py`)

```python
# Write
add_raw_event(source, action, summary, metadata)
add_memory(username, text, memory_type, tags)
add_lesson(skill, outcome, lesson_text)
add_skill_note(skill, note)

# Read
search_memory_simple(username, query, limit=5) -> list[dict]
list_lessons(skill=None, limit=20)             -> list[dict]
list_skill_notes(skill=None)                   -> list[dict]
```

---

## 8. Permissions & Confirm Flow

```python
# Every skill execution goes through this gate:
def gate(skill_name: str, goal: str, user: str) -> str | None:
    skill = get_skill(skill_name)
    if skill.risk_level == "high":
        action_id = create_pending(skill_name, goal, "high", user)
        return action_id  # caller must pause and show confirm prompt
    log_action(user, skill_name, skill.risk_level, "ok")
    return None  # proceed immediately
```

### Telegram Confirm UI

```
⚠️ High-risk action requested

Action: restart_service
Goal: restart tiktok-bot systemd service
Risk: HIGH

To confirm: /confirm_action abc12345
To cancel:  /cancel_action abc12345

This action will execute on your production server.
```

---

## 9. Audit Log

Every task and action writes one JSONL line to `data/audit/actions.jsonl`:

```json
{
  "timestamp":      "2026-05-02T06:54:54Z",
  "user":           "tg_admin",
  "action":         "run_task:search_web",
  "risk_level":     "low",
  "status":         "done",
  "result_summary": "Found 4 results for 'Python 3.13'",
  "task_id":        "abc123",
  "goal":           "tìm thông tin Python 3.13",
  "channel":        "telegram"
}
```

Rules:
- Never log `token`, `password`, `secret`, `key`, `api_key`, `auth` field values.
- Truncate `result_summary` to 200 chars.
- Truncate `goal` to 120 chars.
- All timestamps in UTC ISO 8601.

---

## 10. Telegram Commands Reference

### Always available (no risk)
| Command | Description |
|---------|-------------|
| `/start` `/help` | Welcome + quick start |
| `/menu` | Inline button menu |
| `/status` | Service health |
| `/health` | Backend health check |
| `/skills` | List all registered skills |
| `/tasks` | List recent tasks |
| `/router_status` | 9Router model health |
| `/models` `/model_policy` | Active model per role |
| `/memory` | User memory summary |
| `/reminders` | Reminder list |
| `/agent_blueprint` | Architecture summary |
| `/workers` | Worker registry |
| `/memory_search <q>` | Search semantic memory |
| `/lessons` | Episodic lessons list |
| `/audit_recent` | Last 10 audit entries |
| `/cancel` | Cancel pending session input |

### Confirmation required
| Command | Description |
|---------|-------------|
| `/confirm_action <id>` | Approve a pending high-risk action |
| `/cancel_action <id>` | Cancel a pending high-risk action |

---

## 11. 9Router Model Policy

| Role | Default Model | Fallback Chain |
|------|--------------|----------------|
| `tiktok_chat` | `cx/gpt-5.5` | → cx/gpt-5.4 → openai/gpt-5 → openai/gpt-4o |
| `telegram_chat` | `cx/gpt-5.5` | → cx/gpt-5.4 → openai/gpt-5 → openai/gpt-4o |
| `search_summary` | `cx/gpt-5.5` | → cx/gpt-5.4 → openai/gpt-5 → openai/gpt-4o |
| `chat` | `cx/gpt-5.5` | → cx/gpt-5.4 → openai/gpt-5 → openai/gpt-4o |
| `coding` | `cc/claude-opus-4-7` | → cc/claude-opus-4-6 → cx/gpt-5.3-codex |
| `reasoning` | `cc/claude-opus-4-7` | → openai/o3-pro → openai/o3 → openai/o4-mini |
| `critic` | `cx/gpt-5.3-codex` | → cx/gpt-5.2-codex → cc/claude-sonnet-4-6 |
| `cheap` | `openai/gpt-4o-mini` | → openai/gpt-4.1-mini → openai/gpt-4.1-nano |

**Rules:**
- Never use `coding` or `reasoning` role for normal chat.
- `cheap` is fallback only — never the primary selection for quality tasks.
- ENV overrides: `LLM_CHAT_MODEL`, `LLM_CODING_MODEL`, etc. take absolute priority.

---

## 12. Security Principles

1. **No arbitrary plugin install** — all skills must be code-reviewed and in this repo.
2. **No LLM-generated shell exec** — LLM cannot run bash without human `/confirm_action`.
3. **High-risk always confirms** — `pending_actions.json` + Telegram confirm prompt.
4. **Secrets never in logs** — audit_log strips known secret field names.
5. **Channel isolation** — TikTok and Telegram contexts do not mix.
6. **TikTok scope** — bot only operates inside `TARGET_CHAT_NAME=Chatgibiti`.
7. **Worker filesystem** — future workers write only to `data/workers/<task_id>/`.
8. **Admin-only Telegram** — only `ADMIN_CHAT_ID` can send commands.

---

## 13. Roadmap

### Phase 0 (done) — Foundation
- TikTok bot + sender ID
- Telegram command center
- Task queue, skill registry, audit log, permissions
- 9Router model policy

### Phase 1 (current) — Architecture skeleton
- Research notes + blueprint (this doc)
- `worker_manager.py` skeleton
- `memory_store.py` skeleton
- Telegram commands: `/agent_blueprint`, `/workers`, `/memory_search`, `/lessons`, `/audit_recent`

### Phase 2 (next) — Memory & context
- `memory_store.py` fully functional
- `search_memory_simple` used in LLM context
- `lessons` populated from task outcomes
- Memory pruning (TTL for raw_events)

### Phase 3 (future) — Worker platform
- Docker-based sandbox worker
- Browser worker (Playwright)
- Coding worker (git + tests)
- Content factory (draft + review)

### Phase 4 (future) — Scale
- Multi-user Telegram support
- Facebook channel
- Scheduler / cron tasks
- Webhook triggers
