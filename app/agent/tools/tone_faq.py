"""客服话术库检索工具（主 agent 直接调，轻量）。

设计取舍（见 docs/architecture.md D15）：
- **单集合**：`kb_tone_faq`，只存话术条目（场景/用户说法/建议回复），不混知识库切片；
- **单路检索**：一次 embedding + 一次 Milvus 稠密检索（COSINE），不做 HyDE / 多路 /
  RRF / 重排 / 改写，命中即返回 2~3 条，够主 agent 学语气即可；
- **可降级**：Milvus / embedding 不可用或没命中时返回一句提示，绝不阻断主流程。
"""
import os

from app.clients.milvus_clients import dense_search, get_milvus_client
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings

# 集合名可用 TONE_FAQ_COLLECTION 覆盖；默认独立集合，和 kb_chunks 完全隔离
COLLECTION_NAME = os.getenv("TONE_FAQ_COLLECTION", "kb_tone_faq")
DEFAULT_TOP_K = 3
MAX_TOP_K = 5
# COSINE 相似度阈值：低于它的视为不相关，宁少勿错
MIN_SCORE = 0.30
# 检索时只取必要字段，FAQ 库很小，不做投影裁剪的复杂度
OUTPUT_FIELDS = ["faq_id", "scenario", "question", "answer", "tags"]

_NOT_FOUND = "（话术库没有特别匹配的场景，按【话术规范】正常回答即可。）"
_DEGRADED = "（话术库暂时不可用，按【话术规范】正常回答即可。）"


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
    """把命中话术排成主 agent 易读的参考块（只学语气与结构，不照抄）。"""
    lines = ["参考话术（只学语气与结构，不要照抄，也不要提“话术库”）："]
    for index, hit in enumerate(hits[:limit], start=1):
        entity = hit.get("entity") or {}
        scenario = entity.get("scenario") or "通用场景"
        question = entity.get("question") or ""
        answer = entity.get("answer") or ""
        lines.append(f"{index}) 场景：{scenario}")
        if question:
            lines.append(f"   用户可能会说：{question}")
        if answer:
            lines.append(f"   建议这样回：{answer}")
    return "\n".join(lines)


def search_tone_faq(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """检索客服话术库，取 2~3 条相似场景的回复范例供组织措辞。

    用户着急/生气/委屈，或涉及退款、退货、物流延迟、催发货、催退款、投诉、议价、
    安抚道歉等场景时调用；纯参数、说明书、商品列表等事实问题不必调用。

    :param query: 用户原话或要处理的话术场景（保留口语，别自己概括成书面语）
    :param top_k: 返回条数，默认 3，最大 5
    :return: 参考话术文本；没命中或不可用时返回一句降级提示
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return "（没收到要检索的话术场景）"
    try:
        limit = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
    except (TypeError, ValueError):
        limit = DEFAULT_TOP_K

    try:
        embeddings = generate_embeddings([clean_query]) or {}
        vectors = embeddings.get("dense") or []
        if not vectors:
            logger.warning("[tone_faq] 查询向量化失败，降级")
            return _DEGRADED
        client = get_milvus_client()
        if client is None:
            logger.warning("[tone_faq] Milvus 不可用，降级")
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
            logger.info("[tone_faq] 无高相似话术，返回降级提示")
            return _NOT_FOUND
        logger.info("[tone_faq] 命中 %d 条话术", len(hits))
        return _format_hits(hits, limit)
    except Exception as exc:  # 话术库故障不能影响主流程
        logger.warning("[tone_faq] 检索失败，降级：%s", exc)
        return _DEGRADED


def _check_tone_faq_format() -> list:
    """回归：话术检索结果排版 / 低分过滤 / 降级提示（纯函数，不依赖 Milvus）。"""
    problems: list = []
    hits = [
        {"distance": 0.82, "entity": {"scenario": "退款安抚", "question": "怎么还没退？",
                                      "answer": "理解您着急，我先帮您查进度。"}},
        {"distance": 0.61, "entity": {"scenario": "物流延迟", "question": "怎么还没到？",
                                      "answer": "让您久等了，我先帮您看物流。"}},
        {"distance": 0.05, "entity": {"scenario": "无关", "question": "x", "answer": "y"}},
    ]
    kept = _filter_hits(hits)
    if len(kept) != 2:
        problems.append(f"低相似度应被过滤，实得 {len(kept)} 条")
    text = _format_hits(kept, 2)
    for marker in ("参考话术", "场景：退款安抚", "用户可能会说", "建议这样回"):
        if marker not in text:
            problems.append(f"话术排版缺少：{marker}")
    if "无关" in text:
        problems.append("低分命中泄漏进了结果")
    if not _format_hits([], 3):
        problems.append("空命中时应返回空串")
    if not search_tone_faq("   ").startswith("（没收到"):
        problems.append("空 query 应返回可读提示")
    return problems


def _check_tone_faq_retrieval() -> list:
    """回归（依赖 Milvus）：话术库已导入且能检索到相关场景。"""
    problems: list = []
    text = search_tone_faq("客户着急问退款怎么还没到账", 3)
    if "建议这样回" not in text:
        problems.append(f"话术库没检索到相关话术：{text[:120]}")
    if "退款" not in text:
        problems.append("命中的话术与退款场景无关")
    return problems
