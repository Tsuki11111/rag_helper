from typing_extensions import TypedDict
from typing import List
import copy

class QueryGraphState(TypedDict):
    """
    QueryGraphState 定义了整个查询流程中流转的数据结构。
    """
    session_id: str  # 会话唯一标识
    original_query: str  # 用户原始问题
    # 消息形式的输入/输出键位 —— deepagents 的 CompiledSubAgent 以消息列表派发任务，
    # 要求子图 state 里有这个键。本图只把消息当「入口/出口的载体」，内部流转仍走下面的自定义字段
    messages: list

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
    # 产品名认不出来、需要**用户确认**：置 True 后**本轮结束**、候选交调用方
    # （无状态化后不再图内中断，2026-10-12 —— 原先是 node_ask_user 中断去问用户）。
    # 与 cancelled 的区别：暂停是用户发起（停下就结束），这里是等用户确认才继续
    need_confirm: bool
    # 问用户用的卡片内容：{"question": str, "options": [{item_name, file_title, score, near}],
    # "allow_custom": bool}。会随检查点一起序列化（走 msgpack），所以里面不能放 numpy 标量
    clarify: dict

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    rewritten_query: str  # 改写后的问题
    is_stream: bool  # 是否流式输出标记
    # 联网搜索是否参与本轮（前端的开关）。关掉时那一路上来就返回空，
    # **不是**把节点摘掉 —— 四路并发汇到 node_join 的 fan-in 不能少一条
    enable_web_search: bool
    # 本轮**只有**联网结果、向量库一条都没搜到（node_rerank 判定）。
    # 前端据此在答案上方挂「内容来自网络，仅供参考」的横幅
    web_only: bool

    # ── 检索循环（有界自纠正，2026-10-20）──
    # 探针 query 与意图锚分离：rewritten_query 是 confirm 产出的意图锚（不可变），
    # retrieval_query 只供四路召回使用（循环改写只动它）；**答案生成始终用 rewritten_query**
    # —— 这是防目标漂移的核心，别把两者合并
    retrieval_query: str
    retrieval_round: int          # 已完成的检索轮次（1 起；首轮检索期间为 0）
    retrieval_judge: dict         # 最近一次充分性判定：verdict / missing / rewrite_hint / next / rewrite_ok
    retrieval_history: list       # 每轮 {round, query, verdict, missing, top1_score, n_local, n_web}
    # 第一轮证据（回落用）与「判定充分」那一轮的证据（选中用）。
    # 循环耗尽时**只用第一轮证据**作答 —— 一次糟糕的改写最多浪费成本，不污染答案
    anchor_docs: list
    anchor_web_only: bool
    best_docs: list
    best_web_only: bool


# ========================
# 默认状态（全部为空）
# ========================
query_graph_default_state: QueryGraphState = {
    "session_id": "",
    "original_query": "",
    "messages": [],
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
    "is_stream": False,
    # 默认开：与改动前的行为一致，别让老调用方（自测、脚本）静默换了行为
    "enable_web_search": True,
    "web_only": False,
    # 检索循环：默认关闭（RETRIEVAL_MAX_ROUNDS=1 时 gate 只是直通，不调模型）
    "retrieval_query": "",
    "retrieval_round": 0,
    "retrieval_judge": {},
    "retrieval_history": [],
    "anchor_docs": [],
    "anchor_web_only": False,
    "best_docs": [],
    "best_web_only": False,
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


def _message_text(m) -> tuple:
    """从一条消息（dict 或 LangChain 消息对象）里取 (role, text)"""
    if isinstance(m, dict):
        role, content = m.get("role") or "", m.get("content") or ""
    else:
        role, content = getattr(m, "type", "") or "", getattr(m, "content", "") or ""
    if isinstance(content, list):  # 分块内容：[{"type": "text", "text": ...}, ...]
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(role), str(content)


def from_messages(messages: list, session_id: str = "", **overrides) -> QueryGraphState:
    """
    从消息列表构造图输入 —— 给 deepagents 的 CompiledSubAgent 用（M1 入口适配）。

    主 agent 通过 `task` 工具以消息形式派发任务；本图不消费完整消息历史，
    只取**最后一条用户消息**的文本作为 `original_query` ——
    多轮上下文由主 agent 在任务描述里带全（RAG 侧保持无状态，见 evolution-plan §3）。
    M1 先支持 dict 与 LangChain 消息对象两种读取，最终形状待 M2 实测后收紧。
    """
    text = ""
    for m in reversed(messages or []):
        role, content = _message_text(m)
        if role in ("user", "human"):
            text = content
            break
    return create_query_default_state(session_id=session_id, original_query=text, **overrides)


def resolve_retrieval_query(state: QueryGraphState) -> str:
    """
    四路召回用的检索词：**探针优先**，回退意图锚与原始问题。

    与作答用的 `rewritten_query` 刻意分开（2026-10-20）：循环改写只动 `retrieval_query`，
    答案生成和重排仍用 `rewritten_query` —— 检索可以试探，意图不能被改写带跑。
    """
    return (state.get("retrieval_query") or state.get("rewritten_query")
            or state.get("original_query") or "")


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