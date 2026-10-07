"""
任务状态：进度 / 结果 / 运行登记（共享存储版）

**这里原本全是模块级 Python dict** —— 也就是隐含假设「只有一个进程」：
服务一重启，进行中的导入进度就凭空消失；多 worker 部署时每个 worker 各记各的。
现在落到 Redis 上（`app/clients/redis_utils.py`），并把「单进程」这个前提从代码里拿掉。

**这次只搬了这个模块，没搬 SSE 队列**（`app/utils/sse_utils.py` 的 `_session_stream`
仍是进程内 dict）。所以：进度**能被持久化、能跨重启读回**，但
`/stream/{session}` 与 `/query` 若被分到不同 worker，前端仍收不到事件 ——
真正的多 worker 还差 SSE 那一笔。

## 对外行为刻意保持不变

所有公开函数签名与语义照旧，约 20 个调用文件一行不用改。只有两处刻意的差异：

1. **读不再有副作用**。以前 `get_*` 会经 `_ensure_task` 顺手把 key 建出来；
   现在读是纯的。这是必须的 —— 导入页每 2 秒轮询一次 `/status`，
   读若也刷 TTL，进度就永远回收不掉，TTL 等于白设。
2. `reset_task_progress` / `clear_task` 由「清空列表」变成「删 key」（Redis 存不住
   空集合），可观测结果一致。**顺带会一并清掉 `web_only`** —— 原来的清理列表漏了它，
   只因收尾处每次都会重设才没出事；现在删整个 result，语义更对。

## 后端与降级

| 数据 | 后端策略 | 为什么 |
|---|---|---|
| 进度 / 结果 / 状态 | Redis 主，内存兜底 | 飞行中 Redis 挂掉再恢复，同一条 task 的前后两段可能落在两个后端，表现为泳道中途「丢几盏灯」。**接受**：丢一条进度只是观感，不值得为它引入双写合并（那会让内存态重新变成读路径的一部分） |
| 运行登记（`active` / `stop`） | **双写 + 并集读** | 失败代价不对称：**丢一次停止是功能失效**（用户按了暂停却停不下来）。内存是本地操作、免费；停止标记按 run_id 记而 run_id 全局唯一，所以并集读不可能假阳性 |

降级不静默：第一次降级打一条 WARNING，恢复后再降级会再打一次（见 `_note_fallback`）。
"""
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from app.clients.redis_utils import (
    REDIS_DOWN_ERRORS,
    TaskStoreUnavailable,
    clear_active_lua,
    get_redis,
    note_store_failure,
)
from app.core.logger import logger
from .sse_utils import push_to_session

# ---------------------------
# 键名与 TTL
# ---------------------------
# 键前缀：`task:{id}:...` 放进度与结果，`active:`/`stop:` 放运行登记。
# 前缀是刻意区分开的 —— 自测与排障时 `KEYS 'task:*'` 一眼能看出是进度数据。
_NS_TASK = "task"
_NS_ACTIVE = "active"
_NS_STOP = "stop"

# task 文档下的字段名（对应改造前那四个 dict）
_F_RUNNING = "running"
_F_DONE = "done"
_F_DEGRADED = "degraded"
_F_RESULT = "result"
_F_STATUS = "status"

# 默认存活时长：1 天。**只在写的时候刷新**（见模块顶部的说明 1）
DEFAULT_TTL_SEC = 86400


def _ttl() -> int:
    """从环境读 TTL（秒）；配错就退回默认值，不让一个笔误把 key 变成永不回收"""
    try:
        return max(60, int(os.getenv("REDIS_KEY_TTL_SEC") or DEFAULT_TTL_SEC))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SEC


def _k(task_id: str, field: str) -> str:
    return f"{_NS_TASK}:{task_id}:{field}"


def _k_active(session_id: str) -> str:
    return f"{_NS_ACTIVE}:{session_id}"


def _k_stop(session_id: str) -> str:
    return f"{_NS_STOP}:{session_id}"


def _task_keys(task_id: str) -> List[str]:
    return [_k(task_id, f) for f in (_F_RUNNING, _F_DONE, _F_DEGRADED, _F_RESULT, _F_STATUS)]


# ---------------------------
# 内存后端（Redis 不可用时的降级实现）
# ---------------------------
# set 用 dict 而不是 set()：dict 保留插入顺序，于是「按完成顺序」这个既有性质
# 在降级路径上也保得住（Redis 的 SET 无序，但前端只用成员关系，见 chat.html 的 Set 用法）
_mem_sets: Dict[str, Dict[str, None]] = {}
_mem_hashes: Dict[str, Dict[str, Any]] = {}
_mem_strs: Dict[str, str] = {}

# 运行登记的**权威副本**：
# - active 内存优先（跨进程时才回落 Redis）。内存优先是为了不被 Redis 里残留的
#   上一轮 active 带偏 —— 那会把迟到的停止信号误判成「当前轮的」，进而误杀正在跑的这一轮
# - stop 并集读（内存 ∪ Redis）：写它的可能是另一个 worker（/stop 被分走了）
_mem_active: Dict[str, str] = {}
_mem_stop: Dict[str, str] = {}

