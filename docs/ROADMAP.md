# Roadmap

Tracked sequentially. The agent's `/agent_next` command surfaces the
**first unchecked** item below. Autorun (`làm tới khi hết quota`)
walks down `## Now` then `## Next` skipping items it has already
attempted in this session.

## Done

- [x] TikTok Chatgibiti DM worker with sender identification
- [x] Telegram command center with inline menu
- [x] 9Router LLM gateway with role-based model selection
- [x] Task queue + skill registry + audit log
- [x] Multi-tier memory store (`agent_memory.db`)
- [x] Product DB + CRM (`business.db`)
- [x] Sales consult engine (DB-grounded, never invents prices)
- [x] Telegram persistent menu UX (rebuild-at-bottom + edit-in-place)
- [x] HTML parse fallback in send/edit
- [x] getUpdates offset persistence (no more drop on restart)
- [x] Code task queue (`bot/code_tasks.py`)
- [x] Planner / executor / risk / self_check / worker_roles modules
- [x] Backup + smoke + deploy + rollback scripts
- [x] Task lifecycle state machine (`bot/agent/task_lifecycle.py`)
- [x] Structured planner v2 with per-step risk/test/expected_output
- [x] Prompt builder for Claude/Codex coding sessions
- [x] Eval harness (72→272 evals across 9+ categories) wired to `/agent_evals`
- [x] `/agent_status` + `/agent_metrics` observability dashboards
- [x] Permission session grants (`/grant_session` / `/revoke_session`)
- [x] Self-improve run-once loop (`/self_improve_once`)
- [x] SEO / Marketing engine v0 — keyword research worker
      (Google Autocomplete + DDG, no key) — `bot/seo_research.py`
- [x] Content / image factory v0 — caption writer + image-brief stub
- [x] Owner Command Agent v2 — Vietnamese NL router with 30+ intents,
      autorun pump loop, GPT-5.5 prompt refinement, Claude CLI
      autonomous coding, quota-aware pause+resume
- [x] Tool registry v2 — `discover_skills()` (handler-presence audit),
      `compute_skill_stats()` (runs / success-rate / last-used / stale
      from the audit log), persistent admin overrides via
      `data/skill_overrides.json` (gitignored). `/skills` shows runs,
      success%, last-used, ⚠handler / ⚠stale flags. NL toggle
      ("tắt skill X" / "bật skill X") + `/skill_enable` /
      `/skill_disable`. Stale threshold: 14d. 18 evals locking
      discovery shape, stats bounds, NL classification, and override
      persistence. See `bot/agent/skill_registry.py`.

## Now — Brain capability layer (memory + tools + NL)

> Owner directive: focus autorun on the brain's command-understanding
> and self-improvement capability before adding more business
> features. The agent should be able to receive ANY admin instruction
> in natural Vietnamese and route it correctly.

- [x] Memory v2 v0 — content-hash dedup (`memories.content_hash`),
      `decay_unused_memories()` half-life decay, `lessons_for_retry()`
      failure-only recall wired into runner + `build_memory_context`
      (`skill_hint`, `is_retry`). Embedding-based semantic re-rank still
      pending — current search remains keyword-based.
      Touch `bot/memory_store.py`, `bot/memory.py`, `bot/agent/runner.py`.
      Evals: 13 new in `memory_v2` category (dedup, decay, retry recall).

- [x] NL router v3 — confidence calibration. Short stub directives
      (`claude` / `task` / `code` / `tự` / `làm` / `chạy`) now return
      an `ambiguous` intent with a "ý anh là X hay Y?" prompt instead
      of falling to chat where the backend would hallucinate from
      stale state. Capability questions (`có tool X không` / `bot làm
      được Y không` / `how to Z`) route to `build_missing_tool`
      automatically. New `explain_intent(text)` API surfaces a
      Vietnamese decision trail ("vì sao em hiểu thế") with the
      matched intent, confidence, risk, and translation. New
      `VN_EN_TABLE` + `translate_command()` deterministically maps
      pure-English imperatives (`stop` / `next` / `list tasks` /
      `list skills` / `remember` …) to their VN equivalents before
      `classify()`, no-op when diacritics are present. All in
      `bot/agent/nl_router.py`. 47 new evals in the `nl_router_v3`
      category (`v3_stub`, `v3_build`, `v3_tr`, `v3_tr_noop`,
      `v3_cross`, `v3_explain_*`, `v3_regress`, `v3_table_*`).
      `/agent_evals` total now 350/350 in ~3s.

