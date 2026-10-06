"""
异常分级策略

项目里有十几处「某一路失败就降级、链路继续」的位置（四路召回、图谱、联网、重排……）。
它们此前一律写成 `except Exception: 记日志 + 返回空`，**把编程错误和外部故障混为一谈**：

> 实现 `node_query_kg` 时一个 `NameError` 被「失败不中断链路」的兜底 `except` 降级成 warning，
> 图谱那一路静默返回空、功能等于废了，只有测试断言才发现。

所以这里先把异常**分类**，再按类决定处置方式：

| 分类 | 什么情况 | 怎么处置 |
|---|---|---|
| `FATAL` | 代码自身不一致：`NameError` / `UnboundLocalError` / `ImportError` / `NotImplementedError` / `AssertionError` / `SyntaxError` / `IndentationError` / `RecursionError` | **上抛**。这类问题不能降级掩盖——降级只会让功能静默失效 |
| `REJECTED` | 模型服务的内容审核拒绝（DashScope 的 `data_inspection_failed`） | 降级 + error。**另有一条人话直达用户**，见 `CONTENT_REJECTED_ANSWER` |
| `RETRYABLE` | 暂时性外部故障：超时、连接失败、429、5xx、`ServerSelectionTimeoutError` | 降级 + warning。**Phase 2 的重试接在这里** |
| `BLOCKED` | 重试无用且要人处理：4xx（鉴权/参数）、配置缺失 | 降级 + error（带堆栈） |
| `UNEXPECTED` | 兜底：外部依赖的其它异常、数据结构不符预期 | 降级 + error（带堆栈） |

**为什么 `TypeError` / `KeyError` / `AttributeError` / `IndexError` 不算 FATAL**：
它们既能由我们写错引起，也能由外部返回的数据变形引起（SDK 改结构、接口少字段），
在降级点上分不清。**归类为 `UNEXPECTED`**：仍然降级（保住"一路坏不影响整条链路"的设计），
但记 error 级 + 完整堆栈 + `degraded=true` 标记，可以在日志里查出来——
"静默"才是当初真正的问题，"降级"本身不是。

**为什么暂停在分类、不做重试**：README 的路线把「故障分类重试」放在 Phase 2。
这里只负责把类型分清、处置分明，重试策略等有了稳定的错误分布数据再加——
现在连"哪类错误出现过几次"都还统计不了。
"""
from enum import Enum
from typing import Any

from app.core.logger import logger

NODE_NAME = "error_policy"


class ErrorKind(str, Enum):
    """异常分类。继承 str 便于直接写进结构化日志的字段"""

    FATAL = "fatal"            # 代码自身不一致 → 上抛
    REJECTED = "rejected"      # 模型服务拒答（内容审核）→ 降级，但要给用户一句人话
    RETRYABLE = "retryable"    # 暂时性外部故障 → 降级，Phase 2 在此接重试
    BLOCKED = "blocked"        # 重试无用且需人处理 → 降级 + error
    UNEXPECTED = "unexpected"  # 兜底 → 降级 + error


# 由解释器抛出的、**只可能反映我们自己代码有问题**的异常类型。
# 这类异常与外部数据无关，重试和降级都没有意义——降级只会让它潜伏下来。
FATAL_TYPES = (
    NameError,
    UnboundLocalError,
    ImportError,
    NotImplementedError,
    AssertionError,
    SyntaxError,
    IndentationError,
    RecursionError,
)

# 名字里带这些词的异常按「暂时性」处理（外部服务抖动、网络问题）。
# 用名字而不是 isinstance：这些类型散落在 openai / httpx / pymongo / neo4j / milvus 里，
# 逐个 import 会把这些库变成硬依赖，而它们本就都是可选的外部服务。
RETRYABLE_HINTS = (
    "timeout", "timed out", "connection", "connect", "unavailable",
    "reconnect", "temporarily", "rate limit", "too many requests", "try again",
)

# 可重试的 HTTP 状态码：408 请求超时、425 过早、429 限流、5xx 服务端问题
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# 模型服务「内容审核拒绝」的错误码。DashScope 走 OpenAI 兼容协议，报错体形如
# `{'error': {'code': 'data_inspection_failed', 'type': 'data_inspection_failed', …}}`。
# 换模型服务商时在这里加码 —— 判断依据只有这一个地方，别散到各节点去。
CONTENT_REJECT_CODES = ("data_inspection_failed",)