# 降级警告只打一次，恢复后重置（否则每个 chunk 的暂停检查会刷满日志）
_fallback_warned = False


def _note_fallback(what: str) -> None:
    global _fallback_warned
    if not _fallback_warned:
        _fallback_warned = True
        logger.warning(
            f"任务状态存储降级到进程内存（多 worker 下各记各的，重启会丢）：{what}"
        )


def _mark_recovered() -> None:
    """Redis 又能用了 —— 复位告警标志，这样**下一次**降级仍会告警"""
    global _fallback_warned
    _fallback_warned = False


def _redis_or_none(what: str) -> Optional[Any]:
    """取客户端；熔断期内返回 None 并记一次降级（不抛）"""
    try:
        return get_redis()
    except TaskStoreUnavailable as e:
        _note_fallback(f"{what}：{e}")
        return None


def _run(redis_fn, mem_fn, what: str):
    """
    执行一个 Redis 操作，连不上就降级到内存实现。

    **只对「连不上」降级**（连接/超时错误、熔断期）。其余 Redis 错误照旧上抛 ——
    命令写错、类型不匹配是编程错误，降级只会把它埋掉，与 `error_policy` 的分级原则一致。
    """
    client = _redis_or_none(what)
    if client is None:
        return mem_fn()
    try:
        result = redis_fn(client)
        _mark_recovered()
        return result
    except REDIS_DOWN_ERRORS as e:
        note_store_failure(e)
        _note_fallback(f"{what}：{type(e).__name__}: {e}")
        return mem_fn()


# ---------------------------
# 存储原语
# ---------------------------
def _s_add(key: str, member: str) -> None:
    """
    加入集合。

    **必须是单条原子命令 + 刷 TTL，不能「读出来、判断、写回去」**：
    四路并发检索（向量 / HyDE / 联网 / 图谱）会同时写同一个 session_id 的进度，
    读-改-写会把四路里三路丢掉（见 HANDOFF §3.19 的并发用例）。
    TTL 与写入放进同一个事务，避免「写成功了但 TTL 没刷上」留下永不回收的 key。
    """

    def redis_op(c):
        with c.pipeline(transaction=True) as p:
            p.sadd(key, member)
            p.expire(key, _ttl())
            p.execute()

    _run(redis_op, lambda: _mem_sets.setdefault(key, {}).__setitem__(member, None), f"SADD {key}")


def _s_rem(key: str, member: str) -> None:
    """从集合移除（不刷 TTL：移除不是「还有人在用」的信号）"""

    def redis_op(c):
        c.srem(key, member)

    _run(redis_op, lambda: _mem_sets.get(key, {}).pop(member, None), f"SREM {key}")


def _s_members(key: str) -> List[str]:
    """读集合成员（**纯读，不建 key、不刷 TTL**）"""
    return _run(lambda c: c.smembers(key), lambda: list(_mem_sets.get(key, {})), f"SMEMBERS {key}")


def _h_set(key: str, field: str, value: Any) -> None:
    """写结果字段。值 JSON 编码 —— Redis 的 HASH 只能存字符串"""

    def redis_op(c):
        with c.pipeline(transaction=True) as p:
            p.hset(key, field, json.dumps(value, ensure_ascii=False))
            p.expire(key, _ttl())
            p.execute()

    _run(redis_op, lambda: _mem_hashes.setdefault(key, {}).__setitem__(field, value), f"HSET {key}")


def _h_getall(key: str) -> Dict[str, Any]:
    """
    读整个结果 HASH（**纯读**）。

    内存后端存的是**解码后**的值，Redis 后端取回来当场解码 —— 两条路返回同一形状，
    调用方不必知道底下是哪套。
    """
    return _run(
        lambda c: {f: json.loads(v) for f, v in c.hgetall(key).items()},
        lambda: dict(_mem_hashes.get(key, {})),
        f"HGETALL {key}",
    )


def _set_str(key: str, value: str) -> None:
    def redis_op(c):
        c.set(key, value, ex=_ttl())

    _run(redis_op, lambda: _mem_strs.__setitem__(key, value), f"SET {key}")


def _get_str(key: str) -> Optional[str]:
    """读字符串（**纯读**）。缺失返回 None"""
    return _run(lambda c: c.get(key), lambda: _mem_strs.get(key), f"GET {key}")


def _del(*keys: str) -> None:
    """
    删 key。**两个后端都清** —— 删除是清理动作，内存侧顺手清掉可以避免
    「降级期间写的残留」在下次降级时变成幽灵数据；成本是本地 dict 操作，可忽略
    """
    if not keys:
        return
    for k in keys:
        _mem_sets.pop(k, None)
        _mem_hashes.pop(k, None)
        _mem_strs.pop(k, None)

    def redis_op(c):
        c.delete(*keys)

    _run(redis_op, lambda: None, f"DEL {keys}")


