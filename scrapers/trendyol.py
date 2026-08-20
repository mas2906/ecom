"""
Trendyol Scraper
----------------
Yöntem : ScraperSession (curl-cffi, Chrome TLS taklidi) ile arama sayfasını çek,
         HTML içindeki __INITIAL_STATE__ / __SEARCH_STATE__ JSON'unu parse et,
         ürünleri çıkar.

main.py üzerinden:
  python main.py --platform trendyol --category "laptop" --pages 5

Direkt çalıştırma:
  python scrapers/trendyol.py "laptop"
  python scrapers/trendyol.py "kulaklık" --pages 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from playwright_stealth import Stealth

# Hem doğrudan çalıştırma hem de paket içi import için yol ayarı
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from models import Product
from .base import BaseScraper

log = logging.getLogger(__name__)

BASE = "https://www.trendyol.com"

# Gerçek kullanıcı viewport boyutları
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 800},
]

_CHROME_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


# ---------------------------------------------------------------------------
# URL yardımcıları
# ---------------------------------------------------------------------------

def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _build_url(query: str, page: int) -> str:
    if _is_url(query):
        parsed = urlparse(query)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        params["pi"] = str(page)
        return urlunparse(parsed._replace(query=urlencode(params)))
    q = quote(query)
    return f"{BASE}/sr?q={q}&qt={q}&st={q}&os=1&pi={page}"


# ---------------------------------------------------------------------------
# JSON çıkarma — sayfa HTML'inden ürün listesini al
# ---------------------------------------------------------------------------

_STATE_PATTERNS = [
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:window|</script)", re.DOTALL),
    re.compile(r"window\.__SEARCH_STATE__\s*=\s*(\{.+?\});\s*(?:window|</script)", re.DOTALL),
]

_PRODUCT_PATHS = [
    ["searchResult", "result", "products"],
    ["search", "products"],
    ["productList", "products"],
    ["result", "products"],
    ["products"],
]

_PRODUCTS_ARRAY_PAT = re.compile(
    r'"products"\s*:\s*(\[.+?\])\s*,\s*"(?:facets|filters|pagination)"',
    re.DOTALL,
)


def _extract_json_value(html: str, key: str) -> dict | None:
    """window["key"] = {...}; bloğunu bracket-sayacı ile parse eder."""
    marker = f'"{key}"'
    idx = html.find(marker)
    if idx == -1:
        return None
    eq = html.find("=", idx + len(marker))
    if eq == -1:
        return None
    start = html.find("{", eq)
    if start == -1:
        return None
    depth = 0
    for i in range(start, min(start + 300_000, len(html))):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_products_from_html(html: str) -> list[dict]:
    """HTML'den ürün listesini JSON olarak çıkarır."""

    # Yeni format: window["__single-search-result__PROPS"].data.products
    props = _extract_json_value(html, "__single-search-result__PROPS")
    if props:
        products = (props.get("data") or {}).get("products")
        if isinstance(products, list) and products:
            log.debug("Ürünler __single-search-result__PROPS'tan alındı (%d adet)", len(products))
            return products

    # Eski format: window.__INITIAL_STATE__ / window.__SEARCH_STATE__
    for pat in _STATE_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        try:
            state = json.loads(m.group(1))
            for path in _PRODUCT_PATHS:
                node = state
                for key in path:
                    node = node.get(key) if isinstance(node, dict) else None
                    if node is None:
                        break
                if isinstance(node, list) and node:
                    log.debug("Ürünler JSON state'ten alındı: %s", " > ".join(path))
                    return node
        except json.JSONDecodeError:
            continue

    # Son çare: products dizisini doğrudan yakala
    m = _PRODUCTS_ARRAY_PAT.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return []


# ---------------------------------------------------------------------------
# Ham JSON → Product
# ---------------------------------------------------------------------------

