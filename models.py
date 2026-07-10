from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone


class Product(BaseModel):
    platform: str
    category: str

    product_id: str
    title: str
    brand: Optional[str] = None
    url: str

    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_rate: Optional[float] = None
    currency: str = "TRY"
    in_stock: bool = True

    rating: Optional[float] = None
    review_count: Optional[int] = None

    barcode: Optional[str] = None
    price_badge: Optional[str] = None   # "60 günün en düşük fiyatı" vb.
    seller: Optional[str] = None
    seller_rating: Optional[float] = None

    images: list[str] = Field(default_factory=list)
    scraped_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
