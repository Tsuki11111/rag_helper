"""催发货执行图的状态。与 `product_helper_agent/state.py` 同构：
字段集中声明，节点只读写这里定义过的键（LangGraph 的 TypedDict 不做校验，
未声明的键会被静默丢弃）。"""
import copy
from typing import TypedDict


class UrgeShipmentState(TypedDict):
    session_id: str          # 会话 ID（Java 内部事件带入）
    user_id: str             # 顾客 ID（tool ticket / gRPC metadata 带入）
    tenant_id: str           # 租户（记账/隔离用）
    action_no: str           # Java 动作号；agent 用它回调发送工具
    order_no: str            # 发送工具返回的订单号
    merchant_name: str       # 发送工具返回的店铺名
    message_id: str          # 发送工具返回的站内消息 ID
    error: str               # 发送失败原因；空表示成功
    reply: str               # 最终写给用户的 assistant 回复


urge_shipment_default_state: UrgeShipmentState = {
    "session_id": "",
    "user_id": "",
    "tenant_id": "",
    "action_no": "",
    "order_no": "",
    "merchant_name": "",
    "message_id": "",
    "error": "",
    "reply": "",
}


def create_urge_shipment_state(**overrides) -> UrgeShipmentState:
    """创建默认状态并覆盖指定字段。"""
    state = copy.deepcopy(urge_shipment_default_state)
    state.update(overrides)
    return state