# 内容审核拒绝时给用户看的那句话。
# **为什么放在这里**：识别与文案是同一件事的两面，而两个会撞上它的节点
# （node_item_name_confirm 的提取、node_answer_output 的生成）本来就都 import 本模块，
# 放这儿比让它们互相 import、或者各写一份文案要干净。
CONTENT_REJECTED_ANSWER = (
    "这个问题被模型服务的内容审核拦下了，没能检索知识库、也没能生成回答。\n\n"
    "换一种问法，或者改问产品文档里的具体内容再试一次。"
)


def is_content_rejected(exc: BaseException) -> bool:
    """
    是不是「模型服务的内容审核拒绝」

    为什么单独拎出来：它**不是故障** —— 依赖都好、配置也对、重试多少遍结果都一样，
    是模型服务按合规要求拒答。但它也**不该被当成普通的降级悄悄咽掉**：
    链路会一路降级到空答案（提取被拒 → 拿原问题去检索 → 生成节点再被拒一次），
    用户看到的是一顿空白，且完全不知道原因是「被审核了」。

    所以两个调用方（两个节点）看到它就直接短路给一句人话。

    判断依据优先读 `exc.body`（openai 系把解析后的错误体挂在这里，最可靠），
    取不到再退回在 `str(exc)` 里找 —— 有些包装过的异常只留下文本。
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = f"{err.get('code') or ''} {err.get('type') or ''}"
            if any(c in code for c in CONTENT_REJECT_CODES):
                return True

    text = str(exc)
    return any(c in text for c in CONTENT_REJECT_CODES)


def _http_status(exc: BaseException) -> int:
    """
    取 HTTP 状态码，取不到返回 0

    只认 HTTP 语义的字段：`openai` 系用 `status_code`，`httpx.HTTPStatusError` 在
    `response.status_code` 上。**刻意不读 `.code`** —— Milvus / Neo4j 的 `.code` 是
    各自内部的错误码（如 Milvus 的 1100），拿它当 HTTP 状态码会得出错误结论。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else 0


def classify(exc: BaseException) -> ErrorKind:
    """
    给异常分类。顺序有讲究：先看是不是「我们自己的代码错」，再看外部状态

    :param exc: 捕获到的异常
    :return: 见 `ErrorKind`
    """
    # 1. 代码自身不一致：与外部无关，必须暴露
    if isinstance(exc, FATAL_TYPES):
        return ErrorKind.FATAL

    # 2. 模型服务拒答。**必须排在 HTTP 状态码前面** —— 它也是 400，
    #    不先拦下来就会被下面的 4xx 分支归成 BLOCKED，丢掉「该给用户一句人话」这条信息
    if is_content_rejected(exc):
        return ErrorKind.REJECTED

    # 3. HTTP 状态码（最可靠的信号，有就优先用）
    status = _http_status(exc)
    if status:
        if status in RETRYABLE_STATUS:
            return ErrorKind.RETRYABLE
        if 400 <= status < 500:
            # 鉴权、参数、配额之类：重试不会有不同结果，要人去改配置/代码
            return ErrorKind.BLOCKED

    # 4. 按异常名判断暂时性故障（网络类异常的名字高度一致）
    signature = f"{type(exc).__name__} {exc}".lower()
    if any(hint in signature for hint in RETRYABLE_HINTS):
        return ErrorKind.RETRYABLE

    # 5. 兜底：可能是外部依赖的其它异常，也可能是数据结构不符预期。
    #    仍然降级（保住"一路坏不影响整条链路"），但要留下完整堆栈和可统计的标记
    return ErrorKind.UNEXPECTED


def is_fatal(exc: BaseException) -> bool:
    """是不是「代码自身不一致」那类异常"""
    return classify(exc) is ErrorKind.FATAL


def _mark_degraded(node: str) -> None:
    """
    把「这个节点降级了」记进任务状态，供前端泳道标出来

    为什么需要：降级的节点会返回空结果、**照常算完成**，于是在泳道上跟「正常取到内容」
    长得一模一样（「停了 Neo4j 却还能看到查询知识图谱 ✓」就是这么来的）。

    任务 id 从归因上下文取 —— 查询侧是 session_id、导入侧是 task_id，两者都与 task_utils
    的 key 一致。命令行单跑节点时没有这个上下文，`add_degraded_task` 会静默跳过。
    **这里的异常刻意吞掉**：记录降级失败不该反过来影响降级本身。
    """
    try:
        from app.core.request_context import raw_context
        from app.utils.task_utils import add_degraded_task

        add_degraded_task(raw_context().get("session_id"), node)
    except Exception:
        pass


