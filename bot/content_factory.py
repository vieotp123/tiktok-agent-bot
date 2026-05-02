"""
Content / image factory worker v0 — caption writer + image-brief stub.

Generates a Vietnamese TikTok post draft from a free-text brief:
  - caption        — written by the chat model (`cx/gpt-5.5` via 9Router,
                     role="chat" per `bot/llm_client.py` policy).
  - hashtags       — extracted from the caption response.
  - image_brief    — deterministic placeholder describing composition /
                     subject / style / palette so a future image-gen
                     backend can be plugged in without prompt churn.

The worker NEVER posts. It only drafts. Per `docs/OPERATING_RULES.md` §3
any actual TikTok post is `risk=high` and must go through
`/confirm_action`. This module returns the draft and (optionally) writes
it to `data/content_factory/<ts>_<slug>.json` for admin review.

CLI:

    /opt/tiktok-bot/venv/bin/python3 -m bot.content_factory \\
        "eSIM Nhật 7 ngày 5GB cho khách du lịch"

Cross-references:
- `docs/SELF_OPERATING_AGENT.md` §5 worker roles + §6 model policy.
- `docs/CLAUDE_CODE_WORKER.md` §3 hard rules — no public action.
- `docs/OPERATING_RULES.md` §4 sales replies — never invent prices; if
  the brief mentions a price, the prompt explicitly tells the model to
  quote it verbatim and not invent alternatives.
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

from bot.llm_client import complete

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "content_factory"

CAPTION_MAX_CHARS = 280
HASHTAG_RE = re.compile(r"#[\wÀ-ỹ\d_]+", re.UNICODE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(seed: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", (seed or "").strip().lower(), flags=re.U)
    s = re.sub(r"[\s-]+", "-", s).strip("-")
    return (s or "post")[:n]


def _build_caption_prompt(brief: str, lang: str, audience: str) -> str:
    tone = (
        "lịch sự, thân thiện, không dùng mày/tao, không jargon admin"
        if audience == "customer"
        else "ngắn gọn, trực tiếp, có thể có cảnh báo / id sản phẩm"
    )
    rules = (
        "- KHÔNG bịa giá. Nếu brief có giá thì giữ NGUYÊN. "
        "Nếu không có giá thì đừng nhắc giá.\n"
        "- KHÔNG hứa hẹn tính năng không có trong brief.\n"
        f"- Caption ≤ {CAPTION_MAX_CHARS} ký tự (gồm cả emoji).\n"
        "- 1 dòng CTA cuối (call-to-action) gọn gàng.\n"
        "- 3–6 hashtag liên quan, viết liền, đặt cuối caption."
    )
    return (
        f"Bạn là copywriter TikTok cho thương hiệu eSIM Nhật. "
        f"Viết caption ngôn ngữ {lang}, giọng văn {tone}.\n\n"
        f"Brief:\n{brief.strip()}\n\n"
        f"Yêu cầu:\n{rules}\n\n"
        f"Trả về DUY NHẤT phần caption (không tiêu đề, không giải thích)."
    )


def _extract_hashtags(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in HASHTAG_RE.findall(text):
        tag = m.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out[:8]


def _strip_trailing_hashtags(text: str) -> str:
    if not text:
        return ""
    # Remove a trailing block of hashtags so caption_body reads cleanly.
    return re.sub(
        r"(\s*#[\wÀ-ỹ\d_]+)+\s*$", "", text.strip(), flags=re.UNICODE
    ).strip()


async def write_caption(
    brief:    str,
    lang:     str = "vi",
    audience: str = "customer",
    timeout:  float = 30.0,
) -> dict[str, Any]:
    """Write a TikTok caption for `brief` using the chat model.

    Returns:
        {"caption": str, "caption_body": str, "hashtags": [str],
         "model": str, "error": False}
      | {"caption": "", "hashtags": [], "error": True,
         "error_detail": str}

    Empty/whitespace brief returns a clean error envelope, never raises.
    """
    brief = (brief or "").strip()
    if not brief:
        return {
            "caption":      "",
            "caption_body": "",
            "hashtags":     [],
            "model":        "",
            "error":        True,
            "error_detail": "empty_brief",
        }

    prompt = _build_caption_prompt(brief, lang=lang, audience=audience)
    res = await complete(
        [{"role": "user", "content": prompt}],
        role="chat",
        temperature=0.8,
        max_tokens=400,
        timeout=timeout,
        source="content_factory",
    )
    if res.get("error"):
        return {
            "caption":      "",
            "caption_body": "",
            "hashtags":     [],
            "model":        res.get("model", ""),
            "error":        True,
            "error_detail": res.get("error_detail", "llm_error"),
        }

    caption = (res.get("content") or "").strip()
    caption = caption[: CAPTION_MAX_CHARS + 200]  # safety cap
    return {
        "caption":      caption,
        "caption_body": _strip_trailing_hashtags(caption),
        "hashtags":     _extract_hashtags(caption),
        "model":        res.get("model", ""),
        "error":        False,
    }


def generate_image_brief(brief: str, lang: str = "vi") -> dict[str, Any]:
    """Deterministic placeholder image brief.

    Real image generation is intentionally NOT wired in v0 — see
    `docs/ROADMAP.md` "Content / image factory v0 — image generator
    (placeholder; pluggable)". Returns a structured spec a future
    backend (DALL·E / SDXL / pluggable) can consume.
    """
    seed = (brief or "").strip()
    return {
        "subject":     seed[:120] or "eSIM Nhật Bản",
        "composition": "center subject, rule-of-thirds, vertical 9:16",
        "style":       "modern, clean, soft daylight, lifestyle photo",
        "palette":     ["#E60012", "#FFFFFF", "#1F1F1F", "#F5F5F7"],
        "negative":    "no text overlay, no watermark, no logo",
        "lang":        lang,
        "backend":     "placeholder",
        "generated":   False,
    }


async def draft_post(
    brief:    str,
    lang:     str = "vi",
    audience: str = "customer",
) -> dict[str, Any]:
    """Build a full TikTok post draft: caption + image_brief + meta.

    The result is suitable for serialising to JSON for admin review and
    later hand-off to a publisher (which is `risk=high` and must go via
    `/confirm_action`).
    """
    brief = (brief or "").strip()
    cap = await write_caption(brief, lang=lang, audience=audience)
    img = generate_image_brief(brief, lang=lang)
    return {
        "brief":        brief,
        "lang":         lang,
        "audience":     audience,
        "caption":      cap.get("caption", ""),
        "caption_body": cap.get("caption_body", ""),
        "hashtags":     cap.get("hashtags", []),
        "image_brief":  img,
        "model":        cap.get("model", ""),
        "generated_at": _now_iso(),
        "error":        cap.get("error", False),
        "error_detail": cap.get("error_detail", ""),
    }


def save_post(
    post: dict[str, Any], out_dir: Path | None = None,
) -> Path:
    """Persist a draft to `data/content_factory/<ts>_<slug>.json`.

    The output directory is gitignored — drafts never leak into commits.
    """
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{ts}_{_slug(post.get('brief', 'post'))}.json"
    path = out_dir / fname
    path.write_text(
        json.dumps(post, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def format_post_vi(post: dict[str, Any]) -> str:
    """Telegram-friendly Vietnamese summary (HTML, ≤3500 chars)."""
    if not (post or {}).get("brief"):
        return "⚠ Content factory: thiếu brief."
    if post.get("error"):
        return (
            f"⚠ Content factory lỗi: "
            f"<code>{post.get('error_detail', 'unknown')}</code>"
        )
    lines = [
        f"<b>📝 Caption draft</b> — <code>{post['brief'][:60]}</code>",
        "",
        post.get("caption", "") or "(empty caption)",
    ]
    tags = post.get("hashtags") or []
    if tags:
        lines.append("")
        lines.append("<b>Hashtags:</b> " + " ".join(tags))
    img = post.get("image_brief") or {}
    if img:
        lines.append("")
        lines.append("<b>🖼 Image brief (placeholder):</b>")
        lines.append(f"  • subject: {img.get('subject', '')[:80]}")
        lines.append(f"  • style:   {img.get('style', '')[:80]}")
        lines.append(f"  • backend: {img.get('backend', '')}")
    model = post.get("model") or ""
    if model:
        lines.append("")
        lines.append(f"<i>model={model}</i>")
    return "\n".join(lines)


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bot.content_factory",
        description="Content factory v0 — caption writer + image brief.",
    )
    p.add_argument("brief", nargs="+", help="Free-text brief.")
    p.add_argument("--lang", default="vi")
    p.add_argument("--audience", default="customer",
                   choices=("customer", "admin"))
    p.add_argument("--no-save", action="store_true",
                   help="Skip writing data/content_factory/<ts>_<slug>.json.")
    p.add_argument("--json", action="store_true",
                   help="Print JSON instead of the Vietnamese summary.")
    args = p.parse_args(argv)

    brief = " ".join(args.brief).strip()
    post = asyncio.run(draft_post(
        brief, lang=args.lang, audience=args.audience,
    ))
    if not args.no_save:
        try:
            path = save_post(post)
            post["_saved_to"] = str(path)
        except Exception as e:
            post["_save_error"] = str(e)

    if args.json:
        print(json.dumps(post, ensure_ascii=False, indent=2))
    else:
        print(format_post_vi(post))
        if "_saved_to" in post:
            print(f"\nSaved → {post['_saved_to']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
