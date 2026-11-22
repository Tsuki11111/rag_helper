"""工具结果格式化：只负责把商品/分类变成给模型看的文本。"""
from app.agent.subagents.product_guide.domain.models import ProductCard


class ProductTextFormatter:
    def format(self, products: list[dict], source: str) -> str:
        lines = [f"找到 {len(products)} 个商品（{source}）："]
        for idx, product in enumerate(products, 1):
            lines.append(
                f"{idx}. {product.get('name') or '未知商品'}"
                f"（productId={product.get('productId')}，¥{product.get('price') or '?'}）")
            subtitle = str(product.get("subtitle") or "").strip()
            description = " ".join(str(product.get("description") or "").split())
            if subtitle:
                lines.append(f"   卖点：{subtitle}")
            if description:
                lines.append(f"   简介：{description}")
            meta = self._meta(product)
            if meta:
                lines.append("   " + " · ".join(meta))
            for sku in (product.get("skus") or []):
                if not isinstance(sku, dict):
                    continue
                lines.append(
                    f"   - skuId={sku.get('skuId')}，{sku.get('specText') or '默认规格'}，"
                    f"¥{sku.get('price') or '?'}，库存 {sku.get('stock') if sku.get('stock') is not None else '?'}")
            lines.append(f"   链接：/product/{product.get('productId')}")
        lines.append("推荐时每个商品用 2~3 句话介绍：先讲卖点/适合谁（来自上面的卖点与简介），"
                     "再讲价格/库存/销量/评分；不要只列一行参数，也不要原样大段照抄简介。"
                     "优先选有库存的商品；下单需要 productId + skuId。")
        return "\n".join(lines)

    def format_categories(self, nodes: list[dict]) -> str:
        lines = ["商品分类："]
        lines.extend(self._format_nodes(nodes, 0))
        return "\n".join(lines)

    @staticmethod
    def _meta(product: dict) -> list[str]:
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
        return meta

    def _format_nodes(self, nodes: list[dict], depth: int) -> list[str]:
        lines = []
        for node in nodes:
            lines.append("  " * depth + f"- {node.get('name') or '未命名'}（id={node.get('id')}）")
            children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
            if children:
                lines.extend(self._format_nodes(children, depth + 1))
        return lines

    @staticmethod
    def cards(products: list[dict]) -> list[dict]:
        cards = []
        for product in products:
            specs = [str(sku.get("specText")) for sku in (product.get("skus") or [])
                     if isinstance(sku, dict) and sku.get("specText")]
            cards.append(ProductCard(
                productId=str(product.get("productId") or ""),
                name=product.get("name") or "",
                image=product.get("mainImage") or "",
                price=product.get("price") or "",
                stock=str(product.get("totalStock")
                          if product.get("totalStock") is not None else 0),
                sales=str(product.get("sales") or 0),
                rating=product.get("rating") or "",
                merchantName=product.get("merchantName") or "",
                specText=" / ".join(specs),
                categoryName=product.get("categoryName") or "",
            ).to_dict())
        return cards
