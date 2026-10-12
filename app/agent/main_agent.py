"""
主 agent（deepagents）—— 编排层，网购平台智能客服的唯一「嘴」

进化形态见 evolution-plan §1：主 agent 用 deepagents 编排，RAG 检索图（本仓库的
查询链路）降为它的一个 subagent（`product-kb`）；将来的业务 subagent（查订单等）持续追加。

M2/M3 的范畴：本地 CLI 闭环 —— 问题 → 主 agent 派发 `task` → 各 subagent 干活
→ 主 agent 转述；并确认主 agent 自己的模型调用入账。

两个 subagent 各代表一种形态：
- `product-kb`：**CompiledSubAgent** —— 收一张现成编译好的图（检索链路）
- `order-query`：**字典式声明**（tools + system_prompt，框架自动编译）—— 普通「工具型」子 agent；
  M3 阶段工具只返回固定字符串（模拟多 agent 调度，不设计订单业务，见 evolution-plan §4 M3）

运行（命令行闭环）：
    .venv/Scripts/python.exe -m app.agent.main_agent "烫金机盒怎么装膜?"
    .venv/Scripts/python.exe -m app.agent.main_agent "我买的烫金机不会用，订单 12345"
"""
import sys
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from deepagents import CompiledSubAgent, create_deep_agent

from app.agent.sub_agents.order_query_agent.order_query import query_order
from app.clients.mongo_history_utils import get_recent_messages, save_chat_message
from app.clients.mongo_run_utils import save_query_run
from app.conf.budget_config import budget_config
from app.core.input_guard import INPUT_GUARD_ANSWER, check_user_input
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.usage_tracker import usage_context
from app.lm.lm_utils import get_llm_client
from app.agent.sub_agents.product_helper_agent.main_graph import get_subagent_app
from app.agent.sub_agents.product_helper_agent.state import from_messages
from app.agent.api.query_service import _clarify_fallback_text
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


ORDER_SUBAGENT = {
    "name": "order-query",
    "description": (
        "查询订单状态与物流（模拟数据）。用户提供了订单号、或问「我的订单/快递到哪了/"
        "什么时候到」时用它；返回文本里可能带商品型号，可据此再去 product-kb 检索使用问题。"
    ),
    "system_prompt": "你是订单查询助手。拿到订单号就调用 query_order，把结果如实带回。",
    "tools": [query_order],
}


def build_main_agent():
    """组装主 agent。model 用带记账 callback 的 DashScope 客户端 —— 主 agent 调用也入账"""
    return create_deep_agent(
        model=get_llm_client(),
        system_prompt=load_prompt("main_agent"),
        subagents=[RAG_SUBAGENT, ORDER_SUBAGENT],
    )


# ──────────────────────── 统一执行函数（HTTP / gRPC / CLI 共用）────────────────────────
# 一轮询问的骨架：会话读写、输入护栏、整轮归因与预算、事件流、运行记录 ——
# 平台 infra 的挂点都在这一层（见 evolution-plan §7「平台上移」对照）。

_agent_cache = None


def _get_agent():
    """主 agent 惰性构建 + 进程内复用（编译不便宜，服务场景别每轮重建）"""
    global _agent_cache
    if _agent_cache is None:
        _agent_cache = build_main_agent()
    return _agent_cache


def _clip(text, n: int = 100) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def _safe_save(session_id: str, role: str, text: str) -> None:
    """写会话历史 —— 写不进去是历史的事，绝不能连带弄挂这一轮（与记账同一条规矩）"""
    try:
        save_chat_message(session_id, role, text)
    except Exception as e:
        logger.warning(f"[{NODE_NAME}] 会话历史写入失败（不影响本轮）：{e}")


