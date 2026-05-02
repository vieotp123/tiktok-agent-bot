# SEO / Marketing Engine — v0

This is the v0 contract for the `seo_marketing` worker role. v0 covers
**only** keyword research with free public endpoints. Competitor
crawler (rotated UA) and landing-page brief generator are **future**
phases — not in v0.

Cross-references:

- `docs/SELF_OPERATING_AGENT.md` §5 (worker roles), §6 (model policy).
- `docs/OPERATING_RULES.md` §1 (secrets), §6 (risk levels).
- `docs/CLAUDE_CODE_WORKER.md` §3 (hard rules).
- `docs/ROADMAP.md` "Next (strategic)" — SEO / Marketing engine v0.

## Scope of v0

| In scope                                              | Out of scope (future) |
|-------------------------------------------------------|------------------------|
| Free Google Autocomplete suggestions (no API key)     | Google Trends Pulse / paid Trends API |
| DuckDuckGo top-K results via existing `search_web`    | Competitor site crawler with rotated UA |
| JSON artifact dump under `data/seo_research/`         | Landing-page brief generator |
| Vietnamese Telegram-friendly summary                  | Auto-handoff to content_factory queue |
| CLI invocation                                        | Scheduled daily SEO crawl |

## Module

`bot/seo_research.py`

Public API:

```python
from bot.seo_research import (
    fetch_google_suggestions,   # async — free Google Autocomplete
    research_keyword,           # async — combines suggestions + DDG
    save_research,              # sync — write JSON artifact
    format_research_vi,         # sync — Vietnamese Telegram summary
)
```

`research_keyword(seed, lang="vi", max_results=5)` returns:

```json
{
  "seed":         "eSIM Nhật",
  "lang":         "vi",
  "suggestions":  ["esim nhật bản", "esim nhật giá rẻ", "..."],
  "results": [
    {"title": "...", "url": "https://...", "snippet": "..."}
  ],
  "generated_at": "2026-05-02T12:34:56Z"
}
```

Best-effort by design: on network failure either field may be empty,
but the function never raises.

## CLI

The CLI saves an artifact under `data/seo_research/` (gitignored) and
prints a Vietnamese summary:

```bash
/opt/tiktok-bot/venv/bin/python3 -m bot.seo_research "eSIM Nhật"
```

JSON output (for downstream consumers):

```bash
/opt/tiktok-bot/venv/bin/python3 -m bot.seo_research --json "eSIM Nhật"
```

Skip saving:

```bash
/opt/tiktok-bot/venv/bin/python3 -m bot.seo_research --no-save "eSIM Nhật"
```

## Risk classification

Per `bot/agent/risk.py` and `docs/OPERATING_RULES.md` §6 the worker is
risk=**low**:

- Read-only over public HTTP endpoints (Google Autocomplete, DDG).
- No write to product DB, leads, or audit log.
- No customer outreach. No public posting.
- No `.env` / `storage_state` access.

The corresponding skill is registered in
`bot/agent/skill_registry.py` as `seo_research` (enabled, risk=low,
handler=`research_keyword`).

## Storage

- Output directory: `data/seo_research/<UTC-ts>_<slug>.json`.
- Listed in `.gitignore` — artifacts are never committed.
- Filenames sort chronologically (UTC `YYYYMMDDTHHMMSSZ` prefix).

## Hand-off to content_factory (future)

v0 produces JSON artifacts. The content_factory worker (still a
placeholder per `bot/agent/worker_roles.py`) will later read these
artifacts and queue caption / landing-page draft tasks via
`bot.code_tasks` — all human-reviewed before any public action, per
`docs/OPERATING_RULES.md` §3.

## Testing

```bash
/opt/tiktok-bot/venv/bin/python3 -m py_compile bot/seo_research.py
/opt/tiktok-bot/venv/bin/python3 -m bot.agent.evals --category seo
bash scripts/smoke_test.sh
```

The eval suite locks: module hygiene, slug helper, suggestion-parser
robustness, sentinel filtering, the registered skill metadata, and the
worker role status. See `bot/agent/evals.py::eval_seo`.
