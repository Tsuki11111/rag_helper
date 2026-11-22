import time

from langgraph.constants import END
from langgraph.graph import StateGraph

from app.clients.mongo_checkpoint_clients import get_checkpointer, graph_config
from app.core import stage_timing
from app.core.logger import logger
from app.core.usage_tracker import add_tracked_node, usage_context
from app.agent.subagents.product_kb.application.nodes.node_answer_output import node_answer_output
from app.agent.subagents.product_kb.application.nodes.node_item_name_confirm import node_item_name_confirm
from app.agent.subagents.product_kb.application.nodes.node_query_kg import node_query_kg
from app.agent.subagents.product_kb.application.nodes.node_query_rewrite import node_query_rewrite
from app.agent.subagents.product_kb.application.nodes.node_rerank import node_rerank
from app.agent.subagents.product_kb.application.nodes.node_retrieval_gate import node_retrieval_gate
from app.agent.subagents.product_kb.application.nodes.node_rrf import node_rrf
from app.agent.subagents.product_kb.application.nodes.node_search_bm25 import node_search_bm25
from app.agent.subagents.product_kb.application.nodes.node_search_embedding import node_search_embedding
from app.agent.subagents.product_kb.application.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.agent.subagents.product_kb.application.nodes.node_web_search_mcp import node_web_search_mcp
from app.agent.subagents.product_kb.domain.state import QueryGraphState

# 初始化状态图
def _timed_node(name: str, fn):
    """包一层阶段计时：只观测，不影响节点返回值/异常。"""
    def wrapper(state, *args, **kwargs):
        started = time.perf_counter()
        try:
            return fn(state, *args, **kwargs)
        finally:
            try:
                stage_timing.record(state.get("session_id") or "", name, started)
            except Exception:
                # 观测失败不能影响检索
                pass
    return wrapper


builder = StateGraph(QueryGraphState)

# 注册所有节点
# 用 add_tracked_node 而非 add_node：多包一层归因，让节点内所有模型调用都记到该节点名下
# （账本里的 node 字段），节点内部代码一行都不用改
add_tracked_node(builder, "node_item_name_confirm", _timed_node("node_item_name_confirm", node_item_name_confirm))   # 确认产品名
builder.add_node("node_multi_search", lambda x: x)                   # 虚拟节点：多路搜索分叉点
add_tracked_node(builder, "node_search_embedding", _timed_node("node_search_embedding", node_search_embedding))     # 向量检索
add_tracked_node(builder, "node_search_embedding_hyde", _timed_node("node_search_embedding_hyde", node_search_embedding_hyde))  # HyDE 检索
add_tracked_node(builder, "node_search_bm25", _timed_node("node_search_bm25", node_search_bm25))               # BM25 词法检索
add_tracked_node(builder, "node_query_kg", _timed_node("node_query_kg", node_query_kg))                     # 图谱检索
add_tracked_node(builder, "node_web_search_mcp", _timed_node("node_web_search_mcp", node_web_search_mcp))         # 联网检索
builder.add_node("node_join", lambda x: {})                          # 虚拟节点：多路搜索合并点
add_tracked_node(builder, "node_rrf", _timed_node("node_rrf", node_rrf))                               # RRF 融合排序
add_tracked_node(builder, "node_rerank", _timed_node("node_rerank", node_rerank))                         # 重排序
add_tracked_node(builder, "node_retrieval_gate", _timed_node("node_retrieval_gate", node_retrieval_gate))         # 判断检索充分性
add_tracked_node(builder, "node_query_rewrite", _timed_node("node_query_rewrite", node_query_rewrite))           # 改写检索词（循环）
add_tracked_node(builder, "node_answer_output", _timed_node("node_answer_output", node_answer_output))           # 生成答案

# 虚拟节点的作用：作为流程的「分叉 / 合并中转站」，解决多分支流程的组织问题，本身无业务逻辑

# 设置入口节点
builder.set_entry_point("node_item_name_confirm")


def route_after_item_name_confirm(state: QueryGraphState) -> str:
    """
    产品名确认后的路由（三条）

    1. **认不出产品**（`need_confirm`）→ **本轮结束**（`__end__`）：带着候选
       （`need_confirm` / `clarify`）直接返回。**图不再中断**（无状态化，2026-10-11，
       见 evolution-plan §3）——由调用方（自用界面 / 主 agent）去问用户；
       主 agent 模式下用户回答成为**新一轮任务**，不再有 resume 语义
    2. **已有 `answer`** → 直接出答案，跳过检索。两个来源：外部预置（自测 / 调试用），
       以及确认节点**被模型服务内容审核拒绝**时自己写的那句人话（见 README「异常分级处置」）
    3. 其余（分支 A 已确认产品）→ 正常四路检索

    产出 `need_confirm` 的路径**刻意不写 `answer`**（结构化候选在 `clarify` 里），
    所以 1 与 2 不会同时成立。
    """
    if state.get("need_confirm"):
        logger.info("[route_after_item_name_confirm] 认不出产品，本轮结束并返回候选")
        return "__end__"
    if state.get("answer"):
        logger.info("[route_after_item_name_confirm] 已有answer，跳过检索直接输出")
        return "node_answer_output"
    return "node_multi_search"


