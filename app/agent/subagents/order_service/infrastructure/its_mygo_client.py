"""Java 内部工具接口客户端。

票据从 ContextVar 取（由 gRPC metadata 传入），**模型不参与身份**。

失败处理分三类（2026-11-07 行动能力改造）：
- `unauthorized`：票据缺失/失效 → 明确提示重新发起会话，**不重试**；
- `transient`：超时、连接失败、429/5xx → **只读调用（查订单/目录/地址）有限重试**，
  写操作（prepare/send 草稿）**绝不自动重试**（避免重复草稿/消息）；
- `business`：Java 业务校验拒绝（HTTP 4xx 或 body code != 200）→ 原样转述原因，
  **不重试**，让主 agent 按提示调整或向用户解释。

工具层永远返回 `(data, error)`、不抛异常 —— 工具抛错会打断整轮对话。
`error` 是 `ToolError`（`str` 子类，带 `.kind`），调用方按分类处理，不再匹配中文文案。
"""
import time

import httpx

from app.agent.runtime.tool_context import current_tool_context
from app.agent.runtime.tool_errors import ToolError
from app.conf.its_mygo_config import its_mygo_config
from app.core.logger import logger

# 视为暂时性故障的 HTTP 状态码
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
# body 里的 code 若是这几个，也按暂时性故障处理（Java 网关/限流）
TRANSIENT_BODY_CODES = {429, 500, 502, 503, 504}
# 只读调用的重试次数（不含首次）
READ_RETRIES = 2
# 重试退避基数（秒）：0.3 / 0.6
RETRY_BACKOFF_SEC = 0.3


def _header_ticket() -> str:
    ctx = current_tool_context()
    return ctx.ticket if ctx else ""


def _transient_message(read_only: bool) -> str:
    if read_only:
        return f"平台服务暂时不可用（已重试 {READ_RETRIES} 次），请稍后再试。"
    return "平台服务暂时不可用，这次操作没有提交成功，请稍后再试。"


