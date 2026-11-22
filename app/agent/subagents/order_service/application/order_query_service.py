"""订单查询应用服务：参数收敛、调用 Java、格式化。"""
from app.agent.subagents.order_service.domain.ports import OrderGateway
from app.clients.its_mygo_client import call


class OrderQueryService:
    def __init__(self, call_fn: OrderGateway | None = None):
        self._call = call_fn or call

    def query_orders(self, order_no: str = "", status: str = "", limit: int = 5) -> str:
        """查询当前登录用户自己的订单状态与物流。"""
        safe_order_no = str(order_no or "").strip()
        safe_status = str(status or "").strip()
        try:
            safe_limit = int(limit or 5)
        except (TypeError, ValueError):
            safe_limit = 5
        payload = {"orderNo": safe_order_no, "status": safe_status, "limit": safe_limit}
        data, error = self._call("/internal/agent/orders/query", payload, read_only=True)
        if error:
            return error
        orders = data or []
        if not orders:
            return "没有查到符合条件的订单。"
        return self._format_orders(orders)

    @staticmethod
    def _format_orders(orders: list) -> str:
        lines = [f"共查到 {len(orders)} 笔订单："]
        for idx, order in enumerate(orders, 1):
            if not isinstance(order, dict):
                continue
            items = [i for i in (order.get("items") or []) if isinstance(i, dict)]
            item_text = "；".join(OrderQueryService._format_item(item) for item in items) or "无明细"
            line = (
                f"{idx}. 订单号 {order.get('orderNo') or '未知'}，"
                f"{order.get('statusLabel') or order.get('status') or '未知状态'}，"
                f"店铺：{order.get('merchantName') or '未知店铺'}，"
                f"金额 ¥{order.get('totalAmount') or '未知'}，"
                f"下单时间 {order.get('createdAt') or '未知'}"
            )
            if order.get("paidAt"):
                line += f"，支付时间 {order['paidAt']}"
            if order.get("shippedAt"):
                line += f"，发货时间 {order['shippedAt']}"
            tracking = order.get("trackingNo") or "暂无物流单号"
            logistics = order.get("logistics") or {}
            if logistics:
                status = logistics.get("statusLabel") or logistics.get("status") or "物流更新中"
                eta = logistics.get("eta") or ""
                carrier = logistics.get("carrier") or ""
                courier = logistics.get("courierName") or ""
                extra = " / ".join(x for x in (carrier, courier) if x)
                line += f"\n   商品：{item_text}；物流：{status}"
                if eta:
                    line += f"，预计 {eta} 送达"
                if extra:
                    line += f"（{extra}）"
                line += f"；单号 {tracking}"
            else:
                line += f"\n   商品：{item_text}；物流单号：{tracking}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_item(item: dict) -> str:
        text = item.get("productName") or "未知商品"
        spec = item.get("specText") or ""
        if spec:
            text += f"（{spec}）"
        return f"{text} ×{item.get('quantity') or 0}"
