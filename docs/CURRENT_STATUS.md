# Current Status — Business Agent Platform

_Last updated: 2026-05-02 (dev-agent branch — Autonomous Control Bridge v1)._

> **Worker validation:** Code worker CLI loop verified end-to-end with
> task `ctk_a65f61badc` — smoke test, commit, push, finish, and
> Telegram report all run cleanly via `python -m bot.code_tasks` +
> `bot.telegram_report`. See `docs/CLAUDE_CODE_WORKER.md` for the
> contract this task exercised.

## Claude Quota Probe + Retry v1

- **`bot/claude_quota.py`** extended with v2 honest-tracking schema.
  No fake quota %. State enums: `unknown / available / limited /
  auth_required / error`.
- **`probe_claude_available(force=False)`** — runs a one-shot
  `claude --print "Reply only: CLAUDE_PROBE_OK"` (≤60s, output
  redacted). Success cached 5 min. Limited / auth state honors the
  stored `next_probe_at` so we never spam Claude.
- **`parse_claude_error(text)`** — broad regex over CLI stderr/stdout
  detecting limit / auth / rate-limit phrasing. Extracts:
  - `reset_at` if "resets at YYYY-MM-DD HH:MM UTC" appears
  - `retry_after_seconds` from "retry-after: 90" /
    "try again in 2h 30m" / "in 5 minutes"
  9/9 parser cases pass.
- **`mark_limited(reset_at|retry_after_seconds|None)`** — when no
  exact reset is known, exponential backoff
  `30 → 60 → 120 → 240 min` (cap 4h) per `probe_count`.
- **`mark_auth_required` / `mark_error` / `mark_available`** — each
  records `last_probe_at`, `last_success_at`, `last_limited_at`,
  `last_error_summary`. `last_notified_key` dedupes admin notifications.
- **`format_claude_status_vi()`** — Vietnamese status panel with
  model / status / probe times / reset_at / autorun / queue size.
- **Bridge integration** (`coding_worker_bridge.run_once`):
  - Pre-run quota gate: if `status="limited"` or `auth_required`,
    task is **kept queued** (not failed) and a Vietnamese reply
    surfaces the retry schedule.
  - Post-run failure parser: if `claude` exited non-zero AND the log
    matches a limit/auth pattern, task is re-queued via
    `update_task(status='queued')` and quota state updated.
- **Scheduler** (`_on_due` in `claude_quota.py`): when `reset_at`
  arrives, **probe first**, then run `bridge.run_batch(max_tasks)`
  only if available. If still limited, schedule next backoff.
- **Telegram**:
  - `/claude_status` now uses the rich VN formatter.
  - `/claude_probe` forces a fresh probe.
  - NL: "kiểm tra quota claude" / "probe claude" / "claude còn
    available không" → `quota_status` (auto-probes if cache stale).

### Honest limitation

The Claude Code CLI does **not** expose remaining-quota %. The brain
tracks availability, parses errors, schedules retries, and pauses
tasks — it cannot show "X% remaining" because the upstream doesn't.

## Vietnamese Natural Language v1 changes

- **`bot/agent/nl_router.classify(text)`** — deterministic Vietnamese
  intent classifier. No LLM calls. Recognises:
  `chat / search / status / list_tasks / create_code_task /
  run_next_code_task / run_code_batch / show_files / send_file /
  receive_file_context / make_prompt / refine_prompt /
  grant_permission / revoke_permission / confirm_action /
  cancel_action / quota_schedule / quota_status / self_improve`.
  19/19 representative cases pass.
- **Telegram plain-text path** (`bot/telegram_bot.py`):
    1. legacy `detect_intent` (precision file picker)
    2. **new** `classify` → `_handle_nl_intent`
    3. legacy `_looks_like_task` task router
    4. backend chat fallback
  All NL replies use `send_chat_reply` (NEW message), never edit the
  menu panel.
- **Inline confirm buttons** — `confirm:<action_id>` and
  `cancel:<action_id>` callback patterns. The classifier marks
  `.env` / `storage_state` / `restart` / `merge main` etc. as
  `requires_confirm=True`; a Yes/No keyboard is sent and the action
  only runs after the admin taps ✅ Đồng ý.
