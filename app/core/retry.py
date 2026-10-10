"""
故障分类重试（Phase 2 的最后一项）

**先说清定位：这是「建仪器」，不是「调参」。**

README 原本要求「先攒几天错误分布再定次数与退避」。实测（2026-10-10，约 7 天、
1066 次模型调用）真正的暂时性故障**只有 1 次超时** —— 账本里另外几条「失败」
拆开看是 3 条 `GeneratorExit`（用户点暂停，不是故障）与 2 条内容审核 400
（`rejected`，重试无意义）。按这个量再攒三个月也就多几个样本，
**「等数据再定参数」在本项目的流量下走不通**。所以：

- **决策部分**（哪些类能重试）现在就定死 —— 那是设计问题，不是数据问题
- **数值部分**（次数、退避）先取保守默认、可配（`app/conf/budget_config.py`），
  并把每次重试都记进日志；等真出问题，手上就有数据

规矩与 `error_policy` 的分类一一对应：

| 分类 | 处置 |
|---|---|
| `RETRYABLE`（超时 / 429 / 5xx / 连接失败） | **唯一会重试的一类**；429 认 `Retry-After` |
| `REJECTED`（内容审核拒绝） | **绝不重试** —— 再试一次还是被拒，而用户已经拿到一句人话 |
| `FATAL`（编程错误） | **绝不重试**，直接上抛 —— 重试只会掩盖真实缺陷 |
| `BLOCKED`（4xx 参数 / 鉴权） | **绝不重试** —— 要人去改配置 |
| `UNEXPECTED` | 不重试（保守）—— 分不清是不是暂时性的，宁可不试 |

**流式是特例**：调用方一旦开始往外推字就不能重试（会重复推）。所以 `retry_call` 只管
**一次性调用**；流式那条路只在**还没吐第一个字**之前重试，由调用方自己包，见
`node_answer_output._generate`。
"""
import random
import time
from typing import Any, Callable

from app.conf.budget_config import budget_config
from app.core.error_policy import ErrorKind, classify
from app.core.logger import logger


def _delay_for(attempt: int, exc: BaseException) -> float:
    """
    算退避秒数：指数增长 + 抖动；**429 优先认 `Retry-After`**

    抖动是必要的：四路检索是并发的，同时撞上限流的话，不抖动会一起重试、再一起撞。
    """
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(retry_after, budget_config.retry_max_delay_sec)

    base = budget_config.retry_base_delay_sec * (2 ** (attempt - 1))
    jitter = random.uniform(0, base * 0.3)
    return min(base + jitter, budget_config.retry_max_delay_sec)


def _retry_after_seconds(exc: BaseException):
    """
    取 `Retry-After`（秒）；取不到返回 None

    服务端明确说了等多久就听它的 —— 这比我们自己拍的退避准。
    只认「纯数字」形式；HTTP-date 那种格式不解析（认不出就退回退避，不影响正确性）。
    """
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        for holder in (getattr(exc, "headers", None),
                       getattr(getattr(exc, "response", None), "headers", None)):
            if holder:
                try:
                    raw = holder.get("retry-after") or holder.get("Retry-After")
                except Exception:
                    raw = None
                if raw is not None:
                    break
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def retry_call(fn: Callable, *args: Any, what: str = "调用", **kwargs: Any) -> Any:
    """
    按 `error_policy` 的分类重试；**只有 `RETRYABLE` 会重试**

    :param fn: 要执行的可调用（一次性调用；流式别用它，见模块说明）
    :param what: 日志里的人话描述，如「生成答案」「重排打分」
    :return: fn 的返回值
    :raises: **不可重试的分类原样抛出**（交给调用方的 degrade 处理）；重试用尽后抛最后一个异常
    """
    attempts = max(1, int(budget_config.retry_max_attempts))
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            kind = classify(e)
            if kind is not ErrorKind.RETRYABLE:
                raise                 # 不是暂时性故障：一次都不重试，原样抛给调用方
            if attempt >= attempts:
                logger.warning(
                    f"[重试] {what}：第 {attempt} 次仍失败（{kind.value}），放弃重试：{e}",
                    retry_exhausted=True, retry_attempts=attempt,
                )
                break
            delay = _delay_for(attempt, e)
            # 每一次重试都留痕：这就是「建仪器」的那一半 —— 事后能统计「哪个依赖在抖、
            # 重试几次才成功」，将来真要调次数与退避时不用再重新攒
            logger.warning(
                f"[重试] {what}：第 {attempt} 次失败（{kind.value}），{delay:.1f}s 后重试"
                f"（共 {attempts} 次机会）：{e}",
                retry_attempt=attempt, retry_delay_sec=round(delay, 2),
            )
            time.sleep(delay)
    raise last_exc


def invoke_with_retry(llm: Any, messages: Any, what: str = "LLM 调用") -> Any:
    """
    `llm.invoke(messages)` 的重试版（非流式）

    单独包一层是因为调用点有六七处，逐处写 `retry_call(lambda: llm.invoke(...))` 又啰嗦
    又容易漏 —— 漏了不报错，只是那条路悄悄没有重试。
    """
    return retry_call(lambda: llm.invoke(messages), what=what)


