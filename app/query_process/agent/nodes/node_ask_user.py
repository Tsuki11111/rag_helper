"""
用户确认节点 (node_ask_user)

产品名认不出来时，由图**主动中断**去问用户：前端弹卡片给出候选（含近似候选，附来源文档名）
+ 允许自己填型号；用户选完，从**同一个 thread 恢复**，接着检索并出答案。

在图上：`node_item_name_confirm --(need_confirm)--> node_ask_user --> node_multi_search`

**两条必须守住的规矩**：

1. **`interrupt()` 之前的代码必须无副作用** —— 恢复时整个节点会**从头重跑一遍**
   （实测过：节点函数被执行两次）。所以 pre-interrupt 段只做「读 state、拼 payload」，
   不写库、不记账、不调模型。
2. **只问一轮，不循环** —— 恢复后一律把 `need_confirm` 置回 False。用户自己填的型号
   库里对不上就按原样用，检索不到由 `node_answer_output` 的兜底答复收尾。
   否则「对不上 → 再问 → 还是对不上」会绕不出来。

与「用户主动暂停」的区别：**暂停是用户发起**（停下就结束、不追问），
**这里是图发起**（必须问到答案才继续）。
"""
import sys

from langgraph.types import interrupt

from app.clients.mongo_history_utils import save_chat_message
from app.core.error_policy import degrade, is_fatal
from app.core.logger import logger
from app.query_process.agent.nodes.node_item_name_confirm import (
    step_4_vectorize_and_query,
    step_5_align_item_names,
)
from app.query_process.agent.state import QueryGraphState
from app.utils.task_utils import add_done_task, add_running_task

# 节点名，与 main_graph.py 中注册的名称一致，用于日志前缀
NODE_NAME = "node_ask_user"

# 卡片没给出问题时的兜底
FALLBACK_QUESTION = "请确认你要问的是哪个产品型号。"


def _normalize_choice(session_id: str, raw: str) -> str:
    """
    把用户给的名字对齐到库里的标准产品名

    用户自己填的可能是简称、大小写不同、多打了空格 —— 复用确认节点里现成的那两步
    （向量化检索 + 相似度对齐）。

    **三级回退**：确认（≥0.85）→ 最佳候选（≥0.6）→ 原样使用。

    「最佳候选」这一级是踩过坑才补的：早先只看「确认」，于是用户手填的
    「hak180烫金机」既够不上 0.85、又**明明已经对出了候选** `Brother HAK 180 烫金机`，
    却被原样拿去当 Milvus 过滤器 —— 那个串匹配 **0 条切片**（标准名有 84 条），
    于是整条本地检索静默归零、答案 100% 来自联网搜索，用户完全看不出发生了什么。
    **宁可猜一个最像的，也别拿一个必然匹配不到的串去过滤。**

    :return: 对齐后的名字；输入为空时返回空串
    """
    name = (raw or "").strip()
    if not name:
        return ""
    try:
        query_results = step_4_vectorize_and_query([name])
        align = step_5_align_item_names(query_results)
        confirmed = align.get("confirmed_item_names") or []
        if confirmed:
            logger.info(f"[{NODE_NAME}] 用户输入 {name!r} 对齐到标准名 {confirmed[0]!r}")
            return confirmed[0]
        # 没到确认阈值（0.85）：退一步用最佳候选（候选阈值 0.6，低于它才算真的对不上）
        candidates = align.get("candidates") or []
        guess = (candidates[0].get("item_name") or "") if candidates else ""
        if guess:
            logger.info(
                f"[{NODE_NAME}] 用户输入 {name!r} 没到确认阈值，按最佳候选 {guess!r} 使用"
            )
            return guess
        logger.info(f"[{NODE_NAME}] 用户输入 {name!r} 对不上库里的标准名，按原样使用")
    except Exception as e:
        # 对齐失败不该拦住用户：按他的原话继续
        degrade(NODE_NAME, "对齐用户输入", None, e)
    return name


