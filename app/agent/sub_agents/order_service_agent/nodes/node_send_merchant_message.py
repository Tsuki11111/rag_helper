"""催发货执行图节点：agent 调用 Java 的发送工具。

Java 的 `/internal/agent/actions/urge-shipment/send` 会重新校验动作归属、状态和订单，
再以 `senderType=AGENT` 落库。工具票据由 `run_main_agent` 放进 ContextVar，
`its_mygo_client.call` 自动带 `X-Agent-Ticket`。
"""
from app.agent.sub_agents.order_service_agent.state import UrgeShipmentState
from app.agent.sub_agents.order_service_agent.tools.its_mygo_client import call


def node_send_merchant_message(state: UrgeShipmentState) -> dict:
    data, error = call(
        "/internal/agent/actions/urge-shipment/send",
        {"actionNo": state.get("action_no") or ""},
    )
    if error:
        return {"error": error}

    action = data or {}
    if action.get("status") != "SUCCEEDED":
        return {"error": action.get("errorMessage") or "催发货执行失败"}

    result = action.get("result") or {}
    return {
        "order_no": str(result.get("orderNo") or ""),
        "merchant_name": str(result.get("merchantName") or ""),
        "message_id": str(result.get("messageId") or ""),
        "error": "",
    }
