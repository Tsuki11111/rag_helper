"""平台政策检索工具（主 agent 直接调）。

设计取舍（见 docs/architecture.md D16）：
- MySQL 保存政策原文与章节，是唯一事实源；Milvus 只存章节向量，负责语义召回；
- 独立集合 `kb_platform_policy`，不与产品知识 `kb_chunks`、客服话术 `kb_tone_faq` 混用；
- 单路稠密检索，命中后返回「政策标题 / 章节 / 版本 / 原文」，让模型可引用、可核对；
- 没命中或 Milvus / embedding 不可用时返回降级提示，绝不编造条款。
"""
import os

from app.clients.milvus_clients import dense_search, get_milvus_client
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings

COLLECTION_NAME = os.getenv("PLATFORM_POLICY_COLLECTION", "kb_platform_policy")
DEFAULT_TOP_K = 3
MAX_TOP_K = 5
# 政策条款比话术更要求准确，阈值略高
MIN_SCORE = 0.35
OUTPUT_FIELDS = [
    "section_id", "policy_key", "policy_title", "section_no",
    "heading", "content", "version",
]

_NOT_FOUND = "（政策库没有检索到相关条款，不要猜测；可建议用户查看对应政策页面或转人工。）"
_DEGRADED = "（政策库暂时不可用；如需引用政策，请让用户查看政策页面，不要编造条款。）"


def _filter_hits(hits, min_score: float = MIN_SCORE):
    """过滤低相似度命中；Milvus COSINE 的 distance 越大越相似。"""
    kept = []
    for hit in hits or []:
        try:
            score = float(hit.get("distance"))
        except (TypeError, ValueError):
            continue
        if score >= min_score:
            kept.append(hit)
    return kept


def _format_hits(hits, limit: int) -> str:
    """把命中章节排成可引用的政策参考块。"""
    lines = ["平台政策参考（回答时注明政策标题与章节，不要编造未命中的条款）："]
    for index, hit in enumerate(hits[:limit], start=1):
        entity = hit.get("entity") or {}
        title = entity.get("policy_title") or "平台政策"
        heading = entity.get("heading") or ""
        version = entity.get("version") or ""
        content = (entity.get("content") or "").strip()
        lines.append(f"{index}) 《{title}》{f' · {heading}' if heading else ''}"
                     f"{f'（v{version}）' if version else ''}")
        if content:
            lines.append(f"   原文：{content}")
    return "\n".join(lines)


def search_platform_policy(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """检索平台政策条款，返回可引用的章节原文。

    用户问退款/退货/售后时效、隐私与账号、商家规则、发票、责任边界等政策问题时调用。
    回答必须基于命中原文；没命中就说没查到，不要自行解释或扩大承诺。

    :param query: 用户原话或要查的政策问题（保留口语）
    :param top_k: 返回条数，默认 3，最大 5
    :return: 政策参考文本；没命中或不可用时返回降级提示
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return "（没收到要检索的政策问题）"
    try:
        limit = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
    except (TypeError, ValueError):
        limit = DEFAULT_TOP_K

    try:
        embeddings = generate_embeddings([clean_query]) or {}
        vectors = embeddings.get("dense") or []
        if not vectors:
            logger.warning("[policy_kb] 查询向量化失败，降级")
            return _DEGRADED
        client = get_milvus_client()
        if client is None:
            logger.warning("[policy_kb] Milvus 不可用，降级")
            return _DEGRADED
        result = dense_search(
            client=client,
            collection_name=COLLECTION_NAME,
            dense_vector=vectors[0],
            limit=limit * 2,
            output_fields=OUTPUT_FIELDS,
        )
        hits = (result[0] if result else []) or []
        hits = _filter_hits(hits)[:limit]
        if not hits:
            logger.info("[policy_kb] 无高相似政策条款，返回降级提示")
            return _NOT_FOUND
        logger.info(f"[policy_kb] 命中 {len(hits)} 条政策章节")
        return _format_hits(hits, limit)
    except Exception as exc:  # 政策库故障不能影响主流程
        logger.warning(f"[policy_kb] 检索失败，降级：{exc}")
        return _DEGRADED


def _check_policy_format() -> list:
    """回归：政策检索结果排版 / 低分过滤 / 降级提示（纯函数，不依赖 Milvus）。"""
    problems: list = []
    hits = [
        {"distance": 0.82, "entity": {"policy_title": "退换货与售后政策",
                                      "heading": "三、申请退款", "version": "1.0",
                                      "content": "待发货订单可申请退款。"}},
        {"distance": 0.05, "entity": {"policy_title": "隐私政策",
                                      "heading": "无关", "version": "1.0", "content": "x"}},
    ]
    kept = _filter_hits(hits)
    if len(kept) != 1:
        problems.append(f"低相似度应被过滤，实得 {len(kept)} 条")
    text = _format_hits(kept, 3)
    for marker in ("平台政策参考", "《退换货与售后政策》", "三、申请退款", "原文："):
        if marker not in text:
            problems.append(f"政策排版缺少：{marker}")
    if "隐私政策" in text:
        problems.append("低分命中泄漏进了结果")
    if not search_platform_policy("   ").startswith("（没收到"):
        problems.append("空 query 应返回可读提示")
    return problems


def _check_policy_retrieval() -> list:
    """回归（依赖 Milvus）：政策库已导入且能检索到退款条款。"""
    problems: list = []
    try:
        client = get_milvus_client()
        if client is None or not client.has_collection(COLLECTION_NAME):
            return problems
        rows = client.query(collection_name=COLLECTION_NAME,
                            filter='section_id != ""', output_fields=["count(*)"])
        if not rows or not rows[0].get("count(*)"):
            return problems
    except Exception:
        return problems
    text = search_platform_policy("退款多久能到账", 3)
    if "政策参考" not in text or "退款" not in text:
        problems.append(f"政策库没检索到退款条款：{text[:120]}")
    return problems
