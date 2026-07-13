"""
Telegram Bot bildirimleri — httpx ile Telegram Bot API çağrısı.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from models import Product

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"

PLATFORM_EMOJI = {
    "trendyol": "🟠",
    "hepsiburada": "🔴",
    "amazon": "🔵",
}

PLATFORM_CODE = {
    "trendyol": "TY",
    "amazon": "AMZ",
    "hepsiburada": "HB",
}

# Fiyat düşüşü mesajının alt satırındaki "bu sitede ara" linkleri.
# Gerçek fiyat verisi taşımıyor — sadece ürün başlığıyla arama linki.
COMPARE_LINKS = [
    ("🟧", "Amazon", "https://www.amazon.com.tr/s?k={q}"),
    ("🔵", "Akakçe", "https://www.akakce.com/arama/?q={q}"),
    ("🔴", "Trendyol", "https://www.trendyol.com/sr?q={q}&qt={q}"),
    ("🟠", "HB", "https://www.hepsiburada.com/ara?q={q}"),
    ("🟢", "Google", "https://www.google.com/search?tbm=shop&q={q}"),
]


def _format_try(value: float) -> str:
    """6533.15 -> '6.533,15 ₺', 6847.0 -> '6.847 ₺' (TR biçimi, tam sayıda ondalık yok)."""
    s = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} ₺"


def _compare_links_line(title: str) -> str:
    q = quote(title)
    parts = [f'{emoji} <a href="{url.format(q=q)}">{name}</a>' for emoji, name, url in COMPARE_LINKS]
    return "  ·  ".join(parts)


@dataclass
class Notifier:
    token: str
    chat_id: str
    min_drop_pct: float = 0.0  # 0 = her düşüşte bildir

    async def price_drop(
        self,
        product: Product,
        old_price: float,
        new_price: float,
        low_period_label: Optional[str] = None,
    ) -> None:
        if old_price <= 0 or new_price is None:
            return

        drop_pct = (old_price - new_price) / old_price * 100
        if drop_pct < self.min_drop_pct:
            return

        code = PLATFORM_CODE.get(product.platform, product.platform.upper())
        period_line = f"📉 {low_period_label}\n" if low_period_label else ""

        text = (
            f'[{code}] <a href="{product.url}">{product.title[:90]}</a>\n'
            f"🏷 {_format_try(new_price)}  🕰️ {_format_try(old_price)}  "
            f"⬇️ %{drop_pct:.0f}  (−{_format_try(old_price - new_price)})\n"
            f"{period_line}\n"
            f"{_compare_links_line(product.title)}"
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    _API.format(token=self.token),
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning("Telegram API hatası: %s", resp.text)
            else:
                logger.info(
                    "📲 Bildirim gönderildi: %s → %.2f TRY (-%0.1f%%)",
                    product.title[:40], new_price, drop_pct,
                )
        except Exception as exc:
            logger.warning("Telegram gönderilemedi: %s", exc)

    async def price_badge_alert(self, product: Product) -> None:
        """Amazon 'en düşük fiyat' rozeti bildirimi."""
        emoji = PLATFORM_EMOJI.get(product.platform, "🛍️")
        badge = product.price_badge or ""

        # Rozet metninden süreyi çıkar (60 gün, 30 gün, yıl vb.)
        import re as _re
        period_m = _re.search(r'(\d+)\s*gün', badge, _re.IGNORECASE)
        if period_m:
            period = f"{period_m.group(1)} günün"
        elif "yıl" in badge.lower():
            period = "Yılın"
        else:
            period = "Tarihi"

        price_str = f"{product.price:,.2f} TRY" if product.price else "—"
        brand_line = f" · {product.brand}" if product.brand else ""

        text = (
            f"🏷️ <b>{period} en düşük fiyatı!</b>\n\n"
            f'{emoji} <a href="{product.url}">{product.title[:90]}</a>\n'
            f"<b>{product.platform.upper()}</b>{brand_line}\n\n"
            f"💰 Fiyat: <b>{price_str}</b>\n"
            f"📌 Rozet: <i>{badge}</i>\n"
            f"🏷️ Kategori: {product.category}"
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    _API.format(token=self.token),
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning("Telegram badge hatası: %s", resp.text)
            else:
                logger.info("📲 Rozet bildirimi: %s → %s", badge, product.title[:40])
        except Exception as exc:
            logger.warning("Telegram gönderilemedi: %s", exc)

    async def cross_match(self, match: dict) -> None:
        """Aynı barkodlu ürün her iki platformda da bulunduğunda bildirim gönder."""
        ty_price = match.get("price_trendyol")
        am_price = match.get("price_amazon")
        barcode  = match.get("barcode", "—")

        if ty_price and am_price and ty_price > 0 and am_price > 0:
            diff_pct = abs(ty_price - am_price) / max(ty_price, am_price) * 100
            cheaper  = "🟠 Trendyol" if ty_price < am_price else "🔵 Amazon"
            saving   = abs(ty_price - am_price)
            price_block = (
                f"🟠 Trendyol: <b>{ty_price:,.2f} TRY</b>\n"
                f"🔵 Amazon:   <b>{am_price:,.2f} TRY</b>\n"
                f"💡 {cheaper} %{diff_pct:.1f} daha ucuz · {saving:,.2f} TRY fark"
            )
        else:
            price_block = (
                f"🟠 Trendyol: {'%.2f TRY' % ty_price if ty_price else '—'}\n"
                f"🔵 Amazon:   {'%.2f TRY' % am_price if am_price else '—'}"
            )

        text = (
            f"🔗 <b>Çapraz Eşleşme Bulundu!</b>\n\n"
            f"🏷️ Barkod: <code>{barcode}</code>\n\n"
            f"📦 <a href=\"{match.get('url_trendyol','')}\">Trendyol</a>: {match.get('title_trendyol','')[:70]}\n"
            f"📦 <a href=\"{match.get('url_amazon','')}\">Amazon</a>: {match.get('title_amazon','')[:70]}\n\n"
            f"{price_block}"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    _API.format(token=self.token),
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning("Telegram cross-match hatası: %s", resp.text)
            else:
                logger.info("📲 Çapraz eşleşme bildirimi: %s", barcode)
        except Exception as exc:
            logger.warning("Telegram gönderilemedi: %s", exc)
