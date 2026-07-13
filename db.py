"""
SQLite katmanı — aiosqlite tabanlı.

Tablolar:
  products       → her platform+product_id için tek kayıt (upsert)
  price_history  → her scrape'te fiyat geçmişi satırı eklenir
"""

import logging
import re
from typing import Optional

import aiosqlite

from models import Product

logger = logging.getLogger(__name__)

_CREATE_PRODUCTS = """
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    platform       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    category       TEXT,
    title          TEXT NOT NULL,
    brand          TEXT,
    url            TEXT,
    barcode        TEXT,
    price_badge    TEXT,
    seller         TEXT,
    in_stock       INTEGER DEFAULT 1,
    currency       TEXT DEFAULT 'TRY',
    first_seen_at  TEXT DEFAULT (datetime('now')),
    last_seen_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(platform, product_id)
);
"""

_CREATE_PRICE_HISTORY = """
CREATE TABLE IF NOT EXISTS price_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    platform       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    price          REAL,
    original_price REAL,
    discount_rate  REAL,
    rating         REAL,
    review_count   INTEGER,
    in_stock       INTEGER,
    scraped_at     TEXT DEFAULT (datetime('now'))
);
"""

_CREATE_INDEX_PH = """
CREATE INDEX IF NOT EXISTS idx_ph_lookup
    ON price_history(platform, product_id, scraped_at DESC);
"""

_CREATE_INDEX_BARCODE = """
CREATE INDEX IF NOT EXISTS idx_products_barcode
    ON products(barcode)
    WHERE barcode IS NOT NULL;
"""

_CREATE_WATCHES = """
CREATE TABLE IF NOT EXISTS watches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    platform       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    target_price   REAL,
    created_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, platform, product_id)
);
"""

_CREATE_INDEX_WATCHES = """
CREATE INDEX IF NOT EXISTS idx_watches_product
    ON watches(platform, product_id);
"""

_CREATE_FCM_TOKENS = """
CREATE TABLE IF NOT EXISTS fcm_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    token       TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, token)
);
"""

_UPSERT_PRODUCT = """
INSERT INTO products
    (platform, product_id, category, title, brand, url, barcode, price_badge, seller, in_stock, currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (platform, product_id) DO UPDATE SET
    title        = excluded.title,
    brand        = excluded.brand,
    url          = excluded.url,
    barcode      = COALESCE(excluded.barcode, products.barcode),
    price_badge  = excluded.price_badge,
    seller       = excluded.seller,
    in_stock     = excluded.in_stock,
    last_seen_at = datetime('now');
"""

