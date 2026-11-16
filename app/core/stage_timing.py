"""
检索图阶段耗时采集（进程内、按 session 隔离）

为什么不用 state 字段：五路检索是并行分支，多个节点同时往同一个 state key
写耗时会在 LangGraph 的合并通道里互相覆盖/冲突。这里用一个很小的进程内
字典按 session 记录，图跑完由 `_rag_task` 取快照并清掉。

只做观测，不参与任何业务判断；采集失败也不能影响检索。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

_LOCK = threading.Lock()
_TIMINGS: Dict[str, Dict[str, float]] = {}


def start(session_id: str) -> None:
    """开始一轮采集（同一 session 重入时重置）。"""
    if not session_id:
        return
    with _LOCK:
        _TIMINGS[session_id] = {}


def record(session_id: str, stage: str, started_at: float) -> None:
    """记录一个阶段；stage 用节点名，值单位毫秒。"""
    if not session_id or not stage:
        return
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    with _LOCK:
        bucket = _TIMINGS.setdefault(session_id, {})
        bucket[stage] = round(bucket.get(stage, 0.0) + elapsed_ms, 1)


def snapshot(session_id: str) -> Dict[str, float]:
    """取当前快照（不清理）。"""
    with _LOCK:
        return dict(_TIMINGS.get(session_id) or {})


def clear(session_id: str) -> None:
    with _LOCK:
        _TIMINGS.pop(session_id, None)


def timed(session_id: str, stage: str) -> Tuple[float, None]:
    """给节点用的轻量计时入口：返回 `(开始时间, None)`，配 `record` 使用。"""
    return time.perf_counter(), None
