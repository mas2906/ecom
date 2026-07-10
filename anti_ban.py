"""
Anti-ban katmanı: TLS parmak izi sahteciliği, insan-benzeri gecikmeler,
oturum yönetimi, otomatik backoff.

Her ScraperSession rastgele bir BrowserProfile seçer; UA, TLS impersonation
ve platform başlıkları her zaman tutarlı biçimde eşleştirilir.
429 veya engel durumunda profil + TLS birlikte rotasyona girer.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from curl_cffi.requests import AsyncSession, Response

from config import BROWSER_PROFILES, BrowserProfile, PlatformConfig

logger = logging.getLogger(__name__)


def _pick_profile(exclude: Optional[BrowserProfile] = None) -> BrowserProfile:
    pool = [p for p in BROWSER_PROFILES if p != exclude]
    return random.choice(pool)


def _build_headers(
    profile: BrowserProfile,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict:
    if profile.is_firefox:
        return _firefox_headers(profile, referer=referer, xhr=xhr)
    return _chrome_headers(profile, referer=referer, xhr=xhr)


def _chrome_headers(
    profile: BrowserProfile,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict:
    if xhr:
        headers = {
            "User-Agent": profile.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
    else:
        headers = {
            "User-Agent": profile.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none" if not referer else "same-origin",
            "Sec-Fetch-User": "?1",
        }

    if profile.sec_ch_ua:
        headers["Sec-CH-UA"] = profile.sec_ch_ua
        headers["Sec-CH-UA-Mobile"] = profile.mobile
        headers["Sec-CH-UA-Platform"] = profile.platform

    if referer:
        headers["Referer"] = referer

    return headers


def _firefox_headers(
    profile: BrowserProfile,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict:
    if xhr:
        headers = {
            "User-Agent": profile.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
    else:
        headers = {
            "User-Agent": profile.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none" if not referer else "same-origin",
            "Sec-Fetch-User": "?1",
        }
        # Firefox Sec-CH-UA başlıklarını göndermez

    if referer:
        headers["Referer"] = referer

    return headers


class ScraperSession:
    """
    curl_cffi tabanlı oturum. Her oturum rastgele bir BrowserProfile kullanır;
    UA, TLS handshake ve platform başlıkları tutarlı biçimde eşleştirilir.
    """

    def __init__(self, config: PlatformConfig):
        self._config = config
        self._profile: BrowserProfile = _pick_profile()
        self._session: Optional[AsyncSession] = None
        self._last_req_at: float = 0.0
        self.total_requests: int = 0

    async def __aenter__(self) -> "ScraperSession":
        self._session = AsyncSession(impersonate=self._profile.impersonate)
        logger.debug("Oturum başlatıldı: %s (%s)", self._profile.impersonate, self._profile.ua[:60])
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Gecikme yönetimi
    # ------------------------------------------------------------------

    async def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_req_at
        delay = random.uniform(self._config.delay_min, self._config.delay_max)

        # %12 olasılıkla "sayfayı okuma" molası ver
        if random.random() < 0.12:
            delay += abs(random.gauss(3.5, 1.2))

        remaining = delay - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

        self._last_req_at = time.monotonic()

    async def _rotate_profile(self) -> None:
        """UA + TLS parmak izini birlikte rotasyona sokar; yeni AsyncSession açar."""
        old = self._profile
        self._profile = _pick_profile(exclude=old)
        logger.debug(
            "Profil rotasyonu: %s → %s | %s",
            old.impersonate,
            self._profile.impersonate,
            self._profile.ua[:60],
        )
        if self._session:
            await self._session.close()
        self._session = AsyncSession(impersonate=self._profile.impersonate)

    # ------------------------------------------------------------------
    # HTTP GET
    # ------------------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        xhr: bool = False,
        extra_headers: Optional[dict] = None,
    ) -> Response:
        await self._wait()

        headers = _build_headers(self._profile, referer=referer, xhr=xhr)
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Exception = RuntimeError("bilinmeyen hata")

        for attempt in range(4):
            try:
                resp = await self._session.get(
                    url,
                    headers=headers,
                    timeout=30,
                    allow_redirects=True,
                )
                self.total_requests += 1

                if resp.status_code == 200:
                    return resp

                if resp.status_code == 429:
                    backoff = (2**attempt) * 15 + random.uniform(5, 20)
                    logger.warning(
                        "429 Rate-limit: %s — %ds bekleniyor (deneme %d/4)",
                        url[:60], int(backoff), attempt + 1,
                    )
                    await asyncio.sleep(backoff)
                    await self._rotate_profile()
                    headers = _build_headers(self._profile, referer=referer, xhr=xhr)
                    if extra_headers:
                        headers.update(extra_headers)
                    continue

                if resp.status_code in (403, 503):
                    backoff = (2**attempt) * 10
                    logger.warning(
                        "%d engeli: %s — %ds backoff",
                        resp.status_code, url[:60], int(backoff),
                    )
                    await asyncio.sleep(backoff)
                    continue

                if resp.status_code == 404:
                    raise ValueError(f"404: {url}")

                logger.error("Beklenmeyen HTTP %d: %s", resp.status_code, url[:80])
                return resp

            except ValueError:
                raise
            except Exception as exc:
                last_exc = exc
                backoff = (2**attempt) * 4
                logger.warning("İstek hatası (%s) — %ds sonra tekrar", exc, backoff)
                if attempt == 3:
                    raise
                await asyncio.sleep(backoff)

        raise last_exc
