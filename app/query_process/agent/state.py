from typing_extensions import TypedDict
from typing import List
import copy

class QueryGraphState(TypedDict):
    """
    QueryGraphState 定义了整个查询流程中流转的数据结构。
    """
    session_id: str  # 会话唯一标识
    original_query: str  # 用户原始问题

    # 检索过程中的中间数据
    embedding_chunks: list  # 普通向量检索回来的切片
    hyde_embedding_chunks: list  # HyDE 检索回来的切片
    hyde_doc: str  # HyDE 由大模型生成的假设性文档（检索用的中间产物，便于调试）
    kg_chunks: list  # 知识图谱检索回来的切片
    web_search_docs: list  # 网络搜索回来的文档

    # 排序过程中的数据
    rrf_chunks: list  # RRF 融合排序后的切片
    reranked_docs: list  # 重排序后的最终 Top-K 文档

    # 生成过程中的数据
    prompt: str  # 组装好的 Prompt
    answer: str  # 最终生成的答案
    images: list  # 答案配图，每项 {url, caption}（从切片正文解析，供前端展示）
    # 本轮是否被用户主动暂停（生成作废、本轮就此结束，**不反问用户**）。
    # 注意：LangGraph 的 TypedDict 不做校验，新字段必须先在这里声明，
    # 否则节点写进去会被静默丢弃（踩过，见 HANDOFF §4.4）
    cancelled: bool
    # 产品名认不出来、需要**用户确认**：置 True 后由 node_ask_user 中断去问用户。
    # 与 cancelled 的区别：暂停是用户发起（停下就结束），这里是图发起（必须问到才继续）
    need_confirm: bool
    # 问用户用的卡片内容：{"question": str, "options": [{item_name, file_title, score, near}],
    # "allow_custom": bool}。会随检查点一起序列化（走 msgpack），所以里面不能放 numpy 标量
    clarify: dict

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    rewritten_query: str  # 改写后的问题
    is_stream: bool  # 是否流式输出标记


# ========================
# 默认状态（全部为空）
# ========================
query_graph_default_state: QueryGraphState = {
    "session_id": "",
    "original_query": "",
    "embedding_chunks": [],
    "hyde_embedding_chunks": [],
    "hyde_doc": "",
    "kg_chunks": [],
    "web_search_docs": [],
    "rrf_chunks": [],
    "reranked_docs": [],
    "prompt": "",
    "answer": "",
    "images": [],
    "cancelled": False,
    "need_confirm": False,
    "clarify": {},
    "item_names": [],
    "rewritten_query": "",
    "is_stream": False
}


# ========================
# 创建默认状态（可覆盖）
# ========================
def create_query_default_state(**overrides) -> QueryGraphState:
    """
    创建查询流程的默认状态，支持覆盖字段
    """
    state = copy.deepcopy(query_graph_default_state)
    state.update(overrides)
    return state


# ========================
# 获取干净状态
# ========================
def get_query_default_state() -> QueryGraphState:
    return copy.deepcopy(query_graph_default_state)


# ========================
# ✅ 状态复制函数（你要的）
# ========================
def copy_query_state(state: QueryGraphState, **overrides) -> QueryGraphState:
    """
    复制现有状态并可覆盖字段，深拷贝，不污染原数据
    """
    new_state = copy.deepcopy(state)
    new_state.update(overrides)
    return new_state


if __name__ == "__main__":
    # 测试
    state = create_query_default_state(
        session_id="test_001",
        original_query="华为P60怎么样?",
        is_stream=False
    )
    print("初始化状态：", state)

    # 复制状态
    new_state = copy_query_state(
        state,
        original_query="修改后的问题"
    )
    print("复制后的状态：", new_state)