def _consume_stream(agent, messages: list, on_event, deadline: float = None) -> list:
    """
    消费主 agent 的流：每一步映射成事件（step 事件是 M5 灰字 / gRPC Trace 的原料），
    返回最终 messages。deadline 到点即中断 —— 整轮预算的第二道闸（第一道在各客户端超时）。
    """
    final_messages = messages
    for chunk in agent.stream({"messages": messages}, stream_mode=["updates", "values"]):
        if deadline and time.time() > deadline:
            raise TimeoutError(f"本轮已超出预算 {budget_config.query_wall_clock_budget} 秒")
        mode, data = chunk if isinstance(chunk, tuple) else ("updates", chunk)
        if mode == "values":
            if isinstance(data, dict) and "messages" in data:
                final_messages = data["messages"]
            continue
        for _node, upd in (data or {}).items():
            if not isinstance(upd, dict):
                continue
            for m in upd.get("messages") or []:
                tool_calls = getattr(m, "tool_calls", None) or []
                if tool_calls:
                    for tc in tool_calls:
                        name, args = tc.get("name"), tc.get("args") or {}
                        if name == "task":
                            on_event({"type": "step", "kind": "delegate",
                                      "agent": args.get("subagent_type"),
                                      "detail": _clip(args.get("description"), 90)})
                        else:
                            on_event({"type": "step", "kind": "tool",
                                      "agent": name, "detail": _clip(args, 60)})
                elif isinstance(m, ToolMessage):
                    on_event({"type": "step", "kind": "result", "detail": _clip(m.content)})
                elif isinstance(m, AIMessage) and (m.content or "").strip():
                    on_event({"type": "step", "kind": "reply",
                              "detail": f"生成回复（{len(m.content)} 字符）"})
    return final_messages


def _last_reply(messages: list) -> str:
    """取最后一条「有文本、无工具调用」的 AIMessage —— 主 agent 的最终回复"""
    for m in reversed(messages or []):
        if isinstance(m, AIMessage) and (m.content or "").strip() and not (m.tool_calls or []):
            return m.content
    return ""


def run_main_agent(session_id: str, message: str, on_event=None) -> dict:
    """
    统一执行函数 —— 一轮询问的骨架（HTTP / gRPC / CLI 共用，见 evolution-plan §4 的 M4）

    职责（平台挂点，对照 §7「平台上移」）：
    - **输入护栏先过**：命中直接拒答，不烧主 agent 的模型调用
    - **整轮归因与预算**：`usage_context(trace_id=run_id, …)` 覆盖主 agent 与全部
      subagent；wall-clock 到点由 `_consume_stream` 中断
    - **会话状态 = `chat_message`**：读 `get_recent_messages` 组装上下文、
      写「用户消息 + 最终回复」；不接 checkpointer（状态源就是会话历史）
    - **运行记录 `query_runs`**：一轮一条（从检索图那层上移过来）
    - **事件流**：`on_event({"type": "step" | "final" | "error", …})` ——
      HTTP 推 SSE、gRPC 映射 Event、CLI 渲染灰字，三个消费者同一份原料

    :return: {"run_id", "reply", "outcome", "usage_text"}
    """
    run_id = uuid.uuid4().hex[:16]
    emit = on_event or (lambda ev: None)

    # ① 输入护栏（统一入口先过）
    guard_reason = check_user_input(message)
    if guard_reason:
        logger.warning(f"[{NODE_NAME}] 输入护栏命中（{guard_reason}），拒答、不调模型")
        _safe_save(session_id, "user", message)
        _safe_save(session_id, "assistant", INPUT_GUARD_ANSWER)
        emit({"type": "final", "text": INPUT_GUARD_ANSWER})
        save_query_run({
            "trace_id": run_id, "segment": "start", "session_id": session_id,
            "tenant_id": "", "question": message, "outcome": "blocked",
            "answer_chars": len(INPUT_GUARD_ANSWER),
            "done_list": [], "degraded_list": [], "ts": time.time(),
        })
        return {"run_id": run_id, "reply": INPUT_GUARD_ANSWER,
                "outcome": "blocked", "usage_text": ""}

    # ② 读历史组装上下文（会话状态 = chat_message；读失败自然退化成本轮独立）
    messages = []
    for m in get_recent_messages(session_id):
        text = (m.get("text") or "").strip()
        if not text:
            continue
        messages.append(HumanMessage(content=text) if m.get("role") == "user"
                        else AIMessage(content=text))
    messages.append(HumanMessage(content=message))

    # ③ 用户消息先落库（哪怕这轮随后失败，历史里也看得见问了什么）
    _safe_save(session_id, "user", message)

    # ④ 跑一轮
    t0 = time.time()
    reply, error_msg, outcome = "", "", "answered"
    with usage_context(trace_id=run_id, session_id=session_id, node=NODE_NAME,
                       wall_clock_budget=budget_config.query_wall_clock_budget) as acc:
        try:
            final_messages = _consume_stream(
                _get_agent(), messages, emit,
                deadline=t0 + budget_config.query_wall_clock_budget)
            reply = _last_reply(final_messages)
            if not reply:
                outcome, error_msg = "error", "主 agent 没有产出回复"
        except Exception as e:
            outcome = "error"
            error_msg = f"{type(e).__name__}: {e}"
            logger.error(f"[{NODE_NAME}] 本轮执行失败：{error_msg}", exc_info=True)
            emit({"type": "error", "detail": error_msg})

    # ⑤ 助手回复落库 + 收尾事件 + 运行记录
    if reply:
        _safe_save(session_id, "assistant", reply)
    emit({"type": "final", "text": reply})
    save_query_run({
        "trace_id": run_id, "segment": "start", "session_id": session_id,
        "tenant_id": "", "question": message, "rewritten_query": "",
        "item_names": [], "enable_web_search": True, "is_stream": True,
        "outcome": outcome, "error": error_msg,
        "answer_chars": len(reply), "images_count": 0, "web_only": False,
        "topk_total": 0, "topk_local": 0, "topk_web": 0, "topk_chunk_ids": [],
        "done_list": [], "degraded_list": [],
        "usage": acc.summary(), "ts": time.time(),
    })
    return {"run_id": run_id, "reply": reply, "outcome": outcome,
            "usage_text": acc.text()}


