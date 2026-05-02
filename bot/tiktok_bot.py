"""
TikTok DM AI Bot — Playwright automation.
Anti-loop: DOM-key dedup + TTL sent-cache + is_own detection.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import httpx
from datetime import datetime
from typing import Optional
import pytz

from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")
sys.path.insert(0, "/opt/tiktok-bot")

from playwright.async_api import (
    async_playwright, Page, BrowserContext, Browser,
    Error as PlaywrightError,
)
from bot.tools import (
    is_btc_query, is_shot_command, extract_shot_url,
    screenshot_url, upload_image_tiktok,
)
from bot.reminders import reminder_loop

# ── Config ────────────────────────────────────────────────────────────────────
GROUP_CHAT_URL   = os.getenv("GROUP_CHAT_URL",   "https://www.tiktok.com/messages")
TARGET_CHAT_NAME = os.getenv("TARGET_CHAT_NAME", "Chatgibiti")
BOT_NAME         = os.getenv("BOT_NAME",         "Botchat").lower()
BACKEND_URL      = os.getenv("BACKEND_URL",      "http://localhost:8000")
HEADLESS         = os.getenv("HEADLESS",         "true").lower() == "true"
POLL_INTERVAL    = float(os.getenv("POLL_INTERVAL", "1.2"))
STORAGE_STATE    = "/opt/tiktok-bot/tiktok_storage_state.json"
SCREENSHOTS_DIR  = "/opt/tiktok-bot/screenshots"
TZ               = pytz.timezone("Asia/Tokyo")

BOT_NAME_VARIANTS = {BOT_NAME, "jelly", "botchat", "bot", BOT_NAME.replace(" ", "")}

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN",    "")
TG_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID","")


async def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text[:4000]},
            )
    except Exception:
        pass


def log(tag: str, msg: str) -> None:
    ts = datetime.now(TZ).strftime("%H:%M:%S")
    out = f"[{ts}][{tag}] {msg}"[:220]
    print(out, flush=True)


# ── Poll noise throttle ───────────────────────────────────────────────────────
_zero_items_count = 0   # consecutive polls with 0 items
_ZERO_LOG_EVERY   = 30  # log "0 items" only once per N polls

# ── Sent-text cache with TTL ──────────────────────────────────────────────────
# {normalized_text: sent_timestamp}
_sent_cache: dict[str, float] = {}
SENT_TTL    = 600   # 10 min
SENT_DUP_TTL = 120  # 2 min — block exact duplicate sends


def _norm(t: str) -> str:
    return re.sub(r'\s+', ' ', t).strip().lower()


def sent_cache_add(text: str) -> None:
    now = time.time()
    for variant in _text_variants(text):
        _sent_cache[variant] = now
    # Prune old
    cutoff = now - SENT_TTL
    for k in list(_sent_cache):
        if _sent_cache[k] < cutoff:
            del _sent_cache[k]


def sent_cache_has(text: str, ttl: float = SENT_TTL) -> bool:
    cutoff = time.time() - ttl
    for variant in _text_variants(text):
        ts = _sent_cache.get(variant)
        if ts and ts > cutoff:
            return True
    return False


def _text_variants(text: str) -> list[str]:
    n = _norm(text)
    variants = {n}
    for line in text.split("\n"):
        l = _norm(line)
        if len(l) > 4:
            variants.add(l)
    return list(variants)


# ── Seen-message key set ──────────────────────────────────────────────────────
seen_keys: set[str] = set()
_baseline_done = False


def make_msg_key(data_id: str, sender: str, text: str, index: int) -> str:
    """Stable key: prefer data-id, else hash of sender+text+index."""
    if data_id:
        return f"id:{data_id}"
    raw = f"{sender}|{_norm(text)[:100]}|{index}"
    return "h:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def make_sender_key(sender_name: str, sender_avatar: str, side: str, idx: int) -> str:
    """Stable sender identifier: name → avatar URL → positional fallback."""
    name = (sender_name or "").strip()
    if name and name.lower() not in ("", "unknown", "unknown_user"):
        return "n:" + hashlib.sha1(("name:" + name).encode()).hexdigest()[:12]
    avatar = (sender_avatar or "").strip()
    if avatar:
        return "a:" + hashlib.sha1(("avatar:" + avatar).encode()).hexdigest()[:12]
    return "u:" + hashlib.sha1(f"unknown:{side}:{idx}".encode()).hexdigest()[:12]


def is_bot_own(sender: str, text: str, is_self_dom: bool) -> bool:
    """True if this message is from the bot itself."""
    # DOM-level self detection (most reliable)
    if is_self_dom:
        return True
    # Sender name matches bot
    s = sender.lower().strip()
    if s in BOT_NAME_VARIANTS:
        return True
    # Text starts with bot name prefix ("jelly: ...")
    first = _norm(text).split()[0] if text.strip() else ""
    if first in BOT_NAME_VARIANTS:
        return True
    first_colon = _norm(text).split(":")[0].strip()
    if first_colon in BOT_NAME_VARIANTS:
        return True
    # Text was recently sent by bot
    if sent_cache_has(text):
        return True
    return False


# ── JS: extract messages from DOM ─────────────────────────────────────────────
_JS_EXTRACT = r"""() => {
    const VW = window.innerWidth;
    const VH = window.innerHeight;
    const VIEWPORT_MID = VW / 2;
    const results = [];

    // Chat-box centre for left/right isSelf detection (more reliable than viewport mid)
    const chatBoxEl = document.querySelector('[class*="DivChatBox"]');
    const chatBoxRect = chatBoxEl ? chatBoxEl.getBoundingClientRect() : null;
    const CHAT_MID = chatBoxRect ? chatBoxRect.left + chatBoxRect.width / 2 : VIEWPORT_MID;

    // ── Step 1: Find message container ──────────────────────────────────────
    // Strategy A: TikTok data-e2e attribute
    let container = document.querySelector('[data-e2e="dm-message-list"]');

    // Strategy B: Walk UP from input (finds scrollable ancestor)
    if (!container) {
        const input = (
            document.querySelector('[data-e2e="dm-message-input"]') ||
            document.querySelector('[contenteditable="true"]') ||
            document.querySelector('textarea')
        );
        if (input) {
            let el = input.parentElement;
            for (let i = 0; i < 12 && el; i++) {
                const hasScroll = el.scrollHeight > el.clientHeight + 20;
                const isWide = el.offsetWidth > VW * 0.3;
                if (hasScroll && isWide) { container = el; break; }
                el = el.parentElement;
            }
        }
    }

    // Strategy C: class-name patterns
    if (!container) {
        container = (
            document.querySelector('[class*="DivMessageList"]') ||
            document.querySelector('[class*="MessageList"]') ||
            document.querySelector('[class*="message-list"]') ||
            document.querySelector('[class*="ChatContent"]') ||
            document.querySelector('[class*="chat-content"]')
        );
    }

    // Strategy D: DivChatBottom sibling — find the message list as sibling of input area
    if (!container) {
        const chatBottom = document.querySelector('[class*="DivChatBottom"]');
        if (chatBottom && chatBottom.parentElement) {
            for (const child of chatBottom.parentElement.children) {
                const cls = child.className.toString();
                if (!cls.includes('DivChatBottom') &&
                    !cls.includes('Header') && !cls.includes('header')) {
                    if (child.offsetHeight > 30 || child.children.length > 0) {
                        container = child;
                        break;
                    }
                }
            }
        }
    }

    // Last resort: body
    if (!container) container = document.body;

    // ── Step 2: Find individual message items ────────────────────────────────
    let items = [];
    let itemsSource = '';

    // Try TikTok-specific selectors first
    const specificSels = [
        '[data-e2e="dm-message-item"]',
        '[class*="DivMessageContainer"]',
        '[class*="MessageContainer"]',
        '[class*="message-item"]',
        '[class*="DivMessage"][class*="Item"]',
    ];
    for (const sel of specificSels) {
        const found = container.querySelectorAll(sel);
        if (found.length >= 1) { items = Array.from(found); itemsSource = sel; break; }
    }

    // Fallback A: document-wide [class*="Message"] with width < 75% viewport
    // Intentionally includes full-width row containers (e.g. 888px DivMessageVerticalContainer);
    // isSelf is corrected per-item in Step 3 via narrow bubble detection.
    if (items.length === 0) {
        const msgAll = document.querySelectorAll('[class*="Message"], [class*="message"]');
        const candidates = Array.from(msgAll).filter(el => {
            const r = el.getBoundingClientRect();
            return (
                r.width > 30 && r.width < VW * 0.75 &&
                r.height > 12 && r.height < 500 &&
                el.offsetParent !== null
            );
        });
        // Keep outermost only (de-duplicate nested matches)
        const candSet = new Set(candidates);
        items = candidates.filter(el => {
            let p = el.parentElement;
            while (p) {
                if (candSet.has(p)) return false;
                p = p.parentElement;
            }
            return true;
        });
        if (items.length) itemsSource = 'fallbackA';
    }

    // Fallback B: container descendants, bubble-shaped (wider filter matches rows too)
    if (items.length === 0 && container !== document.body) {
        const all = container.querySelectorAll('div, li');
        const candidates = Array.from(all).filter(el => {
            const r = el.getBoundingClientRect();
            return (
                r.width > 30 && r.width < VW * 0.75 &&
                r.height > 12 && r.height < 500 &&
                el.offsetParent !== null &&
                el.children.length <= 8
            );
        });
        const candSet = new Set(candidates);
        items = candidates.filter(el => {
            let p = el.parentElement;
            while (p && p !== container) {
                if (candSet.has(p)) return false;
                p = p.parentElement;
            }
            return true;
        });
        if (items.length) itemsSource = 'fallbackB';
    }

    // Fallback C: container direct children (full-width rows) — bubble found per-item in Step 3
    if (items.length === 0 && container !== document.body) {
        items = Array.from(container.children).filter(el =>
            el.offsetHeight > 10 && el.offsetParent !== null
        );
        if (items.length) itemsSource = 'fallbackC-rows';
    }

    // ── Step 3: Parse each item ──────────────────────────────────────────────
    for (let idx = 0; idx < items.length; idx++) {
        const el = items[idx];
        if (!el.offsetParent && el.offsetHeight === 0) continue;

        const rect = el.getBoundingClientRect();
        const cls = el.className.toString();

        // ── Find narrow bubble inside full-width row items ───────────────────
        // TikTok rows are typically full-width (DivMessageVerticalContainer/Horizontal ~888px).
        // The actual text bubble is a narrow child inside. Use it for position + text.
        // ── Find narrow bubble for isSelf position detection ─────────────────
        // Full-width rows (e.g. DivMessageVerticalContainer at ~888px) contain the
        // text bubble as a narrow child. Find it to correctly detect left (user) vs
        // right (bot) alignment. Skip avatars and elements without text content.
        let bubble = null;
        if (rect.width > VW * 0.50) {
            const descendants = Array.from(el.querySelectorAll('div, span, p'));
            for (const child of descendants) {
                const childCls = child.className.toString();
                // Skip avatar containers
                if (/avatar|Avatar/i.test(childCls)) continue;
                const cr = child.getBoundingClientRect();
                if (cr.width > 20 && cr.width < rect.width * 0.75 &&
                    cr.height > 8 && cr.height < 300 &&
                    child.offsetParent !== null &&
                    child.textContent.trim().length > 0) {
                    bubble = child;
                    break;
                }
            }
        }

        // ── Self-detection ───────────────────────────────────────────────────
        const isSelfCls = /\b(self|mine|outgoing|right|sent)\b/i.test(cls) ||
                          /DivSelf|Self[A-Z]|Outgoing/i.test(el.innerHTML.substring(0, 300));
        let isSelfPos = false;
        if (bubble) {
            // Use bubble centre relative to chat box mid (left=user, right=bot)
            const br = bubble.getBoundingClientRect();
            isSelfPos = (br.left + br.width / 2) > CHAT_MID;
        } else {
            // Narrow item itself — use its centre vs chat mid
            const centerX = rect.left + rect.width / 2;
            isSelfPos = rect.width < VW * 0.55 && centerX > CHAT_MID;
        }
        const isSelf = isSelfCls || isSelfPos;

        // data-id attribute
        const dataId = el.getAttribute('data-id') ||
                       el.getAttribute('data-message-id') ||
                       el.querySelector('[data-id]')?.getAttribute('data-id') || '';

        // Sender — from explicit label element
        let sender = '';
        const senderEl = el.querySelector(
            '[data-e2e*="sender"], [class*="sender-name"], [class*="SenderName"],' +
            '[class*="nickname"], [class*="Nickname"], [class*="Username"]'
        );
        if (senderEl) sender = senderEl.textContent.trim();

        // ── Enhanced sender identification from avatar img ───────────────────
        // TikTok avatar imgs usually carry the sender's username in alt / aria-label.
        // Avatar src (query-stripped) gives a stable per-sender key even when the
        // name element is absent (e.g. consecutive messages from the same user).
        let senderName = sender;
        let senderAvatar = '';
        const avatarImgsInRow = Array.from(el.querySelectorAll('img'));
        for (const img of avatarImgsInRow) {
            const imgCls   = img.className ? img.className.toString() : '';
            const parentCls = img.parentElement ? img.parentElement.className.toString() : '';
            if (!/avatar/i.test(imgCls) && !/avatar/i.test(parentCls)) continue;
            // Found the avatar img
            const alt  = (img.getAttribute('alt')        || '').trim();
            const aria = (img.getAttribute('aria-label') || '').trim();
            if (!senderName && alt  && alt.length  > 0 && alt.length  < 60) senderName = alt;
            if (!senderName && aria && aria.length > 0 && aria.length < 60) senderName = aria;
            const src = img.getAttribute('src') || '';
            if (src.startsWith('http')) senderAvatar = src.split('?')[0];
            break;  // use first avatar found per row
        }
        const bubbleSide = isSelf ? 'right' : 'left';

        // Message type: text / image / sticker
        let rawType = 'text';
        if (el.querySelector('img[class*="sticker"], [class*="Sticker"]')) rawType = 'sticker';
        else if (el.querySelector('img:not([class*="avatar"]):not([class*="Avatar"])')) rawType = 'image';

        // Text extraction: always from full item (full row works correctly; avatars/chrome
        // are stripped by the clone-and-strip logic below)
        let text = '';
        if (rawType === 'text') {
            const target = el;  // use full item, not bubble (bubble may miss text in siblings)
            const clone = target.cloneNode(true);
            const strip = [
                '[class*="avatar"]','[class*="Avatar"]','[class*="time"]','[class*="Time"]',
                '[class*="Reaction"]','[class*="reaction"]','[class*="tick"]',
                '[class*="sender-name"]','[class*="SenderName"]','[class*="Nickname"]',
                '[data-e2e*="sender"]',
            ];
            strip.forEach(s => clone.querySelectorAll(s).forEach(x => x.remove()));
            const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
            const parts = [];
            let node;
            while ((node = walker.nextNode())) {
                const v = node.textContent.trim();
                if (v) parts.push(v);
            }
            text = parts.join(' ').trim();

            if (sender && text.toLowerCase().startsWith(sender.toLowerCase()))
                text = text.substring(sender.length).replace(/^[\s:]+/, '').trim();

            // Reject UI chrome / garbage
            if (text.length > 300 || /TikTok Upload|All activity|Send a message\.\.\./i.test(text))
                text = '';
        }

        if (!text && rawType === 'text') continue;

        results.push({
            index: idx, dataId, sender: senderName, text, rawType, isSelf,
            senderAvatar, bubbleSide, debugSource: itemsSource, top: rect.top,
        });
    }

    return results;
}"""


# ── JS: one-time DOM debug dump ──────────────────────────────────────────────
_JS_DEBUG_DUMP = r"""() => {
    const VW = window.innerWidth;
    const input = document.querySelector('[data-e2e="dm-message-input"]') ||
                  document.querySelector('[contenteditable="true"]') ||
                  document.querySelector('textarea');
    const inputFound = !!input;
    // Walk up from input
    const ancestry = [];
    if (input) {
        let el = input.parentElement;
        for (let i = 0; i < 10 && el; i++) {
            ancestry.push({
                i, tag: el.tagName,
                cls: el.className.toString().slice(0, 100),
                w: el.offsetWidth, h: el.offsetHeight, scrollH: el.scrollHeight,
                children: el.children.length,
            });
            el = el.parentElement;
        }
    }
    // Count element types
    const counts = {};
    for (const s of ['[data-e2e]','[class*="Message"]','[class*="message"]','[class*="Chat"]','[class*="Bubble"]']) {
        counts[s] = document.querySelectorAll(s).length;
    }
    // Sample first 4 Message elements: class + size
    const msgEls = Array.from(document.querySelectorAll('[class*="Message"]')).slice(0, 6).map(el => {
        const r = el.getBoundingClientRect();
        return { cls: el.className.toString().slice(0, 100), w: Math.round(r.width), h: Math.round(r.height), de2e: el.getAttribute('data-e2e') || '' };
    });
    // dm-message-list check
    const dmList = !!document.querySelector('[data-e2e="dm-message-list"]');
    const dmItem = document.querySelectorAll('[data-e2e="dm-message-item"]').length;
    return { inputFound, ancestry, counts, msgEls, dmList, dmItem };
}"""


# ── JS: chat metadata ────────────────────────────────────────────────────────
_JS_CHAT_INFO = r"""() => {
    // Chat title from header
    let chatTitle = '';
    const titleEl = (
        document.querySelector('[data-e2e="chat-header-title"]') ||
        document.querySelector('[class*="DivChatHeader"] [class*="title"]') ||
        document.querySelector('[class*="ChatHeader"] [class*="title"]') ||
        document.querySelector('[class*="DivChatHeader"] h1') ||
        document.querySelector('[class*="DivChatHeader"] h2') ||
        document.querySelector('[class*="DivChatHeader"] span')
    );
    if (titleEl) chatTitle = titleEl.textContent.trim().slice(0, 80);

    // Member count — TikTok may show "X members" in header
    let memberCount = 0;
    const memberEl = (
        document.querySelector('[data-e2e="chat-member-count"]') ||
        document.querySelector('[class*="member-count" i]') ||
        document.querySelector('[class*="MemberCount"]')
    );
    if (memberEl) {
        const m = memberEl.textContent.match(/\d+/);
        if (m) memberCount = parseInt(m[0]);
    } else {
        // Try to find "N members" text in the header area
        const headerEls = document.querySelectorAll('[class*="DivChatHeader"] *, [class*="ChatHeader"] *');
        for (const el of headerEls) {
            if (el.children.length === 0) {
                const t = el.textContent.trim();
                const m2 = t.match(/^(\d+)\s*(members?|thành viên)/i);
                if (m2) { memberCount = parseInt(m2[1]); break; }
            }
        }
    }

    // Visible avatars in header (rough membership signal)
    const headerAvatarImgs = document.querySelectorAll(
        '[class*="DivChatHeader"] img, [class*="ChatHeader"] img'
    );
    const membersVisible = headerAvatarImgs.length;

    return { chatTitle, memberCount, membersVisible };
}"""


async def get_chat_info(page: Page) -> dict:
    """Return {chatTitle, memberCount, membersVisible} from the current chat DOM."""
    try:
        info = await page.evaluate(_JS_CHAT_INFO)
        return info
    except Exception as e:
        log("chat", f"get_chat_info error: {e}")
        return {"chatTitle": "", "memberCount": 0, "membersVisible": 0}


async def debug_dom_dump(page: Page) -> None:
    """Run once after entering chat to understand TikTok's DOM structure."""
    try:
        info = await page.evaluate(_JS_DEBUG_DUMP)
        log("dom", f"input={info['inputFound']} dm-list={info['dmList']} dm-item={info['dmItem']}")
        for a in info.get('ancestry', [])[:8]:
            log("dom", f"  parent[{a['i']}] {a['tag']} cls={a['cls']!r} {a['w']}x{a['h']} scrollH={a['scrollH']} ch={a['children']}")
        for sel, cnt in info.get('counts', {}).items():
            if cnt > 0:
                log("dom", f"  count {sel!r}={cnt}")
        for j, m in enumerate(info.get('msgEls', [])):
            log("dom", f"  msg[{j}] {m['w']}x{m['h']} de2e={m['de2e']!r} cls={m['cls']!r}")
    except Exception as e:
        log("dom", f"debug_dom_dump error: {e}")