# ---------------------------
# 节点名映射与状态常量
# ---------------------------
# 节点名 -> 中文名映射（用于前端展示）
# 说明：这里的 key 应与 LangGraph 的 add_node("xxx", ...) 中的节点名一致。
_NODE_NAME_TO_CN: Dict[str, str] = {
    "upload_file": "开始上传文件",
    "node_entry": "检查文件",
    "node_pdf_to_md": "PDF转Markdown",
    "node_md_img": "Markdown图片处理",
    "node_item_name_recognition": "主体名称识别",
    "node_document_split": "文档切分",
    "node_bge_embedding": "向量生成",
    "node_dashscope_embedding": "向量生成",
    "node_import_kg": "导入知识图谱",
    "node_import_milvus": "导入向量库",
    "__end__": "处理完成",
    "END": "处理完成",
    # --- Query 流程节点（kb/query_process/main_graph.py）---
    "node_item_name_confirm": "确认问题产品",
    "node_answer_output": "生成答案",
    "node_rerank": "重排序",
    "node_rrf": "倒排融合",
    "node_web_search_mcp": "网络搜索",
    "node_search_embedding": "切片搜索",
    "node_search_embedding_hyde": "切片搜索(假设性文档)",
    "node_multi_search": "多路搜索",
    "node_query_kg": "查询知识图谱",
    "node_join": "多路搜索合并",
    "node_ask_user": "等待用户确认",
}

TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
# 本轮被用户主动暂停（生成作废、已进入询问，本轮结束）
TASK_STATUS_PAUSED = "paused"
# 本轮挂起、在等用户确认产品（图主动中断，状态留在检查点里）
TASK_STATUS_WAITING_USER = "waiting_user"


def _to_cn(node_name: str) -> str:
    """将节点名转换为中文展示名；若无映射则返回原名。"""
    return _NODE_NAME_TO_CN.get(node_name, node_name)


# ---------------------------
# 进度：运行 / 完成 / 降级
# ---------------------------
def add_running_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """
    添加“正在运行”的节点任务。
    """
    _s_add(_k(task_id, _F_RUNNING), node_name)
    if is_stream:
        task_push_queue(task_id)


def add_done_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """
    添加“已完成”的节点任务，并把它从“正在运行”里摘掉。

    注意：改造前这里维护完成顺序（列表 append）。现在两套后端都不保证顺序了 ——
    《前端只用成员关系》。`chat.html` 把 `done_list` 转成 `Set` 后 `has()` 判断，
    `import.html` 用 `includes`，两边都不读数组顺序，所以这个性质没有消费者。
    """
    _s_rem(_k(task_id, _F_RUNNING), node_name)
    _s_add(_k(task_id, _F_DONE), node_name)
    if is_stream:
        task_push_queue(task_id)


def add_degraded_task(task_id: str, node_name: str) -> None:
    """
    记一个「降级过」的节点（重复调用无副作用）

    由 `error_policy` 的两条降级路径调用（异常降级 / 前置检查降级）。
    这里**不推送**事件：让节点结束时那次进度推送自然把它带上，少一条噪音。
    """
    if not task_id or not node_name:
        return          # 没有任务上下文（命令行单跑节点）就不记
    _s_add(_k(task_id, _F_DEGRADED), node_name)


def get_done_task_list(task_id: str) -> List[str]:
    """获取已完成节点列表（中文展示）。未知 task_id 返回空列表，且**不建 key**"""
    return [_to_cn(n) for n in _s_members(_k(task_id, _F_DONE))]


def get_running_task_list(task_id: str) -> List[str]:
    """获取正在运行节点列表（中文展示）。未知 task_id 返回空列表，且**不建 key**"""
    return [_to_cn(n) for n in _s_members(_k(task_id, _F_RUNNING))]


def get_degraded_task_list(task_id: str) -> List[str]:
    """获取降级过的节点列表（中文展示）。未知 task_id 返回空列表，且**不建 key**"""
    return [_to_cn(n) for n in _s_members(_k(task_id, _F_DEGRADED))]


# ---------------------------
# 结果字段与状态
# ---------------------------
def set_task_result(task_id: str, key: str, value: Any) -> None:
    """
    存储任务结果字段（如 answer / images / need_confirm / run_error）。

    值会被 JSON 编码后落库，所以**写入前必须确保可序列化**：Mongo 文档、numpy 标量
    都不行（`clarify` 里的 `score` 已经在 `node_item_name_confirm` 里 `float()` 过，
    正是为此；同类的坑见 HANDOFF §4.15）。
    """
    _h_set(_k(task_id, _F_RESULT), key, value)


