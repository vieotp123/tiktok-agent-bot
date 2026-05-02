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


def is_btc_query(text: str) -> bool:
    kw = ["btc", "bitcoin", "giá coin", "giá btc", "bitcoin hôm nay", "coin hôm nay"]
    t = text.lower()
    return any(k in t for k in kw)


def is_shot_command(text: str) -> bool:
    return text.strip().startswith("/shot ")


def extract_shot_url(text: str) -> str:
    return text.strip()[len("/shot "):].strip()
