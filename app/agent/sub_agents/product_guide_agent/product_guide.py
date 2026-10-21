"""
AI 导购的工具层（M11 / M13）。

数据源是 Java 的商品目录接口；关键词搜不到时自动回退到语义检索。
工具只负责“找商品 / 推荐理由原料”，不负责下单——用户选定后由 order-query
子 agent 用 productId + skuId 准备订单草稿。
"""
from app.agent.sub_agents.order_query_agent.its_mygo_client import call
from app.agent.sub_agents.product_guide_agent.product_cards import add_product_cards
from app.agent.sub_agents.product_guide_agent.semantic_index import semantic_search


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
    kw = (keyword or "").strip()
    try:
        safe_limit = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        safe_limit = 5

    payload: dict = {"keyword": kw, "sort": sort or "recommend", "limit": safe_limit}
    if category_id is not None:
        try:
            payload["categoryId"] = int(category_id)
        except (TypeError, ValueError):
            pass
    for key, value in (("priceMin", price_min), ("priceMax", price_max)):
        if value is not None:
            try:
                payload[key] = float(value)
            except (TypeError, ValueError):
                pass

    data, error = call("/internal/agent/catalog/products", payload)
    if error:
        return error
    products = [p for p in (data or []) if isinstance(p, dict)]

    source = "关键词"
    if len(products) < 2 and kw:
        semantic = semantic_search(kw, safe_limit)
        if semantic:
            products = _merge(products, semantic)
            source = "关键词 + 语义"

    products = products[:safe_limit]
    if not products:
        return ("没有找到符合条件的在售商品。可以换个说法，或告诉我预算/用途/品类，"
                "我再用语义检索找找。")

    _push_cards(products)
    return _format(products, source)


def semantic_search_products(query: str, limit: int = 5) -> str:
    """用自然语言做语义检索（关键词搜不到时用），返回最相关的商品。"""
    q = (query or "").strip()
    if not q:
        return "请提供要搜索的自然语言描述，例如「能打游戏的笔记本」。"
    try:
        safe_limit = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        safe_limit = 5
    products = [p for p in semantic_search(q, safe_limit) if isinstance(p, dict)]
    if not products:
        return "语义检索没有找到相关商品；可以换个说法，或直接告诉我预算和用途。"
    _push_cards(products)
    return _format(products, "语义检索")


def list_categories() -> str:
    """列出商品分类树（含 id），供 search_products 的 category_id 使用。"""
    data, error = call("/internal/agent/catalog/categories", method="GET")
    if error:
        return error
    nodes = [n for n in (data or []) if isinstance(n, dict)]
    if not nodes:
        return "当前没有可用分类。"
    lines = ["商品分类："]
    lines.extend(_format_nodes(nodes, 0))
    return "\n".join(lines)


def _format(products: list[dict], source: str) -> str:
    lines = [f"找到 {len(products)} 个商品（{source}）："]
    for idx, product in enumerate(products, 1):
        lines.append(
            f"{idx}. {product.get('name') or '未知商品'}"
            f"（productId={product.get('productId')}，¥{product.get('price') or '?'}）")
        meta = []
        if product.get("merchantName"):
            meta.append(f"店铺 {product['merchantName']}")
        if product.get("categoryName"):
            meta.append(f"分类 {product['categoryName']}")
        if product.get("totalStock") is not None:
            meta.append(f"库存 {product['totalStock']}")
        if product.get("sales"):
            meta.append(f"销量 {product['sales']}")
        if product.get("rating"):
            meta.append(f"评分 {product['rating']}（{product.get('reviewCount') or 0} 条评价）")
        if meta:
            lines.append("   " + " · ".join(meta))
        for sku in (product.get("skus") or []):
            if not isinstance(sku, dict):
                continue
            lines.append(
                f"   - skuId={sku.get('skuId')}，{sku.get('specText') or '默认规格'}，"
                f"¥{sku.get('price') or '?'}，库存 {sku.get('stock') if sku.get('stock') is not None else '?'}")
        lines.append(f"   链接：/product/{product.get('productId')}")
    lines.append("推荐时优先选有库存的商品，并说明价格/库存/销量/评分；"
                 "下单需要 productId + skuId。")
    return "\n".join(lines)