def get_task_result(task_id: str, key: str, default: Any = "") -> Any:
    """
    获取任务结果字段。

    注意 `default` 的默认值就是空串，与改造前一致 —— 调用方按需传 `[]` / `False` / `{}`。
    **判断「有没有值」要用 `is None` / 显式比较，不要用真值判断**：`answer=""` 是合法值。
    """
    return _h_getall(_k(task_id, _F_RESULT)).get(key, default)


def get_task_status(task_id: str) -> str:
    """
    获取当前任务状态；未设置过返回空字符串
    """
    return _get_str(_k(task_id, _F_STATUS)) or ""


def update_task_status(task_id: str, status_name: str, push_queue: bool = False) -> None:
    """
    更新任务状态。
    """
    _set_str(_k(task_id, _F_STATUS), status_name)
    if push_queue:
        task_push_queue(task_id)


def reset_task_progress(task_id: str) -> None:
    """
    重置**本轮**的进度记录（done / running / degraded）与结果字段

    **为什么必须清**：这些记录按 `session_id` 存，而一个会话有很多轮。不清的话上一轮的
    记录会带到下一轮 —— 实测踩过两次：
    - 上一轮 Neo4j 停着、图谱那一路降级；这一轮 Neo4j 恢复了，泳道**仍然**把它标成降级
    - 上一轮跑全了 8 个节点、这一轮短路只跑了 2 个，泳道会把 8 个都点亮

    在**新开一轮**时调（`run_query_graph` 里 `resume is None` 时）；
    **续跑不要调** —— 恢复的那一段要接着前一段累积。

    **这里是整体删掉 result，而不是逐个 pop 字段**：改造前的清理列表漏了 `web_only`，
    上一轮的 `web_only=True` 能渗进一次「预算中止轮」的非流式响应里（那条路不会重设它）。
    删整个 HASH 顺手把这个洞堵上，语义也更直白。
    """
    _del(
        _k(task_id, _F_RUNNING),
        _k(task_id, _F_DONE),
        _k(task_id, _F_DEGRADED),
        _k(task_id, _F_RESULT),
    )


def clear_running_task_list(task_id: str) -> None:
    """
    只清「正在运行」，保留 done 与 degraded

    给**导入的续跑**用：服务重启后接着跑的那个新进程里，上一个进程留下的 `running`
    成员是幽灵（那个节点已经不在跑了），而 `done` 恰恰是要保留的 ——
    「重启后能看见已完成到哪一站」正是本次搬家的收益。
    """
    _del(_k(task_id, _F_RUNNING))


def clear_task(task_id: str):
    """清掉某个任务的全部状态（进度、结果、状态、运行登记）"""
    _del(*_task_keys(task_id), _k_active(task_id), _k_stop(task_id))
    _mem_active.pop(task_id, None)
    _mem_stop.pop(task_id, None)


def task_push_queue(task_id: str):
    push_to_session(task_id, "progress", {
        "status": get_task_status(task_id),
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
        # 降级过的节点也推给前端 —— 否则「返回空」和「正常取到」在泳道上分不出来
        "degraded_list": get_degraded_task_list(task_id),
    })


# ---------------------------
# 轮次登记与「用户主动暂停」
# ---------------------------
def _active_for(session_id: str) -> Optional[str]:
    """
    当前这一轮的 run_id：**内存优先，Redis 兜底**

    内存优先不是图快，是为了正确性：Redis 里的 active 可能因为中途降级而落后
    （比如 `set_active_run` 那会儿 Redis 正挂着），若一律以 Redis 为准，
    迟到的停止信号会被误判成「当前轮的」，**误杀正在跑的那一轮**。
    跨进程（多 worker）时本进程内存里没有这个会话，才回落到 Redis。
    """
    v = _mem_active.get(session_id)
    if v:
        return v
    return _get_str(_k_active(session_id))


def set_active_run(session_id: str, run_id: str) -> None:
    """登记该会话当前正在跑的轮次（run_id 同时用作日志 trace 与暂停令牌）"""
    _mem_active[session_id] = run_id
    _mem_stop.pop(session_id, None)

    def redis_op(c):
        # 换轮时一并清掉旧的停止标记：标记按 run_id 记，旧标记对新轮本来也无效，
        # 但留着会让 `is_stop_requested` 每轮多一次命中判断，清掉更干净
        with c.pipeline(transaction=True) as p:
            p.set(_k_active(session_id), run_id, ex=_ttl())
            p.delete(_k_stop(session_id))
            p.execute()

    _run(redis_op, lambda: None, f"SET {_k_active(session_id)}")


