"""一次 Converse 内的工具身份上下文（ContextVar）。

Java 在 gRPC metadata 里下发短期票据（`x-agent-tool-ticket`），gRPC 层读出后
经 `run_main_agent` 设进来；工具调用时再取出来回调 Java。

**userId 不经过模型**：工具签名里没有它，模型即使被注入也指定不了别的用户。
"""
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolContext:
    ticket: str = ""
    user_id: str = ""


_TOOL_CONTEXT: ContextVar[ToolContext] = ContextVar(
    "agent_tool_context", default=ToolContext())


def set_tool_context(ticket: str = "", user_id: str = ""):
    return _TOOL_CONTEXT.set(ToolContext(ticket=ticket or "", user_id=user_id or ""))


def reset_tool_context(token) -> None:
    _TOOL_CONTEXT.reset(token)


def current_tool_context() -> ToolContext:
    return _TOOL_CONTEXT.get()
