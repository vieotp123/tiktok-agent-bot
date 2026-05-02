import httpx
import asyncio
import os
from datetime import datetime
import pytz

SCREENSHOTS_DIR = "/opt/tiktok-bot/screenshots"
TZ = pytz.timezone("Asia/Tokyo")


async def get_btc_price() -> str:
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=usd,jpy,vnd&include_24hr_change=true"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        btc = data.get("bitcoin", {})
        usd = btc.get("usd", 0)
        jpy = btc.get("jpy", 0)
        vnd = btc.get("vnd", 0)
        change = btc.get("usd_24h_change", None)
        change_str = f" ({change:+.2f}% 24h)" if change is not None else ""
        return (
            f"Bitcoin hiện tại:\n"
            f"💵 ${usd:,.0f}{change_str}\n"
            f"¥ {jpy:,.0f} JPY\n"
            f"₫ {vnd:,.0f} VND"
        )
    except Exception as e:
        return f"Lấy giá BTC lỗi rồi: {e}"


async def screenshot_url(page_factory, url: str) -> tuple[bool, str]:
    """
    Take screenshot of URL using a new browser page.
    Returns (success, path_or_error).
    """
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"shot_{ts}.png"
    filepath = os.path.join(SCREENSHOTS_DIR, filename)

    page = None
    try:
        page = await asyncio.wait_for(page_factory(), timeout=5)
        await asyncio.wait_for(
            page.goto(url, wait_until="networkidle"),
            timeout=20
        )
        await asyncio.wait_for(
            page.screenshot(path=filepath, full_page=False),
            timeout=5
        )
        return True, filepath
    except asyncio.TimeoutError:
        return False, f"timeout khi mở {url}"
    except Exception as e:
        return False, str(e)
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def upload_image_tiktok(page, filepath: str) -> bool:
    """
    Attempt to upload image to TikTok chat via file input.
    Returns True if upload detected, False otherwise.
    """
    try:
        # Look for file input or image attach button
        selectors = [
            'input[type="file"]',
            '[data-e2e="dm-message-attach"]',
            'button[aria-label*="image"]',
            'button[aria-label*="photo"]',
            '[data-e2e="image-upload"]',
        ]

        file_input = None
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await asyncio.wait_for(el.count(), timeout=2):
                    file_input = el
                    break
            except Exception:
                continue

        if file_input is None:
            # Try to reveal hidden input via click on attach area
            attach_btns = [
                '[data-e2e="dm-message-attach"]',
                'button[class*="attach"]',
            ]
            for sel in attach_btns:
                try:
                    btn = page.locator(sel).first
                    if await asyncio.wait_for(btn.count(), timeout=2):
                        await btn.click()
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue

            # Try again
            try:
                file_input = page.locator('input[type="file"]').first
                cnt = await asyncio.wait_for(file_input.count(), timeout=2)
                if not cnt:
                    return False
            except Exception:
                return False

        await asyncio.wait_for(file_input.set_input_files(filepath), timeout=5)
        await asyncio.sleep(1)

        # Look for send/confirm button
        send_selectors = [
            '[data-e2e="dm-message-send"]',
            'button[data-e2e="send"]',
            'button[type="submit"]',
        ]
        for sel in send_selectors:
            try:
                btn = page.locator(sel).first
                if await asyncio.wait_for(btn.count(), timeout=2):
                    await btn.click()
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue

        # Press Enter as fallback
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)
        return True

    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


async def search_web(query: str, max_results: int = 4) -> list[dict]:
    """
    Search web via DuckDuckGo HTML endpoint.
    Returns list of {title, url, snippet} dicts, or [{title:'error',...}] on failure.
    Never invents links — all URLs come directly from DDG response.
    """
    import re
    from urllib.parse import unquote

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "vi,en;q=0.9",
    }

    def strip_tags(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                        ("&#x27;", "'"), ("&quot;", '"'), ("&nbsp;", " ")]:
            s = s.replace(ent, ch)
        return s.strip()

    def extract_url(href: str) -> str:
        """Resolve DDG redirect URL to real URL."""
        if "uddg=" in href:
            m = re.search(r"uddg=([^&\"]+)", href)
            if m:
                return unquote(m.group(1))
        if href.startswith("//"):
            return "https:" + href
        return href

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            r = await c.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
            )
        html = r.text

        # Extract all title anchors and snippets in document order
        anchors = re.findall(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.S,
        )
        snippets_raw = re.findall(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html, re.S,
        )

        results: list[dict] = []
        for i, (href, title_html) in enumerate(anchors[:max_results]):
            url = extract_url(href)
            title = strip_tags(title_html)
            snippet = strip_tags(snippets_raw[i]) if i < len(snippets_raw) else ""
            if title:
                results.append({"title": title, "url": url, "snippet": snippet})

        return results if results else [
            {"title": "no_results", "url": "", "snippet": "Không tìm thấy kết quả."}
        ]

    except Exception as e:
        return [{"title": "search_error", "url": "", "snippet": f"Lỗi tìm kiếm: {e}"}]


def format_search_results(results: list[dict]) -> str:
    """Format search results as a compact context string for LLM."""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet']}")
        if r.get("url"):
            lines.append(f"    {r['url']}")
    return "\n".join(lines)


def is_search_query(text: str) -> bool:
    """Detect search intent keywords."""
    keywords = [
        "tìm kiếm", "tìm thông tin", "tìm hiểu",
        "search", "mới nhất", "tin mới", "tin tức",
        "trend", "thông tin về", "cho tao biết về",
        "tìm về", "news về", "update về",
    ]
    t = text.lower()
    # Also catch short "tìm X" patterns
    if re.search(r'^tìm\s+\w', t):
        return True
    return any(k in t for k in keywords)


def extract_search_query(text: str) -> str:
    """Strip intent keywords to get clean search query."""
    t = text.strip()
    prefixes = [
        "tìm kiếm ", "tìm thông tin về ", "tìm thông tin ",
        "tìm hiểu về ", "tìm hiểu ", "search ", "tìm về ",
        "thông tin về ", "cho tao biết về ", "tin mới về ",
        "tin tức về ", "update về ", "news về ", "tìm ",
    ]
    tl = t.lower()
    for p in prefixes:
        if tl.startswith(p):
            return t[len(p):].strip()
    return t


import re  # noqa: E402 — used above, re-imported for module-level use


def is_btc_query(text: str) -> bool:
    kw = ["btc", "bitcoin", "giá coin", "giá btc", "bitcoin hôm nay", "coin hôm nay"]
    t = text.lower()
    return any(k in t for k in kw)


def is_shot_command(text: str) -> bool:
    return text.strip().startswith("/shot ")


def extract_shot_url(text: str) -> str:
    return text.strip()[len("/shot "):].strip()