- [x] Brain context builder — `bot/agent/brain_context.py`
      `build_admin_brain_context(query)` builds a compact 4000-char
      system block (5 most relevant `tg_admin` memories, last 10 audit
      entries, current `agent_autorun.state()`, active pending_actions).
      Wired into `backend/server.py::call_llm` only when
      `source == "telegram"` and `username.startswith("tg_admin")` so
      tiktok_chat / file_summary / search paths never see admin
      internals. Defensive imports + try/except keep chat alive on
      flaky DB; raw `payload_json` is never inlined. 9 new evals in
      `brain_context` category (return type, char cap, no payload_json
      leak, header-if-data, no-raise on empty/odd queries, wiring guard).

- [x] Skill auto-suggest — `bot/agent/intent_stats.py` records every
      admin NL intent (`record_intent` / `record_and_check`) with the
      classifier's `summary_vi` reason and last few raw-text samples
      to `data/intent_stats.json` (gitignored, atomic write). Wired
      into `bot/telegram_bot.py` next to the existing `nl_intent=...`
      log line, so every admin message bumps the counter. After N
      uses (default 5) the next admin message gets a "💡 Bro hay
      dùng X" tip — fires ONCE per intent (suppressed via
      `suggested_intents`); chat / unknown / ambiguous are excluded
      so fall-throughs never get suggested. New `intent_stats` eval
      category locks counter / threshold-once / no-suggest-for-chat /
      `top_intents` ordering / wiring presence.

- [ ] Memory NL surface — beyond `nhớ là …` / `quên cái …`, support
      `gắn tag X cho memory Y`, `xem memory liên quan task Z`,
      `học từ task này`, `lesson cho skill X`. Touch
      `bot/agent/nl_router.py`, `bot/telegram_bot.py` (handlers).

- [x] NL eval suite — automated weekly eval that runs 200+ admin
      phrases and reports classifier drift. Output → Telegram digest.
      Cohort lives at `data/nl_eval_cohort.jsonl` (≥200 entries) and is
      replayed by the `nl_cohort` category in `bot/agent/evals.py`.
      `bot/agent/weekly_jobs.py` wraps the replay in a per-ISO-week
      idempotent scheduler that pipes the digest to
      `bot.telegram_report.send_telegram_message`. CLI:
      `python -m bot.agent.weekly_jobs [--force] [--no-telegram]`.

## Next — Business features (after brain layer)

- [ ] Verify real product catalog — replace placeholder prices on the 2
      `active` products with the actual muaesim.vn price list. **Owner
      action required**: `/product_update <id> | price_vnd=… | …` then
      `/product_verify <id>`. The agent will NOT invent prices
      (Operating Rules §4) so this stays in `Next` until owner provides
      the data.

- [ ] Browser / OCR worker v0 — Playwright research worker that can
      read competitor sites for the SEO engine; OCR on customer-uploaded
      screenshots via a vision-role LLM call.

- [ ] Daily-jobs scheduler — daily catalog freshness check (which active
      products haven't been verified in N days), daily lead-followup digest,
      daily SEO crawl.

- [ ] Lead intelligence v1 — LLM-based lead scoring (replace the regex
      baseline), automatic followup scheduling that creates `pending_action`s
      rather than auto-sending DMs.

- [ ] Worker manager UI — `/workers` shows live heartbeats, last-seen,
      model usage / token spend per worker.

## Future / blocked

- [ ] Auto-DM follow-up to leads after `/confirm_action` (high risk;
  needs explicit approval policy + rate limit).
- [ ] Competitor scrape with login (would need credential vault — out of
  scope until secret management is added).
- [ ] Cross-channel CRM merge (Telegram + TikTok + email) — needs an
  email integration first.

## Operating-rule additions to consider

- [ ] Add a "no public action without `/confirm_action`" CI guard that
  greps source for direct send/post calls in non-bot files.
- [ ] Add per-worker token-budget limits so a runaway loop can't burn
  through the 9Router quota.
