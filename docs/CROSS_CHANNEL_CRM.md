# Cross-Channel CRM Merge — Design (blocked on email channel)

This is the design doc for the *Cross-channel CRM merge (Telegram +
TikTok + email)* roadmap item. It is **blocked**: implementation cannot
start until the email channel exists. This doc captures the target
model so the future code_task can be minimal.

Cross-references:

- `docs/SELF_OPERATING_AGENT.md` §3 (task lifecycle), §4 (risk policy),
  §7 (memory policy).
- `docs/OPERATING_RULES.md` §3 (channels), §4 (sales replies),
  §6 (risk levels).
- `docs/CLAUDE_CODE_WORKER.md` §3 (hard rules).
- `docs/ROADMAP.md` — *Future / blocked* (this item).

## Problem

`bot/business_store.py` keys every lead by `(platform, sender_key)` with
a `UNIQUE` constraint:

```sql
CREATE TABLE leads (
    id            TEXT PRIMARY KEY,
    platform      TEXT NOT NULL DEFAULT '',   -- 'telegram' | 'tiktok' | (future) 'email'
    sender_key    TEXT NOT NULL DEFAULT '',
    ...
    UNIQUE(platform, sender_key)
);
```

The same human reaching us on Telegram and TikTok therefore appears as
two unrelated rows. `consulting_logs`, `conversations`, and `followups`
all join through `(platform, sender_key)` or `lead_id`, so any
per-customer view today is per-channel.

The merge must give the agent a single customer record across all
channels — without inventing a join when the link is uncertain.

## Why blocked

The roadmap entry is *Telegram + TikTok + email*. Email is not wired:
no inbox poller, no `platform='email'` rows, no IMAP/SMTP credentials
in `.env`, no email worker role in
`bot/agent/worker_roles.py::list_worker_roles()`. Designing the merge
without the third channel produces a 2-platform stub that has to be
rewritten when email lands.

A separate roadmap item must add the email worker first. Only then
does this item move from *Future / blocked* into *Now*.

## Target data model

Add a thin identity table; do **not** drop `(platform, sender_key)`.

```sql
CREATE TABLE IF NOT EXISTS lead_identities (
    person_id     TEXT NOT NULL,        -- stable, prefix 'pers_'
    lead_id       TEXT NOT NULL,        -- FK → leads.id
    confidence    REAL NOT NULL,        -- 0..1; manual = 1.0
    source        TEXT NOT NULL,        -- 'manual' | 'phone' | 'email' | 'llm'
    created_at    TEXT NOT NULL,
    PRIMARY KEY (person_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_lead_identities_lead ON lead_identities(lead_id);
```

Rules:

- Existing `leads` rows stay one-per-channel. The merge is overlay-only.
- A `person_id` is created lazily the first time two leads are linked.
- `confidence < 0.9` requires admin `/confirm_action` before merge —
  this is high-risk per `docs/OPERATING_RULES.md` §6 (mistakes leak
  one customer's chat history into another's view).
- Unmerge is a row delete on `lead_identities`; the underlying leads
  are never deleted by the merge.

## Public API (target shape)

`bot/business_store.py` gains four functions, all sync, all idempotent:

```python
def link_leads(lead_ids: list[str], *, source: str, confidence: float) -> str: ...
def unlink_lead(lead_id: str) -> bool: ...
def get_person(lead_id: str) -> dict | None: ...   # {person_id, lead_ids, confidence}
def list_person_conversations(person_id: str, limit: int = 50) -> list[dict]: ...
```

`list_person_conversations` is the only new read path: it unions
`conversations` across every `lead_id` mapped to `person_id`, ordered
by `created_at`. Existing per-channel queries are unchanged.

## Match signals (priority order)

1. **Manual** — admin types `/lead_link <id1> <id2>` or NL
   *"gộp lead X với Y"*. `confidence = 1.0`, `source = 'manual'`.
2. **Phone** — when present in `need_summary` or a future `contacts`
   table; normalized to E.164. Auto-merge at `confidence = 0.95`.
3. **Email** — exact match between an `email` lead's `sender_key` and
   a phone-or-email mention in another channel's history. Auto-merge
   at `confidence = 0.9`.
4. **LLM hint** — only as a *suggestion* surfaced in `/audit_recent`,
   never auto-merge. Stored with `source = 'llm'` and
   `confidence < 0.9`, gated by `/confirm_action`.

Username similarity alone is **not** a signal. TikTok and Telegram
handles collide too easily.

## Risk classification

Per `bot/agent/risk.py` and `docs/SELF_OPERATING_AGENT.md` §4:

| Action                                  | Risk    | Notes                                      |
|-----------------------------------------|---------|--------------------------------------------|
| `get_person`, `list_person_conversations` | low   | read-only                                  |
| `link_leads(source='manual')`           | medium  | audit-logged, surfaced in `/audit_recent`  |
| `link_leads(source in {phone,email})`   | medium  | auto-run, audit-logged                     |
| `link_leads(source='llm')` auto         | high    | `pending_action`; admin `/confirm_action`  |
| `unlink_lead`                           | medium  | audit-logged                               |

The hard kill-list in `docs/SELF_OPERATING_AGENT.md` §12 is unchanged.

## Phased plan

| Phase | Owner          | Output                                                |
|-------|----------------|-------------------------------------------------------|
| P0    | (this doc)     | Design captured. No code change. Item stays blocked.  |
| P1    | code_task      | Email worker (`platform='email'`, IMAP poll, role wiring). Unblocks P2. |
| P2    | code_task      | `lead_identities` schema + `link_leads` / `unlink_lead` / `get_person` / `list_person_conversations` + evals. |
| P3    | code_task      | NL handlers (*"gộp lead X với Y"*) + `/lead_link` / `/lead_unlink` Telegram commands. |
| P4    | code_task      | Auto-link rules (phone, email) with audit-log entries. |
| P5    | code_task      | LLM-suggested merges as `pending_action`s.            |

Each phase is one commit, one logical change, per
`docs/CLAUDE_CODE_WORKER.md` §2.

## Out of scope for every phase

- Editing `bot/tiktok_bot.py` (Playwright reader). The merge is a DB
  layer; the readers stay channel-local.
- Auto-DM follow-ups across channels (still in *Future / blocked* per
  the roadmap; needs the rate-limit + approval policy first).
- Public posting of any kind.
- Any change to `(platform, sender_key)` keys on existing tables —
  rename/drop would break every cross-table join.

## Testing (when P2 lands)

```bash
/opt/tiktok-bot/venv/bin/python3 -m py_compile bot/business_store.py
/opt/tiktok-bot/venv/bin/python3 -m bot.agent.evals
bash scripts/smoke_test.sh
```

Eval coverage to add in P2:

- `link_leads` is idempotent and creates one `person_id` for N leads.
- `unlink_lead` removes only that lead's mapping, not the person.
- `list_person_conversations` unions across channels and orders by
  `created_at`.
- High-risk paths (`source='llm'` auto) route through
  `bot.agent.permissions.create_pending`.
