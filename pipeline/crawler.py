"""
CRAWLEE katmanı — kuyruk, retry, eşzamanlılık ve fingerprint yönetimini
crawlee-python'a devreder. requestHandler ham HTML'i mevcut
scrapers.trendyol parser'larına yollar, Product listesi döner.

Proxy şu an KAPALI (proxy_configuration=None). Eklenecek zaman:
  from crawlee.proxy_configuration import ProxyConfiguration
  crawler = PlaywrightCrawler(proxy_configuration=ProxyConfiguration(proxy_urls=[...]))

Direkt çalıştırma:
  python -m pipeline.crawler "laptop" --pages 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import AsyncIterator

from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

from models import Product
from scrapers.trendyol import _build_url, _extract_products_from_html, _is_blocked, _parse_product

log = logging.getLogger(__name__)


async def crawl_trendyol_category(category: str, max_pages: int = 5) -> AsyncIterator[Product]:
    """Crawlee PlaywrightCrawler ile kategori taraması yapar (proxy yok)."""
    results: list[Product] = []

    crawler = PlaywrightCrawler(
        proxy_configuration=None,
        max_requests_per_crawl=max_pages,
        headless=True,
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext) -> None:
        page_num = context.request.user_data.get("page_num", 1)
        html = await context.page.content()

        if _is_blocked(html):
            context.log.warning(f"Bot koruması algılandı — sayfa {page_num}")
            return

        for raw in _extract_products_from_html(html):
            product = _parse_product(raw, category)
            if product:
                results.append(product)
                await context.push_data(product.model_dump(exclude={"images"}))

    requests = [
        Request.from_url(_build_url(category, p), user_data={"page_num": p})
        for p in range(1, max_pages + 1)
    ]
    await crawler.run(requests)

    for product in results:
        yield product


async def _run_standalone(category: str, max_pages: int) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    count = 0
    async for product in crawl_trendyol_category(category, max_pages):
        count += 1
        price_str = f"{product.price:,.2f} TRY" if product.price else "—"
        print(f"  [{count}] {product.title[:60]}  {price_str}")
    print(f"\nTamamlandı: {count} ürün")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Crawlee tabanlı Trendyol taraması (deneysel)")
    parser.add_argument("query", help='Arama terimi veya URL. Örn: "laptop"')
    parser.add_argument("--pages", type=int, default=3, help="Maksimum sayfa (varsayılan: 3)")
    args = parser.parse_args()
    asyncio.run(_run_standalone(args.query, args.pages))


if __name__ == "__main__":
    _cli()
