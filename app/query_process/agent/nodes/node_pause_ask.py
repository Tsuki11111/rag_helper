"""
暂停询问节点 (node_pause_ask)

用户主动点「暂停」之后，由本节点**主动反问**一句：你想调整什么。

为什么要单独一个节点，而不是塞进 node_answer_output：
1. 职责不同 —— 那边负责生成，这边负责把「本轮被暂停」翻译成一次对话
2. 记账归因 —— 独立节点才能让这次询问的模型调用在账本里落到自己名下
   （`add_tracked_node` 注册时包了一层）

在图上：`node_answer_output --(cancelled)--> node_pause_ask --> END`

**它不做的事**：
- 不写 `state['answer']`、不碰 `cancelled` —— 那个标记要留给 query_service 判断本轮结局
- 不参与检索，也不复用本轮任何中间结果：用户回答后是**全新一次运行**

前端拿到的「已暂停」界面与选项都来自这里推的 SSE `paused` 事件。
"""
import json
import sys

from langchain.messages import HumanMessage, SystemMessage

from app.clients.mongo_history_utils import save_chat_message
from app.core.error_policy import degrade, is_fatal
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.state import QueryGraphState
from app.utils.sse_utils import SSEEvent, push_to_session
from app.utils.task_utils import add_done_task, add_running_task, get_done_task_list

# 节点名，与 main_graph.py 中注册的名称一致，用于日志前缀
NODE_NAME = "node_pause_ask"

# 拼进提示词的「已生成内容」上限：只要够看出用户当初在读什么、答案有多啰嗦
MAX_PARTIAL_CHARS = 800
# 最多保留几个选项（太多反而挑花眼）
MAX_OPTIONS = 3

# 模型没生成出来时的兜底：暂停之后**必须问得出来**，问不出来体验最差
FALLBACK_ASK = "已经暂停了。你是想调整哪方面？也可以直接说怎么改，我会重新生成一版。"
FALLBACK_OPTIONS = [
    {"label": "答案太长了，精简一些", "impact": "重新生成一版，只保留关键步骤"},
    {"label": "我要问的不是这个产品", "impact": "补充正确的产品型号后，我会重新检索"},
    {"label": "继续写完", "impact": "重新生成一版完整的回答"},
]


def _build_ask(question: str, partial: str) -> dict:
    """
    让模型生成询问语与候选选项

    任何失败（模型报错、JSON 解析失败、字段缺失）都退回固定话术：
    暂停之后必须问得出来，宁可问得平淡，也不能什么都不说。

    :return: {"question": str, "options": [{"label": str, "impact": str}]}
    """
    function_name = sys._getframe().f_code.co_name
    fallback = {"question": FALLBACK_ASK, "options": FALLBACK_OPTIONS}

    try:
        client = get_llm_client(json_mode=True)
        prompt = load_prompt(
            "pause_ask",
            question=question or "（无）",
            # 空内容要明确写出来，否则模型会对着空白瞎猜用户在看什么
            partial=(partial or "").strip()[:MAX_PARTIAL_CHARS] or "（还没开始写）",
        )
        messages = [
            SystemMessage(content="你是一个产品使用文档的问答助手，正在和用户对话。"),
            HumanMessage(content=prompt),
        ]
        content = (client.invoke(messages).content or "").strip()

        # 兜底：部分模型仍会用 ```json 包裹
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()

        data = json.loads(content)
        ask = (data.get("question") or "").strip() or FALLBACK_ASK

        options = []
        for item in (data.get("options") or [])[:MAX_OPTIONS]:
            if not isinstance(item, dict):
                continue
            label = (item.get("label") or "").strip()
            if not label:
                continue
            options.append({"label": label, "impact": (item.get("impact") or "").strip()})
        # 一个可用选项都没有时整体退回兜底，免得前端弹个空卡片
        if not options:
            options = FALLBACK_OPTIONS

        logger.info(f"[{NODE_NAME}] [{function_name}] 询问已生成，选项{len(options)}个")
        return {"question": ask, "options": options}

    except Exception as e:
        return degrade(NODE_NAME, "生成暂停询问", fallback, e)