def degrade(node: str, what: str, fallback: Any, exc: BaseException, **extra) -> Any:
    """
    降级处置：分类 → 记日志 → 返回兜底值；是编程错误则上抛

    用法（替换原来的 `except: 记日志 + return 空`）：

    ```python
    except Exception as e:
        return degrade(NODE_NAME, "图谱检索", {"kg_chunks": []}, e)
    ```

    **不必非在 `except` 块内调用**：FATAL 分支用 `raise exc` 而不是裸 `raise`，
    后者没有活动异常时会抛 `RuntimeError`（自测里就踩过），
    前者在两种上下文下都能把原异常连同原始堆栈一起抛出去。

    :param node: 节点名。上下文里有节点时会被日志补丁去掉（避免重复），
        命令行单跑节点时它会出现在消息里，正好补上缺失的归因
    :param what: 干了什么事失败，用于人读，如「图谱检索」「生成假设文档」
    :param fallback: 降级时返回的值
    :param exc: 捕获到的异常
    :param extra: 额外的结构化字段，会进日志的 `extra`
    :return: `fallback`（FATAL 时不会返回）
    """
    kind = classify(exc)
    detail = f"{type(exc).__name__}: {exc}"

    if kind is ErrorKind.FATAL:
        # 这条日志本身就是本模块存在的理由：以前它会是一条 warning，然后功能静默失效
        logger.error(
            f"[{node}] {what}遇到编程错误，已上抛（不降级掩盖）：{detail}",
            exc_info=True, degraded=False, kind=kind.value,
        )
        raise exc

    if kind is ErrorKind.REJECTED:
        # 模型服务按合规拒答：**不是"坏了"**，别让它读起来像故障。
        # 调用方（两个 LLM 节点）看到这个 kind 会短路给用户一句人话，见 CONTENT_REJECTED_ANSWER
        logger.error(
            f"[{node}] {what}被模型服务内容审核拒绝（{kind.value}）：{detail}",
            exc_info=True, degraded=True, kind=kind.value,
        )
    elif kind is ErrorKind.RETRYABLE:
        # 暂时性故障：warn 级、不带堆栈（网络抖动的堆栈是噪音，Phase 2 会在这里接重试）
        logger.warning(
            f"[{node}] {what}失败，降级继续（{kind.value}，可重试）：{detail}",
            degraded=True, kind=kind.value,
        )
    else:
        # BLOCKED / UNEXPECTED：要人看，带完整堆栈
        logger.error(
            f"[{node}] {what}失败，降级继续（{kind.value}）：{detail}",
            exc_info=True, degraded=True, kind=kind.value,
        )

    # 记进任务状态：让前端泳道知道这个节点是「降级返回」而不是「正常取到」
    _mark_degraded(node)
    return fallback


def degrade_dependency(node: str, what: str, fallback: Any, reason: str,
                       kind: ErrorKind = ErrorKind.RETRYABLE) -> Any:
    """
    前置检查发现依赖不可用时的降级（**没有异常对象可分类**）

    与 `degrade` 分开，是因为这不是"抛了异常"——是主动探测到依赖不在（Neo4j 没起、
    Milvus 连不上、集合名没配），于是**整段功能跳过**。但它同样属于"功能在静默失效"：
    图谱那一路整段没跑、产品名对齐整段没跑，用户只是觉得"答得不好"，没人会知道原因。

    所以照样打 `degraded` 标记，否则 `log_query --degraded` 会漏掉最常见的一类降级
    （实测就是这样发现的：Neo4j 停掉后那条 warning 查不出来）。

    :param kind: 默认 `RETRYABLE`（多半是服务没起/在重启，起来就好）；
        配置缺失这类"重试也没用、要人改"的传 `ErrorKind.BLOCKED`
    :return: `fallback`
    """
    logger.warning(
        f"[{node}] {what}跳过，降级继续（{kind.value}，依赖不可用）：{reason}",
        degraded=True, kind=kind.value, dependency=reason,
    )
    # 前置检查跳过同样要标出来 —— 它没有异常对象，但功能确实静默失效了
    _mark_degraded(node)
    return fallback


