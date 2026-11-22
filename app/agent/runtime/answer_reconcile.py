"""终稿对账：子调用回传的配图/商品按最终回复裁剪、合并。"""
import re

from app.agent.subagents.product_kb.application.nodes.node_answer_output import CITE_NUM_RE
from app.core.logger import logger

NODE_NAME = "main_agent"


def _merge_sub_images(bucket: list) -> list:
    """
    合并各子调用回传的配图：按 URL 去重、保序、统一成 {url, caption}

    主 agent 可能分多次派发 product-kb（复合场景），同一个 URL 会出现多遍；
    图注为空时给空串，前端按「未标注来源」渲染。
    """
    out, seen = [], set()
    for it in bucket or []:
        if not isinstance(it, dict):
            continue
        url = it.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "caption": it.get("caption") or ""})
    return out


def _merge_products(bucket: list) -> list:
    """合并导购子调用回传的商品卡片：按 productId 去重、保序。

    这里**不截断**：导购一轮可能搜很多次，最终回复引用的商品可能排在第 8 张
    之后；先截断再按回复对账会把它们误删（2026-11-09 实测推荐 5 款只出 1 张卡）。
    数量上限由 `_reconcile_reply_products` 按引用保留并裁到 8 张。
    """
    out, seen = [], set()
    for item in bucket or []:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or "")
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        out.append(item)
    return out


def _reconcile_reply_products(reply: str, products: list) -> list:
    """
    只保留最终回复里引用到的商品卡（与配图对账同一思路）：

    - 优先看 `/product/{id}` 链接；
    - 其次看精确商品名；
    - 一张都没引用时保留前 3 张，避免「推荐了却没有卡片」。

    导购可能中途搜了很多次（关键词 / 语义 / 分类浏览），不能让中间候选
    全部挂在最终回复下面。
    """
    if not products:
        return []
    text = reply or ""
    # 按完整数字取链接：直接子串匹配会让 /product/1 命中 /product/11
    ref_ids = {m.group(1) for m in re.finditer(r"/product/(\d+)", text)}
    kept = []
    for product in products:
        if not isinstance(product, dict):
            continue
        product_id = str(product.get("productId") or "")
        name = (product.get("name") or "").strip()
        if (product_id and product_id in ref_ids) or (name and name in text):
            kept.append(product)
    if kept:
        return kept[:8]
    return products[:3]


def _reconcile_reply_images(reply: str, images: list) -> tuple:
    """
    转述对账：只保留**最终回复里引用到的**图，按引用顺序重编号（1 起）

    检索侧的「引用即附」保证 RAG 原文里引用与图卡一一对应，但主 agent 转述时会
    改写句子 —— 实测会把一部分「如图 N 所示」漏掉（引用 1..16 缺 9/12/16，
    却挂了 16 张）。这里按最终回复的引用做最后一道对账：留下被引用的图并重编号、
    正文引用同步改号；一张都没引用则全部丢弃（与「引用即附」同一口径）。
    **只在单次派发时做**：多次派发两轮的编号空间不同、按编号对不上，
    不能乱改（调用方判断，见 run_main_agent）。
    """
    refs: list = []
    for m in CITE_NUM_RE.finditer(reply or ""):
        n = int(m.group(1) or m.group(2))
        if 1 <= n <= len(images) and n not in refs:
            refs.append(n)
    if not refs:
        if images:
            logger.warning(
                f"[{NODE_NAME}] 最终回复没有引用任何配图，丢弃 {len(images)} 张"
                f"（转述把引用改没了？）")
        return reply, []

    keep = [images[n - 1] for n in refs]
    renum = {n: i + 1 for i, n in enumerate(refs)}

    def _repl(m):
        if m.group(1) is not None:          # 形态 A：「图 3」
            n = int(m.group(1))
            return f"图 {renum[n]}" if n in renum else m.group(0)
        n = int(m.group(2))                 # 形态 B：「第 3 张图」
        return f"第 {renum[n]} 张图" if n in renum else m.group(0)

    return CITE_NUM_RE.sub(_repl, reply), keep