def node_ask_user(state: QueryGraphState) -> dict:
    """
    节点入口：挂起等用户确认；恢复后把选择写进 state，交给下游继续检索
    """
    function_name = sys._getframe().f_code.co_name
    session_id = state["session_id"]
    is_stream = state.get("is_stream", True)

    # ↓↓↓ 这一段会随恢复重跑，**必须无副作用** ↓↓↓
    clarify = state.get("clarify") or {}
    payload = {
        "question": clarify.get("question") or FALLBACK_QUESTION,
        "options": clarify.get("options") or [],
        "allow_custom": clarify.get("allow_custom", True),
    }
    raw_choice = interrupt(payload)
    # ↑↑↑ 恢复后的第一条语句从这里开始 ↑↑↑

    # 挂起期间不该显示成「正在跑」，所以登记放在中断之后
    add_running_task(session_id, function_name, is_stream)
    logger.info(f"[{NODE_NAME}] [{function_name}] 收到用户选择：{raw_choice!r}")

    try:
        # 兼容两种形状：前端传 {"choice": "..."}，或直接传一个字符串
        raw = raw_choice.get("choice") if isinstance(raw_choice, dict) else raw_choice
        raw = str(raw or "").strip()
        chosen = _normalize_choice(session_id, raw)

        # 下游的向量检索与 HyDE 都吃 rewritten_query：把选中的产品名补进去，
        # 否则用户是在卡片里选的，原问题里根本没有型号
        rewritten_query = state.get("rewritten_query") or state.get("original_query") or ""
        if chosen and chosen not in rewritten_query:
            rewritten_query = f"{chosen} {rewritten_query}"

        try:
            save_chat_message(session_id, "user", raw or "（未选择）")
        except Exception as e:
            degrade(NODE_NAME, "用户选择存档", None, e)

        return {
            "need_confirm": False,                 # 只问一轮，别再问
            "item_names": [chosen] if chosen else [],
            "rewritten_query": rewritten_query,
        }

    except Exception as e:
        if is_fatal(e):
            raise
        # 兜底：把「要问用户」的标记清掉继续跑，别把用户永久卡在挂起态
        return degrade(NODE_NAME, "处理用户选择", {"need_confirm": False}, e)
    finally:
        add_done_task(session_id, function_name, is_stream)


def _check_interrupt_resume() -> list:
    """
    自测：迷你图（只有本节点）+ 真 checkpointer，走一遍 interrupt → resume

    验证三条不变量（都踩过或差点踩到）：
    - 挂起时 `invoke` 返回 `__interrupt__`，payload 就是卡片内容
    - 挂起期间**不写任何历史**（pre-interrupt 段无副作用）
    - 恢复后历史**恰好写一条**（节点重跑，但写库在 interrupt 之后）

    需要 mongo 容器（checkpointer 要落库）；**不调任何模型接口**（打桩）。
    :return: 问题描述列表，空表示全部通过
    """
    import os
    import time

    from langgraph.graph import END, StateGraph
    from langgraph.types import Command
    from pymongo import MongoClient

    from app.clients.mongo_checkpoint_utils import get_checkpointer, graph_config

    problems = []
    saved = []
    real_save = globals()["save_chat_message"]
    real_norm = globals()["_normalize_choice"]
    thread = f"ask_selftest_{int(time.time())}"

    try:
        # 打桩：不写库、不调嵌入接口
        globals()["save_chat_message"] = lambda sid, role, text, *a, **k: saved.append((role, text))
        globals()["_normalize_choice"] = lambda sid, raw: f"标准名:{raw}"

        builder = StateGraph(QueryGraphState)
        builder.add_node("node_ask_user", node_ask_user)
        builder.set_entry_point("node_ask_user")
        builder.add_edge("node_ask_user", END)
        app = builder.compile(checkpointer=get_checkpointer()[0])

        cfg = graph_config(thread)
        init_state = {
            "session_id": "ask_selftest",
            "is_stream": False,
            "original_query": "烫金机盒怎么安装？",
            "rewritten_query": "烫金机盒怎么安装？",
            "need_confirm": True,
            "clarify": {
                "question": "选哪个？",
                "options": [{"item_name": "A", "file_title": "a.pdf", "score": 0.9, "near": False}],
                "allow_custom": True,
            },
        }

        r1 = app.invoke(init_state, cfg)
        if "__interrupt__" not in r1:
            problems.append("第一次 invoke 应当中断，实际没有 __interrupt__")
        else:
            value = r1["__interrupt__"][0].value
            if value.get("question") != "选哪个？":
                problems.append(f"中断 payload 的 question 不对：{value!r}")
            if len(value.get("options") or []) != 1:
                problems.append(f"中断 payload 的 options 不对：{value!r}")
        if saved:
            problems.append(f"挂起期间不该写历史，却写了：{saved!r}")

        r2 = app.invoke(Command(resume={"choice": "HAK180"}), cfg)
        if r2.get("need_confirm"):
            problems.append("恢复后 need_confirm 没被置回 False")
        if r2.get("item_names") != ["标准名:HAK180"]:
            problems.append(f"恢复后 item_names 不对：{r2.get('item_names')!r}")
        if len(saved) != 1:
            problems.append(f"历史应恰好写 1 条（节点会重跑但写库在其后），实际 {len(saved)} 条：{saved!r}")
        if "标准名:HAK180" not in (r2.get("rewritten_query") or ""):
            problems.append(f"rewritten_query 没补上产品名：{r2.get('rewritten_query')!r}")

    finally:
        globals()["save_chat_message"] = real_save
        globals()["_normalize_choice"] = real_norm
        try:
            mdb = MongoClient(os.getenv("MONGO_URL"))[os.getenv("MONGO_DB_NAME")]
            for coll in ("checkpoints", "checkpoint_writes"):
                mdb[coll].delete_many({"thread_id": thread})
        except Exception:
            pass
        # 清掉这次自测在共享存储里留下的进度。
        # **以前不清也不显眼**（只污染本进程内存、退出即消失），现在任务状态在 Redis 上，
        # 不清就会留下一条 `task:ask_selftest:*` 到 TTL 才回收，排障时 `KEYS 'task:*'` 会被它干扰。
        # session_id 是固定的（不是带时间戳的），所以每次跑都会复用同一个 key。
        try:
            from app.utils.task_utils import clear_task
            clear_task("ask_selftest")
        except Exception as e:
            problems.append(f"清理自测进度失败：{type(e).__name__}: {e}")

    return problems


