"""
主 agent（deepagents）—— 编排层，网购平台智能客服的唯一「嘴」

进化形态见 evolution-plan §1：主 agent 用 deepagents 编排，RAG 检索图（本仓库的
查询链路）降为它的一个 subagent（`product-kb`）；将来的业务 subagent（查订单等）持续追加。

M2 的范畴：本地 CLI 闭环 —— 问题 → 主 agent 派发 `task` → RAG 作答（配图信息随行）
→ 主 agent 转述；并确认主 agent 自己的模型调用入账。

运行（命令行闭环）：
    .venv/Scripts/python.exe -m app.agent_process.main_agent "烫金机盒怎么装膜?"
"""
import sys
import uuid

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from deepagents import CompiledSubAgent, create_deep_agent

from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.usage_tracker import usage_context
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.main_graph import get_subagent_app
from app.query_process.agent.state import from_messages
from app.query_process.api.query_service import _clarify_fallback_text
from app.utils.task_utils import clear_task

NODE_NAME = "main_agent"


def _rag_task(payload: dict) -> dict:
    """
    RAG 检索图作为 subagent 的适配层（deepagents 的 `task` 工具调它）

    输入契约（对照 deepagents 0.7.23 `middleware/subagents.py` 实测）：
      `{"messages": [HumanMessage(任务描述)], ...父 state 里未被排除的其余键}`
      —— task 工具固定塞一条 HumanMessage(description)，所以从 messages 里取
      最后一条 user/human 消息即任务描述。
    输出契约：**必须返回含 `messages` 的 dict**；主 agent 取其中**最后一条有文本的
      `AIMessage`** 作为工具结果（`_return_command_with_state_update`）。

    另外三条刻意的约定：
    - `is_stream=False`：子 agent 没有 SSE 队列，流式分支只会推给空气
    - 用 `get_subagent_app()`（**不带 checkpointer** 的编译版）：task 调用会带上
      主 agent 的 config（含 thread_id），共用 saver 会把两张图的检查点写进同一个 thread
    - 整体包 `usage_context`：账目与日志归因到这次子调用；session 是临时的，
      跑完 `clear_task` 清掉进度记录
    """
    state_in = from_messages(payload.get("messages") or [],
                             session_id=f"rag_sub_{uuid.uuid4().hex[:8]}",
                             is_stream=False)
    sid, question = state_in["session_id"], state_in["original_query"]
    logger.info(f"[{NODE_NAME}] [rag_task] 收到任务：{question[:60]!r}")

    try:
        with usage_context(session_id=sid, node="rag_subagent") as acc:
            final = get_subagent_app().invoke(state_in)
        text = (final.get("answer") or "").strip()
        if not text and final.get("need_confirm"):
            # 没锁定产品：把候选交给主 agent，由它用对话向用户澄清（无状态化后 RAG 不再自己问）
            text = _clarify_fallback_text(final.get("clarify") or {})
        images = final.get("images") or []
        if text and images:
            # M2 走文本通道：配图 URL 附在答案尾部；结构化贯通留给 M5 / Java 阶段
            text += "\n\n[配图 {} 张：{}]".format(
                len(images), " | ".join((i.get("url") or "") for i in images[:6]))
        if not text:
            text = "检索没有返回可用内容，请换一种问法或确认产品型号。"
        logger.info(f"[{NODE_NAME}] [rag_task] 完成，返回 {len(text)} 字符；子调用记账：{acc.text()}")
        return {"messages": [AIMessage(content=text)]}
    finally:
        clear_task(sid)


RAG_SUBAGENT = CompiledSubAgent(
    name="product-kb",
    description=(
        "产品文档知识库检索（说明书 / 用户手册）。用户问某产品怎么用、怎么装、"
        "故障怎么处理、参数是什么时用它。description 里写清用户原话 + 已知的产品型号"
        "（例如从订单里查到的），信息越全检索越准；工具结果文本会直接返回，你负责转述。"
    ),
    runnable=RunnableLambda(_rag_task),
)


def build_main_agent():
    """组装主 agent。model 用带记账 callback 的 DashScope 客户端 —— 主 agent 调用也入账"""
    return create_deep_agent(
        model=get_llm_client(),
        system_prompt=load_prompt("main_agent"),
        subagents=[RAG_SUBAGENT],
    )


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]).strip() or "Brother HAK 180 烫金机怎么安装烫金膜盒？"
    logger.info(f"[{NODE_NAME}] 命令行闭环，问题：{question}")
    agent = build_main_agent()
    with usage_context(session_id="main_agent_cli", node=NODE_NAME) as acc:
        result = agent.invoke({"messages": [HumanMessage(content=question)]})
    reply = result["messages"][-1]
    print("=" * 64)
    print("主 agent 最终回复：")
    print(reply.text if hasattr(reply, "text") else str(reply.content))
    print("=" * 64)
    print("本轮记账（主 agent + 子调用）：", acc.text())
