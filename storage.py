"""
Çıktı: JSON (JSONL) + CSV + PostgreSQL (opsiyonel)
Fiyat düşüşü tespit edilirse Telegram bildirimi gider.
"""

import asyncio
import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles

from models import Product

logger = logging.getLogger(__name__)

FIELDS = [
    "platform", "category", "product_id", "title", "brand",
    "price", "original_price", "discount_rate", "currency", "in_stock",
    "rating", "review_count", "seller", "seller_rating", "url", "scraped_at",
]


class Storage:
    def __init__(
        self,
        output_dir: str = "./ignored/output",
        db_pool=None,
        notifier=None,          # notifier.Notifier | None
    ):
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._json_path = self._dir / f"products_{ts}.jsonl"
        self._csv_path = self._dir / f"products_{ts}.csv"

        self._csv_file = open(self._csv_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=FIELDS, extrasaction="ignore")
        self._csv_writer.writeheader()

        self._json_lines: list[str] = []
        self._lock = asyncio.Lock()
        self._count = 0

        self._db_pool = db_pool
        self._notifier = notifier

    async def save(self, product: Product) -> None:
        row = product.model_dump(exclude={"images"})

        async with self._lock:
            # --- Dosya çıktısı ---
            self._json_lines.append(json.dumps(row, ensure_ascii=False))
            self._csv_writer.writerow(row)
            self._count += 1
            if self._count % 50 == 0:
                await self._write_json_chunk()

        # --- PostgreSQL (lock dışında — I/O paralel çalışsın) ---
        if self._db_pool and product.price is not None:
            await self._save_to_db(product)

    async def _save_to_db(self, product: Product) -> None:
        from db import get_last_price, save_product  # geç import — db opsiyonel
        from pipeline.change_engine import detect_drop, lowest_price_period
        from pipeline.fanout import notify_watchers
        try:
            opportunity = None
            low_period_label = None
            badge_old_price = None
            if self._notifier and product.price is not None:
                # Kaydetmeden ÖNCE hesapla — yoksa 30 günlük medyan / son fiyat
                # az önce eklenen fiyatı da içerip kendi kendini kirletir.
                opportunity = await detect_drop(
                    self._db_pool, product.platform, product.product_id, product.price
                )
                if opportunity is not None:
                    low_period_label = await lowest_price_period(
                        self._db_pool, product.platform, product.product_id, product.price
                    )
                if product.price_badge:
                    badge_old_price = await get_last_price(
                        self._db_pool, product.platform, product.product_id
                    )

            await save_product(self._db_pool, product)

            if self._notifier and product.price is not None:
                # Fiyat düşüşü bildirimi — 30 günlük medyana göre (DEĞİŞİKLİK MOTORU)
                if opportunity is not None:
                    await self._notifier.price_drop(
                        product, opportunity.median_30d, opportunity.new_price, low_period_label
                    )
                    await notify_watchers(self._db_pool, opportunity, product.title, product.url)

                # Platformun kendi rozeti (ör. Amazon "X günün en düşük fiyatı")
                if product.price_badge:
                    await self._notifier.price_badge_alert(product, badge_old_price)

        except Exception as exc:
            logger.warning("DB kayıt hatası (%s): %s", product.product_id, exc)

    async def flush(self) -> None:
        async with self._lock:
            await self._write_json_chunk()
        self._csv_file.flush()
        self._csv_file.close()
        logger.info(
            "Çıktı: %s | %s (%d ürün)",
            self._json_path, self._csv_path, self._count,
        )

    async def _write_json_chunk(self) -> None:
        if not self._json_lines:
            return
        chunk = "\n".join(self._json_lines) + "\n"
        self._json_lines.clear()
        async with aiofiles.open(self._json_path, "a", encoding="utf-8") as f:
            await f.write(chunk)

    @property
    def count(self) -> int:
        return self._count

    @property
    def json_path(self) -> Path:
        return self._json_path

    @property
    def csv_path(self) -> Path:
        return self._csv_path
