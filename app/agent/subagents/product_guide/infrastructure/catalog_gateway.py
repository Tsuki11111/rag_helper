"""商品目录客户端：隔离 Java 内部接口路径与鉴权细节。"""
from app.clients.its_mygo_client import call
from app.agent.subagents.product_guide.domain.models import ProductQuery
from app.conf.order_service_config import order_service_config


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
        data, error = self._call(
            "/internal/agent/catalog/all", method="GET", read_only=True,
            params={"limit": order_service_config.catalog_page_limit})
        if error:
            raise CatalogError(str(error))
        return [p for p in (data or []) if isinstance(p, dict)]

    def categories(self) -> list[dict]:
        data, error = self._call(
            "/internal/agent/catalog/categories", method="GET", read_only=True)
        if error:
            raise CatalogError(str(error))
        return [n for n in (data or []) if isinstance(n, dict)]


def _check_catalog_gateway_limit() -> list:
    """导购全量目录：显式 limit=100 + read_only，避免静默只索引前 100 且无重试。"""
    problems = []
    calls = []

    def fake_call(path, **kwargs):
        calls.append((path, kwargs))
        return [], None

    JavaCatalogGateway(call_fn=fake_call).all_products()
    if not calls:
        return ["导购目录没有调用 /catalog/all"]
    _, kwargs = calls[0]
    params = kwargs.get("params") or {}
    if params.get("limit") != order_service_config.catalog_page_limit:
        problems.append(f"导购目录 limit 不是 {order_service_config.catalog_page_limit}：{params!r}")
    if not kwargs.get("read_only"):
        problems.append("导购目录没有标 read_only")
    return problems