def clear_active_run(session_id: str, run_id: str = None) -> None:
    """
    一轮结束时清理登记。

    :param run_id: 传了的话，只在该轮仍是当前登记时才清 —— 防止上一轮迟到的收尾
        把下一轮刚登记好的 run_id 抹掉
    """
    if run_id is None or _mem_active.get(session_id) == run_id:
        _mem_active.pop(session_id, None)
        _mem_stop.pop(session_id, None)

    if run_id is None:
        # 与改造前一致：不带 run_id 就是无条件清
        _del(_k_active(session_id), _k_stop(session_id))
        return

    # **必须是单条 Lua（比较 + 删除原子完成），不能「读一次、比一次、删一次」**：
    # 本轮收尾与下一轮登记会真的交叠 —— 读到 active==run1 之后、DEL 之前，
    # 下一轮写进了 run2，那个 DEL 就把 run2 的登记抹掉，**下一轮的暂停静默失效**。
    # 放在进程内存里时这个窗口只有两条字节码，搬到 Redis 会放大成一个网络往返。
    _run(
        lambda c: c.eval(
            clear_active_lua(), 2, _k_active(session_id), _k_stop(session_id), run_id
        ),
        lambda: None,
        f"CAS clear {_k_active(session_id)}",
    )


def request_stop(session_id: str, run_id: str) -> bool:
    """
    请求暂停当前这一轮。

    **必须带上 run_id 且与当前登记一致才生效**：前端的 session_id 是跨轮复用的，
    用户点慢了、或网络延迟导致上一轮的暂停信号在下一轮开跑之后才打到，
    按 session 置位就会误杀新一轮。

    :return: 是否真的置位；False 表示这一轮已经结束了，信号作废
    """
    if _active_for(session_id) != run_id:
        return False
    _mem_stop[session_id] = run_id

    def redis_op(c):
        # **同时把 active 写正**：Redis 里的 active 可能因为中途降级而落后于内存
        # （`set_active_run` 那会儿 Redis 正挂着）。只写 stop 的话，
        # `is_stop_requested` 会读到「active 是旧轮、stop 是新轮」而判定不相等，
        # 暂停就静默失效了。一并写正，两个后端重新对齐。
        with c.pipeline(transaction=True) as p:
            p.set(_k_active(session_id), run_id, ex=_ttl())
            p.set(_k_stop(session_id), run_id, ex=_ttl())
            p.execute()

    _run(redis_op, lambda: None, f"SET {_k_stop(session_id)}")
    return True


def is_stop_requested(session_id: str) -> bool:
    """
    本轮是否被请求暂停。

    节点只按 session_id 问，不必知道 run_id —— 登记表里查得到当前轮次。
    从未登记过的会话（例如单节点自测直接调用节点函数）一律返回 False。

    **热路径**：`node_answer_output` 在流式循环里**每个 chunk 调一次**。
    正常路径下 `_active_for` 命中内存、只剩下面那一次 GET 落 Redis
    （本地约 0.1~0.2 ms，几百个 chunk 合计几十毫秒）。特意不加本地缓存 ——
    缓存会把「点了暂停到真的停下」拖成缓存 TTL 那么久。
    """
    run_id = _active_for(session_id)
    if not run_id:
        return False
    if _mem_stop.get(session_id) == run_id:
        return True
    # 停止标记可能是**另一个 worker** 写的（/stop 请求被分走了），所以必须问 Redis。
    # 并集读在「按 run_id 记标记」下不可能假阳性：run_id 全局唯一
    return _get_str(_k_stop(session_id)) == run_id


# ---------------------------
# 自测（`_check_xxx() -> list[str]`，空列表即通过）
# ---------------------------
# 自测用的 task_id/session_id 前缀：带前缀才敢在 finally 里按 id 清干净
_SELF_PREFIX = "selftest_taskutils"


def _self_id(tag: str) -> str:
    return f"{_SELF_PREFIX}_{tag}"


def _check_run_isolation() -> list:
    """
    暂停的 run_id 隔离：换轮之后，上一轮的停止信号必须作废

    守的是 §3.9 那条返工过的语义 —— 前端的 session_id 跨轮复用，
    只按 session 置位会让迟到的暂停误杀下一轮。
    """
    problems = []
    sid = _self_id("isolation")
    try:
        set_active_run(sid, "run1")
        if request_stop(sid, "run1") is not True:
            problems.append("当前轮的停止请求竟然没置位")
        if is_stop_requested(sid) is not True:
            problems.append("置位了却读不到")

        # 换轮：新的 run_id，旧的停止标记必须失效
        set_active_run(sid, "run2")
        if is_stop_requested(sid) is not False:
            problems.append("换轮后仍报告暂停中（会误杀新一轮）")
        if request_stop(sid, "run1") is not False:
            problems.append("上一轮的迟到停止请求被接受了")

        # 上一轮迟到的收尾不能抹掉新一轮的登记
        clear_active_run(sid, "run1")
        if _active_for(sid) != "run2":
            problems.append("上一轮迟到的 clear 把新一轮的登记抹掉了")

        # 真正的收尾：带上正确 run_id 才清得掉
        clear_active_run(sid, "run2")
        if _active_for(sid) is not None:
            problems.append("带正确 run_id 的收尾没能清掉登记")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(sid)
    return problems


