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

# Hem doğrudan çalıştırma hem de paket içi import için yol ayarı
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from models import Product
from .base import BaseScraper

log = logging.getLogger(__name__)

BASE = "https://www.trendyol.com"


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

# JSON içinde ürün listesini bulmak için olası anahtar yolları
_PRODUCT_PATHS = [
    ["searchResult", "result", "products"],
    ["search", "products"],
    ["productList", "products"],
    ["result", "products"],
    ["products"],
]

# Küçük parça: doğrudan products dizisi
_PRODUCTS_ARRAY_PAT = re.compile(
    r'"products"\s*:\s*(\[.+?\])\s*,\s*"(?:facets|filters|pagination)"',
    re.DOTALL,
)


def _extract_products_from_html(html: str) -> list[dict]:
    """HTML'den ürün listesini JSON olarak çıkarır."""

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

    # Küçük parça fallback
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
        sale  = _num(p.get("sellingPrice") or p.get("discountedPrice") or p.get("salePrice"))
        orig  = _num(p.get("originalPrice") or p.get("listPrice"))
        disc  = _num(p.get("discountRatio") or p.get("discountRate"))
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
    seller = (seller_raw.get("name") if isinstance(seller_raw, dict) else str(seller_raw)).strip() or None

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
# Scraper — ScraperSession kullanır (anti_ban.py)
# ---------------------------------------------------------------------------

class TrendyolScraper(BaseScraper):
    """
    curl-cffi tabanlı Trendyol scraper.
    HTTP isteklerini ScraperSession üzerinden gönderir:
      - Chrome TLS parmak izi taklidi
      - Otomatik rate-limit backoff (429/403/503)
      - İnsan-benzeri gecikmeler ve UA rotasyonu
    """

    async def scrape_category(
        self, category: str, max_pages: int = 5
    ) -> AsyncIterator[Product]:
        label = category[:60]

        # İlk istek: ana sayfayı ziyaret et (çerez al)
        try:
            await self.session.get(BASE)
            await asyncio.sleep(random.uniform(1.0, 2.5))
        except Exception:
            pass

        for page_num in range(1, max_pages + 1):
            url = _build_url(category, page_num)
            log.info("[Trendyol] sayfa %d — %s", page_num, label)

            try:
                resp = await self.session.get(url, referer=BASE)
                html = resp.text
            except Exception as exc:
                log.warning("Sayfa %d alınamadı: %s", page_num, exc)
                break

            products = _parse_page(html, category, page_num)
            if not products:
                log.info("Sayfa %d boş veya bot koruması, duruluyor.", page_num)
                break

            log.info("  -> %d ürün (sayfa %d)", len(products), page_num)
            for p in products:
                yield p


# ---------------------------------------------------------------------------
# Direkt çalıştırma: python scrapers/trendyol.py "laptop" --pages 3
# Storage kullanır → DB + Notifier otomatik devreye girer
# ---------------------------------------------------------------------------

async def _run_standalone(query: str, max_pages: int) -> None:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")

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

    storage = Storage(str(_ROOT / "output"), db_pool=db_pool, notifier=notifier)

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
