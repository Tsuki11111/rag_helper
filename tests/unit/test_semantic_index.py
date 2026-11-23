"""语义索引：缓存复用、失败负缓存、并发只构建一次。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.agent.subagents.product_guide.application.semantic_search_service import (
    SemanticSearchService,
)


class _Catalog:
    def __init__(self, products=None, delay=0.0, fail=False):
        self.products = products or []
        self.delay = delay
        self.fail = fail
        self.calls = 0

    def all_products(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("catalog down")
        return [dict(p) for p in self.products]

    def search(self, query):
        return []

    def categories(self):
        return []


class _Embed:
    def __init__(self, dims=2):
        self.dims = dims
        self.calls = 0

    def embed(self, texts, text_type="document"):
        self.calls += 1
        if len(texts) == 1 and text_type == "query":
            return [[1.0, 0.0][:self.dims]]
        return [[1.0, 0.0][:self.dims] for _ in texts]


def _products():
    return [{"productId": 1, "name": "游戏本", "skus": []},
            {"productId": 2, "name": "办公本", "skus": []}]


def test_cache_reused():
    catalog, embed = _Catalog(_products()), _Embed()
    service = SemanticSearchService(catalog, embed)

    service.search("游戏", limit=1)
    service.search("办公", limit=1)

    assert catalog.calls == 1


def test_failure_is_negative_cached():
    catalog = _Catalog(fail=True)
    service = SemanticSearchService(catalog, _Embed())

    assert service.search("游戏", limit=1) == []
    assert service.search("办公", limit=1) == []
    assert catalog.calls == 1


def test_concurrent_build_runs_once():
    catalog, embed = _Catalog(_products(), delay=0.05), _Embed()
    service = SemanticSearchService(catalog, embed)
    barrier = threading.Barrier(2)

    def _search():
        barrier.wait()
        return service.search("游戏", limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _search(), range(2)))

    assert catalog.calls == 1
    assert all(results)
