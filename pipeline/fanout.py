"""
FAN-OUT — watches tablosundan bu ürünü takip eden kullanıcıları bulur,
her birine FCM push bildirimi yollar.

FCM kimlik bilgisi henüz yoksa NullPushClient devreye girer (sadece loglar).
Gerçek gönderim için:
  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
  push_client = FirebasePushClient()
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from db import SQLitePool, get_fcm_tokens, get_watchers
from pipeline.change_engine import Opportunity

log = logging.getLogger(__name__)


class PushClient(Protocol):
    async def send(self, token: str, title: str, body: str, data: dict) -> None: ...


class NullPushClient:
    """FCM kurulu değilken kullanılan geliştirme fallback'i — sadece loglar."""

    async def send(self, token: str, title: str, body: str, data: dict) -> None:
        log.info("[push:DRY-RUN] token=%s… title=%r body=%r", token[:12], title, body)


class FirebasePushClient:
    """firebase-admin üzerinden gerçek FCM gönderimi."""

    def __init__(self) -> None:
        import firebase_admin
        from firebase_admin import messaging

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        self._messaging = messaging

    async def send(self, token: str, title: str, body: str, data: dict) -> None:
        message = self._messaging.Message(
            token=token,
            notification=self._messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in data.items()},
        )
        self._messaging.send(message)


async def notify_watchers(
    pool: SQLitePool,
    opportunity: Opportunity,
    product_title: str,
    product_url: str,
    push_client: Optional[PushClient] = None,
) -> int:
    """opportunity'i takip eden tüm kullanıcılara push gönderir. Gönderilen sayıyı döndürür."""
    push_client = push_client or NullPushClient()

    watchers = await get_watchers(pool, opportunity.platform, opportunity.product_id)
    sent = 0
    for watcher in watchers:
        target_price = watcher.get("target_price")
        if target_price is not None and opportunity.new_price > target_price:
            continue

        tokens = await get_fcm_tokens(pool, watcher["user_id"])
        for token in tokens:
            await push_client.send(
                token=token,
                title="Fiyat düştü! 🔻",
                body=(
                    f"{product_title[:60]}  {opportunity.new_price:,.2f} TRY "
                    f"(%{opportunity.drop_pct} düşüş)"
                ),
                data={
                    "platform": opportunity.platform,
                    "product_id": opportunity.product_id,
                    "url": product_url,
                    "price": opportunity.new_price,
                },
            )
            sent += 1

    return sent
