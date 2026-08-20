"""
Bilinen ürün yenileme — kategori taraması yerine, DB'de zaten kayıtlı
ürünleri kendi ürün ID'siyle (ASIN / Trendyol content-id) arayıp fiyatını
günceller. Sayfalama yapılmaz, tek istekte biter; bu yüzden WAF/bot
koruması riski kategori taramasına göre çok daha düşüktür.

Zamanlı/otomatik taramalar (api.py) bunu kullanır. Manuel "Tara" (yeni
ürün keşfi) hâlâ kategori taramasını kullanır — barkod/ID yenileme sadece
daha önce görülmüş ürünler için mümkündür.
"""

import logging
from typing import Optional

from storage import Storage

logger = logging.getLogger(__name__)


async def refresh_known_products(
    pool,
    platform: str,
    storage: Storage,
    category: Optional[str] = None,
    limit: int = 100,
) -> int:
    from db import fetch_products_for_refresh
    from scrapers import AmazonScraper, TrendyolScraper

    scraper_cls = {"amazon": AmazonScraper, "trendyol": TrendyolScraper}.get(platform)
    if scraper_cls is None:
        return 0

    rows = await fetch_products_for_refresh(pool, platform=platform, category=category, limit=limit)
    if not rows:
        logger.info("Ürün yenileme (%s): yenilenecek ürün yok", platform)
        return 0

    logger.info("Ürün yenileme (%s): %d ürün", platform, len(rows))
    scraper = scraper_cls(session=None)

    count = 0
    async for product in scraper.scrape_by_barcodes(rows):
        await storage.save(product)
        count += 1
    return count
