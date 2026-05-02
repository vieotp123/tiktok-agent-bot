# Roadmap

Tracked sequentially. The agent's `/agent_next` command surfaces the
**first unchecked** item below.

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

## Now

- [ ] Verify real product catalog — replace placeholder prices on the 2 `active`
  products with the actual muaesim.vn price list. Use
  `/product_update <id> | price_vnd=… | price_jpy=… | notes=verified-YYYY-MM-DD`
  then `/product_verify <id>`.

## Next (strategic)

- [ ] SEO / Marketing engine v0 — keyword research worker (free Google Trends
  + DDG), competitor crawler (rotated UA), landing-page brief generator.
  Output stored as code_tasks for the content_factory.
- [ ] Content / image factory v0 — caption writer (cx/gpt-5.5), image
  generator (placeholder; pluggable), TikTok post drafter that creates
  a `pending_action` for admin review before publishing.
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
