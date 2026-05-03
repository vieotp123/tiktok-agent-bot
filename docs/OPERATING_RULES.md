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

## 10. Owner tooling doctrine

The admin is the platform owner. **Never** respond to an owner request
with a generic "không có quyền". The doctrine is:

1. **Tool exists, action low/medium-risk and within session grant**
   → execute immediately, audit-log entry.
2. **Tool exists, action high-risk** → send the inline ✅ Đồng ý /
   ❌ Hủy keyboard. The action only fires when admin taps ✅.
3. **Tool missing** → queue a code_task that BUILDS the tool. Reply
   in Vietnamese: *"🛠 Tool này chưa có, em sẽ tạo task để tích hợp."*
   Then the Claude Opus 4.7 worker can pick it up.
4. **Hard safety violation** (BLOCKED bucket below) → explain the exact
   rule and offer a safe alternative. Do not execute even with confirm.

## 11. Remote-worker SSH safety

`bot/remote_workers.py` enforces:

- Key auth ONLY. `BatchMode=yes`, `PasswordAuthentication=no`.
- Keys must live under `/opt/tiktok-bot/keys/` and be `chmod 600`.
- Hosts must be explicitly registered via `/worker_add`.
- `data/remote_workers.json` and `keys/` are gitignored — never
  committed.
- Output line-scrubbed for `GITHUB_TOKEN` / `Bearer` / `ghp_*` /
  `sk-*` / `BEGIN OPENSSH PRIVATE KEY` / `password=` BEFORE leaving
  the executor.
- Hard 64 KB output cap, 30 s default timeout.

### SSH command risk classifier

| Risk      | Behaviour                                                 |
|-----------|-----------------------------------------------------------|
| 🛑 blocked | Never run, even with admin confirm. Includes `mkfs`, `dd if=`, `curl/wget … \| bash`, `cat .env / ssh keys / cookies / storage_state`, `ufw disable`, `iptables -F`, `scp`/`rsync` to remote |
| 🔴 high    | Inline ✅ Đồng ý / ❌ Hủy required. Includes `apt install/remove`, `pip install`, `reboot`, `useradd`, `passwd`, `visudo`, `firewalld`, `rm -rf`, `chmod 777`, redirect to `/etc/`, `drop table`, unknown command |
| 🟡 medium  | Session grant or per-command confirm. Includes `systemctl restart/start/stop`, `docker restart`, `git pull`, `mkdir`, `apt update` |
| 🟢 low     | Auto-runs with audit log. Includes `uptime`, `whoami`, `df -h`, `ls`, `journalctl`, `docker ps`, `git status` |

Unknown commands default to **high** — admin must confirm.

## 12. Failure handling

- If a phase or test fails twice, stop and report the root cause.
  Don't loop indefinitely.
- Commit changes only when tests pass. Push only when commits are clean.
- After any service restart, verify `[read] baseline seen=N` appears in
  `tiktok-bot` logs within 10 seconds — silent extractor failure is the
  #1 historical regression.

## 13. Auto-DM follow-up to leads — prerequisites (currently blocked)

Auto-initiating a TikTok DM to a lead — even one we previously talked to
— is a stated **hard non-goal** today (see
`docs/SELF_OPERATING_AGENT.md` §1) and a **high-risk** action under the
risk classifier. `docs/CLAUDE_CODE_WORKER.md` §3 also forbids any code
task from running a public action (DM, post, comment, follow). The
roadmap entry ("Auto-DM follow-up to leads after `/confirm_action`") is
therefore parked in *Future / blocked* until ALL of the following are
in place. None of these may be skipped, even with admin confirm.

1. **One `/confirm_action` per outgoing DM.** A single confirmation
   never authorises a batch. Each DM that the agent proposes to send
   creates its own `pending_action` via
   `bot.agent.permissions.create_pending` with the recipient's
   `sender_key`, the exact draft text, and the lead's last-seen
   timestamp included in `metadata`. The DM only fires after the admin
   taps ✅ on that specific entry.
2. **Hard rate limit, enforced server-side.** Maximum 1 follow-up DM
   per `sender_key` per 14 days, and ≤5 follow-up DMs total per UTC
   day across all leads. The limiter must live next to the sender (not
   in the planner) so that even a buggy planner cannot exceed it.
3. **Quiet-hours window.** No follow-up DM may be queued or sent
   outside 09:00–21:00 in the lead's local time (default JST when
   unknown). Pending actions created outside that window must auto-
   expire after 24h without confirmation.
4. **Conversation recency requirement.** The lead's most recent
   inbound message must be within 30 days. Older leads require a fresh
   inbound before any follow-up can be queued; cold outreach to dormant
   leads stays a stated non-goal.
5. **Opt-out tracking.** A lead-level `dm_optout=true` flag (added to
   `bot/business_store.py::leads`) blocks all future follow-ups. Any
   inbound message containing `stop`, `unsubscribe`, `huỷ`, `không
   nhắn nữa` (case-insensitive) sets the flag automatically and is
   audit-logged.
6. **Audit trail.** Every confirmed-and-sent follow-up DM writes one
   entry to `data/audit/actions.jsonl` via `bot.agent.audit_log` with
   `action="auto_dm_followup_sent"`, `sender_key`, `lead_id`,
   `pending_action_id`, and a 60-char summary of the draft (NEVER the
   full text or any PII beyond the sender_key).
