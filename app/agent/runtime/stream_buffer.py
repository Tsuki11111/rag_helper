"""流式增量缓冲：只回放「最后一条不含工具调用的主层消息」。

`stream_output=True` 时订阅 LangGraph 的 `messages` 流。工具轮次里的中间文本
（模型先写一版表格/结论、再调工具、再重写）不能推给前端，否则 final 到达时整段
替换，用户会看到内容闪一下就没了。这个类把原先散在 `_consume_stream` 里的
5 个可变标志收敛成一个小状态机，便于纯单测。
"""


class DeltaBuffer:
    """按消息 id 缓冲文本；出现工具调用的消息整条丢弃，流结束补发最后一条。"""

    def __init__(self):
        self._text: list[str] = []
        self._message_id = None
        self._has_tool = False
        self._started = False

    def feed(self, chunk) -> None:
        """喂一个 `messages` 流的 message chunk（只处理主层文本增量）。"""
        content = getattr(chunk, "content", "") or ""
        if not isinstance(content, str):
            content = ""
        message_id = getattr(chunk, "id", None)
        if self._started and message_id != self._message_id:
            # 上一条主层消息结束：中间轮次的文本一律丢掉，只留最后一条
            self._text = []
            self._has_tool = False
        self._started = True
        self._message_id = message_id
        if getattr(chunk, "tool_call_chunks", None):
            # 出现工具调用 = 这一条是中间消息，不是给用户的最终回答
            self._has_tool = True
            self._text = []
            return
        if not content or self._has_tool:
            return
        self._text.append(content)

    def finish(self) -> list[str]:
        """流结束：返回最终回答的文本片段（中间轮次/工具消息为空）。"""
        if self._has_tool:
            return []
        return list(self._text)
