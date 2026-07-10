"""
Amazon TR scraper:
Amazon'un bot koruması (Reese84 / PerimeterX) curl_cffi'yi atlayabilir.
Bu yüzden Playwright + playwright-stealth kullanılır; çok daha güvenilir.
"""

import asyncio
import logging
import random
import re
from typing import AsyncIterator, Optional
from urllib.parse import quote

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

BASE = "https://www.amazon.com.tr"
VIEWPORT = {"width": 1366, "height": 768}


class AmazonScraper(BaseScraper):
    async def scrape_category(
        self, category: str, max_pages: int = 5
    ) -> AsyncIterator[Product]:
        seen_asins: set[str] = set()   # sayfalararası ASIN tekil alma

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
                ],
            )
            ctx = await _new_context(browser)
            page = await ctx.new_page()
            await Stealth().apply_stealth_async(page)

            try:
                for page_num in range(1, max_pages + 1):
                    url = f"{BASE}/s?k={quote(category)}&page={page_num}&language=tr_TR"
                    logger.info("[Amazon] sayfa %d / %d — '%s'", page_num, max_pages, category)

                    products = await self._scrape_page(page, url, category, page_num)

                    if not products:
                        logger.info("Sayfa %d boş, duruluyor.", page_num)
                        break

                    new_count = 0
                    for p in products:
                        if p.product_id and p.product_id not in seen_asins:
                            seen_asins.add(p.product_id)
                            yield p
                            new_count += 1

                    if new_count == 0:
                        logger.info("Sayfa %d tamamen mükerrer, duruluyor.", page_num)
                        break

                    if page_num < max_pages:
                        wait = random.uniform(4.0, 10.0)
                        logger.info("  ⏳ %.1fs bekleniyor…", wait)
                        await asyncio.sleep(wait)

            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Tek sayfa scraping
    # ------------------------------------------------------------------

    async def _scrape_page(
        self, page: Page, url: str, category: str, page_num: int
    ) -> list[Product]:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PWTimeout:
            logger.warning("Amazon sayfa %d zaman aşımı, atlanıyor", page_num)
            return []

        # CAPTCHA / robot ekranı kontrolü
        if await self._is_blocked(page):
            logger.warning("Amazon bot koruması algılandı (sayfa %d)", page_num)
            await asyncio.sleep(random.uniform(20, 35))
            return []

        # Ürünlerin yüklenmesini bekle
        try:
            await page.wait_for_selector(
                'div[data-component-type="s-search-result"]', timeout=15_000
            )
        except PWTimeout:
            logger.warning("Ürün kartları yüklenemedi (sayfa %d)", page_num)
            return []

        await _human_scroll(page)

        cards = await page.query_selector_all(
            'div[data-component-type="s-search-result"]'
        )
        products: list[Product] = []

        for card in cards:
            try:
                p = await self._parse_card(card, category)
                if p:
                    products.append(p)
            except Exception as exc:
                logger.debug("Amazon card parse hatası: %s", exc)

        logger.info("  → %d ürün (Playwright)", len(products))
        return products

    async def _parse_card(self, card, category: str) -> Optional[Product]:
        # ASIN
        asin = await card.get_attribute("data-asin") or ""
        if not asin:
            return None

        # URL
        link_el = await card.query_selector("h2 a") or await card.query_selector("a.a-link-normal")
        href = await link_el.get_attribute("href") if link_el else ""
        url = f"{BASE}{href}" if href.startswith("/") else href or f"{BASE}/dp/{asin}"

        # Başlık
        title_el = await card.query_selector("h2 span.a-text-normal") or await card.query_selector("h2 span")
        title = (await title_el.inner_text()).strip() if title_el else ""

        # Marka (genellikle başlıktan önce veya ayrı bir span'da)
        brand_el = await card.query_selector("span.a-size-base-plus.a-color-base")
        brand = (await brand_el.inner_text()).strip() if brand_el else None

        # Fiyat — a-offscreen tam metni (ör: "₺3.821,02") en güvenilir kaynak
        price: Optional[float] = None
        offscreen_el = await card.query_selector("span.a-price:not(.a-text-price) span.a-offscreen")
        if offscreen_el:
            price = _parse_price(await offscreen_el.inner_text())
        if price is None:
            # Fallback: whole + fraction parçalı
            whole_el = await card.query_selector("span.a-price-whole")
            fraction_el = await card.query_selector("span.a-price-fraction")
            if whole_el:
                whole_text = (await whole_el.inner_text()).replace(".", "").replace(",", "").strip()
                fraction_text = (await fraction_el.inner_text()).strip() if fraction_el else "00"
                try:
                    price = float(f"{whole_text}.{fraction_text}")
                except ValueError:
                    pass

        # Orijinal fiyat
        orig_el = await card.query_selector("span.a-price.a-text-price span.a-offscreen")
        original_price: Optional[float] = None
        if orig_el:
            original_price = _parse_price(await orig_el.inner_text())

        # Puan
        rating_el = await card.query_selector("span.a-icon-alt")
        rating: Optional[float] = None
        if rating_el:
            txt = await rating_el.inner_text()
            m = re.search(r"([\d,\.]+)", txt)
            if m:
                rating = float(m.group(1).replace(",", "."))

        # Yorum sayısı — birden fazla seçici dene (sayfa yapısı değişkendir)
        review_count: Optional[int] = None
        for rc_sel in (
            'a[href*="customerReviews"] span',
            "span.s-underline-text",
            "a span[aria-label]",
            "span.a-size-base",
        ):
            rc_el = await card.query_selector(rc_sel)
            if rc_el:
                txt = (await rc_el.inner_text()).replace(".", "").replace(",", "").strip()
                txt = txt.strip("()")
                m = re.search(r"(\d+)", txt)
                if m:
                    review_count = _to_int(m.group(1))
                    break

        # Fiyat rozeti — "60 günün en düşük fiyatı", "30 günün...", "Yılın en düşük fiyatı" vb.
        price_badge: Optional[str] = None
        badge_el = await card.query_selector("span.a-badge-text")
        if badge_el:
            badge_text = (await badge_el.inner_text()).strip()
            if any(kw in badge_text.lower() for kw in ("en düşük", "lowest price", "düşük fiyat")):
                price_badge = badge_text

        # Satıcı (sadece "Amazon.com.tr tarafından..." mesajı varsa)
        seller_el = await card.query_selector("span[class*='sold-by']") or await card.query_selector("div[class*='merchant']")
        seller = (await seller_el.inner_text()).strip() if seller_el else None

        # Görsel
        img_el = await card.query_selector("img.s-image")
        images: list[str] = []
        if img_el:
            src = await img_el.get_attribute("src")
            if src:
                images = [src]

        if not title:
            return None

        return Product(
            platform="amazon",
            category=category,
            product_id=asin,
            title=title,
            brand=brand,
            url=url,
            price=price,
            original_price=original_price,
            in_stock=True,
            rating=rating,
            review_count=review_count,
            price_badge=price_badge,
            seller=seller,
            images=images,
        )

    async def _is_blocked(self, page: Page) -> bool:
        content = await page.content()
        blocked_signals = [
            "Robot doğrulama",
            "CAPTCHA",
            "Enter the characters you see below",
            "api-services-support@amazon.com",
            "automated access",
            "automated requests",
        ]
        return any(s.lower() in content.lower() for s in blocked_signals)


