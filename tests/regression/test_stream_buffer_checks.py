"""流式增量缓冲的 pytest 用例。"""
from app.agent.runtime.stream_buffer import DeltaBuffer


class _Chunk:
    def __init__(self, mid, content="", tool=False):
        self.id = mid
        self.content = content
        self.tool_call_chunks = [{"name": "x"}] if tool else []


def test_delta_buffer_discards_tool_messages():
    """工具轮次的中间文本不能推给前端。"""
    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", "先写一版表格"))
    buf.feed(_Chunk("m1", tool=True))
    buf.feed(_Chunk("m2", "最终回答"))
    assert buf.finish() == ["最终回答"]


def test_delta_buffer_resets_on_new_message():
    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", "第一段"))
    buf.feed(_Chunk("m2", "第二段"))
    assert buf.finish() == ["第二段"]


def test_delta_buffer_ignores_tool_and_non_string():
    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", tool=True))
    buf.feed(_Chunk("m1", "工具后的文本"))
    assert buf.finish() == []

    buf = DeltaBuffer()
    buf.feed(_Chunk("m1", 123))
    buf.feed(_Chunk("m1", ""))
    assert buf.finish() == []