# ── Debug screenshot ──────────────────────────────────────────────────────────
async def debug_screenshot(page: Page, tag: str) -> None:
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        path = f"{SCREENSHOTS_DIR}/debug_{tag}_{ts}.png"
        await page.screenshot(path=path)
        log("debug", f"screenshot: {path}")
    except Exception:
        pass


# ── Browser ───────────────────────────────────────────────────────────────────
async def launch_browser(playwright) -> Browser:
    return await playwright.chromium.launch(
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )


async def create_context(browser: Browser) -> BrowserContext:
    if not os.path.exists(STORAGE_STATE):
        raise FileNotFoundError(f"storage_state not found: {STORAGE_STATE}")
    return await browser.new_context(
        storage_state=STORAGE_STATE,
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )


async def verify_login(page: Page) -> bool:
    try:
        await page.goto("https://www.tiktok.com/messages",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        if re.search(r"login|passport", page.url):
            return False
        return True
    except Exception:
        return False


# ── Navigate to chat ──────────────────────────────────────────────────────────
async def navigate_to_chat(page: Page) -> bool:
    log("chat", f"goto {GROUP_CHAT_URL}")
    try:
        await page.goto(GROUP_CHAT_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log("chat", f"goto fail: {e}")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
        log("chat", "networkidle OK")
    except Exception:
        log("chat", "networkidle timeout, continuing")

    SKIP = {"loading...", "loading", "messages", "tin nhắn", "upload", ""}
    log("chat", "waiting for conversation list...")
    loaded = False
    for i in range(25):
        await asyncio.sleep(1)
        try:
            sample = await page.evaluate("""() => {
                for (const el of document.querySelectorAll('span,p,div,a')) {
                    const t = el.textContent.trim();
                    if (t.length > 3 && t.length < 80 && el.children.length === 0) return t;
                }
                return null;
            }""")
            if sample and sample.lower().strip() not in SKIP:
                log("chat", f"list ready at {i+1}s: {sample[:40]}")
                loaded = True
                break
        except Exception:
            pass

    if not loaded:
        log("chat", "list never loaded — trying anyway")

    await asyncio.sleep(0.5)

    # Click TARGET_CHAT_NAME via JS
    try:
        clicked = await page.evaluate(f"""() => {{
            const t = {json.dumps(TARGET_CHAT_NAME)};
            for (const el of document.querySelectorAll('span,p,div,a')) {{
                if (el.textContent.trim() === t && el.offsetParent) {{ el.click(); return 'exact'; }}
            }}
            for (const el of document.querySelectorAll('span,p,div,a')) {{
                if (el.textContent.trim().includes(t) && el.offsetParent) {{ el.click(); return 'partial'; }}
            }}
            return false;
        }}""")
        if clicked:
            log("chat", f"clicked: {clicked}")
            await asyncio.sleep(2.5)
            if await verify_in_chat(page):
                return True
    except Exception as e:
        log("chat", f"JS click error: {e}")

    # Playwright fallback
    for sel in [f'span:text-is("{TARGET_CHAT_NAME}")', f'div:has-text("{TARGET_CHAT_NAME}")']:
        try:
            el = page.locator(sel).first
            if await asyncio.wait_for(el.count(), timeout=3):
                await el.click()
                await asyncio.sleep(2.5)
                if await verify_in_chat(page):
                    return True
        except Exception:
            continue

    await debug_screenshot(page, "chat_not_found")
    return False


async def verify_in_chat(page: Page) -> bool:
    try:
        if re.search(r"/messages/\d+", page.url):
            log("chat", f"verified via URL: {page.url}")
            return True
        for sel in ['[data-e2e="dm-message-input"]', '[contenteditable="true"]', 'textarea']:
            try:
                if await asyncio.wait_for(page.locator(sel).first.count(), timeout=2):
                    log("chat", "verified: input found")
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


# ── Read messages ─────────────────────────────────────────────────────────────
async def read_new_messages(page: Page) -> list[dict]:
    """
    Return new non-bot messages. Uses DOM key dedup.
    On first call: marks all as baseline (seen), returns [].
    """
    global _baseline_done, _zero_items_count

    try:
        raw = await page.evaluate(_JS_EXTRACT)
    except Exception as e:
        log("read", f"JS error: {e}")
        return []

    # Baseline: always set on FIRST call, even if raw is empty
    if not _baseline_done:
        for m in (raw or []):
            k = make_msg_key(m["dataId"], m["sender"], m["text"], m["index"])
            seen_keys.add(k)
        _baseline_done = True
        log("read", f"baseline seen={len(seen_keys)} raw={len(raw or [])}")
        return []

    if not raw:
        _zero_items_count += 1
        if _zero_items_count % _ZERO_LOG_EVERY == 1:  # log 1st, 31st, 61st... not every poll
            log("read", f"js_extract=0 items (poll #{_zero_items_count})")
        return []

    # Items found — reset zero counter
    _zero_items_count = 0

    new_msgs = []
    for m in raw:
        text          = (m.get("text") or "").strip()
        sender        = (m.get("sender") or "").strip()
        sender_avatar = (m.get("senderAvatar") or "").strip()
        bubble_side   = m.get("bubbleSide", "")
        raw_type      = m.get("rawType", "text")
        is_self       = bool(m.get("isSelf"))
        idx           = m.get("index", 0)
        debug_src     = m.get("debugSource", "")

        # Derive bubble_side fallback if JS didn't send it
        if not bubble_side:
            bubble_side = "right" if is_self else "left"

        sender_key = make_sender_key(sender, sender_avatar, bubble_side, idx)
        key = make_msg_key(m.get("dataId", ""), sender, text, idx)

        # Already seen?
        if key in seen_keys:
            continue

        seen_keys.add(key)

        short = (text or f"[{raw_type}]")[:80]

        # Own message?
        if is_bot_own(sender, text, is_self):
            log("read", f"skip own key={key[:12]} sender_key={sender_key} text={short}")
            continue

        # Empty text + not media
        if not text and raw_type == "text":
            continue

        # Image/sticker with no text
        if raw_type in ("image", "sticker") and not text:
            log("read", f"new media sender={sender!r} sender_key={sender_key} side={bubble_side} type={raw_type}")
            new_msgs.append({
                "key": key, "sender": sender, "sender_avatar": sender_avatar,
                "sender_key": sender_key, "bubble_side": bubble_side,
                "text": "[image]" if raw_type == "image" else "[sticker]",
                "raw_type": raw_type, "is_own": False,
            })
            continue

        log("read", f"new user message sender={sender!r} sender_key={sender_key} side={bubble_side} type={raw_type} src={debug_src!r} text={short!r}")
        new_msgs.append({
            "key": key, "sender": sender, "sender_avatar": sender_avatar,
            "sender_key": sender_key, "bubble_side": bubble_side,
            "text": text, "raw_type": raw_type, "is_own": False,
        })

    return new_msgs


async def refresh_seen(page: Page) -> None:
    """After sending, mark all visible messages as seen to absorb bot's own bubbles."""
    global _baseline_done
    try:
        raw = await page.evaluate(_JS_EXTRACT)
        for m in (raw or []):
            k = make_msg_key(m.get("dataId",""), m.get("sender",""), m.get("text",""), m.get("index",0))
            seen_keys.add(k)
        log("read", f"refresh_seen total_seen={len(seen_keys)}")
    except Exception as e:
        log("read", f"refresh_seen error: {e}")


# ── Send ──────────────────────────────────────────────────────────────────────
async def send_message(page: Page, text: str) -> bool:
    if not text.strip():
        return False
    # Duplicate guard
    if sent_cache_has(text, ttl=SENT_DUP_TTL):
        log("send", f"skip dup text={_norm(text)[:60]}")
        return False

    try:
        inp = None
        for sel in [
            '[data-e2e="dm-message-input"]',
            '[contenteditable="true"]',
            'textarea[placeholder]',
        ]:
            try:
                loc = page.locator(sel).first
                if await asyncio.wait_for(loc.count(), timeout=2):
                    inp = loc
                    break
            except Exception:
                continue

        if inp is None:
            log("send", "no input box")
            return False

        await inp.click()
        await asyncio.sleep(0.15)
        await inp.fill(text)
        await asyncio.sleep(0.25)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)

        sent_cache_add(text)
        log("send", f"text={_norm(text)[:80]}")
        return True

    except Exception as e:
        log("send", f"error: {e}")
        return False


def smart_split(messages_from_backend: list[str], reply_text: str) -> list[str]:
    """Use backend messages list if available, otherwise split reply_text."""
    if messages_from_backend:
        bubbles = [b for b in messages_from_backend if b.strip()]
        return bubbles[:5]
    # Split reply_text
    lines = [l.strip() for l in reply_text.split("\n") if l.strip()]
    SKIP = {"1.","2.","3.","4.","5.","-","•","*"}
    lines = [l for l in lines if l not in SKIP]
    return lines[:5] if lines else [reply_text]


async def send_bubbles(page: Page, bubbles: list[str]) -> None:
    count = 0
    for b in bubbles[:5]:
        if not b.strip():
            continue
        ok = await send_message(page, b)
        if ok:
            count += 1
        await asyncio.sleep(0.5)
    log("send", f"count={count}/{len(bubbles)}")
    # Absorb bot's own messages before next poll
    await asyncio.sleep(1.0)
    await refresh_seen(page)


# ── Screenshot tool ───────────────────────────────────────────────────────────
async def maybe_handle_shot(page: Page, context: BrowserContext, text: str) -> bool:
    if not is_shot_command(text):
        return False
    url = extract_shot_url(text)
    if not url.startswith("http"):
        await send_message(page, f"url không hợp lệ: {url}")
        await refresh_seen(page)
        return True
    log("tool", f"screenshot: {url}")
    try:
        ok, result = await asyncio.wait_for(
            screenshot_url(lambda: context.new_page(), url),
            timeout=25,
        )
    except asyncio.TimeoutError:
        ok, result = False, "timeout"
    if not ok:
        await send_message(page, f"chụp màn hình lỗi: {result}")
        await refresh_seen(page)
        return True
    try:
        uploaded = await asyncio.wait_for(upload_image_tiktok(page, result), timeout=20)
    except asyncio.TimeoutError:
        uploaded = False
    if not uploaded:
        await send_message(page, f"tao chụp được rồi nhưng upload ảnh lỗi, file: {result}")
    await refresh_seen(page)
    return True


# ── Backend call ──────────────────────────────────────────────────────────────
async def call_backend(username: str, content: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(
                f"{BACKEND_URL}/message",
                json={"username": username, "content": content},
            )
        # 400 = garbage content rejected, 503 = error suppressed — both: stay silent
        if r.status_code in (400, 503):
            detail = r.json().get("detail", "")
            log("backend", f"silent drop status={r.status_code} detail={detail}")
            return []
        d = r.json()
        had_error = d.get("error", False)
        reply = d.get("reply", "")
        msgs  = d.get("messages", [])
        log("backend", f"{'error' if had_error else 'ok'} reply={reply[:60]}")
        return smart_split(msgs, reply)
    except Exception as e:
        log("backend", f"error: {e}")
        return []  # Don't send fallback here — backend handles it with rate limit


# ── Main loop ─────────────────────────────────────────────────────────────────
async def bot_loop():
    global _baseline_done

    await tg_send("🤖 TikTok bot khởi động...")
    log("login", "start")

    restart_count = 0
    async with async_playwright() as pw:
        while restart_count < 20:
            browser = context = page = None
            try:
                log("login", f"launch browser restart=#{restart_count}")
                browser = await launch_browser(pw)
                context = await create_context(browser)
                page    = await context.new_page()

                if not await verify_login(page):
                    await debug_screenshot(page, "login_fail")
                    msg = "❌ Login fail — storage_state hết hạn?"
                    log("login", msg); await tg_send(msg)
                    await asyncio.sleep(30)
                    restart_count += 1
                    continue

                log("login", "OK")

                if not await navigate_to_chat(page):
                    await debug_screenshot(page, "chat_fail")
                    msg = f"❌ Không vào được chat '{TARGET_CHAT_NAME}'"
                    log("chat", msg); await tg_send(msg)
                    await asyncio.sleep(15)
                    restart_count += 1
                    continue

                log("chat", f"in '{TARGET_CHAT_NAME}' — polling")
                await tg_send(f"✅ Bot vào chat '{TARGET_CHAT_NAME}', polling.")

                # Boot marker for log clarity
                import os as _os
                log("boot", f"pid={_os.getpid()} file={__file__}")

                # Reset state
                _baseline_done = False
                seen_keys.clear()
                _zero_items_count = 0

                # Scroll message list to bottom so most-recent messages are visible before baseline
                # DOM debug dump — runs once to show real TikTok element structure
                await debug_dom_dump(page)

                # Scroll message list to bottom before baseline
                try:
                    await page.evaluate("""() => {
                        const el = document.querySelector('[data-e2e="dm-message-list"]') ||
                                   document.querySelector('[class*="DivMessageList"]') ||
                                   document.querySelector('[class*="message-list"]') ||
                                   document.body;
                        el.scrollTop = el.scrollHeight;
                    }""")
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

                # Fetch and persist chat metadata for Telegram /tiktok_chat_info
                try:
                    chat_info = await get_chat_info(page)
                    chat_info["chat_name"] = TARGET_CHAT_NAME
                    chat_info["updated_at"] = datetime.now(TZ).isoformat()
                    _chat_info_path = "/opt/tiktok-bot/data/chat_info.json"
                    os.makedirs(os.path.dirname(_chat_info_path), exist_ok=True)
                    with open(_chat_info_path, "w", encoding="utf-8") as _f:
                        json.dump(chat_info, _f, ensure_ascii=False, indent=2)
                    log("chat", f"chat_info title={chat_info.get('chatTitle')!r} members={chat_info.get('memberCount')}")
                except Exception as _e:
                    log("chat", f"chat_info save error: {_e}")

                async def send_reminder(msg: str):
                    await send_message(page, msg)
                    await refresh_seen(page)

                reminder_task = asyncio.create_task(
                    reminder_loop(send_reminder, interval=30)
                )

                while True:
                    try:
                        if page.is_closed():
                            raise PlaywrightError("page closed")

                        msgs = await read_new_messages(page)
                        for m in msgs:
                            text        = m["text"]
                            sender      = m.get("sender", "?")
                            sender_key  = m.get("sender_key", "")
                            bubble_side = m.get("bubble_side", "")
                            log("process", f"sender={sender!r} key={sender_key} side={bubble_side} text={text[:80]!r}")
                            handled = await maybe_handle_shot(page, context, text)
                            if handled:
                                continue
                            if not text.strip():
                                continue
                            replies = await call_backend(TARGET_CHAT_NAME, text)
                            if replies:
                                await send_bubbles(page, replies)
                            else:
                                log("backend", "silent (no reply to send)")

                        await asyncio.sleep(POLL_INTERVAL)

                    except PlaywrightError as e:
                        if "closed" in str(e) or "Target" in str(e):
                            log("watchdog", f"page closed: {e}")
                            await tg_send(f"⚠️ Browser crash")
                            break
                        log("watchdog", f"pw error: {e}")
                        await asyncio.sleep(2)
                    except Exception as e:
                        log("watchdog", f"poll error: {e}")
                        await asyncio.sleep(2)

                reminder_task.cancel()
                try:
                    await reminder_task
                except asyncio.CancelledError:
                    pass

            except FileNotFoundError as e:
                log("login", f"no storage_state: {e}")
                await tg_send("❌ storage_state not found")
                break
            except Exception as e:
                log("watchdog", f"outer: {e}")
                await tg_send(f"⚠️ {str(e)[:200]}")
            finally:
                for obj in (page, context, browser):
                    if obj:
                        try:
                            await obj.close()
                        except Exception:
                            pass

            restart_count += 1
            wait = min(30 * restart_count, 300)
            log("watchdog", f"restart in {wait}s (#{restart_count})")
            await tg_send(f"🔄 Restart #{restart_count}")
            _baseline_done = False
            seen_keys.clear()
            await asyncio.sleep(wait)

    log("watchdog", "max restarts reached")
    await tg_send("❌ Bot dừng (max restarts)")


if __name__ == "__main__":
    asyncio.run(bot_loop())