- **Vietnamese plain-text shortcuts** — `đồng ý / ok làm đi / cho phép`
  confirms the latest pending_action; `hủy / không / đừng` cancels it.
- **Auto-run via NL** — "làm tiếp task code tiếp theo" calls
  `coding_worker_bridge.run_once()` directly; "chạy 2 task tiếp theo"
  calls `run_batch(2)`. No /code_worker_run_once needed.
- **Vietnamese run-result formatter** (`_vi_format_run_result`) —
  status dictionary maps each bridge state into VN text.

Examples (admin types in Telegram):

| Câu thường | Bot làm |
|---|---|
| `làm tiếp task code tiếp theo` | gọi bridge `run_once()`, báo VN |
| `tạo task code sửa lỗi menu` | tạo code_task + dựng prompt sẵn |
| `sửa file .env giúp t` | gửi nút ✅ Đồng ý / ❌ Hủy trước khi làm |
| `claude hết quota, 3 tiếng nữa chạy 1 task` | hẹn quota + autorun=1 |
| `quota claude sao rồi` | trả về `claude_quota.status_summary()` |
| `cấp quyền low_medium 2 tiếng` | `grant_session low_medium 120` |
| `đồng ý` | duyệt pending_action mới nhất |
| `hủy` | hủy pending_action mới nhất |
| `xem file gần đây` | inbox listing |
| `xem agent đang lỗi gì` | `/agent_status` dashboard VN |

## Autonomous Control Bridge v1 changes

- **`bot/coding_worker_bridge.py`** — detects `claude` / `codex` CLI on
  PATH, validates non-interactive support (`claude --print`,
  `codex exec`), and runs the next queued code_task end-to-end:
  invoke CLI → capture sanitised log → smoke + evals → unstage any
  forbidden path (`.env`, `storage_state`, runtime DBs, `.tar.gz`,
  `.log`, `id_rsa`, `.pem`, cookies) → commit on green → push with
  inline-token URL → reset remote URL → mark task done.
- **`bot/claude_quota.py`** — admin-driven quota scheduler at
  `data/claude_quota.json`. Set reset time via `/claude_quota_reset` or
  `/claude_quota_in`. Daemon thread (60s tick) sends one notification
  when due, then optionally fires `run_batch(max_tasks)` if autorun
  is on AND a non-interactive CLI exists.
- **File Hub 2-way upgrades** — inbox is now archived under
  `data/telegram/inbox/YYYY/MM/DD/`. Files >50 MB are refused. Stronger
  block-pattern list (`id_rsa`, `.pem`, `.key`, `.p12`, `auth.json`,
  `credential`). New allowed-send roots: `data/code_prompts`,
  `data/code_worker_logs`, `docs`, `research`, `generated`. New
  commands: `/code_task_from_file <id> <desc>`, `/run_task_with_file`.
  `/send_file` and `/send_photo` now accept either a file_id from
  `/files` or an absolute path. Brain-side helpers
  `bot.telegram_report.send_telegram_file()` and
  `send_telegram_photo()` enforce the same allow/block lists.
- **Telegram commands** added:
  - `/code_worker_run_once`, `/code_worker_run_batch <n>`,
    `/code_worker_status`, `/code_worker_pause`, `/code_worker_resume`
  - `/claude_status`, `/claude_quota_reset <YYYY-MM-DD HH:MM>`,
    `/claude_quota_in <duration>`, `/claude_limited`,
    `/claude_available`, `/claude_autorun_on [n]`, `/claude_autorun_off`
  - `/agent_autonomy_status` — coding tool + bridge + sessions +
    quota + queue + pending + git remote sanity
  - `/code_task_from_file`, `/run_task_with_file`

### Manual setup still required

The bridge **cannot drive Claude/Codex without the CLI installed and
authenticated**. Today this VPS has neither binary on PATH, so
`/code_worker_run_once` correctly returns `no_tool` with the install
instructions. To enable autonomous coding, install one of:

```
npm install -g @anthropic-ai/claude-code   # preferred
claude login                                # interactive once
```

or

```
npm install -g @openai/codex
codex login                                 # interactive once
```

After that, `/code_worker_status` will show the CLI as detected and
`/code_worker_run_once` will execute the next queued task end-to-end.

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
