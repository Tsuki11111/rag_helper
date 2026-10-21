"""商品语义索引（M13）。

商品量小（演示目录几十条），用 DashScope embedding + 内存余弦排序即可，
不引入新的向量库、不把商品同步到 Milvus。量大了再换成 Milvus / 搜索引擎。

索引数据来自 Java 的 `/internal/agent/catalog/all`（只含在售、ACTIVE 店铺）；
缓存默认 5 分钟，`PRODUCT_SEMANTIC_TTL_SEC` 可调。
"""
import math
import os
import threading
import time

from app.agent.sub_agents.order_query_agent.its_mygo_client import call
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings

_TTL = float(os.getenv("PRODUCT_SEMANTIC_TTL_SEC") or 300)
_LOCK = threading.Lock()
_CACHE = {"products": [], "vectors": [], "loaded_at": 0.0}


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


def _refresh():
    data, error = call("/internal/agent/catalog/all", method="GET")
    if error:
        logger.warning(f"[semantic_index] 拉取商品失败：{error}")
        return [], []
    products = [p for p in (data or []) if isinstance(p, dict)]
    if not products:
        return [], []
    texts = [_document(p) for p in products]
    result = generate_embeddings(texts)
    vectors = result.get("dense") or []
    return products, vectors


def _ensure_loaded():
    now = time.time()
    with _LOCK:
        if _CACHE["vectors"] and now - _CACHE["loaded_at"] < _TTL:
            return _CACHE["products"], _CACHE["vectors"]
        try:
            products, vectors = _refresh()
        except Exception as e:
            logger.warning(f"[semantic_index] 构建索引失败：{type(e).__name__}: {e}")
            products, vectors = [], []
        if vectors:
            _CACHE.update(products=products, vectors=vectors, loaded_at=now)
        return products, vectors


def semantic_search(query: str, limit: int = 5) -> list[dict]:
    """按自然语言查询做语义召回；失败时返回空列表，由调用方回退关键词搜索。"""
    q = (query or "").strip()
    if not q:
        return []
    products, vectors = _ensure_loaded()
    if not products or not vectors:
        return []
    try:
        query_result = generate_embeddings([q])
        query_vectors = query_result.get("dense") or []
        if not query_vectors:
            return []
        query_vector = query_vectors[0]
    except Exception as e:
        logger.warning(f"[semantic_index] 查询向量生成失败：{type(e).__name__}: {e}")
        return []

    scored = [(_cosine(query_vector, vector), product)
              for product, vector in zip(products, vectors)]
    scored.sort(key=lambda item: item[0], reverse=True)
    safe_limit = max(1, min(int(limit or 5), 10))
    if not scored:
        return []
    # 相对阈值：只保留与最高分接近的候选，避免把目录里所有商品都塞给模型
    threshold = max(0.15, scored[0][0] * 0.8)
    return [product for score, product in scored[:safe_limit] if score >= threshold]


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _reset_cache() -> None:
    with _LOCK:
        _CACHE.update(products=[], vectors=[], loaded_at=0.0)


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_product_semantic_index() -> list:
    """语义索引：向量排序、缓存复用、embedding 失败降级。"""
    import app.agent.sub_agents.product_guide_agent.semantic_index as module

    problems = []
    real_call, real_embed = module.call, module.generate_embeddings
    calls = {"n": 0, "method": ""}

    products = [
        {"productId": 1, "name": "游戏本", "subtitle": "高性能独显",
         "description": "适合打游戏", "skus": [], "categoryName": "电脑"},
        {"productId": 2, "name": "办公本", "subtitle": "轻薄长续航",
         "description": "适合办公", "skus": [], "categoryName": "电脑"},
    ]

    def _fake_call(path, payload=None, method="POST"):
        calls["n"] += 1
        calls["method"] = method
        return products, ""

    def _fake_embed(texts):
        # 商品两条 + 查询一条；用固定二维向量便于断言
        if len(texts) == 2:
            return {"dense": [[1.0, 0.0], [0.0, 1.0]]}
        return {"dense": [[1.0, 0.0]]}

    module._reset_cache()
    module.call, module.generate_embeddings = _fake_call, _fake_embed
    try:
        top = module.semantic_search("想打游戏", limit=1)
        if not top or top[0].get("productId") != 1:
            problems.append(f"语义排序不对：{top!r}")
        if calls["method"] != "GET":
            problems.append(f"全量商品应走 GET，实得 {calls['method']!r}")
        module.semantic_search("再来一次", limit=1)
        if calls["n"] != 1:
            problems.append(f"索引没有复用缓存，商品接口被调了 {calls['n']} 次")
    finally:
        module.call, module.generate_embeddings = real_call, real_embed
        module._reset_cache()

    # embedding 失败：返回空，让调用方回退关键词
    def _boom(_texts):
        raise RuntimeError("embedding down")

    module.call, module.generate_embeddings = _fake_call, _boom
    try:
        if module.semantic_search("游戏", limit=1):
            problems.append("embedding 失败时没有降级为空列表")
    finally:
        module.call, module.generate_embeddings = real_call, real_embed
        module._reset_cache()

    return problems
