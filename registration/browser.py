"""Browser automation helpers for the registration service.

Thin, reusable Playwright wrappers used by provider plugins. Runs headless in
Docker (Chromium with --no-sandbox). Local debugging: set HEADLESS=false.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page


async def launch(proxy: Optional[str] = None, headless: bool = True) -> tuple[Browser, BrowserContext]:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        proxy={"server": proxy} if proxy else None,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    # Reduce automation fingerprint.
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return browser, context


async def fill_and_submit(page: Page, selector: str, value: str, delay: float = 0.1):
    await page.wait_for_selector(selector, timeout=30000)
    await page.fill(selector, value)
    await asyncio.sleep(delay)


async def click(page: Page, selector: str):
    await page.wait_for_selector(selector, timeout=30000)
    await page.click(selector)


async def wait_for_url(page: Page, fragment: str, timeout: int = 30000):
    await page.wait_for_url(f"**/{fragment}**", timeout=timeout)
