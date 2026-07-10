from dataclasses import dataclass
from typing import NamedTuple, Optional


class PlatformConfig(NamedTuple):
    base_url: str
    delay_min: float
    delay_max: float
    max_concurrent: int


PLATFORMS: dict[str, PlatformConfig] = {
    "trendyol": PlatformConfig(
        base_url="https://www.trendyol.com",
        delay_min=2.0,
        delay_max=5.5,
        max_concurrent=2,
    ),
    "amazon": PlatformConfig(
        base_url="https://www.amazon.com.tr",
        delay_min=3.5,
        delay_max=8.0,
        max_concurrent=1,
    ),
}


@dataclass(frozen=True)
class BrowserProfile:
    """UA + TLS impersonation + platform bilgisini tutarlı biçimde bir arada tutar."""
    ua: str
    impersonate: str   # curl_cffi BrowserType değeri
    platform: str      # Sec-CH-UA-Platform değeri, örn. '"Windows"'
    mobile: str = "?0"

    @property
    def is_firefox(self) -> bool:
        return "Firefox" in self.ua

    @property
    def _chrome_version(self) -> Optional[str]:
        if "Chrome/" in self.ua:
            return self.ua.split("Chrome/")[1].split(".")[0]
        return None

    @property
    def sec_ch_ua(self) -> Optional[str]:
        v = self._chrome_version
        if v is None:
            return None
        return f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not-A.Brand";v="99"'


# Her profil: UA ↔ TLS impersonation ↔ platform tutarlı eşleştirilmiştir.
BROWSER_PROFILES: list[BrowserProfile] = [
    # Chrome – Windows
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        impersonate="chrome146",
        platform='"Windows"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        impersonate="chrome136",
        platform='"Windows"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        impersonate="chrome131",
        platform='"Windows"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        impersonate="chrome124",
        platform='"Windows"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        impersonate="chrome120",
        platform='"Windows"',
    ),
    # Chrome – macOS
    BrowserProfile(
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        impersonate="chrome136",
        platform='"macOS"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        impersonate="chrome131",
        platform='"macOS"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        impersonate="chrome124",
        platform='"macOS"',
    ),
    # Chrome – Linux
    BrowserProfile(
        ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        impersonate="chrome131",
        platform='"Linux"',
    ),
    # Firefox – Windows
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        impersonate="firefox147",
        platform='"Windows"',
    ),
    BrowserProfile(
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        impersonate="firefox135",
        platform='"Windows"',
    ),
    # Firefox – macOS
    BrowserProfile(
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:147.0) Gecko/20100101 Firefox/147.0",
        impersonate="firefox147",
        platform='"macOS"',
    ),
]
