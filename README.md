# Business Agent Platform — Japan eSIM (Chatgibiti)

**Not just a TikTok DM bot.** This repo is the agent platform that powers
the muaesim.vn / Chatgibiti eSIM business: a Telegram command center, a
9Router LLM gateway, a TikTok customer-DM worker, a task queue + skill
registry, persistent memory, and a Product DB + CRM.

```
                     ┌──────────────────────────┐
                     │  Telegram Command Center │   ← admin only
                     │  (inline /menu, edit-in- │
                     │  place UX, full control) │
                     └────────────┬─────────────┘
                                  │
                                  ▼
   ┌──────────────────┐   ┌────────────────────────┐    ┌──────────────────┐
   │ TikTok Worker    │──▶│ Backend (FastAPI)      │◀──▶│ 9Router LLM      │
   │ "Chatgibiti"     │   │ /message, /search,     │    │ (chat / coding / │
   │ Playwright DM    │   │ /router_status, /health│    │  reasoning roles)│
   └────────┬─────────┘   └────────────┬───────────┘    └──────────────────┘
            │                          │
            ▼                          ▼
    ┌──────────────┐         ┌────────────────────┐
    │ Memory Store │         │ Product DB + CRM   │
    │ raw_events   │         │ products / leads   │
    │ memories     │         │ conversations      │
    │ lessons      │         │ consulting_logs    │
    │ task_state   │         │ followups          │
    └──────────────┘         └────────────────────┘
```

## Components

- **Telegram command center** (`bot/telegram_bot.py`) — admin-only.
  - Inline menu **edits in place**; `/menu` does not spam new messages.
  - Categories: Status · Router/Models · Tasks · Search · Files ·
    Skills · Memory · Sales/CRM · Admin.
- **9Router model gateway** (`bot/llm_client.py`) — single chokepoint for
  every LLM call. Validated ENV overrides, 5-minute model cache.
- **TikTok Chatgibiti worker** (`bot/tiktok_bot.py`) — Playwright DM bot
  with sender identification (`make_sender_key`). **Read-only on the
  conversation list; only sends inside the active chat. No public posting.**
- **Backend** (`backend/server.py`) — `/message`, `/router_status`,
  `/health`. Dispatches BTC, search, sales-consult, reminder, generic LLM.
- **Task queue** (`bot/agent/task_queue.py`) — durable SQLite-backed jobs.
- **Skill registry** (`bot/agent/skill_registry.py`) — risk-tagged
  (low/medium/high). High-risk requires `/confirm_action`.
- **Memory store** (`bot/memory_store.py`) — `data/agent_memory.db`.
  Five tables: raw_events, memories, lessons, task_state, memory_usage.
  Hard prompt-context limits: 8 items, 6000 chars.
- **Product DB + CRM** (`bot/business_store.py`) — `data/business.db`.
  Five tables: products, leads, conversations, consulting_logs, followups.
- **Audit log** (`bot/agent/audit_log.py`) — append-only JSONL, no secrets.

## Model policy

| Role                                                | Model                |
|-----------------------------------------------------|----------------------|
| chat / tiktok_chat / telegram_chat / search_summary | `cx/gpt-5.5`         |
| reasoning / coding | best-available Claude — `cc/claude-sonnet-4-7` if listed, else `cc/claude-sonnet-4-6` |
| critic                                              | `cx/gpt-5.3-codex`   |
| vision                                              | `openai/gpt-4o`      |
| cheap / fallback                                    | `openai/gpt-4o-mini` |

Resolved live via `bot/llm_client.select_model(role)` — ENV overrides are
validated against `/models` before use. Chat models never use coding models.

## Sales consulting

The agent answers eSIM/product questions only from the verified product
catalog (`status="active"`). It **never invents prices**. If only
`needs_update` candidates match, the customer sees a polite "we're
checking with admin" reply and the admin sees a warning + candidate list.

Lead scoring (cap 100):

| Signal                  | Points |
|-------------------------|--------|
| asks price              | +30    |
| asks SMS / OTP          | +30    |
| asks hotspot / WiFi     | +20    |
| asks renewable / gia hạn| +20    |
| mentions duration / GB  | +10    |

Lead status: `new` → `needs_followup` (≥20) → `interested` (≥50) →
`converted` / `lost`.