# ------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ------------------------------------------------------------------

async def _new_context(browser) -> BrowserContext:
    return await browser.new_context(
        viewport=VIEWPORT,
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        ignore_https_errors=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


async def _human_scroll(page: Page) -> None:
    """
    Kademeli + rastgele geri-scroll içeren insan benzeri kaydırma.
    Lazy-load içeriklerini tetikler, bot tespitini engeller.
    """
    try:
        total_height = await page.evaluate("document.body.scrollHeight")
        current = 0
        steps = random.randint(6, 12)

        for _ in range(steps):
            step = random.randint(400, 900)
            current = min(current + step, total_height)
            await page.evaluate(f"window.scrollTo(0, {current})")
            await asyncio.sleep(random.uniform(0.4, 1.3))

            # %25 ihtimalle biraz yukarı geri kaydır (gerçek kullanıcı davranışı)
            if random.random() < 0.25:
                back = random.randint(150, 400)
                current = max(0, current - back)
                await page.evaluate(f"window.scrollTo(0, {current})")
                await asyncio.sleep(random.uniform(0.3, 0.8))

        # Tüm lazy-load ürünlerin DOM'a gelmesi için sayfanın en altına in
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(0.3, 0.7))
    except Exception:
        pass




def _parse_price(text: str) -> Optional[float]:
    # Türkçe format: "1.649,00 TL" → binlik ayraç=nokta, ondalık=virgül
    cleaned = re.sub(r"[^\d,.]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(str(val).replace(".", "").replace(",", ""))
    except (ValueError, TypeError):
        return None


def _extract_barcode_from_html(html: str) -> Optional[str]:
    """Amazon ürün detay sayfasından EAN/GTIN barkod çıkar.

    Gerçek kaynak: rhfHandlerParams.keywords — Amazon ürün sayfasının
    JS veri katmanında EAN barkod bu alanda tutulur.
    """
    # 1) rhfHandlerParams.keywords — en güvenilir kaynak
    for m in re.finditer(r'rhfHandlerParams["\s:]+(\{[^}]+\})', html):
        try:
            data = json.loads(m.group(1))
            kw = str(data.get("keywords", "")).strip()
            if re.fullmatch(r'[0-9]{8,14}', kw):
                return kw
        except Exception:
            pass

    # 2) JSON-LD structured data
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                data = data[0]
            for key in ("gtin13", "gtin8", "gtin", "isbn"):
                val = data.get(key)
                if val and re.fullmatch(r'[0-9]{8,14}', str(val).strip()):
                    return str(val).strip()
        except Exception:
            pass

    # 3) Ürün detay tablosu — Türkçe/İngilizce GTIN/EAN etiketleri
    # Amazon TR: "Global Ticaret Tanımlama Numarası" → prodDetAttrValue TD
    gtin_labels = (
        "Global Ticaret Tanımlama Numarası",
        "GTIN", "EAN", "UPC", "Barkod", "ISBN",
    )
    for label in gtin_labels:
        m = re.search(
            rf'{re.escape(label)}[^<]*</th>\s*<td[^>]*>\s*([0-9]{{8,14}})\s*</td>',
            html, re.DOTALL
        )
        if m:
            return _normalize_gtin(m.group(1))

    # Genel th→td tarama (prodDetAttrValue)
    m = re.search(
        r'prodDetAttrValue[^>]*>\s*([0-9]{12,14})\s*<',
        html,
    )
    if m:
        return _normalize_gtin(m.group(1))

    # 4) "ean" / "barcode" alanları herhangi bir JSON içinde
    for pattern in (r'"ean"\s*:\s*"([0-9]{8,14})"', r'"barcode"\s*:\s*"([0-9]{8,14})"'):
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return _normalize_gtin(m.group(1))

    return None


def _normalize_gtin(code: str) -> str:
    """GTIN-14'ü EAN-13'e çevir (başındaki 0'ı at)."""
    code = code.strip()
    if len(code) == 14 and code.startswith("0"):
        return code[1:]
    return code


async def fetch_barcode_amazon(page, url: str) -> Optional[str]:
    """Amazon ürün detay sayfasını ziyaret edip barkod döndürür."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)
        html = await page.content()
        return _extract_barcode_from_html(html)
    except Exception:
        return None
