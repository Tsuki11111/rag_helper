"""流式增量缓冲的回归用例（从 app/agent/stream_buffer.py 迁出）。"""
from app.agent.runtime.stream_buffer import DeltaBuffer


def _check_delta_buffer() -> list:
    """回归：中间轮次不推、最终回答推、工具消息清空、新消息重置。"""
    problems: list = []

    class _Chunk:
        def __init__(self, mid, content="", tool=False):
            self.id = mid
            self.content = content
            self.tool_call_chunks = [{"name": "x"}] if tool else []

    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", "先写一版表格"))
    buf.feed(_Chunk("m1", tool=True))
    buf.feed(_Chunk("m2", "最终回答"))
    if buf.finish() != ["最终回答"]:
        problems.append(f"中间轮次未被丢弃或最终文本丢失：{buf.finish()!r}")

    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", "第一段"))
    buf.feed(_Chunk("m2", "第二段"))
    if buf.finish() != ["第二段"]:
        problems.append(f"新消息未重置旧文本：{buf.finish()!r}")

    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", tool=True))
    buf.feed(_Chunk("m1", "工具后的文本"))
    if buf.finish():
        problems.append(f"工具消息后的文本不应回放：{buf.finish()!r}")

    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", 123))
    buf.feed(_Chunk("m1", ""))
    if buf.finish():
        problems.append("非字符串/空内容不应进入缓冲")
    return problems
