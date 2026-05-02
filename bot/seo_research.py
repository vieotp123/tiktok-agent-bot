"""
SEO research worker v0 — free keyword research.

Combines Google Autocomplete (free public endpoint, no API key) and the
existing DuckDuckGo HTML scraper (`bot.tools.search_web`) to surface
candidate keywords + competing pages for a seed term. Outputs a
structured dict that the content_factory worker can consume.

Read-only over external endpoints. Never writes to product DB or
audit log. Per `docs/OPERATING_RULES.md` §1 it never logs secrets, and
per §6 it is risk=low (pure read-only research).

CLI:

    /opt/tiktok-bot/venv/bin/python3 -m bot.seo_research "eSIM Nhật"

Cross-references:
- `docs/SELF_OPERATING_AGENT.md` §5 worker roles + §6 model policy.
- `docs/CLAUDE_CODE_WORKER.md` §3 hard rules — this module never edits
  TikTok reader or `.env`.
- `docs/SEO_MARKETING_ENGINE.md` describes the v0 contract and the
  planned content_factory hand-off.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from bot.tools import search_web

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "seo_research"

GOOGLE_SUGGEST_URL = "https://suggestqueries.google.com/complete/search"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(seed: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", (seed or "").strip().lower(), flags=re.U)
    s = re.sub(r"[\s-]+", "-", s).strip("-")
    return (s or "seed")[:n]


async def fetch_google_suggestions(
    seed: str, lang: str = "vi", timeout: float = 8.0,
) -> list[str]:
    """Return up to 10 Google Autocomplete suggestions for `seed`.

    Uses the free public endpoint that Firefox's URL bar talks to. No
    API key is required. Empty list on any error — this worker is
    best-effort and never raises.
    """
    seed = (seed or "").strip()
    if not seed:
        return []
    params = {"client": "firefox", "q": seed, "hl": lang}
    headers = {"User-Agent": UA, "Accept-Language": f"{lang},en;q=0.9"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(GOOGLE_SUGGEST_URL, params=params, headers=headers)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    if (isinstance(data, list) and len(data) >= 2
            and isinstance(data[1], list)):
        out: list[str] = []
        seen: set[str] = set()
        for x in data[1]:
            if not isinstance(x, str):
                continue
            v = x.strip()
            if v and v.lower() != seed.lower() and v not in seen:
                seen.add(v)
                out.append(v)
        return out[:10]
    return []


async def research_keyword(
    seed: str, lang: str = "vi", max_results: int = 5,
) -> dict[str, Any]:
    """Run keyword research for `seed`. Returns a structured dict with:

        seed, lang, generated_at, suggestions, results, error?

    `suggestions` come from Google Autocomplete. `results` come from the
    existing DDG search scraper. Sentinel rows (`search_error`,
    `no_results`) from `search_web` are filtered out so consumers see a
    clean list. The function never raises on network failure — it
    returns whatever it could fetch.
    """
    seed = (seed or "").strip()
    if not seed:
        return {
            "seed":         "",
            "lang":         lang,
            "suggestions":  [],
            "results":      [],
            "generated_at": _now_iso(),
            "error":        "empty_seed",
        }

    suggestions, raw_results = await asyncio.gather(
        fetch_google_suggestions(seed, lang=lang),
        search_web(seed, max_results=max_results),
    )

    results: list[dict[str, str]] = []
    for r in (raw_results or []):
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        if title in ("search_error", "no_results", ""):
            continue
        results.append({
            "title":   title,
            "url":     (r.get("url") or "").strip(),
            "snippet": (r.get("snippet") or "").strip(),
        })

    return {
        "seed":         seed,
        "lang":         lang,
        "suggestions":  suggestions,
        "results":      results,
        "generated_at": _now_iso(),
    }


def save_research(
    result: dict[str, Any], out_dir: Path | None = None,
) -> Path:
    """Persist `result` to JSON under `data/seo_research/`. Returns path.

    Output directory is gitignored (see `.gitignore`). Filename is
    `<UTCts>_<slug>.json` so artifacts sort chronologically.
    """
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{ts}_{_slug(result.get('seed', 'seed'))}.json"
    path = out_dir / fname
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def format_research_vi(result: dict[str, Any]) -> str:
    """Vietnamese summary suitable for Telegram (HTML, ≤3500 chars)."""
    if not (result or {}).get("seed"):
        return "⚠ SEO research: thiếu seed keyword."
    lines = [f"<b>🔎 SEO research</b> — <code>{result['seed']}</code>"]
    sugg = result.get("suggestions") or []
    if sugg:
        lines.append("\n<b>Gợi ý từ khóa (Google):</b>")
        for s in sugg[:8]:
            lines.append(f"  • {s}")
    res = result.get("results") or []
    if res:
        lines.append("\n<b>Top web results (DDG):</b>")
        for r in res[:5]:
            t = (r.get("title") or "")[:80]
            u = r.get("url") or ""
            lines.append(f"  • {t} — {u}")
    if not sugg and not res:
        lines.append("\nKhông có dữ liệu — kiểm tra mạng / endpoint.")
    return "\n".join(lines)


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bot.seo_research",
        description="SEO keyword research worker v0 (free, no API key).",
    )
    p.add_argument("seed", nargs="+", help="Seed keyword(s).")
    p.add_argument("--lang", default="vi")
    p.add_argument("--max-results", type=int, default=5)
    p.add_argument("--no-save", action="store_true",
                   help="Skip writing data/seo_research/<ts>_<slug>.json.")
    p.add_argument("--json", action="store_true",
                   help="Print JSON instead of the Vietnamese summary.")
    args = p.parse_args(argv)

    seed = " ".join(args.seed).strip()
    result = asyncio.run(research_keyword(
        seed, lang=args.lang, max_results=args.max_results,
    ))
    if not args.no_save:
        try:
            path = save_research(result)
            result["_saved_to"] = str(path)
        except Exception as e:
            result["_save_error"] = str(e)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_research_vi(result))
        if "_saved_to" in result:
            print(f"\nSaved → {result['_saved_to']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
