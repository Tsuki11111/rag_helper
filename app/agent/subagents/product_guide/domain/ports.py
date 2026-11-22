"""导购领域的端口：应用层只依赖这些 Protocol，不直接依赖 Java/embedding。"""
from typing import Protocol

from app.agent.subagents.product_guide.domain.models import ProductQuery


class CatalogGateway(Protocol):
    def search(self, query: ProductQuery) -> list[dict]:
        ...

    def all_products(self) -> list[dict]:
        ...

    def categories(self) -> list[dict]:
        ...


class EmbeddingGateway(Protocol):
    def embed(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        ...


class CardSink(Protocol):
    def add(self, cards: list[dict]) -> None:
        ...
