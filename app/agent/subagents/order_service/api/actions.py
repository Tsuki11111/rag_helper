"""
M9 / M10：下单与退款申请的工具适配层。

业务在 `application/order_action_service.py`；这里只保留模型工具签名与导出，
方便测试继续从 `api.actions` 取名字。**这些工具只生成草稿，不执行**。
"""
from app.agent.subagents.order_service.application.order_action_service import (
    OrderActionService,
)
from app.agent.subagents.order_service.domain.rules import (
    BATCH_LIMIT,
    REFUNDABLE_STATUSES,
    URGE_REFUNDABLE_STATUSES,
    URGE_SHIPPABLE_STATUSES,
)
from app.clients.its_mygo_client import call
from app.conf.order_service_config import order_service_config


def _service() -> OrderActionService:
    # 每次按当前 call 构造：测试会 monkeypatch 模块级 call
    return OrderActionService(call_fn=call)


def list_addresses() -> str:
    """列出当前用户的收货地址（含地址 id，供 prepare_order 使用）。"""
    return _service().list_addresses()


def _precheck_items(normalized: list[dict]) -> str:
    """校验商品行：商品在售、SKU 存在、库存足够。"""
    return _service().precheck_items(normalized)


def prepare_order(items: list[dict], address_id: int | None = None) -> str:
    """准备下单草稿（**不会真正下单**，必须用户在确认卡片里点确认）。

    :param items: 商品行列表，每行形如
        {"productId": 1, "skuId": 2, "quantity": 1}
    :param address_id: 收货地址 id；不传则用默认地址
    """
    return _service().prepare_order(items, address_id=address_id)


def prepare_refund(order_no: str, reason: str = "") -> str:
    """准备退款申请草稿（**不会真正提交**，必须用户在确认卡片里点确认）。

    :param order_no: 要退款的订单号
    :param reason: 退款原因（可选，≤200 字）
    """
    return _service().prepare_refund(order_no, reason=reason)


def prepare_urge_shipment(order_no: str) -> str:
    """准备催发货消息草稿（**不会直接发送**，必须用户在确认卡片里点确认）。"""
    return _service().prepare_urge_shipment(order_no)


def prepare_urge_refund(order_no: str) -> str:
    """准备催退款消息草稿（**不会直接发送**，必须用户在确认卡片里点确认）。"""
    return _service().prepare_urge_refund(order_no)


def prepare_refunds(order_nos) -> str:
    """批量准备退款申请草稿（逐单一张确认卡片，**不会提交**）。"""
    return _service().prepare_refunds(order_nos)


def prepare_urge_shipments(order_nos) -> str:
    """批量准备催发货草稿（逐单一张确认卡片，**不会发送**）。"""
    return _service().prepare_urge_shipments(order_nos)


def prepare_urge_refunds(order_nos) -> str:
    """批量准备催退款草稿（逐单一张确认卡片，**不会发送**）。"""
    return _service().prepare_urge_refunds(order_nos)

