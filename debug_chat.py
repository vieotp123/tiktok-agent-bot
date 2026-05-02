"""
Debug script: open TikTok messages, wait, dump structure, take screenshot.
"""
import asyncio
import json
import os
import sys
sys.path.insert(0, "/opt/tiktok-bot")
from dotenv import load_dotenv
load_dotenv("/opt/tiktok-bot/.env")
from playwright.async_api import async_playwright

STORAGE_STATE = "/opt/tiktok-bot/tiktok_storage_state.json"
TARGET = os.getenv("TARGET_CHAT_NAME", "Chatgibiti")

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            storage_state=STORAGE_STATE,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        print("[debug] opening messages...")
        await page.goto("https://www.tiktok.com/messages", wait_until="networkidle", timeout=45000)
        print("[debug] networkidle reached")

        # Wait up to 15s for conversation list to render
        print("[debug] waiting for DOM content to render...")
        for i in range(15):
            await asyncio.sleep(1)
            count = await page.evaluate("() => document.querySelectorAll('*').length")
            print(f"  [{i+1}s] DOM nodes: {count}")
            if count > 200:
                print(f"[debug] DOM ready at {i+1}s ({count} nodes)")
                break

        # Screenshot
        await page.screenshot(path="/opt/tiktok-bot/screenshots/debug_manual.png")
        print("[debug] screenshot saved")

        # Dump all visible text
        texts = await page.evaluate("""() => {
            const results = [];
            const els = document.querySelectorAll('*');
            for (const el of els) {
                const t = (el.textContent || '').trim();
                if (t && t.length > 1 && t.length < 100 && el.children.length === 0) {
                    results.push({tag: el.tagName, cls: el.className.substring(0,80), text: t});
                    if (results.length > 150) break;
                }
            }
            return results;
        }""")

        print(f"\n[debug] Found {len(texts)} text nodes:")
        target_found = False
        for t in texts:
            print(f"  <{t['tag']} class='{t['cls']}'> {repr(t['text'])}")
            if TARGET.lower() in t['text'].lower():
                target_found = True
                print(f"  *** FOUND TARGET: {t['text']} ***")

        if not target_found:
            print(f"\n[debug] TARGET '{TARGET}' NOT found in any text node")

        # Try Playwright locator
        print(f"\n[debug] Playwright count for has-text='{TARGET}':")
        for sel in [f'text="{TARGET}"', f'span:has-text("{TARGET}")', f'div:has-text("{TARGET}")', f'*:has-text("{TARGET}")']:
            try:
                cnt = await page.locator(sel).count()
                print(f"  {sel}: {cnt}")
            except Exception as e:
                print(f"  {sel}: error {e}")

        # Dump page title and URL
        print(f"\n[debug] URL: {page.url}")
        print(f"[debug] Title: {await page.title()}")

        await browser.close()

asyncio.run(main())
