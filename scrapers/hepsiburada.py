"""
Hepsiburada scraper — Playwright Chrome + playwright-stealth.
Next.js tabanlı site — __NEXT_DATA__ JSON tercihli, HTML fallback ikincil.
"""

import asyncio
import json
import logging
import random
import re
from typing import AsyncIterator, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    TimeoutError as PWTimeout,
)
from playwright_stealth import Stealth

from anti_ban import ScraperSession
from models import Product
from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE = "https://www.hepsiburada.com"
VIEWPORT = {"width": 1366, "height": 768}


class HepsiburadaScraper(BaseScraper):
    async def scrape_category(
        self, category: str, max_pages: int = 5
    ) -> AsyncIterator[Product]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
                ],
            )
            ctx = await _new_context(browser)
            page = await ctx.new_page()
            await Stealth().apply_stealth_async(page)

            try:
                for page_num in range(1, max_pages + 1):
                    logger.info("[Hepsiburada] sayfa %d / %d — '%s'", page_num, max_pages, category)

                    url = f"{BASE}/ara?q={quote(category)}&sayfa={page_num}"
                    products = await self._scrape_page(page, url, category, page_num)

                    if not products:
                        logger.info("Sayfa %d'de ürün yok, duruluyor.", page_num)
                        break

                    for p in products:
                        yield p

                    await _human_scroll(page)
                    await asyncio.sleep(random.uniform(
                        self.session._config.delay_min,
                        self.session._config.delay_max,
                    ))
            finally:
                await browser.close()

    async def _scrape_page(
        self, page: Page, url: str, category: str, page_num: int
    ) -> list[Product]:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PWTimeout:
            logger.warning("Hepsiburada sayfa %d zaman aşımı, atlanıyor", page_num)
            return []

        if await _is_blocked(page):
            logger.warning("Hepsiburada bot koruması algılandı (sayfa %d)", page_num)
            await asyncio.sleep(random.uniform(20, 35))
            return []

        try:
            await page.wait_for_selector(
                "li[class*='productListContent'], div[class*='product-card'], li[data-product-id]",
                timeout=15_000,
            )
        except PWTimeout:
            pass

        await _human_scroll(page)
        html = await page.content()

        products = self._extract_next_data(html, category)
        if products:
            return products

        logger.debug("__NEXT_DATA__ bulunamadı, HTML fallback deneniyor")
        products = self._parse_html(html, category)
        logger.info("  → %d ürün (HTML)", len(products))
        return products

    def _extract_next_data(self, html: str, category: str) -> list[Product]:
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return []

        try:
            data: dict = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        items = (
            _deep_get(data, "props", "pageProps", "products")
            or _deep_get(data, "props", "pageProps", "initialState", "listings", "products")
            or _deep_get(data, "props", "pageProps", "searchResult", "products")
            or _deep_get(data, "props", "pageProps", "data", "products")
            or _find_key(data, "products")
            or []
        )

        if not isinstance(items, list):
            return []

        products: list[Product] = []
        for item in items:
            try:
                products.append(self._map_product(item, category))
            except Exception as exc:
                logger.debug("HB JSON item hatası: %s", exc)

        logger.info("  → %d ürün (JSON)", len(products))
        return products

    def _map_product(self, item: dict, category: str) -> Product:
        pid = str(item.get("id") or item.get("sku") or item.get("productId", ""))
        slug = item.get("url") or item.get("productUrl") or ""
        url = f"{BASE}{slug}" if slug.startswith("/") else slug or f"{BASE}/{pid}"

        price_raw = item.get("price") or item.get("currentPrice") or item.get("priceInfo", {})
        if isinstance(price_raw, dict):
            price = _to_float(price_raw.get("price") or price_raw.get("discountedPrice"))
            original = _to_float(price_raw.get("originalPrice") or price_raw.get("listingPrice"))
        else:
            price = _to_float(price_raw)
            original = _to_float(item.get("originalPrice") or item.get("listPrice"))

        discount = _to_float(item.get("discountRate") or item.get("discount"))

        rating_raw = item.get("rating") or item.get("averageRating") or item.get("ratingScore")
        if isinstance(rating_raw, dict):
            rating = _to_float(rating_raw.get("average") or rating_raw.get("value"))
        else:
            rating = _to_float(rating_raw)

        review_count = _to_int(
            item.get("reviewCount") or item.get("ratingCount") or item.get("totalRating")
        )

        seller_raw = item.get("merchant") or item.get("seller") or item.get("storefront")
        seller = (
            seller_raw.get("name") or seller_raw.get("merchantName")
            if isinstance(seller_raw, dict)
            else str(seller_raw) if seller_raw else None
        )

        images_raw = item.get("images") or item.get("imageUrls") or []
        images = images_raw[:5] if isinstance(images_raw, list) else []

        return Product(
            platform="hepsiburada",
            category=category,
            product_id=pid,
            title=item.get("name") or item.get("title") or item.get("productName", ""),
            brand=item.get("brand") or item.get("brandName"),
            url=url,
            price=price,
            original_price=original,
            discount_rate=discount,
            in_stock=not item.get("outOfStock", False),
            rating=rating,
            review_count=review_count,
            seller=seller,
            images=images,
        )

    def _parse_html(self, html: str, category: str) -> list[Product]:
        soup = BeautifulSoup(html, "lxml")
        products: list[Product] = []

        cards = (
            soup.select("li[class*='productListContent']")
            or soup.select("div[class*='product-card']")
            or soup.select("li[data-product-id]")
        )

        for card in cards:
            try:
                link = card.select_one("a[href*='/']") or card.select_one("a")
                if not link:
                    continue
                href = link.get("href", "")
                url = f"{BASE}{href}" if href.startswith("/") else href
                pid = card.get("data-product-id") or card.get("data-sku") or href

                title_tag = (
                    card.select_one("h3[class*='title']")
                    or card.select_one("span[class*='product-name']")
                    or card.select_one("a[class*='product']")
                    or link
                )
                title = title_tag.get_text(strip=True) if title_tag else ""

                price_tag = (
                    card.select_one("span[class*='price']")
                    or card.select_one("div[class*='price']")
                )
                price = _parse_price(price_tag.get_text(strip=True)) if price_tag else None

                img_tag = card.select_one("img")
                images: list[str] = []
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src") or ""
                    if src:
                        images = [src]

                if title and url:
                    products.append(Product(
                        platform="hepsiburada",
                        category=category,
                        product_id=str(pid),
                        title=title,
                        url=url,
                        price=price,
                        images=images,
                    ))
            except Exception as exc:
                logger.debug("HB HTML parse hatası: %s", exc)

        return products


async def _new_context(browser) -> BrowserContext:
    return await browser.new_context(
        viewport=VIEWPORT,
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


async def _is_blocked(page: Page) -> bool:
    content = await page.content()
    signals = ["robot", "captcha", "doğrulama", "verification", "challenge"]
    lower = content.lower()
    return any(s in lower for s in signals) and len(content) < 5000


async def _human_scroll(page: Page) -> None:
    try:
        total_height = await page.evaluate("document.body.scrollHeight")
        current = 0
        while current < total_height:
            scroll_by = random.randint(300, 700)
            current = min(current + scroll_by, total_height)
            await page.evaluate(f"window.scrollTo(0, {current})")
            await asyncio.sleep(random.uniform(0.3, 0.9))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(0.5, 1.2))
    except Exception:
        pass


def _deep_get(d: dict, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = _find_key(v, key)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_key(item, key)
            if result is not None:
                return result
    return None


def _parse_price(text: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d,.]", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _to_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
