# Current Status — Agent Platform

_Last updated: 2026-05-02 (dev-agent branch)._

## Stable foundations

- **TikTok Chatgibiti worker** — Playwright-based DM bot in production.
  - Sender identification via `make_sender_key(name, avatar, side, idx)`.
  - JS extractor + baseline + js_items=0 visibility logging.
  - Boot marker + scroll-to-bottom on first poll.
  - Fallback phrase stripper (`_strip_banned`) in backend defense in depth.
- **Telegram command center** — long-poll bot with inline keyboard.
  - `/menu` shows 5 categories: Status, Router/Models, Tasks, Search,
    Files, Skills, Memory, Sales/CRM, Admin.
  - All commands HTML-escaped to avoid parse errors.
- **9Router LLM Gateway** with policy:
  - chat / tiktok_chat / telegram_chat / search_summary → `cx/gpt-5.5`
  - reasoning / coding → `cc/claude-sonnet-4-6`
  - critic → `cx/gpt-5.3-codex`
  - cheap / fallback → `openai/gpt-4o-mini`
  - 5-minute model cache; ENV overrides validated against `/models`.
- **Task queue / Skills / Audit log** — SQLite + JSONL, all working.
- **Search web (DuckDuckGo + LLM summary)** — works.
- **File handling** — Telegram inbox, summarisation via backend.

## Memory foundation (commit `5500b00`)

- SQLite `data/agent_memory.db` with 5 tables:
  `raw_events`, `memories`, `lessons`, `task_state`, `memory_usage`.
- API: `add_memory`, `search_memory`, `add_lesson`, `list_lessons`,
  `update_task_state`, `get_task_state`, `build_memory_context`,
  `get_context_for_task`, `add_raw_event`, `compact_memories`.
- **Prompt hygiene**: max 8 items, max 6000 chars, never inject raw payloads.
- 5 seeded global memories (business context, TikTok fragility,
  AWS duplicate risk, model policy, architecture).
- 2 seeded lessons (DuckDuckGo VN fallback, TikTok DM silent failure).
- Telegram commands: `/memory_search`, `/memory_add`, `/memory_forget`,
  `/memory_compact`, `/memory_context`, `/lessons`.
- Auto-lesson hook in `bot/agent/runner.py` (success → importance 3,
  failure → importance 6).

## Product DB + CRM foundation (current commit)

- SQLite `data/business.db` with 5 tables:
  `products`, `leads`, `conversations`, `consulting_logs`, `followups`.
- File: `bot/business_store.py`.
- Functions: `init_business_db`, CRUD for products / leads / conversations
  / consulting_logs / followups, `detect_esim_intent`, `consult_lookup`,
  `build_consult_reply`, `_extract_query_filters`,
  format helpers for Telegram output.
- **Sales consult engine**:
  - Detects eSIM intent on TikTok / Telegram backend `/message` flow.
  - Looks up products by structured filters (country / SMS / hotspot /
    renewable) **and** keyword match.
  - Active products with verified prices → quoted.
  - `needs_update` products → mentioned but flagged as unverified.
  - **Never invents prices.** If nothing matches, says info missing.
- Logs every consulting call to `consulting_logs` (sender_key, products_used,
  confidence) and emits a `raw_event` to `agent_memory.db` (no PII).
- Auto-upserts a lead per (platform, sender_key) at the first eSIM
  question; logs every inbound + outbound message into `conversations`.
- **Skill registry** — `sales_consult` and `product_lookup` enabled.
- **Telegram commands**: `/products`, `/product_add`, `/product_update`,
  `/consult <q>`, `/leads`, `/lead <id>`, `/lead_add`, `/followups`.
- **Sales/CRM submenu** in main menu.

### Known limitations

- **All seeded products are `status="needs_update"`** with sample data.
  Prices and feature flags MUST be verified before they can be quoted to
  customers as confirmed. The consult engine surfaces these but always
  attaches the "chưa verify" disclaimer.
- No automatic outreach. Send actions require `/confirm_action` (high risk).
- No public posting / scheduled DMs from the agent yet.
- Lead scoring is naïve (`max(1, confidence*10)`) — future LLM-based scoring.
- `consulting_logs` is local SQLite; not yet replicated.

## Next priorities

1. **Verify product catalog** — admin replaces seeded `needs_update` rows
   with real prices and feature flags via `/product_update`.
2. **SEO / Marketing engine** — keyword research worker, competitor lookup,
   landing-page brief generator.
3. **Content / image campaign factory** — caption writer skill,
   image-gen pipeline (currently disabled in skill registry).
4. **Worker manager expansion** — register browser, OCR, image-gen workers
   so they show up in `/workers`.
5. **Lead intelligence** — LLM-based lead scoring, intent classification,
   automatic followup scheduling (still requiring confirm_action for sends).

## Branch / commit policy

- Active branch: `dev-agent`.
- Runtime DBs (`data/agent_memory.db`, `data/business.db`,
  `data/memory.json`, `data/reminders.json`,
  `data/telegram/files.jsonl`, `data/telegram/session_state.json`)
  are **never** committed.
- `.env`, `tiktok_storage_state.json`, `storage_state.json`, backups,
  `*.log` — never committed.