def _num(val) -> Optional[float]:
    try:
        v = float(str(val).replace(",", "."))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _to_int(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _price_fields(raw: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    p = raw.get("price") or {}
    if isinstance(p, dict):
        sale = _num(
            p.get("current") or p.get("discountedPrice") or
            p.get("sellingPrice") or p.get("salePrice")
        )
        orig = _num(
            p.get("old") or p.get("originalPrice") or p.get("listPrice")
        )
        disc = _num(p.get("discountRatio") or p.get("discountRate"))
        price = sale or orig
    else:
        price = _num(p)
        orig  = _num(raw.get("originalPrice") or raw.get("listPrice"))
        disc  = _num(raw.get("discountRatio") or raw.get("discountRate"))

    if price and orig and orig > price and not disc:
        disc = round((orig - price) / orig * 100, 1)

    return price, orig, disc


def _parse_product(raw: dict, category: str) -> Optional[Product]:
    pid = str(raw.get("id") or raw.get("contentId") or "").strip()
    if not pid:
        return None

    title = (raw.get("name") or raw.get("title") or raw.get("productName") or "").strip()
    if not title:
        return None

    url_slug = raw.get("url") or raw.get("productUrl") or ""
    url = f"{BASE}{url_slug}" if url_slug.startswith("/") else url_slug or f"{BASE}/-p-{pid}"

    brand_raw = raw.get("brand") or raw.get("brandName") or {}
    brand = (brand_raw.get("name") if isinstance(brand_raw, dict) else str(brand_raw)).strip() or None

    price, original_price, discount_rate = _price_fields(raw)

    rating_raw = raw.get("ratingScore") or raw.get("rating") or {}
    rating: Optional[float] = None
    review_count: Optional[int] = None
    if isinstance(rating_raw, dict):
        rating       = _num(rating_raw.get("averageRating") or rating_raw.get("score"))
        review_count = _to_int(rating_raw.get("totalCount") or rating_raw.get("reviewCount"))
    else:
        rating = _num(rating_raw)
    if rating is not None and not (0 <= rating <= 5):
        rating = None

    seller_raw = raw.get("seller") or raw.get("merchantName") or {}
    seller_str = seller_raw.get("name") if isinstance(seller_raw, dict) else str(seller_raw)
    seller = seller_str.strip() if seller_str else None

    in_stock: bool = bool(raw.get("inStock", True))

    badge = (
        raw.get("campaignLabel") or raw.get("priceLabel") or
        raw.get("badge") or raw.get("label") or ""
    ).strip() or None

    images_raw = raw.get("images") or raw.get("imageList") or []
    images: list[str] = []
    for img in images_raw:
        src = img.get("url") or img.get("src") if isinstance(img, dict) else str(img)
        if src:
            images.append(src if src.startswith("http") else f"https://cdn.dsmcdn.com{src}")
    if not images:
        main_img = raw.get("image") or raw.get("thumbnailUrl") or ""
        if main_img:
            images = [main_img if main_img.startswith("http") else f"https://cdn.dsmcdn.com{main_img}"]

    return Product(
        platform="trendyol",
        category=category,
        product_id=pid,
        title=title,
        brand=brand,
        url=url,
        price=price,
        original_price=original_price,
        discount_rate=discount_rate,
        rating=rating,
        review_count=review_count,
        seller=seller,
        in_stock=in_stock,
        price_badge=badge,
        images=images,
    )


def _parse_page(html: str, category: str, page_num: int) -> list[Product]:
    raw_list = _extract_products_from_html(html)
    if not raw_list:
        log.debug("JSON ürün listesi bulunamadı (sayfa %d)", page_num)
        return []
    products = [p for raw in raw_list if (p := _parse_product(raw, category))]
    return products


# ---------------------------------------------------------------------------
# Ürün detay sayfası — schema.org ld+json'dan fiyat/başlık/marka çıkar.
# Arama sonucu JSON'u ("__single-search-result__PROPS" vb.) detay
# sayfalarında yok; detay sayfası mikro-servisleri ("envoy_*") parça parça
# ve kararsız bir yapıda — ld+json ise stabil ve platformdan bağımsız.
# ---------------------------------------------------------------------------

_LDJSON_PAT = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)


def _parse_detail_page(html: str, product_id: str, category: str) -> Optional[Product]:
    for m in _LDJSON_PAT.finditer(html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "Product":
            continue

        offers = data.get("offers") or {}
        price = _num(offers.get("price"))
        title = (data.get("name") or "").strip()
        if price is None or not title:
            return None

        brand_raw = data.get("brand") or {}
        brand = brand_raw.get("name") if isinstance(brand_raw, dict) else None
        url = offers.get("url") or data.get("@id") or ""
        in_stock = "instock" in str(offers.get("availability") or "").lower()

        return Product(
            platform="trendyol",
            category=category,
            product_id=str(data.get("sku") or product_id),
            title=title,
            brand=brand,
            url=url,
            price=price,
            in_stock=in_stock,
        )
    return None


# ---------------------------------------------------------------------------
# Playwright yardımcıları
# ---------------------------------------------------------------------------

_WARMUP_PATHS = [
    "/kadin", "/erkek", "/elektronik",
    "/spor-outdoor", "/ev-yasam", "/kozmetik-kisisel-bakim",
]


async def _new_trendyol_page(browser):
    """Her oturum için rastgele viewport + UA ile yeni sayfa açar."""
    viewport = random.choice(_VIEWPORTS)
    ua = random.choice(_CHROME_UAS)
    ctx = await browser.new_context(
        viewport=viewport,
        screen={"width": viewport["width"], "height": viewport["height"]},
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        ignore_https_errors=True,
        user_agent=ua,
        color_scheme="light",
        extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    page = await ctx.new_page()
    await Stealth().apply_stealth_async(page)
    return page


async def _warmup_page(page: Page) -> None:
    """Ana sayfa + rastgele kategori ziyareti ile oturum ısıtır."""
    try:
        await page.goto(BASE, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(random.uniform(2.0, 4.0))
        await _human_scroll(page)

        if random.random() < 0.65:
            path = random.choice(_WARMUP_PATHS)
            await page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=25_000)
            await asyncio.sleep(random.uniform(1.5, 3.0))
    except Exception as exc:
        log.debug("Isınma kısmen başarısız: %s", exc)


async def _human_scroll(page: Page) -> None:
    """Gerçek kullanıcı kaydırma simülasyonu — lazy-load tetikler."""
    try:
        vp_h = await page.evaluate("window.innerHeight")
        total = await page.evaluate("document.body.scrollHeight")
        current = 0
        steps = random.randint(5, 10)

        for _ in range(steps):
            step = max(150, int(random.gauss(vp_h * 0.55, vp_h * 0.2)))
            current = min(current + step, total)
            await page.evaluate(f"window.scrollTo({{top:{current},behavior:'smooth'}})")
            await asyncio.sleep(random.uniform(0.4, 1.4))

            if random.random() < 0.25:
                back = random.randint(100, min(400, current))
                current = max(0, current - back)
                await page.evaluate(f"window.scrollTo({{top:{current},behavior:'smooth'}})")
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # %10 ihtimalle ürüne bakıyormuş gibi duraklama
            if random.random() < 0.10:
                await asyncio.sleep(random.uniform(1.5, 3.5))

            if current >= total:
                break

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(0.8, 1.8))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass


def _is_blocked(html: str) -> bool:
    """Cloudflare / bot engeli sinyalleri."""
    if len(html) < 2_000:
        return True
    signals = ["Just a moment", "cf-browser-verification",
                "Checking your browser", "DDoS protection by Cloudflare"]
    lower = html.lower()
    return any(s.lower() in lower for s in signals)


# ---------------------------------------------------------------------------
# Scraper — Playwright + stealth
# ---------------------------------------------------------------------------

class TrendyolScraper(BaseScraper):
    """
    Playwright + playwright-stealth tabanlı Trendyol scraper.
    Gerçek Chromium açar → Cloudflare JS challenge'larını geçer.
    Rastgele viewport, UA, scroll ve zamanlama ile bot tespiti engellenir.
    """

    async def scrape_category(
        self, category: str, max_pages: int = 5
    ) -> AsyncIterator[Product]:
        label = category[:60]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            page = await _new_trendyol_page(browser)

            try:
                # Isınma — Cloudflare oturum çerezi alır
                log.info("[Trendyol] Isınma navigasyonu…")
                await _warmup_page(page)

                consecutive_empty = 0
                seen_ids: set[str] = set()

                for page_num in range(1, max_pages + 1):
                    url = _build_url(category, page_num)
                    log.info("[Trendyol] sayfa %d — %s", page_num, label)

                    html = await self._load_page(page, url, page_num)
                    if html is None:
                        break

                    products = _parse_page(html, category, page_num)

                    if not products:
                        consecutive_empty += 1
                        log.info(
                            "Sayfa %d boş (arka arkaya %d).",
                            page_num, consecutive_empty,
                        )
                        if consecutive_empty >= 2:
                            log.info("2 arka arkaya boş — duruyoruz.")
                            break
                        await asyncio.sleep(random.uniform(8.0, 18.0))
                        continue

                    consecutive_empty = 0

                    new_products = [p for p in products if p.product_id not in seen_ids]
                    if not new_products:
                        log.info(
                            "Sayfa %d tamamen mükerrer (gerçek sonuç sayısı buraya kadarmış) — duruyoruz.",
                            page_num,
                        )
                        break

                    seen_ids.update(p.product_id for p in new_products)
                    log.info(
                        "  -> %d ürün (sayfa %d, %d yeni)",
                        len(products), page_num, len(new_products),
                    )
                    for p in new_products:
                        yield p

                    if page_num < max_pages:
                        await asyncio.sleep(random.uniform(3.0, 7.0))

            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Bilinen ürünün fiyatını yenile — kategori taraması gibi sayfalama
    # yapmaz, DB'de zaten kayıtlı ürün URL'sini doğrudan ziyaret edip
    # ld+json'dan fiyatı okur (arama sonucu değil, tek ürün = tek istek).
    # Bu yüzden WAF/bot koruması riski kategori taramasına göre çok daha
    # düşüktür. Tarayıcı/oturum bir kez açılır, tüm ürünler aynı sayfa
    # üzerinden sırayla ziyaret edilir.
    # ------------------------------------------------------------------

    async def scrape_by_barcodes(self, items: list[dict]) -> AsyncIterator[Product]:
        """items: [{"product_id": str, "category": str, "url": str}, ...]"""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            page = await _new_trendyol_page(browser)

            try:
                log.info("[Trendyol] Isınma navigasyonu…")
                await _warmup_page(page)

                for i, item in enumerate(items):
                    pid = item.get("product_id")
                    url = item.get("url")
                    if not pid or not url:
                        continue
                    log.info("[Trendyol] ürün yenileme %d/%d — %s", i + 1, len(items), pid)

                    html = await self._load_page(page, url, i + 1)
                    if html is None:
                        continue

                    product = _parse_detail_page(html, pid, item.get("category") or "")
                    if product:
                        yield product
                    else:
                        log.info("  → fiyat okunamadı (%s)", pid)

                    if i < len(items) - 1:
                        await asyncio.sleep(random.uniform(5.0, 12.0))
            finally:
                await browser.close()

    async def _load_page(self, page: Page, url: str, page_num: int) -> str | None:
        """
        Sayfayı yükle, scroll yap, HTML döndür.
        Cloudflare challenge varsa kısa bekle ve tekrar dene.
        """
        for attempt in range(2):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except PWTimeout:
                log.warning("[Trendyol] Sayfa %d zaman aşımı (deneme %d/2)", page_num, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(random.uniform(5.0, 12.0))
                    continue
                return None
            except Exception as exc:
                log.warning("[Trendyol] Sayfa %d yükleme hatası: %s", page_num, exc)
                return None

            # Sayfanın tam yüklenmesini bekle — ağ hızına göre değişir
            await asyncio.sleep(random.uniform(2.5, 5.0))
            html = await page.content()

            if _is_blocked(html):
                log.warning(
                    "[Trendyol] Bot koruması algılandı (sayfa %d, deneme %d/2)",
                    page_num, attempt + 1,
                )
                if attempt == 0:
                    await asyncio.sleep(random.uniform(15.0, 30.0))
                    continue
                log.warning("İkinci denemede de bloklandı.")
                return None

            await _human_scroll(page)
            # Scroll sonrası yeniden al — lazy-load içerik gelmiş olabilir
            html = await page.content()
            return html

        return None


# ---------------------------------------------------------------------------
# Direkt çalıştırma: python scrapers/trendyol.py "laptop" --pages 3
# Storage kullanır → DB + Notifier otomatik devreye girer
# ---------------------------------------------------------------------------

async def _run_standalone(query: str, max_pages: int) -> None:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "ignored" / ".env")

    from config import PLATFORMS
    from anti_ban import ScraperSession
    from storage import Storage

    db_pool = None
    db_url = os.getenv("DB_URL")
    if db_url:
        try:
            from db import create_pool, setup_schema
            db_pool = await create_pool(db_url)
            await setup_schema(db_pool)
            print(f"PostgreSQL bağlı: {db_url.split('@')[-1]}")
        except Exception as exc:
            print(f"DB bağlantısı kurulamadı ({exc}), sadece dosyaya yazılacak.")

    notifier = None
    tg_token = os.getenv("TELEGRAM_TOKEN")
    tg_chat  = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        from notifier import Notifier
        min_drop = float(os.getenv("TELEGRAM_MIN_DROP_PCT", "0"))
        notifier = Notifier(token=tg_token, chat_id=tg_chat, min_drop_pct=min_drop)
        print(f"Telegram bildirimleri aktif (min düşüş: %{min_drop:.1f})")

    storage = Storage(str(_ROOT / "ignored" / "output"), db_pool=db_pool, notifier=notifier)

    async with ScraperSession(PLATFORMS["trendyol"]) as session:
        scraper = TrendyolScraper(session)
        async for product in scraper.scrape_category(query, max_pages):
            await storage.save(product)
            price_str = f"{product.price:,.2f} TRY" if product.price else "—"
            print(f"  [{storage.count}] {product.title[:60]}  {price_str}")

    await storage.flush()

    if db_pool:
        await db_pool.close()

    print(f"\nTamamlandı: {storage.count} ürün")
    print(f"  JSONL : {storage.json_path}")
    print(f"  CSV   : {storage.csv_path}")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Trendyol Scraper")
    parser.add_argument("query",   help='Arama terimi veya URL. Örn: "laptop"')
    parser.add_argument("--pages", type=int, default=5, help="Maksimum sayfa (varsayılan: 5)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    asyncio.run(_run_standalone(args.query, args.pages))


if __name__ == "__main__":
    _cli()
