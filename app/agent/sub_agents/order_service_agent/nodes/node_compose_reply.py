"""催发货执行图节点：生成给用户的完成 / 失败回复，并代商家致歉。"""
from app.agent.sub_agents.order_service_agent.state import UrgeShipmentState


def node_compose_reply(state: UrgeShipmentState) -> dict:
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
