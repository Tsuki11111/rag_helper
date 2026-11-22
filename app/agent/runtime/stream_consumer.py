"""主 agent 流消费：把 LangGraph chunk 映射成执行流事件与最终消息。"""
import time

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.runtime.stream_buffer import DeltaBuffer
from app.agent.runtime.trace_labels import (
    TRACE_MAIN_AGENT,
    trace_subagent_action,
    trace_subagent_name,
    trace_tool_label,
)
from app.conf.budget_config import budget_config
from app.core.logger import logger
from app.utils.task_utils import node_display_name

NODE_NAME = "main_agent"


class PausedByUser(Exception):
    """用户点了暂停；由上层收尾成 paused。"""


def consume_stream(agent, messages: list, on_event, deadline: float = None,
                   stop_check=None, stream_output: bool = False,
                   fact_ledger: list = None) -> list:
    """
    消费主 agent 的流：每一步映射成事件（step 事件是 M5 灰字 / gRPC Trace 的原料），
    返回最终 messages。两处中断：deadline 到点、stop_check() 为真。

    `subgraphs=True`：subagent 在 task 工具里被同步调起，子图节点事件会冒到这条流上；
    子层的工具返回与文本不回放 —— 子 agent 的结果最终以父层「工具返回」出现。
    """
    final_messages = messages
    current_sub = None
    pending_task_id = None
    tool_names_by_id = {}
    delta_buffer = DeltaBuffer()
    modes = ["updates", "values"] + (["messages"] if stream_output else [])
    for chunk in agent.stream({"messages": messages}, stream_mode=modes,
                              subgraphs=True):
        if deadline and time.time() > deadline:
            raise TimeoutError(f"本轮已超出预算 {budget_config.query_wall_clock_budget} 秒")
        if stop_check and stop_check():
            raise PausedByUser()
        ns, mode, data = chunk if isinstance(chunk, tuple) else ((), "updates", chunk)
        if mode == "messages":
            if ns:
                continue
            try:
                msg_chunk, _meta = data
            except Exception:
                continue
            delta_buffer.feed(msg_chunk)
            continue
        if mode == "values":
            if not ns and isinstance(data, dict) and "messages" in data:
                final_messages = data["messages"]
            continue
        for node, upd in (data or {}).items():
            if ns:
                label = node_display_name(node)
                if label:
                    on_event({"type": "step", "kind": "node",
                              "agent": trace_subagent_name(current_sub), "detail": label})
                    continue
                if isinstance(upd, dict):
                    for m in upd.get("messages") or []:
                        for tc in (getattr(m, "tool_calls", None) or []):
                            name = tc.get("name")
                            if name != "task":
                                on_event({"type": "step", "kind": "tool",
                                          "agent": trace_subagent_name(current_sub),
                                          "detail": trace_tool_label(name)})
                continue
            if not isinstance(upd, dict):
                continue
            for m in upd.get("messages") or []:
                tool_calls = getattr(m, "tool_calls", None) or []
                if tool_calls:
                    for tc in tool_calls:
                        name, args = tc.get("name"), tc.get("args") or {}
                        if name == "task":
                            current_sub = args.get("subagent_type")
                            pending_task_id = tc.get("id")
                            tool_names_by_id[pending_task_id] = (
                                f"task:{current_sub or 'subagent'}")
                            on_event({"type": "step", "kind": "delegate",
                                      "agent": trace_subagent_name(current_sub),
                                      "detail": trace_subagent_action(current_sub)})
                        else:
                            tool_names_by_id[tc.get("id")] = name or "tool"
                            on_event({"type": "step", "kind": "tool",
                                      "agent": TRACE_MAIN_AGENT,
                                      "detail": trace_tool_label(name)})
                elif isinstance(m, ToolMessage):
                    if fact_ledger is not None:
                        fact_ledger.append({
                            "tool": tool_names_by_id.get(
                                getattr(m, "tool_call_id", None), "tool"),
                            "content": str(m.content or ""),
                        })
                    actor = TRACE_MAIN_AGENT
                    if pending_task_id and getattr(m, "tool_call_id", None) == pending_task_id:
                        actor = trace_subagent_name(current_sub)
                        current_sub = None
                        pending_task_id = None
                    on_event({"type": "step", "kind": "result",
                              "agent": actor, "detail": "已完成"})
                elif isinstance(m, AIMessage) and (m.content or "").strip():
                    on_event({"type": "step", "kind": "reply",
                              "agent": TRACE_MAIN_AGENT,
                              "detail": f"生成回复（{len(m.content)} 字符）"})
    for piece in delta_buffer.finish():
        on_event({"type": "delta", "text": piece})
    return final_messages


def last_reply(messages: list, since: int = 0) -> str:
    """只在本轮新增消息里取最后一条「有文本、无工具调用」的 AIMessage。"""
    for m in reversed((messages or [])[since:]):
        if isinstance(m, AIMessage) and (m.content or "").strip() and not (m.tool_calls or []):
            return m.content
    return ""