def route_after_retrieval_gate(state: QueryGraphState) -> str:
    """
    检索充分性判定后的两条出边

    - `retry` → 改写检索词，再来一轮（gate 已确认轮次与预算都允许）
    - 其余 → 直接作答（证据已由 gate 选好：充分轮优先，否则回落到第一轮）
    """
    if (state.get("retrieval_judge") or {}).get("next") == "retry":
        return "node_query_rewrite"
    return "node_answer_output"


def route_after_query_rewrite(state: QueryGraphState) -> str:
    """改写成功才回检索；被守卫拒绝则用第一轮证据作答（不空转）"""
    if (state.get("retrieval_judge") or {}).get("rewrite_ok"):
        return "node_multi_search"
    return "node_answer_output"


# 1. 产品名确认 ->（条件分叉）认不出则结束 / 多路搜索 / 直接输出
builder.add_conditional_edges(
    "node_item_name_confirm",
    route_after_item_name_confirm,
    path_map={
        "__end__": END,
        "node_multi_search": "node_multi_search",
        "node_answer_output": "node_answer_output",
    },
)

# 2. 从分叉点并发执行四路检索
builder.add_edge("node_multi_search", "node_search_embedding")
builder.add_edge("node_multi_search", "node_search_embedding_hyde")
builder.add_edge("node_multi_search", "node_search_bm25")
builder.add_edge("node_multi_search", "node_web_search_mcp")
builder.add_edge("node_multi_search", "node_query_kg")

# 3. 五路检索 -> 结果合并点
builder.add_edge("node_search_embedding", "node_join")
builder.add_edge("node_search_embedding_hyde", "node_join")
builder.add_edge("node_search_bm25", "node_join")
builder.add_edge("node_web_search_mcp", "node_join")
builder.add_edge("node_query_kg", "node_join")

# 4. 合并 -> 融合排序 -> 重排 -> 判断充分性 ->（作答 / 改写后再检索一轮）
builder.add_edge("node_join", "node_rrf")
builder.add_edge("node_rrf", "node_rerank")
# 检索循环（2026-10-20）：重排后先判充分性；不足才改写检索词再来一轮，
# 轮次/预算用尽则用第一轮证据作答（见 node_retrieval_gate 的三条红线）。
# 循环关闭（RETRIEVAL_MAX_ROUNDS=1）时 gate 直接判充分，行为与旧版一致
builder.add_edge("node_rerank", "node_retrieval_gate")
builder.add_conditional_edges(
    "node_retrieval_gate",
    route_after_retrieval_gate,
    path_map={"node_query_rewrite": "node_query_rewrite",
              "node_answer_output": "node_answer_output"},
)
builder.add_conditional_edges(
    "node_query_rewrite",
    route_after_query_rewrite,
    path_map={"node_multi_search": "node_multi_search",
              "node_answer_output": "node_answer_output"},
)
# 用户主动暂停时答案作废，但流程照常走到 END：本轮就此结束，**不反问用户**
# （用户想调整什么，自己重新提问即可）。图因缺信息需要用户确认而中断，是另一条线，
# 要靠 checkpointer + interrupt，与本条无关
builder.add_edge("node_answer_output", END)

# 编译生成可执行的 Runnable 应用。
# **惰性编译**：checkpointer 是编译时绑定的，而 Mongo 不可用时我们会降级成内存 saver，
# 等它恢复了得换回真 saver —— 所以按 kind 判断要不要重新编译（重编译很便宜）。
_query_app = None
_query_app_kind = None


def get_query_app():
    """
    取编译好的查询图（惰性，带检查点存储）

    接上检查点后，**每次 `invoke` 都必须带 `thread_id`**（用 `graph_config(thread_id)`），
    否则 LangGraph 直接报错。本项目一轮问答一个 thread，用现成的 `run_id` 即可。
    """
    global _query_app, _query_app_kind
    saver, kind = get_checkpointer()
    if _query_app is None or kind != _query_app_kind:
        _query_app = builder.compile(checkpointer=saver)
        _query_app_kind = kind
        logger.info(f"[main_graph] 查询图已编译，检查点存储={kind}")
    return _query_app


_subagent_app = None


def get_subagent_app():
    """
    取「作为 subagent 用」的编译版本：**不接 checkpointer**。

    为什么单独一个版本：deepagents 的 `task` 工具调用子图时会带上主 agent 的
    config（含它的 thread_id），这里若仍接 saver，两张图的检查点会写进同一个
    thread 串味。而查询图无状态化（2026-10-11）后本就不需要「恢复」，
    不接 saver 没有损失。
    """
    global _subagent_app
    if _subagent_app is None:
        _subagent_app = builder.compile()
    return _subagent_app
