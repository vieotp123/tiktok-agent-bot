import os
import sys
import asyncio
import time
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import pytz

sys.path.insert(0, "/opt/tiktok-bot")
load_dotenv("/opt/tiktok-bot/.env")

from bot.llm_client import complete, router_status
from bot.memory import (
    get_recent_messages, add_message, format_memory_for_prompt,
    get_memory_summary, forget_user,
)
from bot.reminders import add_reminder, parse_reminder_time, format_reminders_list, now_jst
from bot.tools import (
    get_btc_price, is_btc_query,
    search_web, format_search_results, is_search_query, extract_search_query,
)
from bot.business_store import (
    detect_esim_intent, build_consult_reply, compute_lead_score,
    add_consulting_log, upsert_lead, add_conversation,
)
from bot.memory_store import add_raw_event

app = FastAPI(title="TikTok Bot Backend")

BOT_NAME = os.getenv("BOT_NAME", "Botchat")
TZ = pytz.timezone("Asia/Tokyo")

# New clean fallback — no old loop phrases allowed
ERROR_REPLY = "t đang lỗi xử lý, để t xem log đã"
# Rate-limit fallback: track last error reply time per user (max 1 per 10 min)
_error_last_sent: dict[str, float] = {}

# Phrases the LLM must never generate — strip them even if model slips
_BANNED_LLM_PHRASES = [
    "đợi tí m", "não t đang lag", "gửi lại phát nữa t xử",
    "não t lag", "đợi tí", "gửi lại phát nữa",
]


def _strip_banned(text: str) -> str:
    """Strip any banned loop-phrases from LLM output (defense in depth)."""
    for phrase in _BANNED_LLM_PHRASES:
        text = text.replace(phrase, "").strip()
    # Collapse multiple spaces/newlines left behind
    import re
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or "ok, tao nghe mày"

SYSTEM_PROMPT = f"""\
Mày là {BOT_NAME}, biệt danh Jelly — bot chat trong nhóm TikTok.

Tính cách:
- Nói chuyện như bạn thân người Việt, xưng "tao", gọi user là "mày" hoặc "chủ nhân".
- Cục súc nhẹ, cà khịa nhẹ, nhưng tốt bụng và muốn giúp thật.
- Viết tự nhiên, viết tắt kiểu teen Việt: "k", "đc", "r", "m", "t", "thôi".
- Câu ngắn gọn, tối đa 1-4 message ngắn.

RULES BẮT BUỘC:
- KHÔNG bắt đầu bằng "Jelly:" hay "{BOT_NAME}:".
- KHÔNG tự nói đang lag, đang bận, đợi tí — trừ khi thật sự có vấn đề kỹ thuật.
- KHÔNG nhắc "gửi lại" trừ khi user hỏi về thứ chưa gửi rõ.
- KHÔNG lặp lại phrase từ lần trước như "đợi tí m", "não t đang lag", "gửi lại phát nữa t xử".
- KHÔNG mở đầu bằng câu chào dài dòng.
- TRẢ LỜI ĐÚNG tin user mới nhất, không drift sang chủ đề cũ.
- Nếu không biết thì nói thẳng "tao k chắc", không bịa.
- Nếu user gửi ảnh/sticker mà không có text → không trả lời ngẫu nhiên.\
"""

REMINDER_KW = ["nhắc tao", "nhắc mình", "nhắc t ", "nhắc m ", "remind", "đặt nhắc"]
UNSUPPORTED_MEDIA = "m gửi ảnh/sticker à, hiện t đọc chữ trước đã. muốn t phân tích ảnh thì bảo rõ nha"


def _now_str() -> str:
    return now_jst().strftime("%A, %d/%m/%Y %H:%M (Asia/Tokyo)")


def split_bubbles(text: str, max_n: int = 5) -> list[str]:
    """Split AI response into message bubbles sensibly."""
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n")]
    bubbles, buf = [], []
    SKIP = {"1.", "2.", "3.", "4.", "5.", "-", "•", "*"}
    for line in lines:
        if not line:
            if buf:
                bubbles.append(" ".join(buf))
                buf = []
        elif line in SKIP:
            continue
        else:
            buf.append(line)
    if buf:
        bubbles.append(" ".join(buf))
    bubbles = [b for b in bubbles if b.strip()]
    # Merge very short fragments
    merged, i = [], 0
    while i < len(bubbles):
        b = bubbles[i]
        if len(b) < 15 and i + 1 < len(bubbles):
            b = b + " " + bubbles[i + 1]
            i += 2
        else:
            i += 1
        merged.append(b)
    return merged[:max_n]


def is_reminder(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in REMINDER_KW)


async def handle_reminder(username: str, content: str) -> str:
    remind_at = parse_reminder_time(content)
    if remind_at is None:
        return "mày muốn tao nhắc lúc mấy giờ? Nói rõ hơn: '7h tối nay', 'mai 9h sáng'."
    import re
    text = content.lower()
    for kw in REMINDER_KW:
        text = text.replace(kw, "").strip()
    text = re.sub(r'\d{1,2}[h:]\d{0,2}\s*(sáng|tối|chiều|trưa)?', '', text).strip()
    text = re.sub(r'(hôm nay|ngày mai|mai|ngày kia|nay)', '', text).strip(" .,")
    if not text:
        text = content
    add_reminder(username, text, remind_at, content)
    return f"ok tao set nhắc lúc {remind_at.strftime('%d/%m %H:%M')}: \"{text}\""