def _check_normalize_choice() -> list:
    """
    离线自测：用户手填的名字要能被对齐到标准名（只调向量检索，不调 LLM）

    守住的是这条**三级回退**：确认（≥0.85）→ 最佳候选（≥0.6）→ 原样使用。
    缺了中间那一级会出真事故：用户手填的「hak180烫金机」够不上 0.85、却已经对出了候选
    `Brother HAK 180 烫金机`，被原样拿去当 Milvus 过滤器就匹配 **0 条切片**
    （标准名有 84 条），整条本地检索静默归零、答案全来自联网。

    断言分两类，缺一不可：
    - **近似名要被拉回来**（否则本地检索归零）
    - **库里没有的名字要原样返回**（否则会硬凑一个不相干的产品，比 0 条更糟）

    :return: 问题描述列表，空表示通过
    """
    from app.clients.milvus_utils import get_milvus_client
    from app.conf.milvus_config import milvus_config as _mc
    from app.core.error_policy import degrade as _degrade

    problems = []
    # 标准名现查，别写死 —— 写死的话往库里加个产品，用例就开始误报
    try:
        client = get_milvus_client()
        if client is None:
            return ["Milvus 不可用，跳过（用例需要真库）"]
        rows = client.query(collection_name=_mc.item_name_collection,
                            filter="", output_fields=["item_name"], limit=100)
        stored = {r.get("item_name") for r in rows if r.get("item_name")}
    except Exception as e:
        _degrade(NODE_NAME, "自测读取标准名", None, e)
        return []
    if not stored:
        return ["kb_item_names 是空的，无法验证对齐（先把文档导入进去）"]

    for raw in ["hak180烫金机", "Hak180烫金机", "万用表"]:
        got = _normalize_choice("selftest_normalize", raw)
        if got == raw:
            problems.append(f"{raw!r} 没被对齐到标准名（本地检索会归零）")
        elif got not in stored:
            problems.append(f"{raw!r} 对齐出了库里没有的名字 {got!r}")

    # 库里没有的产品：不能硬凑
    for raw in ["小米15", "完全不存在的型号XYZ"]:
        got = _normalize_choice("selftest_normalize", raw)
        if got != raw:
            problems.append(f"库里没有的 {raw!r} 被硬凑成了 {got!r}")
    return problems


if __name__ == '__main__':
    logger.info("=" * 70)
    logger.info("[测试] interrupt → resume 三条不变量（打桩，不调模型）")
    problems = _check_interrupt_resume()
    for p in problems:
        logger.error(f"[测试] [FAIL] {p}")
    if not problems:
        logger.success("[测试] [PASS] 中断 payload、挂起不写历史、恢复只写一条 —— 全部通过")

    logger.info("[测试] 用户手填名字的三级回退（只调向量检索）")
    problems += _check_normalize_choice()
    for p in problems:
        logger.error(f"[测试] [FAIL] {p}")
    if not problems:
        logger.success("[测试] [PASS] 近似名能对齐、库里没有的不硬凑")
    logger.info("=" * 70)