## Telegram commands (admin only)

```
/menu                         Inline control panel (edits in place)
/start /help                  Same as /menu
/cancel                       Clear pending input, return to main menu

# Status / health
/status /health /logs /tiktok_chat_info

# Router / models
/router_status /models /model_policy

# Tasks
/tasks /task <id> /run_task <goal> /cancel_task <id>
/pending_actions /confirm_action <id> /cancel_action <id>

# Skills
/skills /skill <name>

# Files
/files /file <id> /send_file <path>

# Memory
/memory_search <q> /memory_add /memory_forget <id>
/memory_compact /memory_context <q> /lessons [skill]

# Sales / CRM
/products [active|needs_update|disabled]
/product <id>
/product_add /product_update /product_verify <id> /product_disable <id>
/consult <message>
/leads /lead <id> /lead_by_sender [platform:]<sender_key> /lead_add
/consulting_logs [sender_key]
/followups /followup_add <lead_id> | <iso_time> | <note>

# Audit / agent
/audit_recent /agent_blueprint /workers
```

## Quick start

```bash
cd /opt/tiktok-bot
cp .env.example .env
nano .env                       # 9Router key, Telegram token + admin id
cp /path/to/tiktok_storage_state.json /opt/tiktok-bot/
./manage.sh install
./manage.sh start
./manage.sh status
./manage.sh logs                # tiktok-bot live log
```

Manage script:

```
./manage.sh start | stop | restart | status | logs
./manage.sh logs-bot | logs-backend
./manage.sh test-backend | test-storage | compile-check
```

Systemd services: `tiktok-bot`, `tiktok-backend`, `tiktok-telegram`.

## Layout

```
/opt/tiktok-bot/
├── backend/server.py            # FastAPI: /message, /router_status, /health
├── bot/
│   ├── telegram_bot.py          # Admin command center + inline menu
│   ├── tiktok_bot.py            # Playwright Chatgibiti worker
│   ├── llm_client.py            # 9Router gateway, role-based model select
│   ├── tools.py                 # BTC price, search_web, intent helpers
│   ├── memory.py                # Per-user short-term chat memory
│   ├── memory_store.py          # Multi-tier persistent memory
│   ├── business_store.py        # Product DB + CRM (sales consulting)
│   ├── reminders.py             # Reminder scheduler
│   ├── telegram_files.py        # Inbox for files uploaded via Telegram
│   ├── worker_manager.py        # Worker registry
│   └── agent/
│       ├── task_queue.py        # Durable SQLite tasks
│       ├── skill_registry.py    # Risk-tagged skill catalog
│       ├── runner.py            # Skill executor + auto-lesson hook
│       ├── permissions.py       # Pending-action / confirm flow
│       └── audit_log.py         # Append-only JSONL audit
├── data/                        # ALL gitignored (runtime DBs, JSONL, etc.)
├── docs/
│   ├── AGENT_BLUEPRINT.md       # Architecture spec
│   ├── CURRENT_STATUS.md        # What works / next priorities
│   └── OPERATING_RULES.md       # Safety rules (this file)
└── systemd/                     # Service unit files
```

## Dev workflow

```
git checkout dev-agent
# ...edit code...
git status
git add bot docs README.md .gitignore   # never .env / storage / DBs
git commit -m "..."
git push origin dev-agent
```

Gitignored — never committed:

```
.env  *.env
tiktok_storage_state.json  storage_state.json
backups/  screenshots/  *.log
data/memory.json data/reminders.json
data/tasks.db data/agent_memory.db data/business.db data/memory_store.db
data/pending_actions.json  data/chat_info.json
data/audit/  data/telegram/  data/uploads/  data/cache/
```

## Operating rules (summary)

- All LLM calls go through the shared `llm_client.complete(role=...)`.
- TikTok worker is **Chatgibiti-only**: never auto-DMs, never posts publicly,
  never follows accounts.
- High-risk actions (restart, send-DM, deploy) require `/confirm_action`.
- Customer-facing sales replies must never quote `needs_update` products as
  confirmed.
- Tokens, cookies, storage_state must never appear in logs or commits.

See `docs/OPERATING_RULES.md` for the full ruleset and
`docs/AGENT_BLUEPRINT.md` for architecture.