# ──────────────────────── 自测入口（单轮 / 交互）────────────────────────
# 交互模式 = M5「执行流界面」的命令行预演：消费 run_main_agent 的事件流，
# 把调度过程渲染成灰色小字。

_DIM = "\033[90m" if sys.stdout.isatty() else ""
_RESET = "\033[0m" if sys.stdout.isatty() else ""


def _dim(text: str) -> None:
    print(f"{_DIM}{text}{_RESET}")


def _render_event(ev: dict) -> None:
    """事件 → 灰字（CLI 的渲染器；M5 前端是另一套渲染器，消费同一份事件）"""
    if ev.get("type") == "step":
        kind = ev.get("kind")
        if kind == "delegate":
            _dim(f"  ⎿ 派发 → {ev.get('agent')}：{ev.get('detail')}")
        elif kind == "tool":
            _dim(f"  ⎿ 调用工具 {ev.get('agent')}：{ev.get('detail')}")
        elif kind == "result":
            _dim(f"  ⎿ 工具返回：{ev.get('detail')}")
        else:
            _dim(f"  ⎿ {ev.get('detail')}")
    elif ev.get("type") == "error":
        _dim(f"  ⎿ [错误] {ev.get('detail')}")


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]).strip()
    if question:
        logger.info(f"[{NODE_NAME}] 单轮自测，问题：{question}")
        result = run_main_agent("main_agent_cli", question, on_event=_render_event)
        print("=" * 64)
        print("主 agent 最终回复：")
        print(result["reply"])
        print("=" * 64)
        print("本轮记账（主 agent + 子调用）：", result["usage_text"])
    else:
        # Windows 中文环境：管道 / 部分终端下 stdin 编码不一定是 UTF-8 —— 不重配的话，
        # Git Bash 里输入中文会带出代理字符（\udcXX），发给模型 API 时直接 UnicodeEncodeError。
        # 真控制台用的 WindowsConsoleIO 不支持 reconfigure，抛错即忽略 —— 它本来就能读中文。
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("交互模式 —— 直接输入问题；exit / 空行退出；调度过程为灰色小字。")
        while True:
            try:
                q = input("\n你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q.lower() in ("", "exit", "quit"):
                break
            result = run_main_agent("main_agent_cli", q, on_event=_render_event)
            print("\n客服 >", result["reply"] or "（本轮没有生成回复）")
            print(f"[本轮记账] {result['usage_text']}")
