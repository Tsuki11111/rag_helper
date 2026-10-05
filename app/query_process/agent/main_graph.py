from langgraph.constants import END
from langgraph.graph import StateGraph

from app.clients.mongo_checkpoint_utils import get_checkpointer, graph_config
from app.core.logger import logger
from app.core.usage_tracker import add_tracked_node, usage_context
from app.query_process.agent.nodes.node_answer_output import node_answer_output
from app.query_process.agent.nodes.node_ask_user import node_ask_user
from app.query_process.agent.nodes.node_item_name_confirm import node_item_name_confirm
from app.query_process.agent.nodes.node_query_kg import node_query_kg
from app.query_process.agent.nodes.node_rerank import node_rerank
from app.query_process.agent.nodes.node_rrf import node_rrf
from app.query_process.agent.nodes.node_search_embedding import node_search_embedding
from app.query_process.agent.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.query_process.agent.nodes.node_web_search_mcp import node_web_search_mcp
from app.query_process.agent.state import QueryGraphState

# 初始化状态图
builder = StateGraph(QueryGraphState)

# 注册所有节点
# 用 add_tracked_node 而非 add_node：多包一层归因，让节点内所有模型调用都记到该节点名下
# （账本里的 node 字段），节点内部代码一行都不用改
add_tracked_node(builder, "node_item_name_confirm", node_item_name_confirm)   # 确认产品名
add_tracked_node(builder, "node_ask_user", node_ask_user)                     # 认不出产品时中断去问用户
builder.add_node("node_multi_search", lambda x: x)                   # 虚拟节点：多路搜索分叉点
add_tracked_node(builder, "node_search_embedding", node_search_embedding)     # 向量检索
add_tracked_node(builder, "node_search_embedding_hyde", node_search_embedding_hyde)  # HyDE 检索
add_tracked_node(builder, "node_query_kg", node_query_kg)                     # 图谱检索
add_tracked_node(builder, "node_web_search_mcp", node_web_search_mcp)         # 联网检索
builder.add_node("node_join", lambda x: {})                          # 虚拟节点：多路搜索合并点
add_tracked_node(builder, "node_rrf", node_rrf)                               # RRF 融合排序
add_tracked_node(builder, "node_rerank", node_rerank)                         # 重排序
add_tracked_node(builder, "node_answer_output", node_answer_output)           # 生成答案

# 虚拟节点的作用：作为流程的「分叉 / 合并中转站」，解决多分支流程的组织问题，本身无业务逻辑

# 设置入口节点
builder.set_entry_point("node_item_name_confirm")


def route_after_item_name_confirm(state: QueryGraphState) -> str:
    """
    产品名确认后的路由（三条）

    1. **认不出产品**（`need_confirm`）→ `node_ask_user`：图在这里**主动中断**去问用户
       （弹卡片给候选，用户选完从同一个 thread 接着检索）
    2. **外部预置了 answer**（自测 / 调试用）→ 直接出答案，跳过检索
    3. 其余（分支 A 已确认产品）→ 正常四路检索

    产出 `need_confirm` 的路径**刻意不写 `answer`**，所以 1 与 2 不会同时成立。
    """
    if state.get("need_confirm"):
        logger.info("[route_after_item_name_confirm] 认不出产品，转去询问用户")
        return "node_ask_user"
    if state.get("answer"):
        logger.info("[route_after_item_name_confirm] 已有answer，跳过检索直接输出")
        return "node_answer_output"
    return "node_multi_search"


# 1. 产品名确认 ->（条件分叉）问用户 / 多路搜索 / 直接输出
builder.add_conditional_edges(
    "node_item_name_confirm",
    route_after_item_name_confirm,
    path_map={
        "node_ask_user": "node_ask_user",
        "node_multi_search": "node_multi_search",
        "node_answer_output": "node_answer_output",
    },
)

# 用户答完（恢复）之后接着检索：这里不设条件边 —— 只问一轮，
# 用户填的型号对不上库就按原样用，检索不到由生成节点的兜底答复收尾
builder.add_edge("node_ask_user", "node_multi_search")

# 2. 从分叉点并发执行四路检索
builder.add_edge("node_multi_search", "node_search_embedding")
builder.add_edge("node_multi_search", "node_search_embedding_hyde")
builder.add_edge("node_multi_search", "node_web_search_mcp")
builder.add_edge("node_multi_search", "node_query_kg")

# 3. 四路检索 -> 结果合并点
builder.add_edge("node_search_embedding", "node_join")
builder.add_edge("node_search_embedding_hyde", "node_join")
builder.add_edge("node_web_search_mcp", "node_join")
builder.add_edge("node_query_kg", "node_join")

# 4. 合并 -> 融合排序 -> 重排 -> 生成 -> 结束
builder.add_edge("node_join", "node_rrf")
builder.add_edge("node_rrf", "node_rerank")
builder.add_edge("node_rerank", "node_answer_output")
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


