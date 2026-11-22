"""
BM25 检索节点 (node_search_bm25)

作用：对 kb_chunks 的正文/标题做应用层 BM25，补一路「精确词命中」召回。
它和向量、HyDE、图谱一样只产出候选，真正的取舍在 node_rrf 里按权重融合；
BM25 是词法信号，权重刻意低于向量，避免把目录/章节标题顶到前面。
"""
import sys

from app.agent.subagents.product_kb.domain.state import (
    QueryGraphState,
    resolve_retrieval_query,
)
from app.conf.pipeline_config import pipeline_config
from app.core.error_policy import degrade
from app.core.logger import logger
from app.lm.bm25_utils import search_bm25
from app.utils.task_utils import add_done_task, add_running_task

NODE_NAME = "node_search_bm25"


def node_search_bm25(state: QueryGraphState) -> QueryGraphState:
    """
    节点：BM25 词法召回

    :param state: 需包含 session_id / item_names / 检索词
    :return: {"bm25_chunks": [命中切片]}；关闭、失败或无结果均返回空列表
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{NODE_NAME}] [{function_name}] 开始处理")
    add_running_task(state["session_id"], function_name, state.get("is_stream"))

    try:
        if not pipeline_config.bm25_enabled:
            logger.info(f"[{NODE_NAME}] [{function_name}] BM25 已关闭，跳过")
            return {"bm25_chunks": []}

        query = resolve_retrieval_query(state)
        if not query:
            logger.error(f"[{NODE_NAME}] [{function_name}] 无有效查询，返回空结果")
            return {"bm25_chunks": []}

        item_names = state.get("item_names") or []
        hits = search_bm25(
            query,
            limit=pipeline_config.bm25_top_k,
            item_names=item_names,
        )
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 召回 {len(hits)} 条，"
            f"query={query!r}，item_names={item_names}"
        )
        if hits:
            top = hits[0]
            logger.info(
                f"[{NODE_NAME}] [{function_name}] Top1："
                f"BM25={top.get('distance', 0):.4f}，"
                f"标题={(top.get('entity') or {}).get('title', '')[:40]!r}"
            )
        return {"bm25_chunks": hits}

    except Exception as e:
        # 词法路失败只降级自己，不阻断其他召回
        return degrade(NODE_NAME, "BM25 检索", {"bm25_chunks": []}, e)
    finally:
        add_done_task(state["session_id"], function_name, state.get("is_stream"))
        logger.info(f"[{NODE_NAME}] [{function_name}] 处理结束")


if __name__ == "__main__":
    from app.agent.subagents.product_kb.domain.state import create_query_default_state
    from app.utils.task_utils import clear_task

    session_id = "bm25_test"
    st = create_query_default_state(
        session_id=session_id,
        original_query="HAK 180 烫金机怎么安装色带",
        rewritten_query="HAK 180 烫金机怎么安装色带",
        item_names=["Brother HAK 180 烫金机"],
        is_stream=False,
    )
    try:
        result = node_search_bm25(st)
        for hit in (result.get("bm25_chunks") or [])[:5]:
            entity = hit.get("entity") or {}
            logger.info(
                f"[测试] {hit.get('distance', 0):.4f} "
                f"{(entity.get('title') or '')[:40]}"
            )
    finally:
        clear_task(session_id)