def _push_cards(products: list[dict]) -> None:
    cards = []
    for product in products:
        specs = [str(sku.get("specText")) for sku in (product.get("skus") or [])
                 if isinstance(sku, dict) and sku.get("specText")]
        cards.append({
            "productId": str(product.get("productId") or ""),
            "name": product.get("name") or "",
            "image": product.get("mainImage") or "",
            "price": product.get("price") or "",
            "stock": str(product.get("totalStock")
                         if product.get("totalStock") is not None else 0),
            "sales": str(product.get("sales") or 0),
            "rating": product.get("rating") or "",
            "merchantName": product.get("merchantName") or "",
            "specText": " / ".join(specs),
            "categoryName": product.get("categoryName") or "",
        })
    add_product_cards(cards)


def _merge(primary: list[dict], secondary: list[dict]) -> list[dict]:
    out, seen = [], set()
    for product in list(primary) + list(secondary):
        product_id = str(product.get("productId") or "")
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        out.append(product)
    return out


def _format_nodes(nodes: list[dict], depth: int) -> list[str]:
    lines = []
    for node in nodes:
        lines.append("  " * depth + f"- {node.get('name') or '未命名'}（id={node.get('id')}）")
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if children:
            lines.extend(_format_nodes(children, depth + 1))
    return lines


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_product_guide_tools() -> list:
    """导购工具：格式化、语义回退、分类树、错误可读、商品卡片入桶。"""
    import app.agent.sub_agents.product_guide_agent.product_guide as module
    from app.agent.sub_agents.product_guide_agent.product_cards import (
        reset_product_cards,
        set_product_cards,
    )

    problems = []
    real_call, real_semantic = module.call, module.semantic_search
    cards: list = []
    token = set_product_cards(cards)

    product = {
        "productId": 1, "name": "游戏本", "subtitle": "高性能", "mainImage": "img",
        "price": "6999.00", "totalStock": 3, "sales": 12, "rating": "4.8",
        "reviewCount": 5, "merchantName": "数码店", "categoryName": "电脑",
        "skus": [{"skuId": 11, "specText": "颜色:黑", "price": "6999.00", "stock": 3}],
    }

    try:
        # 关键词命中：不走向量检索
        module.call = lambda path, payload=None, method="POST": ([dict(product)], "")
        module.semantic_search = lambda query, limit=5: []
        text = search_products("游戏本", limit=3)
        for expected in ("游戏本", "skuId=11", "6999.00", "库存 3", "销量 12",
                         "评分 4.8", "数码店", "/product/1"):
            if expected not in text:
                problems.append(f"关键词结果缺少 {expected!r}")
        if len(cards) != 1 or cards[0].get("productId") != "1":
            problems.append(f"商品卡片未入桶：{cards!r}")

        # 关键词为空：回退语义
        cards.clear()
        module.call = lambda path, payload=None, method="POST": ([], "")
        module.semantic_search = lambda query, limit=5: [dict(product)]
        text = search_products("能打游戏的本子", limit=3)
        if "语义" not in text or "游戏本" not in text:
            problems.append(f"语义回退没有生效：{text!r}")
        if not cards:
            problems.append("语义结果的商品卡片未入桶")

        # 分类树
        module.call = lambda path, payload=None, method="GET": ([
            {"id": 1, "name": "电脑", "children": [{"id": 2, "name": "笔记本", "children": []}]},
        ], "")
        text = list_categories()
        for expected in ("电脑（id=1）", "笔记本（id=2）"):
            if expected not in text:
                problems.append(f"分类树缺少 {expected!r}")

        # 错误原样返回
        module.call = lambda path, payload=None, method="POST": (None, "平台服务暂时不可用")
        text = search_products("游戏本")
        if "暂时不可用" not in text:
            problems.append(f"错误提示不清晰：{text!r}")
    finally:
        module.call, module.semantic_search = real_call, real_semantic
        reset_product_cards(token)

    return problems
