from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ConverseRequest(_message.Message):
    __slots__ = ("session_id", "message")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    message: str
    def __init__(self, session_id: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class StopRequest(_message.Message):
    __slots__ = ("session_id", "run_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    run_id: str
    def __init__(self, session_id: _Optional[str] = ..., run_id: _Optional[str] = ...) -> None: ...

class StopReply(_message.Message):
    __slots__ = ("stopped",)
    STOPPED_FIELD_NUMBER: _ClassVar[int]
    stopped: bool
    def __init__(self, stopped: _Optional[bool] = ...) -> None: ...

class ListSessionsRequest(_message.Message):
    __slots__ = ("limit",)
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    limit: int
    def __init__(self, limit: _Optional[int] = ...) -> None: ...

class SessionInfo(_message.Message):
    __slots__ = ("session_id", "title", "count", "last_at")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    LAST_AT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    title: str
    count: int
    last_at: str
    def __init__(self, session_id: _Optional[str] = ..., title: _Optional[str] = ..., count: _Optional[int] = ..., last_at: _Optional[str] = ...) -> None: ...

class ListSessionsReply(_message.Message):
    __slots__ = ("sessions",)
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    sessions: _containers.RepeatedCompositeFieldContainer[SessionInfo]
    def __init__(self, sessions: _Optional[_Iterable[_Union[SessionInfo, _Mapping]]] = ...) -> None: ...

class HistoryRequest(_message.Message):
    __slots__ = ("session_id", "limit")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    limit: int
    def __init__(self, session_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class Image(_message.Message):
    __slots__ = ("url", "caption")
    URL_FIELD_NUMBER: _ClassVar[int]
    CAPTION_FIELD_NUMBER: _ClassVar[int]
    url: str
    caption: str
    def __init__(self, url: _Optional[str] = ..., caption: _Optional[str] = ...) -> None: ...

class HistoryMessage(_message.Message):
    __slots__ = ("role", "text", "images", "ts")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    TS_FIELD_NUMBER: _ClassVar[int]
    role: str
    text: str
    images: _containers.RepeatedCompositeFieldContainer[Image]
    ts: str
    def __init__(self, role: _Optional[str] = ..., text: _Optional[str] = ..., images: _Optional[_Iterable[_Union[Image, _Mapping]]] = ..., ts: _Optional[str] = ...) -> None: ...

class HistoryReply(_message.Message):
    __slots__ = ("messages",)
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    messages: _containers.RepeatedCompositeFieldContainer[HistoryMessage]
    def __init__(self, messages: _Optional[_Iterable[_Union[HistoryMessage, _Mapping]]] = ...) -> None: ...

class SessionStart(_message.Message):
    __slots__ = ("session_id", "run_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    run_id: str
    def __init__(self, session_id: _Optional[str] = ..., run_id: _Optional[str] = ...) -> None: ...

class Trace(_message.Message):
    __slots__ = ("kind", "agent", "detail")
    KIND_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    kind: str
    agent: str
    detail: str
    def __init__(self, kind: _Optional[str] = ..., agent: _Optional[str] = ..., detail: _Optional[str] = ...) -> None: ...

class Delta(_message.Message):
    __slots__ = ("delta",)
    DELTA_FIELD_NUMBER: _ClassVar[int]
    delta: str
    def __init__(self, delta: _Optional[str] = ...) -> None: ...

class Usage(_message.Message):
    __slots__ = ("calls", "failed", "total_tokens", "cost", "elapsed_ms", "unpriced_calls")
    CALLS_FIELD_NUMBER: _ClassVar[int]
    FAILED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COST_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    UNPRICED_CALLS_FIELD_NUMBER: _ClassVar[int]
    calls: int
    failed: int
    total_tokens: int
    cost: float
    elapsed_ms: int
    unpriced_calls: int
    def __init__(self, calls: _Optional[int] = ..., failed: _Optional[int] = ..., total_tokens: _Optional[int] = ..., cost: _Optional[float] = ..., elapsed_ms: _Optional[int] = ..., unpriced_calls: _Optional[int] = ...) -> None: ...

class FinalEvent(_message.Message):
    __slots__ = ("answer", "images", "web_only")
    ANSWER_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    WEB_ONLY_FIELD_NUMBER: _ClassVar[int]
    answer: str
    images: _containers.RepeatedCompositeFieldContainer[Image]
    web_only: bool
    def __init__(self, answer: _Optional[str] = ..., images: _Optional[_Iterable[_Union[Image, _Mapping]]] = ..., web_only: _Optional[bool] = ...) -> None: ...

class ErrorEvent(_message.Message):
    __slots__ = ("message",)
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    message: str
    def __init__(self, message: _Optional[str] = ...) -> None: ...

class Event(_message.Message):
    __slots__ = ("start", "trace", "delta", "usage", "final", "error")
    START_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    FINAL_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    start: SessionStart
    trace: Trace
    delta: Delta
    usage: Usage
    final: FinalEvent
    error: ErrorEvent
    def __init__(self, start: _Optional[_Union[SessionStart, _Mapping]] = ..., trace: _Optional[_Union[Trace, _Mapping]] = ..., delta: _Optional[_Union[Delta, _Mapping]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., final: _Optional[_Union[FinalEvent, _Mapping]] = ..., error: _Optional[_Union[ErrorEvent, _Mapping]] = ...) -> None: ...
