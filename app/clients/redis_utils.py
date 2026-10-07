"""
Redis 连接（任务状态的共享存储）

`app/utils/task_utils.py` 原本把任务进度、结果与暂停标志全放在模块级 dict 里，
也就是**隐含假设只有一个进程**：服务一重启，进行中的导入进度就凭空消失；
多 worker 部署时每个 worker 各记各的。这里提供连接，让它落到共享存储上。

**为什么是 Redis 而不是 Mongo**（项目已经有三处 Mongo 客户端）：
- 暂停标志在流式生成里**每个 chunk 查一次**（`node_answer_output._generate`），
  是热路径，Redis 单次往返比 Mongo 便宜一个量级
- TTL 是原生的，泄漏的 key 自动回收；Mongo 的 TTL 索引有「必须在建集合时带上、
  事后加不了」的坑（见 `mongo_checkpoint_utils`）
- 需要 SET 的原子「加入/移除」语义（四路并发检索写同一个 key，不能读-改-写）

**降级策略**：Redis 不可用时 `task_utils` 退回进程内存实现（与 checkpointer 的
「Mongo 不可用 → 内存 saver」同构），**不让每个节点都炸**。如果哪天多 worker 部署了，
那就退化成「各 worker 各记各的」—— 比整条链路挂掉好。

用法：
    .venv/Scripts/python.exe -m app.clients.redis_utils     # 自测连接与熔断
"""
import os
import time
from typing import Any, Optional

import redis

from app.core.logger import logger

# 连接与读写的超时（秒）。**必须显式给**：redis-py 默认不限，
# 而所有调用都在业务关键路径上（每个节点至少两次），Redis 挂掉时不能把请求拖住 ——
# 同样的快失败理由见 mongo_usage_utils 的 SERVER_SELECTION_TIMEOUT_MS
SOCKET_TIMEOUT_SEC = 2.0

# 连接失败后的熔断时长（秒）：这段时间内不再尝试连接
CONNECT_FAILURE_COOLDOWN_SEC = 60.0

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

_client: Optional["redis.Redis"] = None
# 熔断截止时间戳（time.time()）。0 表示没在熔断
_unavailable_until = 0.0

# Lua：仅当 active 仍是**自己那一轮**时才清掉登记。
#
# 为什么必须原子：这是「读一次、比一次、删一次」的 check-then-act。上一轮的收尾
# （`clear_active_run`，在 `run_query_graph` 的 finally 里）与下一轮的登记
# （`set_active_run`）可能真的交叠 —— 读到 active==run1 之后、DEL 之前，下一轮写进了
# run2，那个 DEL 就把 run2 的登记抹掉，**下一轮的暂停静默失效**。
# 放在进程内存里时这个窗口只有两条字节码（GIL 下几微秒），搬到 Redis 会放大成一个
# 网络往返 —— 所以这不是「保持现状」，是顺手修掉一个既有隐患。
_CLEAR_ACTIVE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('DEL', KEYS[2])
    return 1