7. **Kill switch.** `data/auto_dm_followup_enabled` (a single-byte
   file containing `1` or `0`, gitignored) is checked on every send.
   Default **off**. The admin must flip it to `1` via a Telegram
   command that itself is risk=high (so it ALSO requires
   `/confirm_action`).
8. **Eval coverage.** `bot/agent/evals.py` must include cases that
   verify (a) the rate limiter rejects the 2nd DM in a 14-day window,
   (b) the quiet-hours guard rejects out-of-window sends, (c) an
   opt-out flag blocks send regardless of admin confirm, and (d) the
   kill switch defaults off after a fresh deploy.

Until §1–8 are all implemented, reviewed, and locked in evals, no
worker (Telegram, planner_executor, code_worker, content_factory) may
add code that initiates a TikTok DM. The only sanctioned reply path
remains the Chatgibiti-only loop in `bot/tiktok_bot.py`, which replies
inside an active conversation we were DM'd into first (see §3).

The risk classifier in `bot/agent/risk.py` matches `auto-dm`,
`follow-up.*lead`, and any goal carrying an explicit `(high risk`
annotation. Any future roadmap item touching this area must therefore
flow through `bot.agent.permissions.create_pending` — not through a
queued `code_task`.

## 14. Per-worker token-budget limits

A *runaway loop* is any worker that keeps emitting LLM calls past its
intended unit of work — a stuck retry, a planner that re-plans the
same goal, an autorun cycle that fails to converge. The 9Router quota
is shared across every worker, so one runaway can starve the TikTok
responder, the Telegram admin loop, and the code worker at once. To
prevent that, every worker MUST respect the per-call, per-cycle, and
per-day budgets below. Cross-references:
`docs/SELF_OPERATING_AGENT.md` §6 (model policy) and §14 (failure
handling); `docs/CLAUDE_CODE_WORKER.md` §3 (hard rules).

### 14.1 Per-call ceilings (hard cap)

`bot.llm_client.complete(role=..., max_tokens=N)` — `N` must NEVER
exceed the role's documented ceiling. The numbers below are the caps
in force today and the upper bound any new caller must stay under.

| Role             | `max_tokens` cap | Typical use                       |
|------------------|------------------|-----------------------------------|
| `chat`           | 600              | Telegram / TikTok normal reply    |
| `telegram_chat`  | 600              | Telegram admin reply              |
| `tiktok_chat`    | 600              | TikTok DM reply                   |
| `search_summary` | 600              | DDG result summarisation          |
| `reasoning`      | 3500             | planner / supervisor LLM explain  |
| `coding`         | 3500             | `prompt_builder` LLM refine       |
| `critic`         | 1500             | code review pass                  |
| `vision`         | 1000             | OCR / image description           |
| `cheap`          | 400              | one-shot cheap fallback           |

A caller that needs more MUST either split the work across multiple
calls or queue a `code_task` with `risk≥medium` so the admin sees it
in `/audit_recent`. No silent inflation.

### 14.2 Per-cycle budget (soft cap, per autorun cycle)

One autorun cycle = one `coding_worker_bridge` invocation = one
Claude/Codex CLI session for one `code_task`. The contract:

- chat-grade calls (`chat` / `telegram_chat` / `tiktok_chat` /
  `search_summary`) — **≤ 8 calls / cycle**.
- `reasoning` / `coding` calls — **≤ 4 calls / cycle**.
- `critic` calls — **≤ 2 calls / cycle**.

If a single cycle blows past either ceiling, the bridge MUST record
the cycle as `budget_exceeded` (a critical status under
`bot/agent/supervisor.py::CRITICAL_STATUSES`). The existing drift
halt (>1 critical fail in 6 non-pause cycles) then stops the loop on
the second consecutive blow-out — see `docs/SELF_OPERATING_AGENT.md`
§14.

### 14.3 Per-day budget (soft cap, per worker role)

A 24h rolling window per role:

- chat-grade roles combined — **≤ 800 calls / day**.
- `reasoning` / `coding` combined — **≤ 200 calls / day**.
- `critic` — **≤ 80 calls / day**.

These numbers track the historical 9Router quota with ~30 % headroom.
Anything above is by definition a runaway and MUST page the admin via
`bot.telegram_report` with a summary that fits in 200 chars and never
includes the request payload.

### 14.4 How to enforce

1. **Static — read this section before adding ANY new `complete()`
   call.** A reviewer (human or `/ultrareview`) MUST reject a PR that
   adds an LLM call without an explicit `max_tokens=` argument or
   that raises a role's cap.
2. **Runtime — `bot/agent/supervisor.py::check_drift()` already
   halts an autorun after >1 critical failure in 6 non-pause cycles.**
   `budget_exceeded` is a critical status, so the existing halt
   covers the per-cycle ceiling automatically once a worker begins
   emitting that status.
3. **External — quota errors from 9Router (HTTP 429 / response field
   `quota_limited`) are PAUSE statuses, never critical.** Autorun
   pauses and resumes per the rule in
   `docs/SELF_OPERATING_AGENT.md` §14, never hard-stops. This
   protects against legitimate quota throttling without conflating
   it with a buggy worker.

### 14.5 Escape hatch

A genuine multi-step research goal that needs more than the
per-cycle ceiling MUST be queued as a `code_task` with `risk=medium`
and the description MUST contain the literal substring
`(token-budget waiver)`. The supervisor surfaces such tasks in
`/audit_recent` so the admin can audit them after the fact. No
silent overrides; no waiver implies blanket future approval.
