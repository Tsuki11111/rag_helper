"""LangChain 工具适配层：只负责参数透传，业务在 ProductGuideService。"""
from app.agent.subagents.product_guide.application.formatter import ProductTextFormatter
from app.agent.subagents.product_guide.application.product_guide_service import (
    ProductGuideService,
)
from app.agent.subagents.product_guide.application.semantic_search_service import (
    SemanticSearchService,
)
from app.agent.subagents.product_guide.infrastructure.card_sink import ContextVarCardSink
from app.agent.subagents.product_guide.infrastructure.catalog_gateway import JavaCatalogGateway
from app.agent.subagents.product_guide.infrastructure.embedding_gateway import (
    DashScopeEmbeddingGateway,
)
from app.agent.tools.profile_tools import get_user_profile

_service: ProductGuideService | None = None


def configure_service(service: ProductGuideService) -> None:
    """测试/组合根注入；生产默认懒加载。"""
    global _service
    _service = service


def get_service() -> ProductGuideService:
    global _service
    if _service is None:
        catalog = JavaCatalogGateway()
        semantic = SemanticSearchService(catalog, DashScopeEmbeddingGateway())
        _service = ProductGuideService(
            catalog, semantic, ProductTextFormatter(), ContextVarCardSink())
    return _service


def search_products(keyword: str = "",
                    category_id: int | None = None,
                    price_min: float | None = None,
                    price_max: float | None = None,
                    sort: str = "recommend",
                    limit: int = 5) -> str:
    """搜索在售商品，返回价格、库存、销量、评分、店铺和 SKU。

    :param keyword: 关键词，如「游戏本」「A4 纸」；也可以传一句自然语言
    :param category_id: 可选分类 id（先用 list_categories 查）
    :param price_min: 可选最低价
    :param price_max: 可选最高价
    :param sort: recommend（默认）/ sales / price_asc / price_desc / newest
    :param limit: 最多返回几个，默认 5（Java 侧上限 10）
    """
    return get_service().search(
        keyword, category_id, price_min, price_max, sort, limit)


def semantic_search_products(query: str, limit: int = 5) -> str:
    """用自然语言做语义检索（关键词搜不到时用），返回最相关的商品。"""
    return get_service().semantic_search(query, limit)


def list_categories() -> str:
    """列出商品分类树（含 id），供 search_products 的 category_id 使用。"""
    return get_service().list_categories()


PRODUCT_GUIDE_TOOLS = [
    get_user_profile,
    search_products,
    semantic_search_products,
    list_categories,
]
