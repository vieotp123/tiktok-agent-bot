# Current Status — Business Agent Platform

_Last updated: 2026-05-02 (dev-agent branch — Self-Improving Agent v1)._

## Self-Improving Agent v1 changes

- **Task lifecycle state machine** at `bot/agent/task_lifecycle.py`.
  Ten states with a table-driven transition validator and per-attempt
  audit-log entries. `validate_transition`, `transition_task`,
  `can_auto_execute`, `task_state_summary`, `transition_table`.
- **Structured planner v2** at `bot/agent/planner.py`. Each plan now
  carries `plan_id`, `risk_level`, `model_role`, `rationale`,
  `success_criteria`, `stop_conditions`, and per-step
  `id` / `title` / `skill` / `args` / `risk_level` / `requires_confirm` /
  `expected_output` / `test`. Plan-level risk is the max of step risks.
- **Prompt builder** at `bot/agent/prompt_builder.py`. Generates a
  10-section markdown Claude/Codex prompt + test plan + final-report
  template for any coding-task description. Saves long prompts to
  `data/code_prompts/<id>.md` (gitignored).
- **Eval harness** at `bot/agent/evals.py`. Nine categories cover
  router/risk/planner/lifecycle/menu/memory/files/tasks/prompt. **72/72
  evals pass in <7s**. Wired to `/agent_evals`.
- **Observability**: `/agent_status` (full dashboard) and
  `/agent_metrics` (compact one-liner). Both query pending counts,
  memory counts, last audit lines, last deploy.
- **Permission sessions** at `bot/agent/sessions.py`. Time-boxed
  scopes (`low_only`, `low_medium`, `code_low_medium`,
  `admin_readonly`) with hard-rule kill-list (always-confirm patterns
  override any granted scope). Commands: `/permissions`,
  `/grant_session <scope> <minutes>`, `/revoke_session`.
- **Self-improve loop v1** at `bot/agent/self_improve.py`. Run-once
  (`/self_improve_once`): reads ROADMAP, checks queue, queues
  low/medium coding tasks for the Claude/Codex worker, creates
  pending_action for high-risk roadmap items. **Never edits source
  from runtime chat model.**
- **Framework study** updated with full gap analysis and stolen-vs-not
  patterns from OpenClaw / LangGraph / OpenHands / CrewAI.

## v1.5 changes

- Telegram persistent menu UX hardened:
  - `do:*` actions now ALWAYS edit the menu in place (≤ 3500 chars) or
    send a separate result message + park the menu with "Result sent above"
    (> 3500 chars). The menu never spawns a fresh copy on action results.
  - New public helpers per spec:
    `get_menu_state`, `save_menu_state`, `send_or_edit_menu`,
    `show_action_result`, `result_keyboard`.
  - `/menu`, `/start`, `/help`, `/cancel` reuse the existing menu message.
  - Pending-input completion + file-upload reply also go through the
    menu-edit path.
- Sales/CRM submenu adds two buttons: **Verify Product** and
  **Disable Product** (input flows already wired).
- Lead status no longer downgrades (`new < needs_followup < interested
  < converted/lost`). Lead score only rises on auto-update.

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
