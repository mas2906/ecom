"""
DEĞİŞİKLİK MOTORU — yeni fiyatı son 30 günün medyanıyla kıyaslar.
Medyan × DROP_THRESHOLD altına düşerse "fırsat" olarak işaretler.
"""

from __future__ import annotations

import logging
import statistics
from typing import NamedTuple, Optional

from db import SQLitePool, get_price_history

log = logging.getLogger(__name__)

DROP_THRESHOLD = 0.85   # yeni fiyat < medyan * 0.85 → fırsat
MIN_SAMPLES = 3         # medyan güvenilir sayılması için gereken minimum gözlem

# En uzun periyottan en kısaya — ilk uyan (gerçek rekor düşük) kullanılır.
PERIOD_WINDOWS = [
    ("-180 days", "6 aylık en düşük fiyat"),
    ("-90 days", "3 aylık en düşük fiyat"),
    ("-30 days", "30 günün en düşük fiyatı"),
]


class Opportunity(NamedTuple):
    platform: str
    product_id: str
    new_price: float
    median_30d: float
    drop_pct: float


async def detect_drop(
    pool: SQLitePool, platform: str, product_id: str, new_price: float
) -> Optional[Opportunity]:
    """Son 30 günün medyanına göre fırsat var mı kontrol eder."""
    if new_price is None:
        return None

    history = await get_price_history(pool, platform, product_id, since="-30 days")
    if len(history) < MIN_SAMPLES:
        log.debug(
            "Medyan için yetersiz veri (%s/%s): %d örnek", platform, product_id, len(history)
        )
        return None

    median_30d = statistics.median(history)
    if median_30d <= 0 or new_price >= median_30d * DROP_THRESHOLD:
        return None

    drop_pct = round((median_30d - new_price) / median_30d * 100, 1)
    return Opportunity(
        platform=platform,
        product_id=product_id,
        new_price=new_price,
        median_30d=median_30d,
        drop_pct=drop_pct,
    )


async def lowest_price_period(
    pool: SQLitePool, platform: str, product_id: str, new_price: float
) -> Optional[str]:
    """
    new_price, son 6/3/1 ay içindeki gerçek en düşük fiyatsa (kayıtlardan
    düşük veya eşitse) uygun etiketi döndürür; değilse None.
    """
    for since, label in PERIOD_WINDOWS:
        history = await get_price_history(pool, platform, product_id, since=since)
        if len(history) >= MIN_SAMPLES and new_price <= min(history):
            return label
    return None