end
return 0
"""


class TaskStoreUnavailable(Exception):
    """
    任务状态存储暂不可用（连接失败后的熔断期内）。

    **必须定义在模块级**：loguru 的 sink 开了 `enqueue=True`，日志记录要 pickle 进
    multiprocessing 队列，函数内定义的类 pickle 不了 —— 每打一条带该异常的日志就会
    连炸几个处理器、日志全丢（踩过，见 HANDOFF §3.19）。
    """


# 「连不上」这一类错误。调用方（task_utils）据此决定**降级**；
# 其余 Redis 错误（命令写错、类型不匹配）不上抛不管 —— 那是编程错误，
# 降级只会把它埋掉，与 error_policy 的分级原则一致
REDIS_DOWN_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _build_client() -> "redis.Redis":
    """
    构造客户端。

    `decode_responses=True`：让返回值是 `str` 而不是 `bytes` —— 业务侧存的全是
    节点名、JSON 文本这类字符串，省掉满地 `decode()`。注意因此**不能**用它存二进制。
    """
    url = os.getenv("REDIS_URL") or DEFAULT_REDIS_URL
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=SOCKET_TIMEOUT_SEC,
        socket_timeout=SOCKET_TIMEOUT_SEC,
        # 健康检查：长时间空闲后连接可能已被对端关掉，不加这个就会在下次命令上撞一次
        # 陈旧连接错误、白白触发一次降级
        health_check_interval=30,
    )


def get_redis() -> "redis.Redis":
    """
    获取 Redis 客户端单例（懒加载）

    **失败后熔断 60 秒**：连不上时异常会让单例保持为 None，于是**下一次调用又要等一遍
    连接超时** —— 每个节点都要写进度，Redis 挂掉时整条链路会一步一卡。
    所以失败后这段时间内直接抛 `TaskStoreUnavailable`，让调用方迅速走内存降级。

    这里不做 `ping()` 预检：多一次往返，而真正连不上时第一条命令就会失败、
    同样会触发熔断。**首次失败要多付一次 SOCKET_TIMEOUT_SEC**，这是刻意的取舍。
    """
    global _client, _unavailable_until
    if time.time() < _unavailable_until:
        raise TaskStoreUnavailable("Redis 连接失败后的冷却期内，改用内存存储")
    if _client is None:
        try:
            _client = _build_client()
        except Exception:
            _unavailable_until = time.time() + CONNECT_FAILURE_COOLDOWN_SEC
            raise
    return _client


def note_store_failure(exc: Any) -> None:
    """
    把「某次操作失败其实是 Redis 连不上」告诉本模块，好让**后续**调用立刻走降级。

    与 `mongo_checkpoint_utils.note_checkpointer_failure` 是同一个形状、同一个理由：
    单例一旦建好，之后的失败只会表现为 `redis.ConnectionError`／`TimeoutError`，
    光看异常类型分不清是「这一条命令有问题」还是「整个 Redis 没了」。
    只在确实是连接类错误时置熔断。

    :param exc: 捕获到的异常
    """
    global _unavailable_until
    if isinstance(exc, REDIS_DOWN_ERRORS):
        _unavailable_until = time.time() + CONNECT_FAILURE_COOLDOWN_SEC


def is_available() -> bool:
    """
    Redis 当前是否可用（供探活与自测使用，不做熔断判定）

    带超时的 `ping()`，连不上返回 False 而不抛 —— 调用方（回归套件的探活）需要的是
    一个布尔值，不是异常。
    """
    if time.time() < _unavailable_until:
        return False
    try:
        return bool(_build_client().ping())
    except Exception:
        return False


def clear_active_lua() -> str:
    """暴露给 task_utils 的 CAS 脚本（集中在这里，免得散落在业务模块里）"""
    return _CLEAR_ACTIVE_LUA


def _reset_cooldown() -> None:
    """测试用：清掉熔断状态，让下一次调用重新尝试连接"""
    global _unavailable_until
    _unavailable_until = 0.0


# ---------------------------
# 自测
# ---------------------------
def _check_connection() -> list:
    """Redis 在时能拿到客户端；连不上时 `is_available` 返回 False 且不抛"""
    problems = []
    try:
        if is_available():
            client = get_redis()
            client.set("selftest:redis_utils", "1", ex=30)
            if client.get("selftest:redis_utils") != "1":
                problems.append("写入后读回不是 '1'")
            client.delete("selftest:redis_utils")
        else:
            # 可用性探测本身不该抛 —— 这是它存在的意义
            if time.time() < _unavailable_until:
                pass        # 处于熔断期也算正常，不判问题
    except Exception as e:
        problems.append(f"连接自测失败：{type(e).__name__}: {e}")
    return problems


def _check_unavailable_is_fast() -> list:
    """
    连不上时必须**快速失败**，不能把调用方拖满超时。

    在独立进程里验（见 regression 里那条用例的说明）—— 这里只做静态检查：
    超时常量必须显式设过小值。
    """
    problems = []
    if not (0 < SOCKET_TIMEOUT_SEC <= 5):
        problems.append(f"SOCKET_TIMEOUT_SEC 应为小值，当前 {SOCKET_TIMEOUT_SEC}")
    if not (0 < CONNECT_FAILURE_COOLDOWN_SEC <= 300):
        problems.append(f"CONNECT_FAILURE_COOLDOWN_SEC 不合理，当前 {CONNECT_FAILURE_COOLDOWN_SEC}")
    if "GET" not in clear_active_lua():
        problems.append("CAS 脚本内容不对（拿不到预期脚本）")
    return problems


if __name__ == "__main__":
    for name, fn in (("连接与读写", _check_connection), ("快失败常量与脚本", _check_unavailable_is_fast)):
        errs = fn()
        print(f"[{'FAIL' if errs else 'PASS'}] {name}")
        for e in errs:
            print(f"    - {e}")
