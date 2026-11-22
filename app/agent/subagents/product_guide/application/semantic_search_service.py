"""商品语义检索服务：内存索引 + 缓存，依赖通过构造函数注入。"""
import math
import threading
import time

from app.agent.subagents.product_guide.domain.ports import CatalogGateway, EmbeddingGateway
from app.conf.product_guide_config import product_guide_config
from app.core.logger import logger


class SemanticSearchService:
    def __init__(self, catalog: CatalogGateway, embedding: EmbeddingGateway,
                 ttl_sec: float | None = None):
        self._catalog = catalog
        self._embedding = embedding
        self._ttl = ttl_sec if ttl_sec is not None else product_guide_config.semantic_ttl_sec
        self._lock = threading.Lock()
        self._products: list[dict] = []
        self._vectors: list[list[float]] = []
        self._loaded_at = 0.0

    def search(self, query: str, limit: int = 5) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        products, vectors = self._ensure_loaded()
        if not products or not vectors:
            return []
        try:
            query_vectors = self._embedding.embed([q], text_type="query")
            if not query_vectors:
                return []
            query_vector = query_vectors[0]
        except Exception as e:
            logger.warning(f"[semantic_index] 查询向量生成失败：{type(e).__name__}: {e}")
            return []

        scored = [(_cosine(query_vector, vector), product)
                  for product, vector in zip(products, vectors)]
        scored.sort(key=lambda item: item[0], reverse=True)
        safe_limit = max(1, min(int(limit or product_guide_config.default_limit),
                                product_guide_config.max_limit))
        if not scored:
            return []
        threshold = max(product_guide_config.semantic_min_score,
                        scored[0][0] * product_guide_config.semantic_relative_score)
        return [product for score, product in scored[:safe_limit] if score >= threshold]

    def reset(self) -> None:
        with self._lock:
            self._products, self._vectors, self._loaded_at = [], [], 0.0

    def _ensure_loaded(self) -> tuple[list[dict], list[list[float]]]:
        now = time.time()
        with self._lock:
            if self._vectors and now - self._loaded_at < self._ttl:
                return self._products, self._vectors
            try:
                products = self._catalog.all_products()
                vectors = self._embedding.embed(
                    [self._document(p) for p in products]) if products else []
            except Exception as e:
                logger.warning(f"[semantic_index] 构建索引失败：{type(e).__name__}: {e}")
                products, vectors = [], []
            if vectors:
                self._products, self._vectors, self._loaded_at = products, vectors, now
            return products, vectors

    @staticmethod
    def _document(product: dict) -> str:
        parts = [
            product.get("name") or "",
            product.get("subtitle") or "",
            product.get("description") or "",
            product.get("categoryName") or "",
            product.get("merchantName") or "",
        ]
        for sku in product.get("skus") or []:
            if isinstance(sku, dict) and sku.get("specText"):
                parts.append(str(sku["specText"]))
        return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _check_product_semantic_index() -> list:
    """语义索引：向量排序、缓存复用、embedding 失败降级。"""
    products = [
        {"productId": 1, "name": "游戏本", "subtitle": "高性能独显",
         "description": "适合打游戏", "skus": [], "categoryName": "电脑"},
        {"productId": 2, "name": "办公本", "subtitle": "轻薄长续航",
         "description": "适合办公", "skus": [], "categoryName": "电脑"},
    ]

    class _Catalog:
        def __init__(self):
            self.calls = 0

        def all_products(self):
            self.calls += 1
            return [dict(p) for p in products]

        def search(self, query):
            return []

        def categories(self):
            return []

    class _Embed:
        def __init__(self):
            self.calls = 0

        def embed(self, texts, text_type="document"):
            self.calls += 1
            if len(texts) == 2:
                return [[1.0, 0.0], [0.0, 1.0]]
            return [[1.0, 0.0]]

    problems = []
    catalog, embed = _Catalog(), _Embed()
    service = SemanticSearchService(catalog, embed)
    top = service.search("想打游戏", limit=1)
    if not top or top[0].get("productId") != 1:
        problems.append(f"语义排序不对：{top!r}")
    service.search("再来一次", limit=1)
    if catalog.calls != 1:
        problems.append(f"索引没有复用缓存，商品接口被调了 {catalog.calls} 次")

    class _Boom:
        def embed(self, texts, text_type="document"):
            raise RuntimeError("embedding down")

    service = SemanticSearchService(_Catalog(), _Boom())
    if service.search("游戏", limit=1):
        problems.append("embedding 失败时没有降级为空列表")
    return problems