if __name__ == '__main__':
    """
    端到端流程测试：验证检索图骨架能否走通

    当前节点均为骨架实现（仅 sleep + 返回空结果），本测试关注的是**流程结构**：
    1. 图能编译
    2. 节点按预期顺序执行（分叉/合并生效）
    3. 条件路由正确（有 answer 时跳过检索直接输出）

    说明：四路检索是并发的，LangGraph 不保证它们的执行先后，因此断言采用
    「集合相等」而非「顺序相等」，只校验该跑的节点都跑了。
    """
    import time

    from app.query_process.agent.state import create_query_default_state
    from app.utils.task_utils import clear_task, get_done_task_list, get_task_status

    logger.info("=" * 70)
    logger.info("[检索图测试] 开始端到端流程测试")
    logger.info("=" * 70)

    # ---------- 场景1：正常检索流程（应走完四路检索 → RRF → 重排 → 生成）----------
    session_id = f"query_test_{int(time.time())}"
    init_state = create_query_default_state(
        session_id=session_id,
        original_query="烫金膜盒怎么安装？",
        is_stream=False,   # 关闭流式，避免打字机效果拖慢测试
    )

    logger.info(f"[检索图测试] 场景1：正常检索流程，session_id={session_id}")
    start = time.time()
    # 包一层记账/日志上下文：命令行跑图也给它一条 trace。
    # 不包的话节点名有归因（add_tracked_node 在图上）、trace 与租户却空着，
    # 日志串不成一条、账目也归不到「哪一次运行」——服务入口是包了的，这里要对齐。
    with usage_context(session_id=session_id) as acc:
        final_state = get_query_app().invoke(init_state, graph_config(f"selftest_{session_id}"))
    elapsed = time.time() - start
    logger.info(f"[检索图测试] 本次问答记账：{acc.text()}")

    done = get_done_task_list(session_id)
    logger.info(f"[检索图测试] 执行完成，耗时 {elapsed:.1f} 秒")
    logger.info(f"[检索图测试] 任务状态：{get_task_status(session_id)}")
    logger.info(f"[检索图测试] 已完成节点（{len(done)}个）：{done}")

    # 该场景下必须执行的节点（节点名会被 task_utils 转成中文）
    # 注意：node_multi_search / node_join 是虚拟节点（lambda），不记录任务进度，故不在此列
    expected_all = {
        "确认问题产品",
        "切片搜索",                    # node_search_embedding
        "切片搜索(假设性文档)",         # node_search_embedding_hyde
        "查询知识图谱",                 # node_query_kg
        "网络搜索",                     # node_web_search_mcp
        "倒排融合",                     # node_rrf
        "重排序",                       # node_rerank
        "生成答案",                     # node_answer_output
    }
    actual = set(done)
    problems = []

    missing = expected_all - actual
    if missing:
        problems.append(f"未执行的节点：{missing}")

    if not final_state.get("answer"):
        problems.append("最终 state 中没有 answer")
    if "embedding_chunks" not in final_state:
        problems.append("state 中缺少 embedding_chunks 字段")

    logger.info(f"[检索图测试] 最终 answer：{final_state.get('answer', '')[:60]}...")

    # ---------- 场景2：产品名确认后的路由三分支（纯函数，不依赖任何服务）----------
    #
    # 这一场景原先是「给初始状态预置 answer 走短路」的图内测试。现在「认不出产品」不再
    # 产出一句现成答案，而是**中断去问用户**，那种写法不再代表真实路径（而且它要真跑
    # 确认节点、要 Milvus 与模型才能触发）。改成直接断言路由函数：秒级、零依赖。
    logger.info("")
    logger.info("[检索图测试] 场景2：路由三分支")
    route_cases = [
        ({"need_confirm": True}, "node_ask_user", "认不出产品 → 问用户"),
        ({"answer": "预置答案"}, "node_answer_output", "已有答案 → 直接输出"),
        ({}, "node_multi_search", "确认到产品 → 继续检索"),
    ]
    for st, expect, desc in route_cases:
        got = route_after_item_name_confirm(st)
        logger.info(f"    {desc}：{got}")
        if got != expect:
            problems.append(f"{desc} 应路由到 {expect}，实际 {got}")

    # ---------- 汇总 ----------
    logger.info("")
    logger.info("=" * 70)
    if problems:
        for p in problems:
            logger.error(f"[检索图测试] [FAIL] {p}")
        logger.error(f"[检索图测试] 测试失败，共{len(problems)}项未通过")
    else:
        logger.success("[检索图测试] [PASS] 流程结构验证全部通过")
    logger.info("=" * 70)

    # 清理内存态任务记录
    clear_task(session_id)
