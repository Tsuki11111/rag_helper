"""催发货执行图节点：把最终回复写入会话历史（assistant）。

内部事件本身不落用户消息；用户历史里只出现买小购的回复。
"""
from app.agent.sub_agents.order_service_agent.state import UrgeShipmentState
from app.clients.mongo_history_clients import save_chat_message


def node_save_reply(state: UrgeShipmentState) -> dict:
    save_chat_message(
        state["session_id"],
        "assistant",
        state["reply"],
        user_id=state.get("user_id") or None,
        tenant_id=state.get("tenant_id") or None,
    )
    return {}
