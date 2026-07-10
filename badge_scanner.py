"""
Amazon Rozet Tarayıcısı — "Yılın/30/60/90 günün en düşük fiyatı" rozetli
ürünleri geniş kategori aramaları yaparak toplar.
"""

import asyncio
import logging
import random
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

from models import Product

logger = logging.getLogger(__name__)

BASE = "https://www.amazon.com.tr"

# Amazon TR'de geniş kapsam sağlayan arama terimleri
DEFAULT_CATEGORIES = [
    "laptop", "telefon", "tablet", "kulaklık", "monitör",
    "televizyon", "beyaz eşya", "oyuncak", "lego", "kitap",
    "spor", "kamera", "oyun konsolu", "akıllı saat", "klavye",
]

BADGE_KEYWORDS = [
    "en düşük fiyatı",
    "lowest price",
    "en düşük",
]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _parse_price(text: str) -> Optional[float]:
    import re
    cleaned = re.sub(r"[^\d,.]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_badge(html_snippet: str) -> Optional[str]:
    """Kart HTML'inden rozet metnini çıkar."""
    from bs4 import BeautifulSoup as BS
    soup = BS(html_snippet, "lxml")
    badge_el = soup.select_one("span.a-badge-text")
    if badge_el:
        text = badge_el.get_text(strip=True)
        if any(kw in text.lower() for kw in BADGE_KEYWORDS):
            return text
    return None


async def _parse_page_for_badges(html: str, category: str) -> list[Product]:
    """Arama sayfası HTML'inden rozet li ürünleri ayrıştırır."""
    import re
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select('div[data-component-type="s-search-result"]')
    products = []

    for card in cards:
        # Rozet var mı?
        badge_el = card.select_one("span.a-badge-text")
        if not badge_el:
            continue
        badge_text = badge_el.get_text(strip=True)
        if not any(kw in badge_text.lower() for kw in BADGE_KEYWORDS):
            continue

        # ASIN
        asin = card.get("data-asin", "")
        if not asin:
            continue

        # Başlık
        title_el = card.select_one("h2 span.a-text-normal") or card.select_one("h2 span")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        # URL
        link_el = card.select_one("h2 a")
        href = link_el.get("href", "") if link_el else ""
        url = f"{BASE}{href}" if href.startswith("/") else href or f"{BASE}/dp/{asin}"

        # Fiyat (a-offscreen)
        offscreen = card.select_one("span.a-price:not(.a-text-price) span.a-offscreen")
        price = _parse_price(offscreen.get_text(strip=True)) if offscreen else None

        # Orijinal fiyat
        orig_el = card.select_one("span.a-price.a-text-price span.a-offscreen")
        orig_price = _parse_price(orig_el.get_text(strip=True)) if orig_el else None

        # İndirim oranı
        discount_rate = None
        if price and orig_price and orig_price > price:
            discount_rate = round((orig_price - price) / orig_price * 100, 1)

        # Puan
        rating_el = card.select_one("span.a-icon-alt")
        rating = None
        if rating_el:
            m = re.search(r"([\d,\.]+)", rating_el.get_text())
            if m:
                try:
                    rating = float(m.group(1).replace(",", "."))
                except ValueError:
                    pass

        # Görsel
        img_el = card.select_one("img.s-image")
        images = [img_el["src"]] if img_el and img_el.get("src") else []

        products.append(Product(
            platform="amazon",
            category=category,
            product_id=asin,
            title=title,
            url=url,
            price=price,
            original_price=orig_price,
            discount_rate=discount_rate,
            rating=rating,
            price_badge=badge_text,
            images=images,
        ))

    return products


async def scan_badges(
    categories: Optional[list[str]] = None,
    pages: int = 3,
    on_product=None,          # async callback(product) — her ürün bulununca çağrılır
    on_progress=None,         # async callback(msg: str)
) -> list[Product]:
    """
    Amazon TR'yi geniş kategorilerde tarayıp rozet li ürünleri döndürür.

    Args:
        categories: Aranacak kategoriler (None → DEFAULT_CATEGORIES)
        pages: Her kategori için taranacak sayfa sayısı
        on_product: Ürün bulununca çağrılan async callback
        on_progress: Durum mesajı callback'i
    """
    cats = categories or DEFAULT_CATEGORIES
    all_products: list[Product] = []
    seen_asins: set[str] = set()

    async def _log(msg):
        logger.info(msg)
        if on_progress:
            await on_progress(msg)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            user_agent=_UA,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
        )
        await Stealth().apply_stealth_async(await context.new_page())
        await context.pages[0].close()

        for cat in cats:
            await _log(f"🔍 Rozet tarama: Amazon / \"{cat}\"")

            for page_num in range(1, pages + 1):
                url = (
                    f"{BASE}/s?k={cat.replace(' ', '+')}"
                    f"&page={page_num}&ref=sr_pg_{page_num}"
                )
                page: Page = await context.new_page()
                try:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        await page.wait_for_timeout(random.randint(1500, 2500))
                    except PWTimeout:
                        pass

                    content = await page.content()

                    # Bot engeli
                    if "Üzgünüz" in content or "robot" in content.lower():
                        await _log(f"  ⚠️ Amazon engelledi ({cat} sayfa {page_num}), atlanıyor")
                        await page.close()
                        continue

                    products = await _parse_page_for_badges(content, cat)

                    new_count = 0
                    for p in products:
                        if p.product_id not in seen_asins:
                            seen_asins.add(p.product_id)
                            all_products.append(p)
                            new_count += 1
                            if on_product:
                                await on_product(p)

                    if new_count:
                        await _log(f"  ✓ {cat} sayfa {page_num} → {new_count} rozetli ürün")

                    await asyncio.sleep(random.uniform(1.0, 2.2))

                except Exception as e:
                    logger.warning("Badge scan hatası (%s s.%d): %s", cat, page_num, e)
                finally:
                    await page.close()

        await browser.close()

    await _log(f"━━━ Rozet tarama tamamlandı: {len(all_products)} ürün ━━━")
    return all_products
