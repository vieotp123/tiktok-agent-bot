# Operating Rules — Business Agent Platform

These are **enforced safety rules** for any worker (TikTok, Telegram, future
SEO/content/browser/OCR/image-gen workers) and any developer touching the code.

## 1. Secrets

- Never log tokens, API keys, cookies, or `storage_state` content.
- Never commit `.env`, `*.env`, `tiktok_storage_state.json`,
  `storage_state.json`, backups, or `*.log`.
- Never echo secrets back into chat replies, system prompts, or audit log.
- The audit logger (`bot/agent/audit_log.py`) silently strips keys whose
  name matches `token / password / secret / key / api_key / auth`.

## 2. LLM gateway

- All LLM calls **must** go through `bot/llm_client.complete(role=...)`.
  No direct OpenAI/Anthropic SDK use elsewhere.
- Never use a coding model for normal chat. Roles separate intent from model.
  - chat / tiktok_chat / telegram_chat / search_summary → fast chat model
    (`cx/gpt-5.5`)
  - reasoning / coding → best-available Claude (`cc/claude-sonnet-4-7` if
    listed; otherwise `cc/claude-sonnet-4-6`)
  - critic → `cx/gpt-5.3-codex`
  - vision → `openai/gpt-4o`
  - cheap / fallback → `openai/gpt-4o-mini`
- ENV overrides for any role must be validated against `/models` before use.

## 3. Channels

- **Telegram** is the admin command center. Only `TELEGRAM_ADMIN_CHAT_ID`
  may use the menu and callbacks. Unauthorized callbacks must not edit
  the admin menu.
- **TikTok** is **Chatgibiti-only**: the worker reads + replies inside
  the active conversation only. It must not:
  - DM users we have not been DM'd by.
  - Post on the public feed, comment, or like.
  - Follow, unfollow, or message other accounts.
- All public actions and any customer outreach require `/confirm_action`.

## 4. Sales replies

- The product catalog has three statuses: `active`, `needs_update`,
  `disabled`.
  - `active` may be quoted with price + features to customers.
  - `needs_update` may **never** be quoted as confirmed in customer-facing
    replies. Admin-side replies show them with explicit warnings.
  - `disabled` must never be suggested anywhere.
- **Never invent prices.** If no active product matches, say so clearly
  ("Hiện gói khớp chưa có trong danh mục đã xác nhận…").
- Customer-facing tone (`audience="customer"`) is polite, no mày/tao,
  no admin jargon, no product ids.
- Admin-facing tone (`audience="admin"`) is direct and exposes ids,
  confidence, and warnings.

## 5. Memory hygiene

- `raw_events` is append-only and **must never** be inlined into LLM prompts.
- Prompt context is built only via `build_memory_context()` with hard
  caps: 8 items, 6000 chars.
- `payload_json` from raw events is excluded from prompt context.
- Lessons are injected as few-shot examples only when the same skill
  re-runs.
- Skill notes require human review before activation.

## 6. Risk levels and confirm flow

Skills declare a risk level in `bot/agent/skill_registry.py`:

| Level  | Behavior                                                    |
|--------|-------------------------------------------------------------|
| low    | run immediately, audit-log entry on completion              |
| medium | run + audit-log entry; surface visibly in `/audit_recent`   |
| high   | create `pending_action`, require `/confirm_action <id>`     |

High-risk examples: `restart_service`, `send_tiktok_dm`, `git_commit`,
`deploy`. The Telegram menu wraps Restart Bot in a `confirm:` callback
that requires explicit "Yes, restart" press.

## 7. Git workflow

- Active branch: `dev-agent`. Merge to `main` only after admin review.
- Stage explicit paths: `git add bot docs README.md .gitignore`.
- Never `git add -A` or `git add .` (would catch runtime DBs / .env).
- Runtime DBs are gitignored; verify via `git status` before commit.
- Force-push to `main` is forbidden.

## 8. Logs

Every menu render and every callback emits a structured log line:

```
[telegram] menu mode=edit message_id=… view=…
[telegram] menu mode=send message_id=… view=…
[telegram] callback data=… chat_id=… msg_id=…
[telegram] pending_input=… chat_id=…
[telegram] menu edit failed reason=…
```

Backend logs every consult with intent, confidence, products_used, and
lead score. No customer message content is logged beyond a 60-char
summary.

## 9. Telegram File Hub (2-way)

- **Receive**: any document/photo/audio/video/voice from
  `TELEGRAM_ADMIN_CHAT_ID` is downloaded via `getFile`, saved under
  `data/telegram/inbox/YYYY/MM/DD/`, indexed in
  `data/telegram/files.jsonl`, and acknowledged with a `file_id`.
  Hard 50 MB cap per upload; uploads are never executed.
- **Send (bot side)**: `/send_file <file_id|path>` and
  `/send_photo <file_id|path>`. `file_id` is resolved against the
  inbox index; `path` must resolve under one of the allowed roots:
  `data/telegram/inbox`, `data/code_prompts`, `data/code_worker_logs`,
  `reports`, `screenshots`, `generated`, `docs`, `research`.
- **Send (brain side)**: `bot.telegram_report.send_telegram_file(path)`
  and `send_telegram_photo(path)` — same allow-list, same block-list,
  enforced by `bot.telegram_files.is_safe_send_path`.
- **Always blocked by filename pattern** (regardless of root):
  `.env`, `storage_state`, `tiktok_storage_state`, `cookies`,
  `private_key`, `secret`, `token`, `id_rsa`, `.pem`, `.key`, `.p12`,
  `auth.json`, `credential`, `.log`.
- `data/telegram/` is gitignored — uploads and the JSONL index are
  never committed.

## 10. Failure handling

- If a phase or test fails twice, stop and report the root cause.
  Don't loop indefinitely.
- Commit changes only when tests pass. Push only when commits are clean.
- After any service restart, verify `[read] baseline seen=N` appears in
  `tiktok-bot` logs within 10 seconds — silent extractor failure is the
  #1 historical regression.
