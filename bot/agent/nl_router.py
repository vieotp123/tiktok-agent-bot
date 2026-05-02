"""
Natural-language Telegram intent router (Vietnamese-first).

Scope of v1: deterministic recognition of file-send requests like
"gửi file ROADMAP cho t" / "cho t xem CURRENT_STATUS". Returns a
single intent dict that the Telegram dispatch loop can act on
without an LLM round-trip.

Larger intent set (run worker, create code task, quota schedule,
permissions) is planned but intentionally NOT implemented here —
ship one slice end-to-end first.

Public API:
  detect_intent(text) -> dict | None
  resolve_file_request(query) -> list[dict]
  handle_send_file_intent(intent, chat_id) -> str  (Vietnamese reply)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO = Path("/opt/tiktok-bot")

# Vietnamese trigger phrases for "send me file X". Matched
# case-insensitively. The capture group holds the file-name query.
_SEND_PATTERNS = [
    re.compile(r"\bg(?:ử|u)i\s+file\s+(.+?)(?:\s+(?:cho|gi(?:ú|u)p)\s+(?:t(?:ôi)?|m(?:ình)?))?\s*[\.!\?]?$",
               re.IGNORECASE),
    re.compile(r"\bg(?:ử|u)i\s+(.+?)\s+(?:cho|gi(?:ú|u)p)\s+(?:t(?:ôi)?|m(?:ình)?)\s*[\.!\?]?$",
               re.IGNORECASE),
    re.compile(r"\bcho\s+(?:t(?:ôi)?|m(?:ình)?)\s+xem\s+(?:file\s+)?(.+?)\s*[\.!\?]?$",
               re.IGNORECASE),
    re.compile(r"\bxem\s+file\s+(.+?)\s*[\.!\?]?$", re.IGNORECASE),
    re.compile(r"\b(?:đ|d)(?:ọ|o)c\s+file\s+(.+?)\s*[\.!\?]?$", re.IGNORECASE),
]

# Known doc aliases — friendly Vietnamese / shorthand → canonical path.
# Resolution is case-insensitive. Add carefully: only files inside
# telegram_files.ALLOWED_SEND_ROOTS will actually go out.
_DOC_ALIASES: dict[str, str] = {
    "current_status":         "docs/CURRENT_STATUS.md",
    "currentstatus":          "docs/CURRENT_STATUS.md",
    "current status":         "docs/CURRENT_STATUS.md",
    "trạng thái":             "docs/CURRENT_STATUS.md",
    "trang thai":             "docs/CURRENT_STATUS.md",
    "status":                 "docs/CURRENT_STATUS.md",
    "roadmap":                "docs/ROADMAP.md",
    "operating_rules":        "docs/OPERATING_RULES.md",
    "operating rules":        "docs/OPERATING_RULES.md",
    "rules":                  "docs/OPERATING_RULES.md",
    "claude_code_worker":     "docs/CLAUDE_CODE_WORKER.md",
    "claude code worker":     "docs/CLAUDE_CODE_WORKER.md",
    "self_operating_agent":   "docs/SELF_OPERATING_AGENT.md",
    "self operating agent":   "docs/SELF_OPERATING_AGENT.md",
}


def detect_intent(text: str) -> dict | None:
    """Return a structured intent dict if `text` matches a known
    natural-language pattern, else None.

    Currently recognises only `send_file`. Returns:
        {"intent": "send_file", "query": "<raw query>", "confidence": float}
    """
    s = (text or "").strip()
    if not s:
        return None
    for pat in _SEND_PATTERNS:
        m = pat.search(s)
        if m:
            q = m.group(1).strip().strip('.,!?;:"\'')
            if q:
                return {"intent": "send_file", "query": q,
                        "confidence": 0.9}
    return None


def _norm(q: str) -> str:
    return q.lower().strip().strip('.,!?;:"\'')


def resolve_file_request(query: str) -> list[dict]:
    """Resolve a natural-language file query to candidate paths.

    Returns a list of {label, path, source} dicts. Empty list = no
    confident match. Multiple = ambiguous, caller should ask user
    to disambiguate.
    """
    q = _norm(query)
    if not q:
        return []
    matches: list[dict] = []
    seen: set[str] = set()

    # 1) Exact alias hit (highest confidence).
    if q in _DOC_ALIASES:
        path = str(REPO / _DOC_ALIASES[q])
        if os.path.isfile(path):
            matches.append({"label": _DOC_ALIASES[q], "path": path,
                            "source": "alias"})
            seen.add(path)
            return matches

    # 2) Substring hit against alias keys (handles partial doc names
    #    like "current_status" written as "current status hôm nay").
    for alias, rel in _DOC_ALIASES.items():
        if alias in q or q in alias:
            path = str(REPO / rel)
            if path not in seen and os.path.isfile(path):
                matches.append({"label": rel, "path": path,
                                "source": "alias_substring"})
                seen.add(path)

    # 3) Direct filename hit inside docs/ (case-insensitive). Lets
    #    "ROADMAP.md" / "roadmap.md" / "roadmap" all resolve.
    docs_dir = REPO / "docs"
    if docs_dir.is_dir():
        ql = q.replace(" ", "_").lower()
        for f in docs_dir.iterdir():
            if not f.is_file():
                continue
            stem = f.stem.lower()
            if stem == ql or stem.startswith(ql) or ql in stem:
                p = str(f)
                if p not in seen:
                    matches.append({"label": f"docs/{f.name}",
                                    "path": p, "source": "docs_glob"})
                    seen.add(p)

    # 4) Recent uploaded files in the inbox (label = filename).
    try:
        from bot.telegram_files import list_files
        for rec in list_files(20):
            fn = (rec.get("filename") or "").lower()
            if not fn:
                continue
            if q in fn or fn.startswith(q):
                p = rec.get("local_path", "")
                if p and p not in seen and os.path.isfile(p):
                    matches.append({
                        "label": f"inbox: {rec['filename']}",
                        "path": p, "source": "inbox",
                        "file_id": rec.get("file_id"),
                    })
                    seen.add(p)
    except Exception:
        pass

    return matches


async def handle_send_file_intent(intent: dict, chat_id: str | int) -> str:
    """Execute a recognised send_file intent. Replies in Vietnamese.

    Caller (telegram_bot.dispatch) should use this when
    detect_intent(text) returns an `intent=send_file`.
    """
    from bot.telegram_files import is_safe_send_path
    from bot.telegram_bot import send_document, send_photo

    query = intent.get("query", "").strip()
    candidates = resolve_file_request(query)

    if not candidates:
        return (f"🤔 Mình chưa tìm thấy file nào khớp với "
                f"<b>{query}</b>.\n"
                f"Thử tên khác (ví dụ: <i>ROADMAP</i>, "
                f"<i>CURRENT_STATUS</i>, <i>OPERATING_RULES</i>) "
                f"hoặc dùng <code>/files</code> để xem danh sách.")

    if len(candidates) > 1:
        lines = [f"🔎 Có {len(candidates)} file khớp với "
                 f"<b>{query}</b>. Chọn rõ hơn nha:"]
        for c in candidates[:6]:
            lines.append(f"• <code>{c['label']}</code>")
        if len(candidates) > 6:
            lines.append(f"… và {len(candidates) - 6} file khác.")
        return "\n".join(lines)

    target = candidates[0]
    path   = target["path"]
    label  = target["label"]

    # Defense in depth — let telegram_files's allow/block list
    # decide. The resolver already restricts to safe roots, but if
    # someone adds a future alias outside the allow-list we still
    # refuse to send.
    ok, reason = is_safe_send_path(path)
    if not ok:
        return (f"🚫 Không gửi được <b>{label}</b>.\n"
                f"Lý do: {reason}")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        sent = await send_photo(chat_id, path,
                                 caption=f"📤 {label}")
    else:
        sent = await send_document(chat_id, path,
                                    caption=f"📤 {label}")
    if sent:
        return f"✅ Đã gửi <b>{label}</b>."
    return f"❌ Gửi <b>{label}</b> thất bại — kiểm tra log."


# ─────────────────────────────────────────────────────────────────────────────
# v2 — full intent classifier for the Vietnamese command-center plain-text path.
#
# The legacy `detect_intent`/`handle_send_file_intent` above remains the
# preferred path for explicit "gửi file X" requests (it has tighter file
# disambiguation). The new `classify()` below is the catch-all router used
# by bot/telegram_bot.py whenever the legacy detector returns None.
#
# Deterministic-only: never calls an LLM (saves quota, audit-friendly).
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass as _dataclass
from typing import Optional as _Optional


@_dataclass(frozen=True)
class Intent:
    name:             str
    confidence:       float
    summary_vi:       str
    args:             dict
    risk_level:       str
    requires_confirm: bool


_RE = re.compile

_PATTERNS_RUN_NEXT = (
    _RE(r"\b(làm|chạy)\s+(tiếp|next|kế\s*tiếp)\s+(task|code)?", re.I),
    _RE(r"\bchạy\s+task\s+(tiếp|kế\s*tiếp|sau)\b", re.I),
    _RE(r"\bcho\s+claude\s+(chạy|làm)\b", re.I),
    _RE(r"\b(làm|chạy)\s+claude\b", re.I),
    _RE(r"\b(làm|chạy)\s+(task\s+)?code\s+tiếp", re.I),
    _RE(r"\brun_next_code_task\b", re.I),
    _RE(r"\brun\s+next\s+code", re.I),
    # "tự code tiếp đi" / "tự tìm lỗi rồi sửa" — owner tells the worker to
    # continue the next coding task. NO hour/duration → run_next.
    _RE(r"^\s*tự\s+code\s+tiếp\s*(đi|nha)?\s*[\.!\?]*$", re.I),
    _RE(r"\btự\s+(tìm|find)\s+(lỗi|bug)\s+(rồi|và)\s+(sửa|fix)\b", re.I),
    _RE(r"\btiếp\s*tục(?:\s+task|\s+code)?\b", re.I),
)

_PATTERNS_RUN_BATCH = (
    _RE(r"\bchạy\s+(\d+)\s+task", re.I),
    _RE(r"\b(\d+)\s+task\s+(tiếp|tiep|next)", re.I),
    _RE(r"\brun\s+batch\s+(\d+)", re.I),
)

_PATTERNS_CREATE_CODE = (
    _RE(r"\b(tạo|thêm|add|create)\s+(task\s+)?code\b", re.I),
    _RE(r"\bsửa\s+(code|lỗi|bug|file)\b", re.I),
    _RE(r"\b(viết|write)\s+(code|module|file|hàm|function)\b", re.I),
    _RE(r"\bthêm\s+(tính\s*năng|feature|chức\s*năng|skill)\b", re.I),
    _RE(r"\b(refactor|hoàn\s*thiện|cải\s*thiện)\s+(code|module|hàm)\b", re.I),
    _RE(r"\bfix\s+(bug|lỗi|menu|telegram|tele)\b", re.I),
    _RE(r"\b(implement|tạo)\s+(file|module|skill|worker)\b", re.I),
    _RE(r"\b(sửa|fix)\s+(menu|tele|telegram|bot)\b", re.I),
    # "tạo task <verb> ..." — generic create-task phrasing
    _RE(r"\btạo\s+task\s+(?!tiếp|kế|sau)\w+", re.I),
    _RE(r"\b(làm|build|tích\s*hợp)\s+(api|integration)\b", re.I),
    _RE(r"\bđọc\s+file\s+rồi\s+sửa\b", re.I),
)

_PATTERNS_QUOTA = (
    _RE(r"\b(claude|opus|sonnet)\s+(hết\s+(quota|quote)|limit|out\s+of)",
        re.I),
    _RE(r"\bhẹn\s+(\d+\s*(?:h|tiếng|giờ|m|phút))", re.I),
    _RE(r"\b(\d+\s*(?:h|tiếng|giờ|m|phút))\s+(nữa|sau)\s+(chạy|cho|run)",
        re.I),
    _RE(r"\bkhi\s+nào\s+(hồi|reset|về)\s+quota", re.I),
    # "hết quota thì hẹn chạy tiếp" / "khi có quota thì chạy tiếp"
    _RE(r"\bhết\s+quota\s+thì\s+(hẹn|chạy|\d|thử|probe)", re.I),
    _RE(r"\bkhi\s+(có|hồi|về)\s+quota\s+thì", re.I),
    _RE(r"\bquota\s+về\s+thì", re.I),
    # "1 tiếng thử lại" / "1h thử lại" / "60 phút probe lại"
    _RE(r"\b\d+\s*(tiếng|giờ|h|phút|m)\s+(thử|probe|chạy)\s+lại", re.I),
    _RE(r"\bcó\s+quota\s+thì\s+(tự\s+)?chạy\s+tiếp", re.I),
)

_PATTERNS_QUOTA_STATUS = (
    _RE(r"\bquota\s+(sao|còn|status|thế\s*nào|ra\s*sao)", re.I),
    _RE(r"\bclaude\s+(còn|sao\s*rồi|thế\s*nào)", re.I),
    _RE(r"\bquota\s+claude\b", re.I),
    _RE(r"\b/?claude_status\b", re.I),
    _RE(r"\b(kiểm\s*tra|check|test|probe)\s+(quota|claude)\b", re.I),
    _RE(r"\b/?claude_probe\b", re.I),
    _RE(r"\bclaude\s+(còn|đang)\s+(work|chạy\s+được|available)", re.I),
    # Yes/no question forms — admin asking "is claude limited?".
    # These must be QUOTA_STATUS (read state), NOT QUOTA_SCHEDULE.
    _RE(r"\bclaude\s+(đã\s+|bị\s+)?limit\s+(chưa|không|hay)", re.I),
    _RE(r"\bclaude\s+(có\s+)?limit\s+(không|hay)\b", re.I),
    _RE(r"\bclaude\s+(đã\s+)?hết\s+quota\s+(chưa|không)", re.I),
    _RE(r"\bclaude\s+(còn\s+)?(xài|dùng|chạy)\s+(được\s+)?(không|chưa)", re.I),
    _RE(r"\bclaude\s+available\s+(chưa|không)", re.I),
    _RE(r"\bclaude\s+(ok|okay|ổn)\s+(không|chưa)", re.I),
    _RE(r"\bclaude\s+(rảnh|free)\s+(chưa|không)", re.I),
    _RE(r"\bclaude\s+(còn\s+)?(work|hoạt\s+động)\s+(không|chưa)", re.I),
    _RE(r"\bclaude\s+(đang|hiện)\s+sao\b", re.I),
)

_PATTERNS_GRANT_PERM = (
    _RE(r"\bcấp\s+(quyền|session|grant)", re.I),
    _RE(r"\bgrant\s+(session|access|quyền)", re.I),
    _RE(r"\bcho\s+phép\s+\d+\s*(phút|tiếng|h|m|giờ)", re.I),
    _RE(r"\bfull\s+(quyền|access)\s+\d+", re.I),
)

_PATTERNS_REVOKE = (
    _RE(r"\bthu\s+hồi\s+quyền", re.I),
    _RE(r"\b(revoke|hủy)\s+(session|quyền)\b", re.I),
)

_PATTERNS_CONFIRM = (
    _RE(r"^(đồng\s*ý|đồng\s*ý\s*đi|cho\s+phép|ok\s+làm\s+đi|ok\s+chạy|"
        r"yes|y|đc|được|chấp\s*nhận|approve|confirm)\s*[\.!]*$", re.I),
)

_PATTERNS_CANCEL = (
    _RE(r"^(hủy|không|đừng|stop|cancel|no|n|khoan|đừng\s+làm|"
        r"dừng|dừng\s*lại|dừng\s*đi|đừng\s+làm\s+nữa)\s*[\.!]*$",
        re.I),
)

_PATTERNS_STATUS = (
    _RE(r"\b(kiểm\s*tra|check|xem)\s+(agent|trạng\s*thái|status|sức\s*khỏe)",
        re.I),
    _RE(r"\bagent\s+(lỗi|đang|status|sao)", re.I),
    _RE(r"\b/?agent_(status|health|metrics|autonomy_status)\b", re.I),
)

# Self-introspection intents — admin asks the agent about itself.

_PATTERNS_WHOAMI_MODEL = (
    _RE(r"\b(m|mày|bot|agent)\s+(đang\s+)?(dùng|chạy|sử\s*dụng|use)\s+"
        r"(model|llm|gì|cái\s+gì)", re.I),
    _RE(r"\bmodel\s+nào\s+(đang\s+)?(chạy|dùng|sử\s*dụng)", re.I),
    _RE(r"\b(agent|bot)\s+(đang\s+)?(chạy|dùng)\s+model\s+(gì|nào)", re.I),
    _RE(r"\b(m|mày|bot|agent)\s+(đang\s+)?là\s+(model|llm)\s+(gì|nào)", re.I),
    _RE(r"\bxem\s+model\s+đang\s+(chạy|dùng)", re.I),
)

_PATTERNS_WHOAMI_RUNTIME = (
    _RE(r"\b(m|mày|bot|agent)\s+(dùng|chạy|sử\s*dụng)\s+claude\s+cli\b", re.I),
    _RE(r"\bclaude\s+cli\s+hay\s+(9router|api|router)", re.I),
    _RE(r"\b9router\s+hay\s+claude\s+cli\b", re.I),
    _RE(r"\b(m|mày|bot|agent)\s+chạy\s+(qua\s+)?(đâu|cách\s+nào|kiểu\s+gì)", re.I),
    _RE(r"\b(m|mày)\s+(gọi|call)\s+(model|llm)\s+(qua|kiểu)", re.I),
    _RE(r"\bruntime\s+(của\s+)?(m|mày|agent|bot)", re.I),
    _RE(r"\b(m|mày|bot)\s+(dùng|chạy)\s+gì\s+để\s+code\b", re.I),
)

_PATTERNS_RECENT_ACTIVITY = (
    _RE(r"\b(m|mày|bot|agent)\s+(vừa|mới|đang|hiện)\s+làm\s+(gì|cái\s+gì)", re.I),
    _RE(r"\b(làm|chạy)\s+(cái\s+)?gì\s+(xong|rồi|đó)\b", re.I),
    _RE(r"\b(báo\s*cáo|recent|mới\s*nhất)\s+(activity|hoạt\s*động|"
        r"việc\s*làm)", re.I),
    _RE(r"\bxem\s+(việc\s+)?vừa\s+làm", re.I),
    _RE(r"\b(m|mày|agent)\s+(làm|chạy)\s+xong\s+(cái\s+)?gì", re.I),
)

_PATTERNS_NEXT_MISSION = (
    _RE(r"\bmục\s*(tiêu|đích)\s+(tiếp\s*theo|tiếp|kế\s*tiếp|next)", re.I),
    _RE(r"\bnext\s+(mission|task|goal)\b", re.I),
    _RE(r"\b(roadmap|kế\s*hoạch)\s+(kế\s*tiếp|tiếp\s*theo|tiếp)", re.I),
    _RE(r"\bsắp\s+làm\s+gì\b", re.I),
    _RE(r"\b(việc|task)\s+(tiếp\s*theo|kế\s*tiếp)\s+(là\s+)?gì", re.I),
    _RE(r"\bxem\s+roadmap\b", re.I),
    _RE(r"\b/?roadmap\b", re.I),
    _RE(r"\bmission\s+(của\s+)?(m|mày|agent)", re.I),
)

_PATTERNS_WHOAMI = (
    _RE(r"^\s*(m|mày|bot|agent)\s+là\s+(ai|gì|cái\s*gì)\s*[\?!\.]?$", re.I),
    _RE(r"\bmục\s*(đích|tiêu)\s+của\s+(m|mày|agent|bot)\s+(là\s+)?gì", re.I),
    _RE(r"\b(m|mày|bot|agent)\s+(được\s+)?train\s+để\s+(làm\s+)?(gì|cái\s+gì)",
        re.I),
    _RE(r"\b(giới\s*thiệu|introduce)\s+(bản\s*thân|m|mày|về\s+(m|mày|agent))",
        re.I),
    _RE(r"\b/?whoami\b", re.I),
    _RE(r"\bnhiệm\s*vụ\s+của\s+(m|mày|agent)\b", re.I),
)

# Diag-style intents — "agent đang kẹt ở đâu" / "diag"
_PATTERNS_DIAG = (
    _RE(r"\bagent\s+(đang\s+)?(kẹt|stuck|stall|treo|đứng)", re.I),
    _RE(r"\bxem\s+(diag|agent\s+diag)\b", re.I),
    _RE(r"\b/?agent_diag\b", re.I),
    _RE(r"\bbot\s+(đang\s+)?(kẹt|stuck|treo|đứng)", re.I),
    _RE(r"\bdiag\b", re.I),
)

_PATTERNS_LIST_TASKS = (
    _RE(r"\bcòn\s+(task|code)\s+(gì|nào)", re.I),
    _RE(r"\b(liệt\s*kê|xem|list)\s+(task|code)", re.I),
    _RE(r"\bcòn\s+gì\s+(chưa|cần)\s+(làm|chạy)", re.I),
)

_PATTERNS_SEARCH = (
    _RE(r"\b(tìm|search|google)\s+thông\s+tin", re.I),
    _RE(r"\btìm\s+(trend|xu\s*hướng|news|tin)", re.I),
)

_PATTERNS_SELF_IMPROVE = (
    _RE(r"\btự\s+(cải\s*thiện|improve|hoàn\s*thiện)\s+(brain|agent|bản\s*thân)?",
        re.I),
    _RE(r"\b(làm|cho)\s+(brain|agent)\s+(thông\s*minh|xịn|tốt\s*hơn)", re.I),
    _RE(r"\b/?self_improve(_once)?\b", re.I),
)

# Brain-Evolution loop — continuous self-improve until quota / stop
_PATTERNS_BRAIN_EVOLVE_START = (
    _RE(r"\bbrain\s*[_\s]?evolve\s*(start|begin)?\b", re.I),
    _RE(r"\bbắt\s*đầu\s+(brain\s*evolve|tự\s*cải\s*thiện)", re.I),
    _RE(r"\blàm\s+đến\s+khi\s+(hết|cạn)\s+quota", re.I),
    _RE(r"\b/?brain_evolve_start\b", re.I),
    # "tự cải thiện brain" (with the explicit word "brain") → loop, not
    # one-shot. Plain "tự cải thiện" without "brain" still hits the
    # legacy self_improve one-shot path.
    _RE(r"\btự\s*cải\s*thiện\s+brain\b", re.I),
)
_PATTERNS_BRAIN_EVOLVE_STOP = (
    _RE(r"\bdừng\s+(tự\s*cải\s*thiện|brain\s*evolve)", re.I),
    _RE(r"\bstop\s+brain\s*evolve\b", re.I),
    _RE(r"\b/?brain_evolve_stop\b", re.I),
    _RE(r"\bt(?:ao|ôi)?\s+dừng\s+thì\s+mới\s+dừng", re.I),
)
_PATTERNS_BRAIN_EVOLVE_STATUS = (
    _RE(r"\b(tiến\s*độ|status)\s+(tự\s*cải\s*thiện|brain\s*evolve)", re.I),
    _RE(r"\b/?brain_evolve_status\b", re.I),
    _RE(r"\bxem\s+brain\s*evolve\b", re.I),
)

# ── Agent autorun loop (10–12h owner work session) ──────────────────────────
_PATTERNS_AGENT_AUTORUN_START = (
    # "tự làm việc 12 tiếng" / "làm việc độc lập 10 tiếng"
    _RE(r"\b(tự|độc\s*lập)\s+(làm|làm\s*việc|code|chạy)\s+"
        r"\d+\s*(tiếng|giờ|h)\b", re.I),
    _RE(r"\blàm\s*việc\s+(độc\s*lập|tự\s*động)\s+\d+\s*(tiếng|giờ|h)", re.I),
    _RE(r"\b(làm|chạy|code)\s+\d+\s*(tiếng|giờ|h)\s+(không\s*ngừng|liên\s*tục)",
        re.I),
    # "cài code liên tục cho tới khi t bảo dừng"
    _RE(r"\b(cài|làm|chạy)\s+code\s+liên\s*tục\b", re.I),
    _RE(r"\b(làm|chạy|code)\s+(liên\s*tục|không\s*ngừng)\s+(tới|cho\s*tới|"
        r"đến)\s+(khi|lúc)\s+(t|tao|tôi|owner|admin)?\s*(bảo|nói|kêu)?\s*"
        r"(dừng|stop)", re.I),
    _RE(r"\b/?agent_autorun_start\b", re.I),
    _RE(r"\bautorun\s+(start|on|begin)\b", re.I),
    # "tự code 12 tiếng" (only when hours are present) — handled by the
    # \d+\s*tiếng patterns above. "tự code tiếp đi" without an hour
    # falls through to run_next_code_task / create_code_task.
)

# ── Admin shell-style intents — apt/restart/git/cat .env ─────────────────
# Used to fulfil owner-tooling doctrine: these phrases MUST get an
# explicit grant-gated or confirm reply, never a generic refusal.
_PATTERNS_SYSTEM_PKG_INSTALL = (
    _RE(r"\bapt\s+(install|remove|update|upgrade)\b", re.I),
    _RE(r"\b(npm|pip|pip3)\s+install\b", re.I),
)
_PATTERNS_SYSTEM_RESTART = (
    _RE(r"\brestart\s+(bot|service|tiktok|backend|nginx)", re.I),
    _RE(r"\bsystemctl\s+(restart|stop|start)\s+\S+", re.I),
    _RE(r"\b(khởi\s*động\s*lại)\s+(bot|service|tiktok|backend)", re.I),
)
_PATTERNS_GIT_COMMIT_PUSH = (
    _RE(r"\bcommit[/\s]+push\b", re.I),
    _RE(r"\bcommit\s+(và\s+|and\s+)?push\s+dev-?agent", re.I),
    _RE(r"\bpush\s+dev-?agent\b", re.I),
    _RE(r"\btest\s+xong\s+(đẩy|push)\s+git\b", re.I),
    _RE(r"\b/?commit_and_push\b", re.I),
)
_PATTERNS_DUMP_SECRET = (
    _RE(r"\bcat\s+\.env\b", re.I),
    _RE(r"\bcat\s+.*storage[_\s]?state", re.I),
    _RE(r"\bgửi\s+(?:tao|t|tôi|admin)\s+\.env", re.I),
    _RE(r"\bdump\s+(secret|env|token|key)\b", re.I),
    _RE(r"\bin\s+(token|secret|api[\s_]key)\s+ra\b", re.I),
)

_PATTERNS_AGENT_AUTORUN_STOP = (
    _RE(r"\b/?agent_autorun_stop\b", re.I),
    _RE(r"\bdừng\s+(autorun|làm\s*việc|tự\s*động)\b", re.I),
    _RE(r"\bstop\s+autorun\b", re.I),
    _RE(r"\b(đừng|không|không\s+cần)\s+làm\s+(nữa|thêm)\b", re.I),
)

_PATTERNS_AGENT_AUTORUN_STATUS = (
    _RE(r"\b/?agent_autorun_status\b", re.I),
    _RE(r"\b(xem|status)\s+autorun\b", re.I),
    _RE(r"\bautorun\s+(status|sao\s*rồi|còn\s+chạy)", re.I),
)

# ── Agent progress — "đang làm tới đâu rồi" / "sao im vậy" ──────────────────
_PATTERNS_AGENT_PROGRESS = (
    # "đang làm tới đâu rồi" / "làm tới đâu rồi" / "làm tới đâu" — đang optional
    _RE(r"\b(đang\s+)?làm\s+(tới|đến)\s+đâu(\s+(rồi|chưa))?\b", re.I),
    _RE(r"\bsao\s+im\s+(vậy|thế)\b", re.I),
    _RE(r"\bxem\s+tiến\s*độ\b", re.I),
    _RE(r"\b(kẹt|stuck)\s+(ở\s+)?đâu", re.I),
    _RE(r"\bagent\s+đang\s+lỗi\s+gì\b", re.I),
    _RE(r"\bbáo\s+tiến\s*độ\b", re.I),
    _RE(r"\b/?agent_progress\b", re.I),
    _RE(r"\bcòn\s+(task|việc)\s+gì\s+chưa\s+làm\b", re.I),
    _RE(r"\bxem\s+log\s+gần\s+(nhất|đây)\b", re.I),
    _RE(r"\btiến\s*độ\s+task\b", re.I),
    # "task chạy đến đâu rồi" / "task xong chưa"
    _RE(r"\btask\s+(đang\s+)?(chạy|làm)\s+(đến|tới)\s+đâu", re.I),
    _RE(r"\btask\s+xong\s+(chưa|rồi)\b", re.I),
)

# ── EsimAccess / API integration intents ────────────────────────────────────
_PATTERNS_ESIM_DOCS = (
    _RE(r"\bđọc\s+docs?\s+(esim\s*access|esimaccess)\b", re.I),
    _RE(r"\b(xem|read)\s+(docs?|tài\s*liệu)\s+(esim\s*access|esimaccess)",
        re.I),
    _RE(r"\bdocs?\s+esim\s*access\b", re.I),
)

_PATTERNS_ESIM_CURL_DRY = (
    _RE(r"\b(tạo|viết|sinh)\s+curl\s+(esim\s*access|esimaccess)\b", re.I),
    _RE(r"\b(test|thử)\s+(dry[-\s]?run|dryrun)\s+(api\s+)?esim", re.I),
    _RE(r"\bdry[-\s]?run\s+(api\s+)?esim", re.I),
    _RE(r"\bcurl\s+esim\s*access\s+(mẫu|dry)", re.I),
)

_PATTERNS_ESIM_ORDER_REAL = (
    _RE(r"\b(gọi|chạy|call)\s+api\s+(mua|order)\s+esim\s+thật\b", re.I),
    _RE(r"\b(mua|order)\s+esim\s+(thật|that|real)\b", re.I),
    _RE(r"\btạo\s+order\s+esim\b", re.I),
    _RE(r"\bcurl\s+esim\s*access\s+(mua|order)\b", re.I),
)

# ── System / network high-risk ("mở port 8080", "ufw allow ...") ───────────
_PATTERNS_SYSTEM_NETWORK_HIGH = (
    _RE(r"\bmở\s+port\s+\d+\b", re.I),
    _RE(r"\bopen\s+port\s+\d+\b", re.I),
    _RE(r"\bufw\s+(allow|enable|disable)\b", re.I),
    _RE(r"\biptables\s+-[A-Z]", re.I),
    _RE(r"\bsecurity\s+group\s+(allow|add|update)", re.I),
    _RE(r"\bnginx\s+(reload|restart)", re.I),
    _RE(r"\bexpose\s+(public|internet|service|port)\b", re.I),
)

# ── Web search / browser ────────────────────────────────────────────────────
_PATTERNS_WEB_SEARCH_NL = (
    _RE(r"\bsearch\s+web\s+(tìm|cho|về)\b", re.I),
    _RE(r"\btìm\s+(thông\s*tin|tin|nhà\s+cung\s+cấp|đối\s+thủ|news)\s+", re.I),
    _RE(r"\bdùng\s+api\s+search\b", re.I),
    _RE(r"\bweb\s+search\b", re.I),
    _RE(r"\b(google|duckduckgo|ddg)\s+tìm\b", re.I),
)

_PATTERNS_BROWSER_TASK = (
    _RE(r"\bmở\s+(trình\s*duyệt|browser)\s+(tìm|đọc|crawl)", re.I),
    _RE(r"\bcrawl\s+(trang|page|url|web)\b", re.I),
    _RE(r"\bđọc\s+(trang|page|url|web|docs?\s+api)\b", re.I),
    _RE(r"\b(mở|open)\s+browser\b", re.I),
    _RE(r"\bplaywright\s+(crawl|read|tìm)\b", re.I),
)

# Memory NL: "nhớ X" / "quên X" / "tìm trong memory X"
_PATTERNS_MEMORY_ADD = (
    _RE(r"^nhớ\s+(?:là\s+|rằng\s+)?(.+)$", re.I),
    _RE(r"^lưu\s+(?:lại\s+)?(?:nhớ\s+)?(.+)$", re.I),
    _RE(r"\bremember\s+(?:that\s+)?(.+)$", re.I),
    _RE(r"\b/?memory_add\b\s*(.+)?", re.I),
)
_PATTERNS_MEMORY_SEARCH = (
    _RE(r"\btìm\s+(?:trong\s+)?memory\s+(.+)$", re.I),
    _RE(r"\b/?memory_search\b\s*(.+)?", re.I),
    _RE(r"\b(xem|check)\s+memory\s+(.+)$", re.I),
)
_PATTERNS_MEMORY_FORGET = (
    _RE(r"\bquên\s+(?:cái\s+)?(.+)$", re.I),
    _RE(r"\b/?memory_forget\b\s*(.+)?", re.I),
)

_PATTERNS_SHOW_FILES = (
    _RE(r"\b(liệt\s*kê|xem|show|list)\s+(file|files)", re.I),
    _RE(r"\bfile\s+(gần\s*đây|recent|mới\s+nhất)\b", re.I),
)

# OCR run intents — admin asks to extract text from a saved image.
# Scoped so they DO NOT collide with `build_missing_tool` (which matches
# "thêm/tạo/cài tool ocr"). Build-tool patterns require a verb prefix;
# the patterns here trigger only on action-on-image phrasing or the
# explicit /ocr command.
_PATTERNS_OCR_RUN = (
    _RE(r"^/ocr\b", re.I),
    _RE(r"\bocr\s+(?:file\s+|image\s+|ảnh\s+)?[A-Za-z0-9_./\-]+", re.I),
    _RE(r"\b(đ|d)(ọ|o)c\s+(text|chữ|chu)\s+(?:trong\s+|từ\s+|tu\s+)?ảnh",
        re.I),
    _RE(r"\b(đ|d)(ọ|o)c\s+(text|chữ|chu)\s+(?:trong\s+|từ\s+|tu\s+)?anh",
        re.I),
    _RE(r"\bextract\s+text\s+(?:from\s+)?(?:image|photo|ảnh|anh)", re.I),
    _RE(r"\b(trích|trich)\s+(text|chữ|chu)\s+(?:trong\s+|từ\s+)?(ảnh|anh)",
        re.I),
    _RE(r"\bphân\s*tích\s+ảnh\s+\S+", re.I),
    _RE(r"\bphan\s*tich\s+anh\s+\S+", re.I),
    _RE(r"\bxem\s+(text|chữ|chu)\s+(?:trong\s+)?(ảnh|anh)\b", re.I),
    _RE(r"\bread\s+image\s+\S+", re.I),
)

# Owner-tooling doctrine: when admin asks for a capability we don't
# have, classify as build_missing_tool → create a code_task to build it
# instead of refusing. Patterns favour explicit "build a tool" phrasing.
_PATTERNS_BUILD_TOOL = (
    _RE(r"\b(thêm|tạo|cài|build|add|integrate)\s+(tool|công\s*cụ)\b", re.I),
    _RE(r"\b(thêm|tạo|cài|add|build)\s+(tool\s+)?(ssh|ocr|browser|"
        r"playwright|seo|scraper|crawler|vision|image[\s_]gen)\b", re.I),
    _RE(r"\b(tích\s*hợp|integrate)\s+(tool|công\s*cụ|module|skill)\b", re.I),
    _RE(r"\bvậy\s+m\s+cài\s+tool\b", re.I),
    _RE(r"\bm\s+cài\s+tool\b", re.I),
    _RE(r"\btạo\s+task\s+làm\s+tool\b", re.I),
    _RE(r"\bcài\s+tool\s+kết\s+nối\b", re.I),
    # "cho agent kết nối VPS khác" / "kết nối worker mới"
    _RE(r"\bkết\s+nối\s+(vps|worker|server|máy)\s+(khác|mới|thêm)", re.I),
    _RE(r"\b(thêm|onboard|add)\s+(vps|worker|server|máy)\s+(mới|thêm)", re.I),
)

# Remote-worker / SSH control intents.
_PATTERNS_REMOTE_WORKER_LIST = (
    _RE(r"\bxem\s+(remote\s+)?worker", re.I),
    _RE(r"\b(liệt\s*kê|list)\s+(remote\s+)?worker", re.I),
    _RE(r"\b/?workers_remote\b", re.I),
)
_PATTERNS_REMOTE_WORKER_HEALTH = (
    _RE(r"\b(kiểm\s*tra|check|test|health)\s+(remote\s+)?(vps|worker|server)\s*(\w+)?", re.I),
    _RE(r"\b(vps|worker|server)\s*(\w+)?\s+(còn\s+sống\s+không|alive|"
        r"đang\s+sống\s+không|sao\s*rồi)", re.I),
    _RE(r"\b/?worker_(test|health)\b", re.I),
)
_PATTERNS_SSH_EXEC = (
    _RE(r"\bssh\s+(\w+)\s+(.+)$", re.I),
    _RE(r"\bchạy\s+lệnh\s+trên\s+(vps|worker)\s*(\w+)?\s*[:\-]?\s*(.+)$", re.I),
    _RE(r"\b/?ssh_exec\b", re.I),
    _RE(r"\bxem\s+(dung\s*lượng|disk|df)\s+(?:trên|của)?\s*(vps|worker)\s*(\w+)", re.I),
    _RE(r"\b(restart|khởi\s*động\s*lại)\s+(bot|service)\s+(?:trên|bên)?\s*(vps|worker)\s*(\w+)", re.I),
)

_PATTERNS_HIGH_RISK = (
    _RE(r"(?:^|\s|/)\.env\b"),       # match `.env` even after whitespace
    _RE(r"\bstorage[_\s]?state\b", re.I),
    _RE(r"\btiktok_storage_state\b", re.I),
    _RE(r"\bsystemctl\b"),
    _RE(r"\brestart\s+(bot|service|tiktok)\b", re.I),
    _RE(r"\bxóa\s+(data|database|toàn\s+bộ)", re.I),
    _RE(r"\b(post|đăng)\s+(bài|tiktok|tweet)\b", re.I),
    _RE(r"\b(dm|gửi\s+dm)\s+(cho\s+)?khách", re.I),
    _RE(r"\bmerge\s+main\b", re.I),
    _RE(r"\bgit\s+push\s+main\b", re.I),
    _RE(r"\bdeploy\s+(prod|production)\b", re.I),
    # Network / firewall — public exposure
    _RE(r"\bmở\s+port\s+\d+\b", re.I),
    _RE(r"\bopen\s+port\s+\d+\b", re.I),
    _RE(r"\bufw\s+(allow|enable|disable)\b", re.I),
    _RE(r"\biptables\b", re.I),
    _RE(r"\bsecurity\s+group\b", re.I),
    _RE(r"\bnginx\s+(reload|restart|config)", re.I),
    _RE(r"\bexpose\s+(public|internet|service)\b", re.I),
    # Money / order / public DM
    _RE(r"\b(mua|order|thanh\s*toán|payment)\s+(esim|sim)\s+thật\b", re.I),
    _RE(r"\bgọi\s+api\s+(mua|order)\s+esim\s+thật\b", re.I),
    _RE(r"\btạo\s+order\s+esim\b", re.I),
    _RE(r"\bgửi\s+(dm|tin\s+nhắn|message)\s+(cho\s+)?khách", re.I),
    _RE(r"\b(public|đăng)\s+post\b", re.I),
    # Secret dump
    _RE(r"\bcat\s+\.env\b", re.I),
    _RE(r"\bdump\s+(secret|env|token)\b", re.I),
    _RE(r"\bin\s+(token|secret|api[\s_]key)\b", re.I),
)


_DURATION_RE = re.compile(
    r"(?P<h>\d+)\s*(?:h|tiếng|giờ)\s*(?:(?P<m>\d+)\s*(?:m|phút))?|"
    r"(?P<m_only>\d+)\s*(?:m|phút|min)",
    re.I,
)


def parse_duration_vi(text: str) -> _Optional[int]:
    """Return minutes parsed from a Vietnamese duration token, or None."""
    if not text:
        return None
    m = _DURATION_RE.search(text)
    if not m:
        return None
    if m.group("m_only"):
        return int(m.group("m_only"))
    return int(m.group("h") or 0) * 60 + int(m.group("m") or 0)


def is_confirm_phrase(text: str) -> bool:
    return bool(text) and any(p.search(text.strip()) for p in _PATTERNS_CONFIRM)


def is_cancel_phrase(text: str) -> bool:
    return bool(text) and any(p.search(text.strip()) for p in _PATTERNS_CANCEL)


def _has_any(text: str, patterns) -> bool:
    return any(p.search(text) for p in patterns)


def classify(text: str) -> Intent:
    """Deterministic Vietnamese intent classifier. Never calls an LLM."""
    if not text or not text.strip():
        return Intent("unknown", 0.0, "", {}, "low", False)

    t = text.strip()

    # 1. Confirm/cancel phrases — only when the message is JUST that
    if is_confirm_phrase(t):
        return Intent("confirm_action", 0.95,
                      "Xác nhận hành động đang chờ.", {}, "low", False)
    if is_cancel_phrase(t):
        return Intent("cancel_action", 0.95,
                      "Hủy hành động đang chờ.", {}, "low", False)

    high_risk = _has_any(t, _PATTERNS_HIGH_RISK)

    # 2. Quota intents — STATUS (yes/no questions) BEFORE SCHEDULE so
    #    "claude đã limit chưa" / "claude limit chưa" goes to read-state
    #    not schedule-retry.
    if _has_any(t, _PATTERNS_QUOTA_STATUS):
        return Intent("quota_status", 0.9,
                      "Xem trạng thái quota Claude.", {}, "low", False)
    if _has_any(t, _PATTERNS_QUOTA):
        mins = parse_duration_vi(t) or 0
        m2 = re.search(r"\bchạy\s+(\d+)\s+task", t, re.I)
        max_tasks = int(m2.group(1)) if m2 else 0
        return Intent("quota_schedule", 0.9,
                      "Hẹn lịch chạy lại Claude khi hồi quota.",
                      {"minutes": mins, "max_tasks": max_tasks,
                       "raw": t}, "low", False)

    # 3. Run-batch BEFORE run-next
    for p in _PATTERNS_RUN_BATCH:
        m = p.search(t)
        if m:
            try:
                n = int(m.group(1))
            except Exception:
                n = 1
            return Intent("run_code_batch", 0.9,
                          f"Chạy batch {n} task code tiếp theo.",
                          {"n": max(1, min(n, 3))}, "medium", False)

    if _has_any(t, _PATTERNS_RUN_NEXT):
        return Intent("run_next_code_task", 0.9,
                      "Chạy task code tiếp theo qua bridge.",
                      {}, "medium", False)

    if _has_any(t, _PATTERNS_CREATE_CODE):
        risk = "high" if high_risk else "medium"
        return Intent("create_code_task", 0.85,
                      "Tạo task code mới và queue cho worker.",
                      {"description": t}, risk,
                      requires_confirm=high_risk)

    # ── Owner-tooling doctrine (BEFORE ssh_exec; "thêm tool ssh" must
    #    create a build task, not be parsed as `ssh <something>`) ───────
    if _has_any(t, _PATTERNS_BUILD_TOOL):
        return Intent("build_missing_tool", 0.9,
                      "Tạo code_task để build tool còn thiếu.",
                      {"description": t}, "medium", False)

    # ── OCR run (after build_missing_tool so "thêm tool ocr" still
    #    routes to build, not run) ─────────────────────────────────────
    if _has_any(t, _PATTERNS_OCR_RUN):
        # Best-effort: pull a file_id-shaped token or an image path. The
        # handler will resolve it against the inbox or allow-list.
        m = re.search(r"\b(?:/ocr|ocr|read\s+image)\s+"
                      r"([A-Za-z0-9_./\-]+)", t, re.I)
        target = m.group(1).strip() if m else ""
        return Intent("ocr_image", 0.9,
                      "Trích text từ ảnh đã upload.",
                      {"target": target, "raw": t}, "low", False)

    # ── Remote-worker control ──────────────────────────────────────────
    # "ssh worker2 uptime" / "kiểm tra worker2" / "worker2 còn sống không"
    if _has_any(t, _PATTERNS_REMOTE_WORKER_HEALTH):
        m = re.search(
            r"\b(?:vps|worker|server)\s*(\w+)?\s+"
            r"(?:còn\s+sống|alive|sao\s*rồi)", t, re.I)
        wid = m.group(1) if m else ""
        if not wid:
            m2 = re.search(r"\b(?:vps|worker|server)\s*(\w+)", t, re.I)
            wid = m2.group(1) if m2 else ""
        return Intent("remote_worker_health", 0.9,
                      f"Health-check remote worker {wid or '?'}.",
                      {"worker_id": wid}, "low", False)

    if _has_any(t, _PATTERNS_REMOTE_WORKER_LIST):
        return Intent("remote_worker_list", 0.9,
                      "Liệt kê remote workers đã đăng ký.",
                      {}, "low", False)

    # ssh_exec / "chạy lệnh trên worker" / "xem dung lượng worker2" /
    # "restart bot trên vps2"
    for p in _PATTERNS_SSH_EXEC:
        m = p.search(t)
        if m:
            wid = ""
            cmd = ""
            # Pattern 1: "ssh <id> <cmd>"
            if p.pattern.startswith(r"\bssh\s+"):
                wid, cmd = m.group(1), m.group(2)
            else:
                # Heuristic: last group is command-shaped, group containing
                # \w+ near "worker" is the id
                groups = [g for g in m.groups() if g]
                if groups:
                    # try to match a worker id and a command separately
                    wmatch = re.search(r"(?:vps|worker|server)\s*(\w+)",
                                       t, re.I)
                    wid = wmatch.group(1) if wmatch else ""
                    # Predefined verbs:
                    if "dung lượng" in t.lower() or "disk" in t.lower():
                        cmd = "df -h"
                    elif "restart" in t.lower() or "khởi động lại" in t.lower():
                        cmd = "systemctl status tiktok-bot"  # safe default;
                        # actual restart will need confirm via the high-risk
                        # path inside SSH classifier
                    else:
                        cmd = groups[-1]
            risk = "high" if _has_any(t, _PATTERNS_HIGH_RISK) else "medium"
            return Intent("ssh_exec", 0.85,
                          f"Chạy lệnh trên worker {wid or '?'}.",
                          {"worker_id": wid, "command": cmd},
                          risk, requires_confirm=(risk == "high"))

    # ── Agent autorun (10–12h owner work loop) ───────────────────────
    # STATUS first (most specific), then START, then STOP.
    # STOP is intentionally checked AFTER autorun_status so
    # "autorun status" doesn't match "stop autorun" path.
    if _has_any(t, _PATTERNS_AGENT_AUTORUN_STATUS):
        return Intent("agent_autorun_status", 0.95,
                      "Xem trạng thái agent autorun.",
                      {}, "low", False)
    if _has_any(t, _PATTERNS_AGENT_AUTORUN_START):
        # Hours + max_tasks parsed from text
        from bot.agent.agent_autorun import (parse_hours_vi,
                                              parse_max_tasks_vi)
        hrs = parse_hours_vi(t, default=12.0)
        mx  = parse_max_tasks_vi(t, default=20)
        return Intent("agent_autorun_start", 0.95,
                      f"Khởi động agent autorun {hrs}h, max {mx} task.",
                      {"hours": hrs, "max_tasks": mx,
                       "objective": t[:300]},
                      "medium", False)
    if _has_any(t, _PATTERNS_AGENT_AUTORUN_STOP):
        return Intent("agent_autorun_stop", 0.95,
                      "Dừng agent autorun.",
                      {}, "low", False)

    # ── Agent progress — "đang làm tới đâu rồi" / "sao im vậy" ───────
    if _has_any(t, _PATTERNS_AGENT_PROGRESS):
        return Intent("agent_progress", 0.95,
                      "Báo cáo tiến độ task hiện tại + "
                      "autorun + claude.",
                      {}, "low", False)

    # ── System / network high-risk action — needs confirm ───────────
    if _has_any(t, _PATTERNS_SYSTEM_NETWORK_HIGH):
        return Intent("system_network_action", 0.95,
                      "Hành động public/network — cần admin "
                      "bấm Đồng ý vì có rủi ro mở port / firewall / "
                      "expose internet.",
                      {"raw": t}, "high", True)

    # ── Secret dump — block, propose safe alternative ────────────────
    if _has_any(t, _PATTERNS_DUMP_SECRET):
        return Intent("system_secret_dump", 0.95,
                      "Yêu cầu dump secret — em sẽ KHÔNG in nguyên "
                      "giá trị. Đề xuất xem tên key thay thế.",
                      {"raw": t}, "high", True)

    # ── Package install — medium, grant-gated ────────────────────────
    if _has_any(t, _PATTERNS_SYSTEM_PKG_INSTALL):
        return Intent("system_pkg_install", 0.9,
                      "Cài package — chạy nếu có grant low_medium, "
                      "ngược lại tạo pending_action.",
                      {"raw": t}, "medium", False)

    # ── systemctl restart — medium, grant-gated ──────────────────────
    if _has_any(t, _PATTERNS_SYSTEM_RESTART):
        return Intent("system_restart", 0.9,
                      "Restart service — chạy nếu có grant, ngược "
                      "lại tạo pending_action.",
                      {"raw": t}, "medium", False)

    # ── git commit + push dev-agent ──────────────────────────────────
    if _has_any(t, _PATTERNS_GIT_COMMIT_PUSH):
        return Intent("system_git_push", 0.9,
                      "Commit + push dev-agent — chạy nếu có grant, "
                      "ngược lại tạo pending_action.",
                      {"raw": t}, "medium", False)

    # ── EsimAccess intents — order_real is HIGH (money) ──────────────
    if _has_any(t, _PATTERNS_ESIM_ORDER_REAL):
        return Intent("esim_order_real", 0.95,
                      "Tạo order eSIM thật qua EsimAccess (cần "
                      "xác nhận vì có rủi ro tiền).",
                      {"raw": t}, "high", True)
    if _has_any(t, _PATTERNS_ESIM_CURL_DRY):
        return Intent("esim_curl_dry", 0.9,
                      "Tạo curl dry-run cho EsimAccess (không "
                      "gọi mua thật).", {"raw": t}, "low", False)
    if _has_any(t, _PATTERNS_ESIM_DOCS):
        return Intent("esim_docs", 0.9,
                      "Đọc docs EsimAccess từ memory/docs.",
                      {}, "low", False)

    # ── Web search / browser ─────────────────────────────────────────
    if _has_any(t, _PATTERNS_BROWSER_TASK):
        return Intent("browser_task", 0.85,
                      "Browser/crawl task — nếu chưa có Playwright "
                      "thì tạo code_task build tool.",
                      {"raw": t}, "medium", False)
    if _has_any(t, _PATTERNS_WEB_SEARCH_NL):
        # Best-effort: extract a query after "search" or "tìm"
        q = re.sub(r"^.*?(?:search\s+web|tìm)\s+", "", t,
                   count=1, flags=re.I).strip()
        return Intent("web_search_nl", 0.85,
                      "Search web qua tool search_web hiện có.",
                      {"query": q or t}, "low", False)

    # Brain-evolve START matches BEFORE self_improve so "tự cải thiện brain"
    # without a stop word goes to the loop, not the one-shot.
    if _has_any(t, _PATTERNS_BRAIN_EVOLVE_STOP):
        return Intent("brain_evolve_stop", 0.95,
                      "Dừng brain evolution loop.", {}, "low", False)
    if _has_any(t, _PATTERNS_BRAIN_EVOLVE_STATUS):
        return Intent("brain_evolve_status", 0.95,
                      "Xem trạng thái brain evolution loop.",
                      {}, "low", False)
    if _has_any(t, _PATTERNS_BRAIN_EVOLVE_START):
        # Pick max_tasks from "X task" if mentioned
        m = re.search(r"\b(\d+)\s+task", t, re.I)
        n = int(m.group(1)) if m else 1
        return Intent("brain_evolve_start", 0.9,
                      f"Bắt đầu brain evolution loop (max={n}).",
                      {"max_tasks": max(1, min(n, 3))},
                      "medium", False)

    # Memory NL — must come BEFORE generic self_improve so "nhớ là …"
    # doesn't get swallowed.
    for p in _PATTERNS_MEMORY_ADD:
        m = p.search(t)
        if m:
            content = (m.group(1) or "").strip() if m.lastindex else ""
            if content:
                return Intent("memory_add", 0.9,
                              "Lưu vào memory.",
                              {"content": content}, "low", False)
    for p in _PATTERNS_MEMORY_SEARCH:
        m = p.search(t)
        if m:
            q = (m.group(1) if m.lastindex else "") or ""
            q = q.strip().strip('?.,!')
            if q:
                return Intent("memory_search", 0.9,
                              "Tìm trong semantic memory.",
                              {"query": q}, "low", False)
    for p in _PATTERNS_MEMORY_FORGET:
        m = p.search(t)
        if m:
            target = (m.group(1) or "").strip() if m.lastindex else ""
            return Intent("memory_forget", 0.85,
                          "Xóa memory theo id hoặc nội dung.",
                          {"target": target}, "medium", False)

    if _has_any(t, _PATTERNS_SELF_IMPROVE):
        return Intent("self_improve", 0.85,
                      "Chạy /self_improve_once để queue mission tiếp theo.",
                      {}, "medium", False)

    # 4. Files (legacy detect_intent already handles "gửi file X")
    if _has_any(t, _PATTERNS_SHOW_FILES):
        return Intent("show_files", 0.9,
                      "Liệt kê file inbox.", {}, "low", False)

    # 5. Permissions
    if _has_any(t, _PATTERNS_GRANT_PERM):
        mins  = parse_duration_vi(t) or 30
        scope = "low_medium"
        tl = t.lower()
        if "low_only" in tl:        scope = "low_only"
        elif "code" in tl:          scope = "code_low_medium"
        elif "readonly" in tl:      scope = "admin_readonly"
        return Intent("grant_permission", 0.85,
                      f"Cấp quyền {scope} trong {mins} phút.",
                      {"scope": scope, "minutes": mins}, "medium", False)
    if _has_any(t, _PATTERNS_REVOKE):
        return Intent("revoke_permission", 0.9,
                      "Thu hồi quyền session.", {}, "low", False)

    # 6. Status / list / search
    # ── Self-introspection (more specific than DIAG / STATUS) ──────────
    # RUNTIME first — "m dùng gì để code" mentions "code" so it's a
    # runtime question, not a model-name question.
    if _has_any(t, _PATTERNS_WHOAMI_RUNTIME):
        return Intent("whoami_runtime", 0.95,
                      "Giải thích kiến trúc runtime: 9Router cho chat/"
                      "search, Claude CLI cho coding.", {}, "low", False)
    if _has_any(t, _PATTERNS_WHOAMI_MODEL):
        return Intent("whoami_model", 0.95,
                      "Báo cáo model nào đang chạy (chat/coding/etc).",
                      {}, "low", False)
    if _has_any(t, _PATTERNS_RECENT_ACTIVITY):
        return Intent("recent_activity", 0.95,
                      "Tóm tắt hoạt động gần nhất: audit log + last "
                      "code_task done + brain_evolve.", {}, "low", False)
    if _has_any(t, _PATTERNS_NEXT_MISSION):
        return Intent("next_mission", 0.95,
                      "Hiển thị mission tiếp theo: roadmap + queue.",
                      {}, "low", False)
    if _has_any(t, _PATTERNS_WHOAMI):
        return Intent("whoami", 0.95,
                      "Giới thiệu agent: mission, kiến trúc, model "
                      "policy.", {}, "low", False)

    if _has_any(t, _PATTERNS_DIAG):
        return Intent("agent_diag", 0.95,
                      "Hiển thị bảng diag: queue, worker, claude, "
                      "pending, dirty tree.", {}, "low", False)

    if _has_any(t, _PATTERNS_STATUS):
        return Intent("status", 0.9,
                      "Xem dashboard sức khỏe agent.", {}, "low", False)
    if _has_any(t, _PATTERNS_LIST_TASKS):
        return Intent("list_tasks", 0.9,
                      "Liệt kê task code đang queue/running.",
                      {}, "low", False)
    if _has_any(t, _PATTERNS_SEARCH):
        return Intent("search", 0.7,
                      "Tìm kiếm web.", {"query": t}, "low", False)

    # 7. Default: chat
    return Intent("chat", 0.5,
                  "Chat thường — chuyển backend trả lời.",
                  {"text": t}, "low", False)
