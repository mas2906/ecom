from abc import ABC, abstractmethod
from typing import AsyncIterator

from anti_ban import ScraperSession
from models import Product


class BaseScraper(ABC):
    def __init__(self, session: ScraperSession):
        self.session = session

    @abstractmethod
    async def scrape_category(
        self, category: str, max_pages: int = 5
    ) -> AsyncIterator[Product]:
        """Kategori bazlı ürün akışı üretir."""
        ...
