# Current Status — Business Agent Platform

_Last updated: 2026-05-02 (dev-agent branch — Business Agent v1)._

## Stable foundations

- **TikTok Chatgibiti worker** — Playwright-based DM bot.
  - Sender identification via `make_sender_key(name, avatar, side, idx)`.
  - JS extractor + baseline + js_items=0 visibility logging.
  - Boot marker + scroll-to-bottom on first poll.
  - Fallback phrase stripper (`_strip_banned`) in backend defense in depth.
- **Telegram command center** — long-poll bot with inline keyboard.
  - **Edit-in-place UX** (Business Agent v1): `/menu` reuses one menu
    message; navigation calls `editMessageText` instead of resending.
  - Active menu message_id persisted in `data/telegram/menu_state.json`
    (24h TTL). Edit failure → automatic fallback to send + state update.
  - Categories: Status, Router/Models, Tasks, Search, Files, Skills,
    Memory, Sales/CRM, Admin.
  - Logs every transition: `menu mode=edit`, `menu mode=send`,
    `callback data=…`, `pending_input=…`, `menu edit failed reason=…`.
- **9Router LLM Gateway**:
  - chat / tiktok_chat / telegram_chat / search_summary → `cx/gpt-5.5`
  - reasoning / coding → `cc/claude-sonnet-4-6` (Sonnet 4.7 not yet
    available on 9Router; preference chain auto-upgrades when listed)
  - critic → `cx/gpt-5.3-codex`
  - vision → `openai/gpt-4o`
  - cheap / fallback → `openai/gpt-4o-mini`
  - 5-minute model cache; ENV overrides validated against `/models`.
- **Task queue / Skills / Audit log** — SQLite + JSONL.
- **Search web (DuckDuckGo + LLM summary)** — works.
- **File handling** — Telegram inbox, summarisation via backend.

## Memory foundation

- SQLite `data/agent_memory.db` — 5 tables (raw_events, memories, lessons,
  task_state, memory_usage).
- API: `add_memory`, `search_memory`, `add_lesson`, `list_lessons`,
  `update_task_state`, `get_task_state`, `build_memory_context`,
  `get_context_for_task`, `add_raw_event`, `compact_memories`.
- Hard prompt-context limits: 8 items / 6000 chars / no raw payload.
- 5 seeded global memories, 2 seeded lessons.
- Auto-lesson hook in `bot/agent/runner.py` (success → importance 3,
  failure → importance 6).

## Product DB + CRM (Business Agent v1)

- SQLite `data/business.db` with 5 tables.
- Status values: **active** (verified, OK to quote), **needs_update**
  (admin sees warning, customer never sees as confirmed), **disabled**
  (never suggested).
- Telegram commands:
  `/products [active|needs_update|disabled]`, `/product <id>`,
  `/product_add`, `/product_update`, `/product_verify`, `/product_disable`,
  `/consult <q>`, `/leads`, `/lead <id>`, `/lead_by_sender`, `/lead_add`,
  `/consulting_logs [sender_key]`, `/followups`, `/followup_add`.
- Sales/CRM submenu: All Products · Active · Needs Update · Consult ·
  Leads · Followups · Add Product · Update Product.
- **Sales consult engine** (`build_consult_reply(query, audience=…)`):
  - `audience="customer"` (TikTok DM) — polite tone, no mày/tao,
    never quotes needs_update as confirmed.
  - `audience="admin"` (Telegram) — direct, with warnings + product ids.
  - Filters by structured features (country / SMS / hotspot / renewable)
    AND keyword match. Excludes disabled products.
  - **Never invents prices.** No active match → "no verified info".
- **Lead scoring** (cap 100): price +30, SMS/OTP +30, hotspot +20,
  renew +20, duration/data +10. Status auto-set: ≥50 → interested;
  ≥20 → needs_followup; otherwise new.
- Every consulting call logs to `consulting_logs` (sender_key,
  products_used JSON, confidence) and emits a `raw_event` to
  `agent_memory.db` (no PII / prices).
- Auto-upserts a lead per (platform, sender_key) at first eSIM intent;
  logs every inbound + outbound message into `conversations`.

### Known limitations

- **Catalog needs real data**: 5 sample products are seeded, 2 marked
  active for test (Softbank 30d/50GB · 850k VND · ¥4500; Docomo+SMS
  30d/20GB · 1.1M VND · ¥5800). Replace with real verified prices via
  `/product_update` + `/product_verify` before showing to real customers.
- No automatic outreach. Send actions still require `/confirm_action`.
- No public posting / scheduled DMs from the agent yet.
- Lead scoring is naïve (keyword-based) — future: LLM intent classifier.
- `consulting_logs` is local SQLite; not yet replicated.

## Next priorities

1. **Verify real product catalog** — replace sample seed data with the
   actual price list from muaesim.vn.
2. **SEO / Marketing engine** — keyword research, competitor lookup,
   landing-page brief generator.
3. **Content / image campaign factory** — caption writer + image-gen
   pipeline (currently placeholder skills in registry).
4. **Browser / OCR worker** — Playwright research worker that can read
   competitor sites; OCR on customer-uploaded screenshots.
5. **Worker manager expansion** — register browser, OCR, image-gen
   workers so they appear in `/workers`.
6. **Scheduler / daily jobs** — daily catalog freshness check, daily
   lead-followup digest, daily SEO crawl.

## Branch / commit policy

- Active branch: `dev-agent`.
- Runtime DBs (`data/agent_memory.db`, `data/business.db`, `data/memory.json`,
  `data/reminders.json`, `data/telegram/files.jsonl`,
  `data/telegram/session_state.json`, `data/telegram/menu_state.json`)
  are **never** committed.
- `.env`, `tiktok_storage_state.json`, `storage_state.json`, backups,
  `*.log` — never committed.
