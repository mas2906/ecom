"""
Anti-ban katmanı: TLS parmak izi sahteciliği, insan-benzeri gecikmeler,
oturum yönetimi, otomatik backoff.

Profil havuzu: masaüstü Chrome, Firefox, Android Chrome, iOS Safari.
Her ScraperSession rastgele bir profil seçer; UA, TLS impersonation,
platform başlıkları ve header formatı her zaman tutarlı biçimde eşleştirilir.
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


def _pick_profile(
    exclude: Optional[BrowserProfile] = None,
    exclude_impersonate: Optional[str] = None,
) -> BrowserProfile:
    pool = [
        p for p in BROWSER_PROFILES
        if p != exclude and p.impersonate != exclude_impersonate
    ]
    if not pool:  # tüm impersonation'lar exclude ise sadece objeyi hariç tut
        pool = [p for p in BROWSER_PROFILES if p != exclude]
    return random.choice(pool)


# ---------------------------------------------------------------------------
# Header builder'lar — her tarayıcı tipi için ayrı
# ---------------------------------------------------------------------------

def _build_headers(
    profile: BrowserProfile,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict:
    if profile.is_safari:
        return _safari_ios_headers(profile, referer=referer, xhr=xhr)
    if profile.is_firefox:
        return _firefox_headers(profile, referer=referer, xhr=xhr)
    if profile.is_mobile:
        return _android_chrome_headers(profile, referer=referer, xhr=xhr)
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
            "DNT": "1",
        }

    if profile.sec_ch_ua:
        headers["Sec-CH-UA"] = profile.sec_ch_ua
        headers["Sec-CH-UA-Mobile"] = profile.mobile
        headers["Sec-CH-UA-Platform"] = profile.platform

    if referer:
        headers["Referer"] = referer

    return headers


def _android_chrome_headers(
    profile: BrowserProfile,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict:
    """Android Chrome — mobil UA + mobil Sec-CH-UA başlıkları."""
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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none" if not referer else "same-origin",
            "Sec-Fetch-User": "?1",
            # X-Requested-With kasıtlı çıkarıldı — uygulama header'ı tarayıcıdan gelmez
        }

    if profile.sec_ch_ua:
        headers["Sec-CH-UA"] = profile.sec_ch_ua
        headers["Sec-CH-UA-Mobile"] = "?1"
        headers["Sec-CH-UA-Platform"] = '"Android"'

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
            "DNT": "1",
            # Firefox Sec-CH-UA göndermez
        }

    if referer:
        headers["Referer"] = referer

    return headers


def _safari_ios_headers(
    profile: BrowserProfile,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict:
    """iOS Safari — Sec-CH-UA ve Sec-Fetch-* yoktur, Accept formatı farklıdır."""
    if xhr:
        headers = {
            "User-Agent": profile.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "tr-TR,tr;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }
    else:
        headers = {
            "User-Agent": profile.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
            # Safari Sec-Fetch-* göndermez (özellikle eski versiyonlar)
        }

    if referer:
        headers["Referer"] = referer

    return headers


# ---------------------------------------------------------------------------
# ScraperSession
# ---------------------------------------------------------------------------

class ScraperSession:
    """
    curl_cffi tabanlı oturum.
    Profil havuzu: desktop Chrome/Firefox + Android Chrome + iOS Safari.
    UA, TLS handshake ve header formatı seçilen profile göre otomatik eşleştirilir.
    """

    def __init__(self, config: PlatformConfig):
        self._config = config
        self._profile: BrowserProfile = _pick_profile()
        self._session: Optional[AsyncSession] = None
        self._last_req_at: float = 0.0
        self.total_requests: int = 0
        self._req_count_session: int = 0  # mevcut profildeki başarılı istek sayısı

    async def __aenter__(self) -> "ScraperSession":
        self._session = AsyncSession(impersonate=self._profile.impersonate)
        logger.debug(
            "Oturum başlatıldı: %s | %s | mobil=%s",
            self._profile.impersonate,
            self._profile.ua[:70],
            self._profile.is_mobile,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Gecikme yönetimi
    # ------------------------------------------------------------------

    async def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_req_at

        # Gaussian dağılım — insan davranışına daha yakın
        mid = (self._config.delay_min + self._config.delay_max) / 2
        std = (self._config.delay_max - self._config.delay_min) / 4
        delay = max(self._config.delay_min, random.gauss(mid, std))
        delay = min(delay, self._config.delay_max * 1.5)

        # %20 olasılıkla "sayfa okuma" molası
        if random.random() < 0.20:
            read_pause = abs(random.gauss(5.0, 2.0))
            delay += read_pause
            logger.debug("Okuma molası: +%.1fs", read_pause)

        # Her 8-12 istekte bir "nefes alma" molası — bot pattern kırar
        breath_every = random.randint(8, 12)
        if self._req_count_session > 0 and self._req_count_session % breath_every == 0:
            breath = random.uniform(10.0, 25.0)
            logger.debug("Nefes molası (%d. istek): %.1fs", self._req_count_session, breath)
            await asyncio.sleep(breath)

        remaining = delay - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

        self._last_req_at = time.monotonic()

    async def _rotate_profile(self) -> None:
        """UA + TLS parmak izini birlikte rotasyona sokar; yeni AsyncSession açar."""
        old = self._profile
        # Aynı TLS impersonation'a düşmemek için impersonate değerini de dışla
        self._profile = _pick_profile(exclude=old, exclude_impersonate=old.impersonate)
        self._req_count_session = 0
        logger.info(
            "Profil rotasyonu: %s → %s (%s)",
            old.impersonate,
            self._profile.impersonate,
            "mobil" if self._profile.is_mobile else "masaüstü",
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
                    timeout=35,
                    allow_redirects=True,
                )
                self.total_requests += 1

                if resp.status_code == 200:
                    self._req_count_session += 1
                    return resp

                if resp.status_code == 429:
                    backoff = (2 ** attempt) * 20 + random.uniform(10, 30)
                    logger.warning(
                        "429 Rate-limit: %s — %.0fs bekleniyor (deneme %d/4)",
                        url[:60], backoff, attempt + 1,
                    )
                    await asyncio.sleep(backoff)
                    await self._rotate_profile()
                    headers = _build_headers(self._profile, referer=referer, xhr=xhr)
                    if extra_headers:
                        headers.update(extra_headers)
                    continue

                if resp.status_code in (403, 503):
                    backoff = (2 ** attempt) * 12 + random.uniform(5, 15)
                    logger.warning(
                        "%d engeli: %s — %.0fs backoff (deneme %d/4)",
                        resp.status_code, url[:60], backoff, attempt + 1,
                    )
                    await asyncio.sleep(backoff)
                    if attempt >= 1:
                        await self._rotate_profile()
                        headers = _build_headers(self._profile, referer=referer, xhr=xhr)
                        if extra_headers:
                            headers.update(extra_headers)
                    continue

                if resp.status_code == 404:
                    raise ValueError(f"404: {url}")

                logger.error("Beklenmeyen HTTP %d: %s", resp.status_code, url[:80])
                return resp

            except ValueError:
                raise
            except Exception as exc:
                last_exc = exc
                backoff = (2 ** attempt) * 5 + random.uniform(2, 8)
                logger.warning("İstek hatası (%s) — %.0fs sonra tekrar", exc, backoff)
                if attempt == 3:
                    raise
                await asyncio.sleep(backoff)

        raise last_exc