def _infer_source(username: str) -> str:
    """Derive log source tag from username convention."""
    u = username.lower()
    if u.startswith("tg_admin_file"):
        return "file_summary"
    if u.startswith("tg_"):
        return "telegram"
    if u.startswith("task_"):
        return "task"
    return "tiktok"


async def call_llm(username: str, content: str, source: str = "backend") -> tuple[str, bool]:
    """Call LLM, return (reply_text, had_error)."""
    mem = format_memory_for_prompt(username)
    recent = get_recent_messages(username, n=8)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\n\nHôm nay: {_now_str()}"}
    ]
    if mem:
        messages.append({"role": "system", "content": f"Memory về user:\n{mem}"})
    # Admin chat fallback only: inject brain context (memories, audit,
    # autorun state, pending_actions). Skipped for tiktok_chat / file
    # summary / search paths so customer DMs never see admin internals.
    if source == "telegram" and username.startswith("tg_admin"):
        try:
            from bot.agent.brain_context import build_admin_brain_context
            ctx = build_admin_brain_context(content)
        except Exception as e:
            print(f"[backend] brain_context error: {e}", flush=True)
            ctx = ""
        if ctx:
            messages.append({"role": "system", "content": ctx})
    for m in recent[-6:]:
        role = "user" if m["role"] == "user" else "assistant"
        messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": content})

    # Choose role based on source — never use coding model for normal chat
    chat_role = "telegram_chat" if source == "telegram" else "tiktok_chat"
    result = await complete(messages, role=chat_role, temperature=0.8, source=source)

    if result.get("error"):
        detail = result.get("error_detail", "unknown")
        print(f"[backend] LLM error: {detail}", flush=True)
        return ERROR_REPLY, True

    raw_reply = (result.get("content") or ERROR_REPLY).strip()
    return _strip_banned(raw_reply), False


class MessageRequest(BaseModel):
    username: str
    content: str
    source: str = ""   # optional: "tiktok" | "telegram" | "task" | "file_summary" | ...


class MessageResponse(BaseModel):
    reply: str
    messages: list[str]
    user_role: str = "normal"
    error: bool = False
    error_detail: str = ""


_GARBAGE_PATTERNS = ["TikTok Upload", "All activity", "Send a message...", "© 2026 TikTok"]


def _is_garbage_content(text: str) -> bool:
    """Detect TikTok UI dumps or other garbage masquerading as user messages."""
    if len(text) > 400:
        return True
    for pat in _GARBAGE_PATTERNS:
        if pat in text:
            return True
    return False


def _rate_limited_error(username: str) -> str | None:
    """Return ERROR_REPLY if within rate limit (max 1 per 10 min), else None."""
    now = time.time()
    last = _error_last_sent.get(username, 0)
    if now - last < 600:
        return None  # suppress, already sent recently
    _error_last_sent[username] = now
    return ERROR_REPLY


