"""导购应用服务：用例编排，不直接依赖 Java/embedding 实现。"""
from app.agent.subagents.product_guide.application.formatter import ProductTextFormatter
from app.agent.subagents.product_guide.application.semantic_search_service import (
    SemanticSearchService,
)
from app.agent.subagents.product_guide.domain.models import ProductQuery, SortMode
from app.agent.subagents.product_guide.domain.ports import (
    CardSink,
    CatalogGateway,
)
from app.agent.subagents.product_guide.infrastructure.catalog_gateway import CatalogError
from app.conf.product_guide_config import product_guide_config


class ProductGuideService:
    def __init__(self, catalog: CatalogGateway, semantic: SemanticSearchService,
                 formatter: ProductTextFormatter, card_sink: CardSink):
        self._catalog = catalog
        self._semantic = semantic
        self._formatter = formatter
        self._card_sink = card_sink

    def search(self, keyword: str = "", category_id: int | None = None,
               price_min: float | None = None, price_max: float | None = None,
               sort: str = "recommend",
               limit: int = product_guide_config.default_limit) -> str:
        safe_limit = _limit(limit)
        query = ProductQuery(
            keyword=(keyword or "").strip(),
            category_id=_int_or_none(category_id),
            price_min=_float_or_none(price_min),
            price_max=_float_or_none(price_max),
            sort=_sort(sort),
            limit=safe_limit,
        )
        try:
            products = self._catalog.search(query)
        except CatalogError as e:
            return str(e)

        source = "关键词"
        if len(products) < 2 and query.keyword:
            semantic = self._semantic.search(query.keyword, safe_limit)
            if semantic:
                products = _merge(products, semantic)
                source = "关键词 + 语义"

        products = products[:safe_limit]
        if not products:
            return ("没有找到符合条件的在售商品。可以换个说法，或告诉我预算/用途/品类，"
                    "我再用语义检索找找。")
        self._card_sink.add(ProductTextFormatter.cards(products))
        return self._formatter.format(products, source)

    def semantic_search(self, query: str,
                        limit: int = product_guide_config.default_limit) -> str:
        q = (query or "").strip()
        if not q:
            return "请提供要搜索的自然语言描述，例如「能打游戏的笔记本」。"
        safe_limit = _limit(limit)
        products = [p for p in self._semantic.search(q, safe_limit) if isinstance(p, dict)]
        if not products:
            return "语义检索没有找到相关商品；可以换个说法，或直接告诉我预算和用途。"
        self._card_sink.add(ProductTextFormatter.cards(products))
        return self._formatter.format(products, "语义检索")

    def list_categories(self) -> str:
        try:
            nodes = self._catalog.categories()
        except CatalogError as e:
            return str(e)
        if not nodes:
            return "当前没有可用分类。"
        return self._formatter.format_categories(nodes)


def _limit(value) -> int:
    try:
        return max(1, min(int(value or product_guide_config.default_limit),
                          product_guide_config.max_limit))
    except (TypeError, ValueError):
        return product_guide_config.default_limit


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort(value) -> SortMode:
    try:
        return SortMode(str(value or "recommend"))
    except ValueError:
        return SortMode.RECOMMEND


def _merge(primary: list[dict], secondary: list[dict]) -> list[dict]:
    out, seen = [], set()
    for product in list(primary) + list(secondary):
        product_id = str(product.get("productId") or "")
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        out.append(product)
    return out


def _check_product_guide_tools() -> list:
    """导购服务：格式化、语义回退、分类树、错误可读、商品卡片入桶。"""
    class _Catalog:
        def __init__(self, products=None, error=None):
            self.products = products or []
            self.error = error

        def search(self, query):
            if self.error:
                raise CatalogError(self.error)
            return [dict(p) for p in self.products]

        def all_products(self):
            return [dict(p) for p in self.products]

        def categories(self):
            return [{"id": 1, "name": "电脑",
                     "children": [{"id": 2, "name": "笔记本", "children": []}]}]

    class _Semantic:
        def __init__(self, products=None):
            self.products = products or []

        def search(self, query, limit=5):
            return [dict(p) for p in self.products]

    class _Sink:
        def __init__(self):
            self.cards = []

        def add(self, cards):
            self.cards.extend(cards)

    product = {
        "productId": 1, "name": "游戏本", "subtitle": "高性能独显",
        "description": "适合打游戏和视频剪辑，16G 内存。", "mainImage": "img",
        "price": "6999.00", "totalStock": 3, "sales": 12, "rating": "4.8",
        "reviewCount": 5, "merchantName": "数码店", "categoryName": "电脑",
        "skus": [{"skuId": 11, "specText": "颜色:黑", "price": "6999.00", "stock": 3}],
    }
    problems = []
    sink = _Sink()
    service = ProductGuideService(
        _Catalog([product]), SemanticSearchService(_Catalog([product]), _Embed()),
        ProductTextFormatter(), sink)
    text = service.search("游戏本", limit=3)
    for expected in ("游戏本", "卖点：高性能独显", "简介：适合打游戏和视频剪辑",
                     "skuId=11", "6999.00", "库存 3", "销量 12", "评分 4.8",
                     "数码店", "/product/1", "2~3 句话介绍"):
        if expected not in text:
            problems.append(f"关键词结果缺少 {expected!r}")
    if len(sink.cards) != 1 or sink.cards[0].get("productId") != "1":
        problems.append(f"商品卡片未入桶：{sink.cards!r}")

    sink.cards.clear()
    service = ProductGuideService(
        _Catalog([]), SemanticSearchService(_Catalog([product]), _Embed()),
        ProductTextFormatter(), sink)
    text = service.search("能打游戏的本子", limit=3)
    if "语义" not in text or "游戏本" not in text:
        problems.append(f"语义回退没有生效：{text!r}")
    if not sink.cards:
        problems.append("语义结果的商品卡片未入桶")

    text = service.list_categories()
    for expected in ("电脑（id=1）", "笔记本（id=2）"):
        if expected not in text:
            problems.append(f"分类树缺少 {expected!r}")

    broken = ProductGuideService(
        _Catalog(error="平台服务暂时不可用"), SemanticSearchService(_Catalog([]), _Embed()),
        ProductTextFormatter(), sink)
    if "暂时不可用" not in broken.search("游戏本"):
        problems.append("错误提示不清晰")
    return problems


class _Embed:
    def embed(self, texts, text_type="document"):
        return [[1.0, 0.0] for _ in texts]
