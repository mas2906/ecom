"""
Barkod zenginleştirici — DB'deki barkodu olmayan ürünlerin
detay sayfalarını ziyaret edip barkodu günceller.
"""

import asyncio
import logging
import random
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _get_barcode(page, url: str, platform: str) -> Optional[str]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(random.randint(1200, 2000))
        html = await page.content()

        if platform == "trendyol":
            from scrapers.trendyol import _extract_barcode_from_html
        else:
            from scrapers.amazon import _extract_barcode_from_html

        return _extract_barcode_from_html(html)
    except Exception as e:
        logger.debug("Barkod alınamadı (%s): %s", url, e)
        return None


async def enrich_barcodes(
    pool,
    platform: Optional[str] = None,
    limit: int = 50,
    concurrency: int = 2,
) -> int:
    from db import fetch_without_barcode, update_barcode

    rows = await fetch_without_barcode(pool, platform=platform, limit=limit)
    if not rows:
        logger.info("Barkod zenginleştirme: zaten tüm ürünler dolu")
        return 0

    logger.info("Barkod zenginleştirme: %d ürün ziyaret edilecek", len(rows))
    updated = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            user_agent=_UA,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
        )
        await Stealth().apply_stealth_async(await context.new_page())
        await context.pages[0].close()

        sem = asyncio.Semaphore(concurrency)

        async def process(row):
            nonlocal updated
            async with sem:
                page = await context.new_page()
                try:
                    bc = await _get_barcode(page, row["url"], row["platform"])
                    if bc:
                        await update_barcode(pool, bc, row["platform"], row["product_id"])
                        updated += 1
                        logger.info(
                            "  ✓ [%s] %s → barkod: %s",
                            row["platform"], row["product_id"], bc,
                        )
                    await asyncio.sleep(random.uniform(0.8, 1.8))
                finally:
                    await page.close()

        await asyncio.gather(*[process(r) for r in rows])
        await browser.close()

    logger.info("Barkod zenginleştirme tamamlandı: %d / %d güncellendi", updated, len(rows))
    return updated