@app.post("/message", response_model=MessageResponse)
async def handle_message(req: MessageRequest):
    username = req.username.strip() or "user"
    content  = req.content.strip()
    source   = req.source.strip() if req.source else _infer_source(username)

    if not content:
        raise HTTPException(status_code=400, detail="content rỗng")

    low = content.lower()

    # Commands
    if low.startswith("/memory"):
        s = get_memory_summary(username)
        return MessageResponse(reply=s, messages=[s])
    if low.startswith("/forget"):
        forget_user(username)
        m = "ok tao xoá memory của mày rồi."
        return MessageResponse(reply=m, messages=[m])
    if low.startswith("/reminder") or low.startswith("/reminders"):
        m = format_reminders_list(username)
        return MessageResponse(reply=m, messages=[m])
    if low.startswith("/router_status"):
        st = await router_status()
        m = str(st)
        return MessageResponse(reply=m, messages=[m])

    # Media/sticker (empty useful content)
    if content in ("[image]", "[sticker]", "[video]", "[file]"):
        return MessageResponse(reply=UNSUPPORTED_MEDIA, messages=[UNSUPPORTED_MEDIA])

    # Garbage detection — TikTok UI dumps, overly long single-blob text
    if _is_garbage_content(content):
        print(f"[backend] garbage content rejected len={len(content)} user={username}", flush=True)
        raise HTTPException(status_code=400, detail="garbage_content")

    # BTC price
    if is_btc_query(content):
        btc = await get_btc_price()
        add_message(username, "user", content)
        add_message(username, "assistant", btc)
        return MessageResponse(reply=btc, messages=split_bubbles(btc))

    # Web search
    if is_search_query(content):
        q = extract_search_query(content)
        print(f"[backend] search query={q!r}", flush=True)
        results = await search_web(q, max_results=4)
        context = format_search_results(results)
        # Ask LLM to summarize results in bot's voice
        search_prompt = (
            f"User hỏi: \"{content}\"\n\n"
            f"Kết quả tìm web (DuckDuckGo):\n{context}\n\n"
            "Tóm tắt bằng tiếng Việt, ngắn gọn tự nhiên như bạn thân. "
            "Đừng bịa thêm, dựa trên kết quả trên. "
            "Nếu không có kết quả hữu ích thì nói thẳng."
        )
        mem = format_memory_for_prompt(username)
        messages = [{"role": "system", "content": SYSTEM_PROMPT + f"\n\nHôm nay: {_now_str()}"}]
        if mem:
            messages.append({"role": "system", "content": f"Memory về user:\n{mem}"})
        messages.append({"role": "user", "content": search_prompt})
        result = await complete(messages, role="search_summary", temperature=0.6,
                                source=source if source else "search")
        if result.get("error"):
            reply = f"Tìm được nhưng tóm tắt lỗi. Kết quả thô:\n{context[:500]}"
        else:
            reply = _strip_banned((result.get("content") or "").strip())
        add_message(username, "user", content)
        add_message(username, "assistant", reply)
        bubbles = split_bubbles(reply)
        return MessageResponse(reply=reply, messages=bubbles or [reply])

    # eSIM / sales consulting (DB-grounded, never invents prices)
    if detect_esim_intent(content):
        # Determine platform from source first → drives tone selection
        platform = "tiktok" if source in ("tiktok", "") else (
            "telegram" if source.startswith("telegram") else (source or "internal")
        )
        # Customer-facing tone for TikTok DMs; admin tone elsewhere
        audience = "customer" if platform == "tiktok" else "admin"
        reply, product_ids, confidence = build_consult_reply(content, audience=audience)
        score = compute_lead_score(content)
        sender_key  = username
        sender_name = username

        # Choose lead status by intent strength
        if score >= 50:
            lead_status = "interested"
        elif score >= 20:
            lead_status = "needs_followup"
        else:
            lead_status = "new"

        # Upsert lead
        try:
            lead_id = upsert_lead(
                platform=platform, sender_key=sender_key,
                username=username, display_name=username,
                source=source, need_summary=content[:120],
                lead_score=score, status=lead_status,
            )
        except Exception as e:
            print(f"[consult] upsert_lead error: {e}", flush=True)
            lead_id = ""

        # Log inbound + outbound conversation
        try:
            add_conversation(
                lead_id=lead_id, platform=platform,
                sender_key=sender_key, sender_name=sender_name,
                direction="in", message=content,
                intent="esim_consult",
                product_suggested=",".join(product_ids[:3]),
            )
            add_conversation(
                lead_id=lead_id, platform=platform,
                sender_key=sender_key, sender_name=sender_name,
                direction="out", message=reply,
                intent="esim_consult",
                product_suggested=",".join(product_ids[:3]),
            )
        except Exception as e:
            print(f"[consult] add_conversation error: {e}", flush=True)

        # Log consulting record
        try:
            add_consulting_log(
                platform=platform, sender_key=sender_key, sender_name=sender_name,
                user_message=content, bot_reply=reply,
                products_used=product_ids, confidence=confidence,
            )
        except Exception as e:
            print(f"[consult] add_consulting_log error: {e}", flush=True)

        # Memory: raw event (no PII / no prices)
        try:
            add_raw_event(
                source=platform, action="sales_consult",
                summary=(f"consult q={content[:60]!r} conf={confidence:.2f} "
                         f"score={score} status={lead_status} prods={len(product_ids)}"),
                actor=sender_key, event_type="consulting",
                tags=["sales", "esim"],
            )
        except Exception as e:
            print(f"[consult] add_raw_event error: {e}", flush=True)

        add_message(username, "user", content)
        add_message(username, "assistant", reply)
        bubbles = split_bubbles(reply) or [reply]
        return MessageResponse(reply=reply, messages=bubbles)

    # Reminder
    if is_reminder(content):
        reply = await handle_reminder(username, content)
        add_message(username, "user", content)
        add_message(username, "assistant", reply)
        return MessageResponse(reply=reply, messages=[reply])

    # General LLM
    add_message(username, "user", content)
    reply, had_error = await call_llm(username, content, source=source)

    if had_error:
        err_msg = _rate_limited_error(username)
        if err_msg is None:
            # Suppressed — log but don't send anything
            print(f"[backend] error suppressed (rate limit) user={username}", flush=True)
            raise HTTPException(status_code=503, detail="llm_error_suppressed")
        return MessageResponse(reply=err_msg, messages=[err_msg], error=True, error_detail="llm_error")

    add_message(username, "assistant", reply)
    bubbles = split_bubbles(reply)
    if not bubbles:
        bubbles = [reply]

    return MessageResponse(
        reply=reply,
        messages=bubbles,
        error=False,
    )


@app.get("/health")
async def health():
    from bot.reminders import now_jst
    return {"status": "ok", "time": now_jst().isoformat()}


@app.get("/router_status")
async def get_router_status():
    return await router_status()