if __name__ == '__main__':
    """自测：分类是否符合预期、degrade 的四种走向"""

    # 用假的异常类型模拟各来源，避免自测依赖外部库
    class FakeTimeout(Exception):
        pass

    class FakeAPIStatus(Exception):
        def __init__(self, status_code):
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    class ServerSelectionTimeoutError(Exception):
        pass

    class FakeBadRequest(Exception):
        """模拟 openai 的 BadRequestError：状态码 + 解析好的错误体挂在 .body"""

        def __init__(self, status_code, body=None):
            super().__init__(f"HTTP {status_code} {body or ''}")
            self.status_code = status_code
            self.body = body

    # DashScope 内容审核的真实报错体（2026-10-06 实测遇到的那一条，原样抄下来）
    _reject_body = {
        "error": {
            "message": "Input data may contain inappropriate content.",
            "type": "data_inspection_failed",
            "param": None,
            "code": "data_inspection_failed",
        },
        "id": "chatcmpl-711",
    }

    cases = [
        # 编程错误 → FATAL
        (NameError("name 'x' is not defined"), ErrorKind.FATAL),
        (UnboundLocalError("local variable 'y' referenced before assignment"), ErrorKind.FATAL),
        (ImportError("No module named 'foo'"), ErrorKind.FATAL),
        (AssertionError("不该发生"), ErrorKind.FATAL),
        # 模型服务拒答 → REJECTED（也是 400，必须先于 BLOCKED 被拦下）
        (FakeBadRequest(400, _reject_body), ErrorKind.REJECTED),
        (Exception("data_inspection_failed 无 body 时靠文本兜底"), ErrorKind.REJECTED),
        # 暂时性 → RETRYABLE
        (TimeoutError("timed out"), ErrorKind.RETRYABLE),
        (FakeTimeout("read timeout"), ErrorKind.RETRYABLE),
        (ConnectionError("connection reset"), ErrorKind.RETRYABLE),
        (ServerSelectionTimeoutError("No servers found yet"), ErrorKind.RETRYABLE),
        (FakeAPIStatus(429), ErrorKind.RETRYABLE),
        (FakeAPIStatus(503), ErrorKind.RETRYABLE),
        # 重试无用 → BLOCKED（普通 400 不能被误判成拒答）
        (FakeAPIStatus(401), ErrorKind.BLOCKED),
        (FakeAPIStatus(400), ErrorKind.BLOCKED),
        (FakeBadRequest(400, {"error": {"code": "invalid_parameter"}}), ErrorKind.BLOCKED),
        # 兜底 → UNEXPECTED
        (ValueError("RERANK_API_KEY 未配置"), ErrorKind.UNEXPECTED),
        (KeyError("output"), ErrorKind.UNEXPECTED),
        (TypeError("unsupported operand"), ErrorKind.UNEXPECTED),
    ]

    problems = []
    for exc, want in cases:
        got = classify(exc)
        if got is not want:
            problems.append(f"{type(exc).__name__} 应为 {want.value}，实际 {got.value}")

    # degrade 的四种走向
    # FATAL 必须上抛，不能被降级吞掉
    try:
        degrade("node_x", "图谱检索", {"kg_chunks": []}, NameError("拼错的变量"))
        problems.append("NameError 被降级吞掉了，应上抛")
    except NameError:
        pass

    if degrade("node_x", "联网搜索", {"docs": []}, TimeoutError("timed out")) != {"docs": []}:
        problems.append("RETRYABLE 应返回兜底值")
    if degrade("node_x", "重排", [], ValueError("配置缺失")) != []:
        problems.append("UNEXPECTED 应返回兜底值")
    if degrade("node_x", "LLM 提取产品名", {"item_names": []},
               FakeBadRequest(400, _reject_body)) != {"item_names": []}:
        problems.append("REJECTED 应返回兜底值")

    # **事故复现式（2026-10-06）**：异常文本里嵌着外部服务的 JSON 原文，花括号会被
    # loguru 的 `message.format()` 当成替换字段，导致 degrade 里那行日志自己抛
    # KeyError 把真故障顶掉。这条用例守着它不再复发。
    try:
        degrade("node_x", "LLM 提取产品名", {"item_names": []}, FakeBadRequest(400, _reject_body))
    except Exception as e:
        problems.append(f"degrade 打印带花括号的异常文本时抛了 {type(e).__name__}：{e}")

    for p in problems:
        logger.error(f"[测试] [FAIL] {p}")
    if not problems:
        logger.success(f"[测试] [PASS] 异常分级验证通过（{len(cases)} 个分类 + 5 个降级走向）")

    # 结构化字段：degraded / kind 应能进 JSONL 的 extra
    degrade("node_x", "演示", [], TimeoutError("timeout"))
