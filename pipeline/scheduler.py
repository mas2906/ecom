"""
SCHEDULER — "hangi ürün ne zaman taranmalı?"

Fiyatı oynak ürünler sık, durgun ürünler seyrek taranır. Her tarama
sonrası aralık yeniden hesaplanıp job kendi kendini yeniden zamanlar.
apscheduler'ı (mevcut requirements'ta zaten var, bkz. api.py) kullanır.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import SQLitePool, get_price_history, get_price_history_for_category

log = logging.getLogger(__name__)

MIN_INTERVAL_HOURS = 2.0
MAX_INTERVAL_HOURS = 48.0
DEFAULT_INTERVAL_HOURS = 12.0   # geçmiş veri yokken (ilk tarama)

Callback = Callable[[str, str], Awaitable[None]]     # (platform, key) -> None
IntervalFn = Callable[[str, str], Awaitable[float]]  # (platform, key) -> saat


def _interval_from_history(history: list[float]) -> float:
    """
    Fiyat listesinin varyasyon katsayısına göre bir sonraki taramaya kadar
    geçecek saati hesaplar. Oynaklık yüksekse sık, düşükse seyrek tarama yapılır.
    """
    if len(history) < 3:
        return DEFAULT_INTERVAL_HOURS

    mean = statistics.fmean(history)
    if mean <= 0:
        return DEFAULT_INTERVAL_HOURS

    cv = statistics.pstdev(history) / mean  # varyasyon katsayısı (0 = hiç oynamıyor)
    # cv 0 -> MAX_INTERVAL, cv >= 0.15 -> MIN_INTERVAL (doğrusal ölçek)
    scale = max(0.0, min(1.0, cv / 0.15))
    interval = MAX_INTERVAL_HOURS - scale * (MAX_INTERVAL_HOURS - MIN_INTERVAL_HOURS)
    return round(interval, 1)


async def next_interval_hours(pool: SQLitePool, platform: str, product_id: str) -> float:
    """Tek bir ürünün fiyat oynaklığına göre sonraki tarama aralığı (saat)."""
    history = await get_price_history(pool, platform, product_id, since="-30 days")
    return _interval_from_history(history)


async def next_interval_hours_for_category(pool: SQLitePool, platform: str, category: str) -> float:
    """
    Bir kategorideki (jobs.json'daki arama işi) tüm ürünlerin toplu fiyat
    oynaklığına göre sonraki tarama aralığı (saat). Tekil ürün fiyatını
    yeniden çekecek bir mekanizma henüz yok — bu yüzden adaptif zamanlama
    şimdilik kategori/arama işi seviyesinde çalışıyor.
    """
    history = await get_price_history_for_category(pool, platform, category, since="-30 days")
    return _interval_from_history(history)


class AdaptiveScheduler:
    """
    Adaptif frekansla tarama tetikleyen scheduler sarmalayıcısı.
    `key`, taranan şeyin kimliğidir — bugün bir kategori/arama işi
    (bkz. next_interval_hours_for_category), yarın tekil bir product_id
    (bkz. next_interval_hours) olabilir; scheduler bu ayrımı bilmez.
    """

    def __init__(self, interval_fn: IntervalFn, callback: Callback, timezone: str = "Europe/Istanbul"):
        self._interval_fn = interval_fn
        self._callback = callback
        self._scheduler = AsyncIOScheduler(timezone=timezone)

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def schedule(self, platform: str, key: str) -> None:
        """Bir kereliğine zamanlar; her çalışmadan sonra kendini yeniden zamanlar."""
        interval = await self._interval_fn(platform, key)
        job_id = f"{platform}:{key}"

        self._scheduler.add_job(
            self._run_and_reschedule,
            "date",
            run_date=datetime.now() + timedelta(hours=interval),
            args=[platform, key],
            id=job_id,
            replace_existing=True,
            # Varsayılan 1sn tolerans çok dar — reload/meşguliyet anında
            # tetikleme sessizce atlanmasın.
            misfire_grace_time=3600,
        )
        log.info("Zamanlandı: %s → %.1f saat sonra", job_id, interval)

    async def _run_and_reschedule(self, platform: str, key: str) -> None:
        try:
            await self._callback(platform, key)
        finally:
            await self.schedule(platform, key)
