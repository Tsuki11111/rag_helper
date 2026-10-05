"""
LangGraph 检查点存储器（MongoDB）

给查询图接上 checkpointer，让图能把中间状态存下来 —— 这是「图因缺信息**主动中断**、
等用户回答后从断点继续」的前置能力（`interrupt()` 离了 checkpointer 根本不生效）。

选 MongoDB 而不是本地文件：项目本来就有 mongo 容器与 `MONGO_DB_NAME`，不新增服务；
`MongoDBSaver` 还自带 TTL，省得自己写清理。

**为什么要降级**：checkpointer 在**关键路径**上（每个节点写完就写一次检查点），
一次问答要写 8 次左右。所以连接失败必须快失败 + 熔断，期间退回内存 saver，
保证服务照常能问答。代价是那段时间没有持久化：中断/续跑不可用，进程重启即丢。
Mongo 恢复后下一次运行会自动换回真 saver（`main_graph.get_query_app` 按 kind 决定要不要重编译）。

**已知覆盖不到的情况**：只在「建 saver 时」判断可用性。若 Mongo 是**跑到一半挂的**，
那一次问答会直接失败（检查点写不进去），由 `note_checkpointer_failure()` 让下一次运行
改用内存 saver —— 不至于一直撞同一堵墙到重启。
"""
import os
import time

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.mongodb import MongoDBSaver
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.core.logger import logger

# 检查点保留时长。state 里带着四路召回回来的切片正文，一次问答约 8 次检查点、几百 KB，
# 所以 TTL 不是可有可无的 —— 不设就会无上限增长
CHECKPOINT_TTL_SECONDS = 7 * 24 * 3600

# Mongo 选不到节点时的等待上限（毫秒）。
# 同 mongo_usage_utils：pymongo 默认 30 秒，而检查点在关键路径上，必须失败快一点
SERVER_SELECTION_TIMEOUT_MS = 2000

# 连接失败后的熔断时长（秒）：期内直接用内存 saver，不再去撞连接超时
CONNECT_FAILURE_COOLDOWN_SEC = 60.0

# 存储器类型：真存储 / 降级的内存版
KIND_MONGO = "mongo"
KIND_MEMORY = "memory"

_saver: BaseCheckpointSaver | None = None
_saver_kind: str | None = None
# 熔断截止时间戳（time.time()）；0 表示没在熔断
_unavailable_until = 0.0


def graph_config(thread_id: str) -> dict:
    """
    图的调用配置

    接上 checkpointer 后，**每次 `invoke` 都必须带 thread_id**，否则 LangGraph 报错。
    本项目把 `thread_id` 定为**一轮问答一个**（直接用现成的 `run_id`），
    这样多轮之间不会串味。
    """
    return {"configurable": {"thread_id": thread_id}}


def _build_mongo_saver() -> BaseCheckpointSaver:
    """建 MongoDB saver（初始化会真的连库并建索引，所以调用方必须处理异常）"""
    url = os.getenv("MONGO_URL")
    db_name = os.getenv("MONGO_DB_NAME")
    if not url or not db_name:
        raise RuntimeError("MONGO_URL / MONGO_DB_NAME 未配置，无法建立检查点存储")

    client = MongoClient(url, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS)
    return MongoDBSaver(client=client, db_name=db_name, ttl=CHECKPOINT_TTL_SECONDS)


def _is_mongo_error(exc: BaseException) -> bool:
    """异常链里有没有 pymongo 的错（判断「是不是库的问题」）"""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, PyMongoError):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def note_checkpointer_failure(exc: BaseException) -> None:
    """
    图运行中检查点写入失败时调用，让**下一次**运行改用内存 saver

    没有这一步的话，Mongo 跑着跑着挂了会让每一轮问答都撞同一堵墙，
    直到进程重启才恢复。
    """
    global _saver, _saver_kind, _unavailable_until
    if not _is_mongo_error(exc):
        return
    _unavailable_until = time.time() + CONNECT_FAILURE_COOLDOWN_SEC
    if _saver_kind == KIND_MONGO:
        _saver = None            # 丢掉坏掉的 saver，下次重建
        _saver_kind = None
        logger.warning(
            f"[checkpointer] 检查点写入失败，已切换：{CONNECT_FAILURE_COOLDOWN_SEC:.0f} 秒内"
            f"改用内存检查点（那段时间的中断/续跑不可用）：{exc}"
        )


def get_checkpointer() -> tuple[BaseCheckpointSaver, str]:
    """
    取检查点存储器，返回 `(saver, kind)`

    `kind` 是 `"mongo"` 或 `"memory"`，调用方靠它判断要不要**重新编译**图
    （降级过之后 Mongo 恢复了，得换回真 saver）。

    内存 saver 只在本进程内有效：重启即失效，也就是说降级那段时间的中断/续跑不可用。
    """
    global _saver, _saver_kind, _unavailable_until

    # 熔断期内：上次刚失败过，直接给内存 saver，不再去撞连接超时
    if _saver_kind == KIND_MEMORY and time.time() < _unavailable_until:
        return _saver, KIND_MEMORY
    # 已经是真 saver 且没被判定失败过：一直用它
    if _saver_kind == KIND_MONGO:
        return _saver, KIND_MONGO

    try:
        _saver = _build_mongo_saver()
        _saver_kind = KIND_MONGO
        _unavailable_until = 0.0
        logger.info(
            f"[checkpointer] 已接入 MongoDB 检查点，TTL {CHECKPOINT_TTL_SECONDS // 86400} 天"
        )
    except Exception as e:
        _unavailable_until = time.time() + CONNECT_FAILURE_COOLDOWN_SEC
        _saver_kind = KIND_MEMORY
        if _saver is None or not isinstance(_saver, InMemorySaver):
            _saver = InMemorySaver()
        logger.warning(
            f"[checkpointer] MongoDB 不可用，降级为内存检查点"
            f"（{CONNECT_FAILURE_COOLDOWN_SEC:.0f} 秒内不再重试）：{e}"
            f"；这段时间内中断/续跑不可用，服务重启即丢"
        )
    return _saver, _saver_kind