def node_pause_ask(state: QueryGraphState) -> dict:
    """
    节点入口：问用户「想调整什么」，并把询问推给前端

    :param state: 需包含 session_id / is_stream，以及被暂停时留下的 partial_answer
    :return: 空字典 —— 只推事件，不改 state（尤其不动 cancelled）
    """
    function_name = sys._getframe().f_code.co_name
    session_id = state["session_id"]
    is_stream = state.get("is_stream", True)
    add_running_task(session_id, function_name, is_stream)
    logger.info(f"[{NODE_NAME}] [{function_name}] 节点处理开始")

    try:
        question = state.get("rewritten_query") or state.get("original_query") or ""
        ask = _build_ask(question, state.get("partial_answer") or "")

        if is_stream:
            push_to_session(
                session_id,
                SSEEvent.PAUSED,
                {
                    "question": ask["question"],
                    "options": ask["options"],
                    # 带上**真实进度**：本轮没有 final，前端只能靠这里渲染泳道；
                    # 不给的话前端无从知道跑了哪些节点（历史上它会把全部节点点亮，
                    # 变成谎报「已完成」，这里正好一并纠正）
                    "done_list": get_done_task_list(session_id),
                },
            )

        # 询问语存进历史：下一轮模型能看到「它问过什么、用户答了什么」，对话才连贯
        try:
            save_chat_message(session_id, "assistant", ask["question"])
            logger.info(f"[{NODE_NAME}] [{function_name}] 询问已存档")
        except Exception as e:
            degrade(NODE_NAME, "暂停询问存档", None, e)

        return {}

    except Exception as e:
        # 没有可降级的内容要给用户，编程错误直接上抛
        if is_fatal(e):
            raise
        return degrade(NODE_NAME, "暂停询问", {}, e)
    finally:
        add_done_task(session_id, function_name, is_stream)
        logger.info(f"[{NODE_NAME}] [{function_name}] 节点处理结束")


if __name__ == '__main__':
    """
    本地测试：询问生成（会真调模型）+ 兜底路径（打桩，不调接口）
    """
    import time

    from app.query_process.agent.state import create_query_default_state
    from app.utils.task_utils import clear_task

    logger.info("=" * 70)
    logger.info("[测试] 兜底路径：模型生成的 JSON 不可用时应退回固定话术")
    real_client = get_llm_client

    class _BadClient:
        """返回一段不是 JSON 的内容，逼出解析失败分支"""
        def invoke(self, messages):
            class _R:
                content = "我猜你想改点什么（这里故意不返回 JSON）"
            return _R()

    try:
        globals()["get_llm_client"] = lambda *a, **k: _BadClient()
        fallback_ask = _build_ask("烫金机盒怎么安装？", "已写了一半的答案")
    finally:
        globals()["get_llm_client"] = real_client

    if fallback_ask["question"] != FALLBACK_ASK:
        logger.error(f"[测试] [FAIL] 解析失败应退回兜底话术，实际：{fallback_ask['question']!r}")
    elif not fallback_ask["options"]:
        logger.error("[测试] [FAIL] 兜底话术必须带选项")
    else:
        logger.success("[测试] [PASS] 解析失败时退回兜底话术与选项")

    logger.info("=" * 70)
    logger.info("[测试] 真实生成：会调一次模型（需要可用的 API Key）")
    session_id = f"pause_ask_test_{int(time.time())}"
    st = create_query_default_state(
        session_id=session_id,
        original_query="Brother HAK 180 烫金机怎么安装烫金膜盒？",
        rewritten_query="Brother HAK 180 烫金机怎么安装烫金膜盒？",
        is_stream=False,   # 不推事件，只看生成结果
        cancelled=True,
        partial_answer="烫金膜盒的安装分为三步。第一步，打开机身右侧的支架盖……",
    )
    try:
        node_pause_ask(st)
        logger.info("[测试] 真实生成路径执行完毕（询问内容见上方日志）")
    except Exception as e:
        logger.error(f"[测试] [FAIL] 执行失败：{e}", exc_info=True)
    finally:
        clear_task(session_id)
    logger.info("=" * 70)
