"""商品目录客户端：隔离 Java 内部接口路径与鉴权细节。"""
from app.agent.subagents.order_service.infrastructure.its_mygo_client import call
from app.agent.subagents.product_guide.domain.models import ProductQuery


class CatalogError(RuntimeError):
    """目录接口不可用/业务错误；由应用层转成可读提示。"""


class JavaCatalogGateway:
    def __init__(self, call_fn=None):
        self._call = call_fn or call

    def search(self, query: ProductQuery) -> list[dict]:
        data, error = self._call(
            "/internal/agent/catalog/products", query.to_payload(), read_only=True)
        if error:
            raise CatalogError(str(error))
        return [p for p in (data or []) if isinstance(p, dict)]

    def all_products(self) -> list[dict]:
        data, error = self._call("/internal/agent/catalog/all", method="GET")
        if error:
            raise CatalogError(str(error))
        return [p for p in (data or []) if isinstance(p, dict)]

    def categories(self) -> list[dict]:
        data, error = self._call(
            "/internal/agent/catalog/categories", method="GET", read_only=True)
        if error:
            raise CatalogError(str(error))
        return [n for n in (data or []) if isinstance(n, dict)]
