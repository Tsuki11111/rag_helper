"""催发货的「买小购」执行图（M8）。

用户在平台确认催发货后，Java 通过 gRPC 内部事件触发本图：

    send_merchant_message（agent 调 Java 发送工具，Java 校验并落库）
        → compose_reply（生成完成 / 失败 + 代商家致歉的回复）
        → save_reply（作为 assistant 消息写入会话历史）

**真正写聊天表仍在 Java**：agent 只调用 `/internal/agent/actions/urge-shipment/send`，
由 Java 校验动作归属、订单状态和幂等后以 `senderType=AGENT` 落库。
图不保存内部事件本身，用户历史里只出现买小购的回复。
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.sub_agents.order_service_agent.its_mygo_client import call
from app.clients.mongo_history_clients import save_chat_message


class UrgeShipmentState(TypedDict):
    session_id: str
    user_id: str
    tenant_id: str
    action_no: str
    order_no: str
    merchant_name: str
    message_id: str
    error: str
    reply: str


def _send_merchant_message(state: UrgeShipmentState) -> dict:
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


def _compose_reply(state: UrgeShipmentState) -> dict:
    merchant = (state.get("merchant_name") or "商家").strip()
    order_no = (state.get("order_no") or "").strip()
    order_part = f"订单 {order_no} " if order_no else "您的订单 "

    if state.get("error"):
        reply = (
            f"这次催发货没有成功：{state['error']}。\n\n"
            "给您带来不便，我代商家向您说声抱歉；您可以稍后重试，或直接联系店铺客服。"
        )
    else:
        reply = (
            f"催发货已完成：已经替您通知「{merchant}」尽快发货。"
            f"{order_part}目前是待发货状态，请您再耐心等待一下。\n\n"
            "给您带来不便，我代商家向您说声抱歉；"
            "如果商家仍未发货，随时告诉我，我再帮您跟进。"
        )
    return {"reply": reply}


def _save_reply(state: UrgeShipmentState) -> dict:
    save_chat_message(
        state["session_id"],
        "assistant",
        state["reply"],
        user_id=state.get("user_id") or None,
        tenant_id=state.get("tenant_id") or None,
    )
    return {}


def _build_graph():
    graph = StateGraph(UrgeShipmentState)
    graph.add_node("send_merchant_message", _send_merchant_message)
    graph.add_node("compose_reply", _compose_reply)
    graph.add_node("save_reply", _save_reply)
    graph.add_edge(START, "send_merchant_message")
    graph.add_edge("send_merchant_message", "compose_reply")
    graph.add_edge("compose_reply", "save_reply")
    graph.add_edge("save_reply", END)
    return graph.compile()


_GRAPH = _build_graph()


def run_urge_shipment(session_id: str, user_id: str, tenant_id: str,
                      action_no: str) -> str:
    result = _GRAPH.invoke({
        "session_id": session_id,
        "user_id": user_id or "",
        "tenant_id": tenant_id or "",
        "action_no": action_no or "",
        "order_no": "",
        "merchant_name": "",
        "message_id": "",
        "error": "",
        "reply": "",
    })
    return result["reply"]


# ── 离线自测（registry 见 app/core/regression.py）──

def _check_urge_shipment_graph() -> list:
    """催发货执行图：调发送工具、成功/失败文案、只写一条 assistant。"""
    import app.agent.sub_agents.order_service_agent.urge_shipment_graph as module

    problems = []
    saved = []
    calls = []
    original_call = module.call
    original_save = module.save_chat_message

    def _fake_save(*args, **kwargs):
        saved.append((args, kwargs))

    module.save_chat_message = _fake_save
    try:
        module.call = lambda path, payload=None, method="POST": (
            calls.append((path, payload, method)) or
            ({"status": "SUCCEEDED",
              "result": {"orderNo": "NO123", "merchantName": "平台自营",
                         "messageId": 18},
              "errorMessage": ""}, None))
        reply = run_urge_shipment("sid-urge", "42", "tenant-a", "ACTION1")
        for expected in ("催发货已完成", "NO123", "平台自营", "抱歉"):
            if expected not in reply:
                problems.append(f"成功回复缺少「{expected}」：{reply!r}")
        if not calls or not calls[0][0].endswith(
                "/internal/agent/actions/urge-shipment/send"):
            problems.append(f"未调用发送工具：{calls!r}")
        if not calls or calls[0][1].get("actionNo") != "ACTION1":
            problems.append(f"发送工具未带 actionNo：{calls!r}")
        if len(saved) != 1:
            problems.append(f"成功回复应只写 1 条历史，实际 {len(saved)} 条")
        elif saved[0][0][1] != "assistant":
            problems.append(f"回复写入角色不是 assistant：{saved[0][0][1]!r}")

        saved.clear()
        module.call = lambda path, payload=None, method="POST": (
            None, "店铺已停用")
        reply = run_urge_shipment("sid-urge", "42", "tenant-a", "ACTION2")
        if "没有成功" not in reply or "抱歉" not in reply:
            problems.append(f"失败回复不完整：{reply!r}")
        if len(saved) != 1:
            problems.append(f"失败回复应只写 1 条历史，实际 {len(saved)} 条")
    finally:
        module.call = original_call
        module.save_chat_message = original_save

    return problems