def call(path: str, payload: dict | None = None, method: str = "POST",
         read_only: bool = False, params: dict | None = None) -> tuple:
    """调用 Java 内部接口，返回 `(data, error)`。

    :param path: Java 内部接口路径
    :param payload: POST body
    :param method: GET / POST
    :param read_only: 只读调用才允许重试（超时 / 429 / 5xx）；写操作绝不自动重试
    :param params: GET 查询参数
    :return: `(data, "")` 成功；`(None, 可读错误文案)` 失败
    """
    ticket = _header_ticket()
    if not ticket:
        return None, ToolError("unauthorized", "当前会话没有平台工具权限（仅在平台内的客服会话中调用）。")

    url = f"{its_mygo_config.base_url}{path}"
    headers = {"X-Agent-Ticket": ticket}
    attempts = 1 + (READ_RETRIES if read_only else 0)
    last_message = _transient_message(read_only)

    for attempt in range(1, attempts + 1):
        try:
            if method.upper() == "GET":
                resp = httpx.get(url, headers=headers, params=params,
                                 timeout=its_mygo_config.tool_timeout_seconds)
            else:
                resp = httpx.post(url, json=payload or {}, headers=headers,
                                  timeout=its_mygo_config.tool_timeout_seconds)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning(f"[its_mygo_client] {path} 暂时性故障（{type(e).__name__}），"
                           f"第 {attempt}/{attempts} 次")
            if attempt < attempts:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return None, ToolError("transient", last_message)
        except Exception as e:
            # 非暂时性异常（编程错误等）：不重试，但必须留下堆栈，避免把 bug 伪装成平台抖动
            logger.error(f"[its_mygo_client] {path} 调用出现非预期异常："
                         f"{type(e).__name__}: {e}", exc_info=True)
            return None, ToolError("internal", "平台内部错误，请稍后再试。")

        # HTTP 层状态码
        if resp.status_code == 401:
            return None, ToolError("unauthorized", "平台工具凭证已失效，请重新发起会话后再试。")
        if resp.status_code in TRANSIENT_STATUS:
            logger.warning(f"[its_mygo_client] {path} 返回 {resp.status_code}，"
                           f"第 {attempt}/{attempts} 次")
            if attempt < attempts:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return None, ToolError("transient", last_message)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(f"[its_mygo_client] {path} 返回 {e.response.status_code}："
                           f"{e.response.text[:200]}")
            return None, ToolError(
                "business", "操作未完成：平台暂时无法处理这次请求，请按提示核对后重试。")

        try:
            body = resp.json()
        except Exception:
            logger.warning(f"[its_mygo_client] {path} 返回非 JSON 响应")
            return None, ToolError("invalid_response", "平台返回格式异常，请稍后再试。")

        if not isinstance(body, dict) or body.get("code") != 200:
            code = body.get("code") if isinstance(body, dict) else None
            if code == 401:
                return None, ToolError(
                    "unauthorized", "平台工具凭证已失效，请重新发起会话后再试。")
            if code in TRANSIENT_BODY_CODES:
                logger.warning(f"[its_mygo_client] {path} body code={code}，"
                               f"第 {attempt}/{attempts} 次")
                if attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SEC * attempt)
                    continue
                return None, ToolError("transient", last_message)
            message = body.get("message") if isinstance(body, dict) else ""
            return None, ToolError(
                "business",
                f"操作未完成：{message or '平台拒绝了这次请求'}（重试无效，请按提示调整）。")
        return body.get("data"), ""

    return None, ToolError("transient", last_message)


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_tool_failure_policy() -> list:
    """失败分类与重试策略：只读有限重试、写操作不重试、业务错误不重试、401 不重试。"""
    from app.agent.runtime.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post, real_get, real_sleep = httpx.post, httpx.get, time.sleep
    time.sleep = lambda s: None      # 重试退避不真等

    class _Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {"code": 200, "data": {"ok": True}}
            self.text = str(self._body)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=self)

        def json(self):
            return self._body

    token = set_tool_context("ticket-abc", "42")
    # 期望次数写死（不要引用 READ_RETRIES 本身）：否则把重试次数改成 0，
    # 断言也会跟着变 0，mutation 就抓不住（2026-11-07 实际踩到）
    expected_read_calls = 3     # 1 次 + 2 次重试
    try:
        # ① 只读 + 暂时性故障：重试后成功（2 次失败 + 1 次成功）
        state = {"n": 0}

        def _flaky(*a, **k):
            state["n"] += 1
            if state["n"] <= 2:
                raise httpx.ConnectTimeout("timeout")
            return _Resp()

        httpx.post = _flaky
        data, err = call("/internal/agent/orders/query", {"orderNo": "X"}, read_only=True)
        if state["n"] != expected_read_calls:
            problems.append(f"只读重试次数不对：期望 {expected_read_calls} 次，实为 {state['n']} 次")
        if err or not data:
            problems.append(f"只读重试成功后仍报错：{err!r}")

        # ② 只读 + 一直 503：用尽重试，返回「暂时不可用」
        state["n"] = 0

        def _always_503(*a, **k):
            state["n"] += 1
            return _Resp(status=503)

        httpx.post = _always_503
        data, err = call("/internal/agent/orders/query", {}, read_only=True)
        if state["n"] != expected_read_calls:
            problems.append(f"一直 503 应重试 {expected_read_calls} 次，实为 {state['n']} 次")
        if "暂时不可用" not in (err or ""):
            problems.append(f"503 提示不清晰：{err!r}")

        # ③ 写操作 + 超时：**只调 1 次**，不能重试出重复草稿
        state["n"] = 0

        def _write_timeout(*a, **k):
            state["n"] += 1
            raise httpx.ReadTimeout("timeout")

        httpx.post = _write_timeout
        data, err = call("/internal/agent/actions/order", {"items": []})
        if state["n"] != 1:
            problems.append(f"写操作不应重试：期望 1 次，实为 {state['n']} 次")
        if "没有提交成功" not in (err or ""):
            problems.append(f"写操作失败文案缺少「没有提交成功」：{err!r}")

        # ④ 业务错误：原样转述、不重试
        state["n"] = 0

        def _business(*a, **k):
            state["n"] += 1
            return _Resp(body={"code": 400, "message": "库存不足"})

        httpx.post = _business
        data, err = call("/internal/agent/actions/order", {})
        if state["n"] != 1:
            problems.append(f"业务错误不应重试：实为 {state['n']} 次")
        if "库存不足" not in (err or "") or "重试无效" not in (err or ""):
            problems.append(f"业务错误文案不清晰：{err!r}")

        # ⑤ 401：提示重新发起会话、不重试
        state["n"] = 0

        def _unauth(*a, **k):
            state["n"] += 1
            return _Resp(status=401)

        httpx.post = _unauth
        data, err = call("/internal/agent/orders/query", {}, read_only=True)
        if state["n"] != 1:
            problems.append(f"401 不应重试：实为 {state['n']} 次")
        if "重新发起会话" not in (err or ""):
            problems.append(f"401 提示不清晰：{err!r}")

        # ⑥ 编程错误：不能被伪装成平台暂时不可用（要留堆栈、只调 1 次）
        state["n"] = 0

        def _bug(*a, **k):
            state["n"] += 1
            raise ValueError("bad payload")

        httpx.post = _bug
        data, err = call("/internal/agent/orders/query", {}, read_only=True)
        if state["n"] != 1:
            problems.append(f"编程错误不应重试：实为 {state['n']} 次")
        if "内部错误" not in (err or "") or "暂时不可用" in (err or ""):
            problems.append(f"编程错误被误判为平台故障：{err!r}")
    finally:
        httpx.post, httpx.get = real_post, real_get
        time.sleep = real_sleep
        reset_tool_context(token)
    return problems
