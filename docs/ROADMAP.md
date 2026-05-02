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

- [ ] Tool registry v2 — auto-discover registered skills at startup,
      surface them in `/skills` with risk/last-used/success-rate, allow
      admin to enable/disable a skill via NL ("tắt skill X"), warn when
      a skill hasn't been used in N days. Touch
      `bot/agent/skill_registry.py`, `bot/agent/runner.py`.
      Add evals: skill discovery, enable/disable NL, stats.

- [ ] NL router v3 — confidence calibration (don't fall to chat when
      a partial pattern matched; ask "ý anh là X hay Y?"), unknown-
      intent → propose `build_missing_tool` automatically, intent
      explanation API ("vì sao em hiểu thế"), and a deterministic
      VN->EN intent translation table for cross-cultural commands.
      Touch `bot/agent/nl_router.py`. Add 30+ new evals covering
      ambiguous phrases.

- [ ] Brain context builder — when answering a free-form admin
      question (chat fallback), inject the most relevant 3-5 memories,
      last 10 audit entries, current autorun state, and current
      pending_actions into the LLM system prompt. Touch
      `backend/server.py` chat path. Add evals: context relevance.

- [ ] Skill auto-suggest — on every admin message, log the chosen
      intent and a short reason; surface "Bro hay dùng X" suggestions
      after N uses. Build `bot/agent/intent_stats.py`. Add evals.

- [ ] Memory NL surface — beyond `nhớ là …` / `quên cái …`, support
      `gắn tag X cho memory Y`, `xem memory liên quan task Z`,
      `học từ task này`, `lesson cho skill X`. Touch
      `bot/agent/nl_router.py`, `bot/telegram_bot.py` (handlers).

- [ ] NL eval suite — automated weekly eval that runs 200+ admin
      phrases and reports classifier drift. Output → Telegram digest.
      Touch `bot/agent/evals.py`. Add eval cohort file
      `data/nl_eval_cohort.jsonl`.

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
