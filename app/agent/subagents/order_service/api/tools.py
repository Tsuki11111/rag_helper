"""订单工具适配层：对模型暴露的工具统一从这里导出。"""
from app.agent.subagents.order_service.api.actions import (
    list_addresses,
    prepare_order,
    prepare_refund,
    prepare_refunds,
    prepare_urge_refund,
    prepare_urge_refunds,
    prepare_urge_shipment,
    prepare_urge_shipments,
)
from app.agent.subagents.order_service.api.query import query_orders

ORDER_TOOLS = [
    query_orders,
    list_addresses,
    prepare_order,
    prepare_refund,
    prepare_urge_shipment,
    prepare_urge_refund,
    prepare_refunds,
    prepare_urge_shipments,
    prepare_urge_refunds,
]

__all__ = ["ORDER_TOOLS", "query_orders", "list_addresses", "prepare_order",
           "prepare_refund", "prepare_urge_shipment", "prepare_urge_refund",
           "prepare_refunds", "prepare_urge_shipments", "prepare_urge_refunds"]
