"""Java 内部工具接口客户端。

票据从 ContextVar 取（由 gRPC metadata 传入），**模型不参与身份**。
所有工具都通过这里回调 Java；无票据 / HTTP 失败 / 业务错误统一降级成可读文本，
不抛异常（工具抛错会打断整轮对话）。
"""
import httpx

from app.agent.tool_context import current_tool_context
from app.conf.its_mygo_config import its_mygo_config
from app.core.logger import logger


def call(path: str, payload: dict | None = None, method: str = "POST"):
    """调用 Java 内部接口，返回 (data, error)。

    error 为空串表示成功；否则是给用户看的可读提示。
    """
    ctx = current_tool_context()
    if not ctx.ticket:
        return None, "当前会话没有平台工具权限（仅在平台内的客服会话中可用）。"

    url = f"{its_mygo_config.base_url}{path}"
    headers = {"X-Agent-Ticket": ctx.ticket}
    try:
        if method.upper() == "GET":
            resp = httpx.get(url, headers=headers,
                             timeout=its_mygo_config.tool_timeout_seconds)
        else:
            resp = httpx.post(url, json=payload or {}, headers=headers,
                              timeout=its_mygo_config.tool_timeout_seconds)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning(f"[its_mygo_client] {path} 返回 {e.response.status_code}："
                       f"{e.response.text[:200]}")
        if e.response.status_code == 401:
            return None, "平台工具凭证已失效，请重新发起会话后再试。"
        return None, "平台服务暂时不可用，请稍后再试。"
    except Exception as e:
        logger.warning(f"[its_mygo_client] {path} 调用失败：{type(e).__name__}: {e}")
        return None, "平台服务暂时不可用，请稍后再试。"

    if not isinstance(body, dict) or body.get("code") != 200:
        if isinstance(body, dict) and body.get("code") == 401:
            return None, "平台工具凭证已失效，请重新发起会话后再试。"
        message = body.get("message") if isinstance(body, dict) else ""
        return None, f"操作失败：{message or '未知错误'}"
    return body.get("data"), ""
