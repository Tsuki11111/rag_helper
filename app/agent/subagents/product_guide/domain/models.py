"""导购领域模型：只放纯数据与枚举，不依赖外部客户端。"""
from dataclasses import dataclass
from enum import Enum


class SortMode(str, Enum):
    RECOMMEND = "recommend"
    SALES = "sales"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    NEWEST = "newest"


@dataclass(frozen=True)
class ProductQuery:
    keyword: str = ""
    category_id: int | None = None
    price_min: float | None = None
    price_max: float | None = None
    sort: SortMode = SortMode.RECOMMEND
    limit: int = 5

    def to_payload(self) -> dict:
        payload: dict = {"keyword": self.keyword, "sort": self.sort.value, "limit": self.limit}
        if self.category_id is not None:
            payload["categoryId"] = self.category_id
        if self.price_min is not None:
            payload["priceMin"] = self.price_min
        if self.price_max is not None:
            payload["priceMax"] = self.price_max
        return payload


@dataclass(frozen=True)
class ProductCard:
    productId: str
    name: str
    image: str
    price: str
    stock: str
    sales: str
    rating: str
    merchantName: str
    specText: str
    categoryName: str

    def to_dict(self) -> dict:
        return {
            "productId": self.productId,
            "name": self.name,
            "image": self.image,
            "price": self.price,
            "stock": self.stock,
            "sales": self.sales,
            "rating": self.rating,
            "merchantName": self.merchantName,
            "specText": self.specText,
            "categoryName": self.categoryName,
        }