def _check_clear_active_is_atomic() -> list:
    """
    `clear_active_run(run_id)` 必须**一条 Redis 命令**完成「比较 + 删除」

    这是防 TOCTOU 的护栏：如果被改回「先 GET 再 DEL」两条独立命令，
    上一轮的收尾就能在两条命令之间把下一轮的登记删掉（→ 下一轮暂停静默失效）。
    代理里记下这次调用发出的命令名，断言只有一条 `eval`。

    依赖 Redis（没有它就观察不到命令）—— 所以 regression 里标 `redis` 依赖，
    不可用时整条 SKIP 而不是 FAIL。
    """
    problems = []
    sid = _self_id("atomic")
    recorded: List[str] = []
    # **改当前模块的 globals，不要 `import app.utils.task_utils as tu` 再改 tu 的属性**：
    # `python -m` 会把本文件当 `__main__` 执行，那个 import 会再加载出**第二个模块对象**，
    # 改的是另一份 globals，打桩等于没打（这里踩过）。同样的写法见 node_answer_output 自测。
    g = globals()
    real_get_redis = g["get_redis"]

    class _Spy:
        """把命令名记下来再转发，不改行为"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            def wrapper(*args, **kwargs):
                recorded.append(name)
                return getattr(self._inner, name)(*args, **kwargs)

            return wrapper

    try:
        set_active_run(sid, "run1")
        recorded.clear()
        g["get_redis"] = lambda: _Spy(real_get_redis())
        clear_active_run(sid, "run1")
    except TaskStoreUnavailable:
        return []           # 熔断期内观察不到命令，交给回归套件的依赖探活去判
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        g["get_redis"] = real_get_redis
        clear_task(sid)

    if not problems:
        if recorded != ["eval"]:
            problems.append(
                f"清登记发出了 {recorded}，应只有一条 'eval'（两条独立命令之间存在 TOCTOU）"
            )
    return problems


def _check_parallel_writes() -> list:
    """
    四路并发写同一个 task_id：会崩溃吗？最终状态对吗？

    对应检索图里四路并发（向量 / HyDE / 联网 / 图谱）都往同一个 session_id 写进度。

    ⚠️ **这条抓不住「读-改-写」**：实测把 `add_done_task` 换成
    「SMEMBERS → 过滤 → 重建集合」之后，本条**三次全绿** —— 四个线程因为启动开销
    天然错开，靠调度碰运气复现不了丢更新。真正锁住那条不变量的是
    `_check_no_read_modify_write`（确定性地断言写入路径上不发读命令）。
    这里留着，是为了覆盖并发下的崩溃与最终状态。
    """
    import threading

    problems = []
    tid = _self_id("parallel")
    nodes = ["node_search_embedding", "node_search_embedding_hyde",
             "node_web_search_mcp", "node_query_kg"]
    errors: List[str] = []

    def worker(node: str):
        try:
            add_running_task(tid, node)
            add_done_task(tid, node)
        except Exception as e:          # 线程里的异常不会让主线程知道，自己收着
            errors.append(f"{node}: {type(e).__name__}: {e}")

    try:
        threads = [threading.Thread(target=worker, args=(n,)) for n in nodes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            problems.extend(errors)
        done = set(get_done_task_list(tid))
        expect = {_to_cn(n) for n in nodes}
        if done != expect:
            problems.append(f"完成的节点应为 {sorted(expect)}，实际 {sorted(done)}（并发丢了更新）")
        if get_running_task_list(tid):
            problems.append(f"完成后仍留着运行中的节点：{get_running_task_list(tid)}")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
    return problems


def _check_no_read_modify_write() -> list:
    """
    写进度**不能是「读-改-写」**—— 写入路径上一条读命令都不该发

    这才是真正锁住并发不丢更新的用例。共享存储上四路并发写同一个 key，
    只要「先读出来、再写回去」，中间夹进来的那次写入就会丢。
    靠线程调度复现不了（`_check_parallel_writes` 在错误实现上三次全绿），
    所以改成**确定性地**看行为：打桩记下调用期间发出的命令，
    断言里面没有任何读命令（`SMEMBERS` / `GET` / `HGETALL` …）。

    依赖 Redis（没有它就观察不到命令）—— regression 里标 `redis`，不可用时 SKIP。
    """
    problems = []
    tid = _self_id("rmw")
    recorded: List[str] = []
    g = globals()
    real_get_redis = g["get_redis"]

    class _Spy:
        """只记顶层命令名（pipeline 内部的方法不走这里，够用了）"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            def wrapper(*args, **kwargs):
                recorded.append(name)
                return getattr(self._inner, name)(*args, **kwargs)

            return wrapper

    # 任何读命令出现都算违规 —— 这些名字来自 redis-py 的 Redis 类
    read_cmds = {"smembers", "get", "mget", "hgetall", "hget", "hmget", "exists", "scard", "sismember"}
    try:
        set_active_run(f"{tid}_guard", "r1")     # 先把 Redis 打通，别让连接失败混进来
        clear_active_run(f"{tid}_guard", "r1")
        g["get_redis"] = lambda: _Spy(real_get_redis())

        recorded.clear()
        add_running_task(tid, "node_rrf")
        bad = set(recorded) & read_cmds
        if bad:
            problems.append(f"add_running_task 发了读命令 {sorted(bad)}（读-改-写在并发下会丢更新）")

        recorded.clear()
        add_done_task(tid, "node_rrf")
        bad = set(recorded) & read_cmds
        if bad:
            problems.append(f"add_done_task 发了读命令 {sorted(bad)}（读-改-写在并发下会丢更新）")
    except TaskStoreUnavailable:
        return []
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        g["get_redis"] = real_get_redis
        clear_task(tid)
    return problems