_INSERT_PRICE_HISTORY = """
INSERT INTO price_history
    (platform, product_id, price, original_price, discount_rate, rating, review_count, in_stock)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""

_GET_LAST_PRICE = """
SELECT price FROM price_history
WHERE platform = ? AND product_id = ?
ORDER BY scraped_at DESC
LIMIT 1;
"""

_GET_BADGE_PRODUCTS = """
SELECT p.platform, p.category, p.title, p.url, p.price_badge,
    (SELECT ph.price        FROM price_history ph WHERE ph.platform=p.platform AND ph.product_id=p.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS price,
    (SELECT ph.original_price FROM price_history ph WHERE ph.platform=p.platform AND ph.product_id=p.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS original_price,
    (SELECT ph.discount_rate  FROM price_history ph WHERE ph.platform=p.platform AND ph.product_id=p.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS discount_rate,
    (SELECT ph.scraped_at    FROM price_history ph WHERE ph.platform=p.platform AND ph.product_id=p.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS scraped_at
FROM products p
WHERE p.price_badge IS NOT NULL
ORDER BY scraped_at DESC
LIMIT 200;
"""

_GET_CROSS_MATCHES = """
SELECT
    ty.title        AS title_trendyol,
    am.title        AS title_amazon,
    ty.barcode,
    ty.url          AS url_trendyol,
    am.url          AS url_amazon,
    ty.brand        AS brand,
    (SELECT ph.price FROM price_history ph WHERE ph.platform=ty.platform AND ph.product_id=ty.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS price_trendyol,
    (SELECT ph.price FROM price_history ph WHERE ph.platform=am.platform AND ph.product_id=am.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS price_amazon,
    (SELECT ph.scraped_at FROM price_history ph WHERE ph.platform=ty.platform AND ph.product_id=ty.product_id ORDER BY ph.scraped_at DESC LIMIT 1) AS scraped_at
FROM products ty
JOIN products am
    ON ty.barcode = am.barcode
    AND ty.platform = 'trendyol'
    AND am.platform = 'amazon'
WHERE ty.barcode IS NOT NULL
ORDER BY scraped_at DESC;
"""

_GET_RECENT = """
SELECT p.platform, p.category, p.title, p.url, p.barcode,
       ph.price, ph.original_price, ph.discount_rate, ph.scraped_at
FROM products p
JOIN price_history ph
  ON p.platform = ph.platform AND p.product_id = ph.product_id
ORDER BY ph.scraped_at DESC
LIMIT 100;
"""

_UPDATE_BARCODE = """
UPDATE products SET barcode = ?
WHERE platform = ? AND product_id = ? AND barcode IS NULL
"""

_FETCH_WITHOUT_BARCODE = """
SELECT product_id, platform, url FROM products
WHERE barcode IS NULL AND url IS NOT NULL
"""

_GET_PRICE_HISTORY_SINCE = """
SELECT price FROM price_history
WHERE platform = ? AND product_id = ? AND price IS NOT NULL
    AND scraped_at >= datetime('now', ?)
ORDER BY scraped_at ASC;
"""

_GET_PRICE_HISTORY_FOR_CATEGORY = """
SELECT ph.price FROM price_history ph
JOIN products p ON p.platform = ph.platform AND p.product_id = ph.product_id
WHERE ph.platform = ? AND p.category = ? AND ph.price IS NOT NULL
    AND ph.scraped_at >= datetime('now', ?)
ORDER BY ph.scraped_at ASC;
"""

_UPSERT_WATCH = """
INSERT INTO watches (user_id, platform, product_id, target_price)
VALUES (?, ?, ?, ?)
ON CONFLICT (user_id, platform, product_id) DO UPDATE SET
    target_price = excluded.target_price;
"""

_GET_WATCHERS = """
SELECT user_id, target_price FROM watches
WHERE platform = ? AND product_id = ?;
"""

_UPSERT_FCM_TOKEN = """
INSERT INTO fcm_tokens (user_id, token)
VALUES (?, ?)
ON CONFLICT (user_id, token) DO NOTHING;
"""

_GET_FCM_TOKENS = """
SELECT token FROM fcm_tokens WHERE user_id = ?;
"""


# ── Pool wrapper (asyncpg-uyumlu arayüz) ──────────────────────────────────────

class _Conn:
    def __init__(self, conn: aiosqlite.Connection):
        self._c = conn

    async def fetch(self, query: str, *args) -> list[dict]:
        async with self._c.execute(query, args) as cur:
            cols = [d[0] for d in (cur.description or [])]
            rows = await cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def fetchrow(self, query: str, *args) -> Optional[dict]:
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None

    async def execute(self, query: str, *args) -> None:
        await self._c.execute(query, args)
        await self._c.commit()


class _AcquireCtx:
    def __init__(self, path: str):
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> _Conn:
        self._conn = await aiosqlite.connect(self._path)
        return _Conn(self._conn)

    async def __aexit__(self, *_) -> None:
        if self._conn:
            await self._conn.close()


class SQLitePool:
    def __init__(self, path: str):
        self._path = path

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._path)

    async def close(self) -> None:
        pass


# ── Public API ─────────────────────────────────────────────────────────────────

async def create_pool(dsn: str) -> SQLitePool:
    # dsn: sqlite:///path/to/file.db  veya  sadece dosya yolu
    if dsn.startswith("sqlite:///"):
        path = dsn[len("sqlite:///"):]
    elif dsn.startswith("sqlite://"):
        path = dsn[len("sqlite://"):]
    else:
        path = dsn
    pool = SQLitePool(path)
    logger.info("SQLite bağlantısı kuruldu: %s", path)
    return pool


async def setup_schema(pool: SQLitePool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_PRODUCTS)
        await conn.execute(_CREATE_PRICE_HISTORY)
        await conn.execute(_CREATE_INDEX_PH)
        try:
            await conn.execute(_CREATE_INDEX_BARCODE)
        except Exception:
            pass
        await conn.execute(_CREATE_WATCHES)
        await conn.execute(_CREATE_INDEX_WATCHES)
        await conn.execute(_CREATE_FCM_TOKENS)
    logger.info("DB şeması hazır (products + price_history + watches + fcm_tokens)")


async def get_last_price(
    pool: SQLitePool, platform: str, product_id: str
) -> Optional[float]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_LAST_PRICE, platform, product_id)
    if row and row["price"] is not None:
        return float(row["price"])
    return None


async def save_product(pool: SQLitePool, product: Product) -> Optional[float]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_LAST_PRICE, product.platform, product.product_id)
        prev_price = float(row["price"]) if (row and row["price"] is not None) else None

        await conn.execute(
            _UPSERT_PRODUCT,
            product.platform,
            product.product_id,
            product.category,
            product.title,
            product.brand,
            product.url,
            product.barcode,
            product.price_badge,
            product.seller,
            int(product.in_stock) if product.in_stock is not None else 1,
            product.currency,
        )

        await conn.execute(
            _INSERT_PRICE_HISTORY,
            product.platform,
            product.product_id,
            product.price,
            product.original_price,
            product.discount_rate,
            product.rating,
            product.review_count,
            int(product.in_stock) if product.in_stock is not None else None,
        )

    return prev_price


async def get_badge_products(pool: SQLitePool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_GET_BADGE_PRODUCTS)
    return rows


async def get_cross_matches(pool: SQLitePool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_GET_CROSS_MATCHES)
    return rows


async def get_recent(pool: SQLitePool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_GET_RECENT)
    return rows


async def update_barcode(pool: SQLitePool, barcode: str, platform: str, product_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_UPDATE_BARCODE, barcode, platform, product_id)


async def fetch_without_barcode(pool: SQLitePool, platform: Optional[str] = None, limit: int = 100) -> list[dict]:
    q = _FETCH_WITHOUT_BARCODE
    args: tuple = ()
    if platform:
        q += " AND platform = ?"
        args = (platform,)
    q += f" LIMIT {limit}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(q, *args)
    return rows


async def get_price_history(
    pool: SQLitePool, platform: str, product_id: str, since: str = "-30 days"
) -> list[float]:
    """Belirtilen aralıktaki (varsayılan: son 30 gün) fiyat listesini döndürür."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_GET_PRICE_HISTORY_SINCE, platform, product_id, since)
    return [float(r["price"]) for r in rows]


async def get_price_history_for_category(
    pool: SQLitePool, platform: str, category: str, since: str = "-30 days"
) -> list[float]:
    """Bir kategorideki tüm ürünlerin belirtilen aralıktaki fiyatlarını döndürür."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_GET_PRICE_HISTORY_FOR_CATEGORY, platform, category, since)
    return [float(r["price"]) for r in rows]


async def add_watch(
    pool: SQLitePool, user_id: str, platform: str, product_id: str, target_price: Optional[float] = None
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_UPSERT_WATCH, user_id, platform, product_id, target_price)


async def get_watchers(pool: SQLitePool, platform: str, product_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        return await conn.fetch(_GET_WATCHERS, platform, product_id)


async def save_fcm_token(pool: SQLitePool, user_id: str, token: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_UPSERT_FCM_TOKEN, user_id, token)


async def get_fcm_tokens(pool: SQLitePool, user_id: str) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_GET_FCM_TOKENS, user_id)
    return [r["token"] for r in rows]