# ---------------------------
# 自测用的替身异常。**必须定义在模块级**：loguru 的 sink 开了 enqueue=True，
# 日志记录要 pickle 进队列，函数内定义的类 pickle 不了（踩过，见 HANDOFF §3.19）
# ---------------------------
class _FakeTimeout(Exception):
    """名字里带 timeout，classify 会归成 RETRYABLE"""


class _Fake429(Exception):
    """带 429 状态码与 Retry-After 头"""

    def __init__(self, retry_after=None):
        super().__init__("HTTP 429 rate limited")
        self.status_code = 429
        if retry_after is not None:
            self.headers = {"retry-after": str(retry_after)}


class _FakeRejected(Exception):
    """内容审核拒绝：也是 400，但**不该重试**"""

    def __init__(self):
        super().__init__("400 - {'error': {'code': 'data_inspection_failed'}}")
        self.status_code = 400
        self.body = {"error": {"code": "data_inspection_failed"}}


def _check_retry_policy() -> list:
    """
    离线自测：重试策略（纯逻辑，不调任何接口、不真等）

    守四件事：**该重试的会重试**、**用尽后抛最后一个异常**、
    **不该重试的一次都不试**（这条最要紧：对着内容审核拒绝重试只会白烧配额）、
    **429 认 `Retry-After`**。

    做法是把 `time.sleep` 换掉、把退避时间记下来 —— 既不用真等，也能断言"等了多久"。

    :return: 问题描述列表，空表示通过
    """
    import app.core.retry as retry_module

    problems = []
    delays = []
    real_sleep = retry_module.time.sleep
    retry_module.time.sleep = lambda s: delays.append(s)
    try:
        # ① 第一次超时、第二次成功
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeTimeout("timed out")
            return "ok"

        delays.clear()
        if retry_call(flaky, what="自测") != "ok":
            problems.append("重试后成功，却没拿到结果")
        if calls["n"] != 2 or len(delays) != 1:
            problems.append(f"重试行为不对：调用 {calls['n']} 次、退避 {len(delays)} 次（各应为 2 / 1）")

        # ② 一直超时：用尽后抛最后一个异常，且尝试次数正好是配置值
        delays.clear()
        hits = {"n": 0}

        def always_timeout():
            hits["n"] += 1
            raise _FakeTimeout("timed out")

        try:
            retry_call(always_timeout, what="自测")
            problems.append("重试用尽后没有抛异常")
        except _FakeTimeout:
            pass
        if hits["n"] != budget_config.retry_max_attempts:
            problems.append(
                f"尝试次数应为 {budget_config.retry_max_attempts}，实为 {hits['n']}")

        # ③ 不该重试的分类：一次都不重试、原样抛出
        for exc, label in (
            (_FakeRejected(), "内容审核拒绝（REJECTED）"),
            (NameError("name 'x' is not defined"), "编程错误（FATAL）"),
            (ValueError("参数不对"), "分不清的（UNEXPECTED）"),
        ):
            delays.clear()

            def boom(exc=exc):
                raise exc

            try:
                retry_call(boom, what="自测")
                problems.append(f"{label} 竟然没抛异常")
            except Exception:
                pass
            if delays:
                problems.append(f"{label} 被重试了 {len(delays)} 次（一次都不该重试）")

        # ④ 429 认 Retry-After（1 秒）——不按退避基数算
        delays.clear()
        state = {"n": 0}

        def rate_limited():
            state["n"] += 1
            if state["n"] == 1:
                raise _Fake429(retry_after=1.0)
            return "ok"

        retry_call(rate_limited, what="自测")
        if not delays or abs(delays[0] - 1.0) > 1e-6:
            problems.append(f"没认 Retry-After：实得退避 {delays!r}，应为 [1.0]")

        # ⑤ Retry-After 再大也不越过上限
        delays.clear()
        state["n"] = 0

        def rate_limited_long():
            state["n"] += 1
            if state["n"] == 1:
                raise _Fake429(retry_after=9999.0)
            return "ok"

        retry_call(rate_limited_long, what="自测")
        if not delays or delays[0] > budget_config.retry_max_delay_sec:
            problems.append(f"退避没被上限拦住：{delays!r} > {budget_config.retry_max_delay_sec}")

        # ⑥ 普通退避要落在 [基数, 基数×1.3] 之间（含抖动）
        delays.clear()
        state["n"] = 0

        def plain_timeout():
            state["n"] += 1
            if state["n"] == 1:
                raise _FakeTimeout("timed out")
            return "ok"

        retry_call(plain_timeout, what="自测")
        base = budget_config.retry_base_delay_sec
        if not delays or not (base - 1e-9 <= delays[0] <= base * 1.3 + 1e-9):
            problems.append(f"退避不在 [{base}, {base * 1.3}] 内：{delays!r}")
    finally:
        retry_module.time.sleep = real_sleep
    return problems


if __name__ == '__main__':
    """自测：重试策略（纯逻辑，不调接口、不真等）"""
    _problems = _check_retry_policy()
    print(f"[{'PASS' if not _problems else 'FAIL'}] 故障分类重试策略")
    for _p in _problems:
        print(f"  [FAIL] {_p}")