def _check_result_roundtrip() -> list:
    """
    结果字段往返后**类型不变**

    值被 JSON 编码后落库，所以逐字段验一遍：`images` 是 list[dict]、`need_confirm`
    是 bool、`clarify` 里的 `score` 是 float。顺带守住 `answer=""` 不被当成缺省
    （判缺失要用 `is None`，不能用真值判断 —— 这是 `_build_history` 那类静默 bug 的同款）。
    """
    problems = []
    tid = _self_id("result")
    try:
        set_task_result(tid, "answer", "")
        set_task_result(tid, "images", [{"url": "http://x/a.png", "caption": "图注"}])
        set_task_result(tid, "web_only", True)
        set_task_result(tid, "need_confirm", False)
        set_task_result(tid, "clarify", {
            "question": "是哪一个？",
            "options": [{"item_name": "万用表RS-12", "file_title": "a.pdf",
                         "score": 0.8834, "near": True}],
            "allow_custom": True,
        })

        if get_task_result(tid, "answer") != "":
            problems.append("空字符串答案没有原样读回（真值判断会把它当成缺省）")
        images = get_task_result(tid, "images")
        if images != [{"url": "http://x/a.png", "caption": "图注"}]:
            problems.append(f"images 往返不一致：{images!r}")
        if get_task_result(tid, "web_only") is not True:
            problems.append("web_only 往返后不是 True")
        if get_task_result(tid, "need_confirm") is not False:
            problems.append("need_confirm 往返后不是 False（缺省判断被混淆了）")

        clarify = get_task_result(tid, "clarify")
        score = (((clarify or {}).get("options") or [{}])[0]).get("score")
        if not isinstance(score, float) or abs(score - 0.8834) > 1e-9:
            problems.append(f"clarify 里的 score 往返后类型/值变了：{score!r}")
        if (clarify or {}).get("allow_custom") is not True:
            problems.append("clarify.allow_custom 往返后不是 True")

        # 缺省值必须原样返回调用方给的那个（调用方会传 [] / False / {}）
        sentinel = object()
        if get_task_result(tid, "没有这个字段", sentinel) is not sentinel:
            problems.append("缺失字段没有返回调用方给的缺省值")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
    return problems


def _check_reset_clears_result() -> list:
    """新开一轮时，结果字段（含 `web_only`）必须全部清掉"""
    problems = []
    tid = _self_id("reset")
    try:
        add_done_task(tid, "node_rrf")
        add_degraded_task(tid, "node_query_kg")
        add_running_task(tid, "node_rerank")
        for k, v in (("answer", "上一轮的答案"), ("images", [{"url": "u"}]),
                     ("web_only", True), ("need_confirm", True),
                     ("clarify", {"question": "q"}), ("run_error", "e")):
            set_task_result(tid, k, v)

        reset_task_progress(tid)

        if get_done_task_list(tid) or get_running_task_list(tid) or get_degraded_task_list(tid):
            problems.append("进度没清干净")
        for k, default in (("answer", ""), ("images", []), ("need_confirm", False),
                           ("clarify", {}), ("run_error", "")):
            if get_task_result(tid, k, default) != default:
                problems.append(f"结果字段 {k} 没被清掉（上一轮的会渗进这一轮）")
        # 改造前的清理列表漏了 web_only，这一条专门守它
        if get_task_result(tid, "web_only", False) is not False:
            problems.append("web_only 没被清掉（上一轮的纯联网横幅会渗进这一轮）")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
    return problems


def _check_unknown_getters_are_pure() -> list:
    """
    读未知 task_id 不能建 key、也不能刷 TTL

    导入页每 2 秒轮询一次 `/status`，读若顺手把 key 建出来或刷新存活时间，
    进度就**永远回收不掉**，TTL 等于白设。所以读必须是纯的。

    TTL 那部分要**先把 TTL 压成一个短值再读**：直接拿默认值前后对比是分辨不出来的
    —— Redis 的 TTL 精度是秒，而写入与读取发生在同一秒内，`after > before` 永远为假
    （第一版就是这么写的，对错误实现照样全绿）。
    """
    problems = []
    tid = _self_id("unknown")
    probes = [
        (_self_id("ttl_done"), _k(_self_id("ttl_done"), _F_DONE),
         lambda i: add_done_task(i, "node_rrf"), lambda i: get_done_task_list(i)),
        (_self_id("ttl_result"), _k(_self_id("ttl_result"), _F_RESULT),
         lambda i: set_task_result(i, "answer", "a"), lambda i: get_task_result(i, "answer")),
        (_self_id("ttl_status"), _k(_self_id("ttl_status"), _F_STATUS),
         lambda i: update_task_status(i, TASK_STATUS_PROCESSING), lambda i: get_task_status(i)),
    ]
    try:
        if get_done_task_list(tid) or get_running_task_list(tid) or get_degraded_task_list(tid):
            problems.append("未知 task_id 读出了非空进度")
        if get_task_result(tid, "answer", "") != "":
            problems.append("未知 task_id 读出了结果字段")
        if get_task_status(tid) != "":
            problems.append("未知 task_id 读出了状态")

        client = get_redis()
        if client.exists(_k(tid, _F_DONE)) or client.exists(_k(tid, _F_RESULT)):
            problems.append("读未知 task_id 竟然把 key 建出来了")

        # 有内容的 key：把 TTL 压到 30 秒，再读几次 —— 纯读应当让它继续倒数
        for tid2, key, write, read in probes:
            write(tid2)
            client.expire(key, 30)
            before = client.ttl(key)
            for _ in range(3):
                read(tid2)
            after = client.ttl(key)
            if after > before:
                problems.append(
                    f"读操作刷新了 TTL（{key}：{before}s → {after}s），进度将永远回收不掉"
                )
            if not client.exists(key):
                problems.append(f"{key} 读完之后不见了")
    except TaskStoreUnavailable as e:
        problems.append(f"Redis 不可用，本用例只在有 Redis 时有意义：{e}")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
        for tid2, _key, _w, _r in probes:
            clear_task(tid2)
    return problems


def _check_backend_fallback() -> list:
    """
    Redis 连不上时必须降级到进程内存：API 可用、不抛、暂停照旧生效（同进程内）

    **必须在子进程里跑**：把 REDIS_URL 指向死端口会写进 `redis_utils` 的模块级
    熔断状态，在本进程里跑就会污染后面所有用例（它们会跟着一起降级）。
    照 §3.12 的做法，用环境变量在独立进程里验。
    """
    code = (
        "import os\n"
        "os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'\n"   # 没有服务在听
        "from app.utils.task_utils import (\n"
        "    add_running_task, add_done_task, get_done_task_list, get_running_task_list,\n"
        "    set_task_result, get_task_result, set_active_run, request_stop,\n"
        "    is_stop_requested, clear_task)\n"
        "tid = 'selftest_fallback'\n"
        "add_running_task(tid, 'node_rrf')\n"
        "add_done_task(tid, 'node_rrf')\n"
        "assert get_done_task_list(tid) == ['倒排融合'], get_done_task_list(tid)\n"
        "assert get_running_task_list(tid) == [], get_running_task_list(tid)\n"
        "set_task_result(tid, 'images', [{'url': 'u', 'caption': 'c'}])\n"
        "assert get_task_result(tid, 'images') == [{'url': 'u', 'caption': 'c'}]\n"
        "set_active_run('s1', 'r1')\n"
        "assert request_stop('s1', 'r1') is True\n"
        "assert is_stop_requested('s1') is True\n"
        "assert request_stop('s1', 'r0') is False\n"
        "clear_task(tid)\n"
        "print('OK')\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        # encoding 显式给 utf-8：本机默认是 GBK，子进程打的中文会解码失败 ——
        # 那会让 r.stdout 变成 None，把真实结论盖掉（踩过）
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        return [f"子进程起不来：{type(e).__name__}: {e}"]
    if r.returncode != 0 or "OK" not in r.stdout:
        return [f"Redis 不可用时没能降级到内存（退出码 {r.returncode}）："
                f"{(r.stdout or '')[-300:]}{(r.stderr or '')[-500:]}"]
    return []


if __name__ == "__main__":
    checks = [
        ("暂停的 run_id 隔离", _check_run_isolation),
        ("清登记是单条原子命令", _check_clear_active_is_atomic),
        ("四路并发不丢更新", _check_parallel_writes),
        ("写进度不是读-改-写", _check_no_read_modify_write),
        ("结果字段 JSON 往返", _check_result_roundtrip),
        ("重置清掉全部结果字段", _check_reset_clears_result),
        ("读未知 task_id 无副作用", _check_unknown_getters_are_pure),
        ("Redis 挂掉时降级到内存", _check_backend_fallback),
    ]
    n_fail = 0
    for name, fn in checks:
        errs = fn()
        n_fail += 1 if errs else 0
        print(f"[{'FAIL' if errs else 'PASS'}] {name}")
        for e in errs:
            print(f"    - {e}")
    print(f"\n{len(checks) - n_fail}/{len(checks)} 通过")
