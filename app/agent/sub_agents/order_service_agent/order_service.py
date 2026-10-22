"""
order-service 的工具层（M7：接 Java 真实订单接口）。

数据来源：Java 单体 `POST /internal/agent/orders/query`，凭本轮 Converse 的短期票据
鉴权。票据经 gRPC metadata → ContextVar 传进来，**工具签名里没有 userId**，
模型即使被注入也指定不了别的用户。

只读工具。催办 / 下单 / 退款等写操作按 evolution-plan §1 红线，由 Java 校验并执行，
不在这个工具里做。
"""
import httpx

from app.agent.sub_agents.order_service_agent.its_mygo_client import call


def query_orders(order_no: str = "", status: str = "", limit: int = 5) -> str:
    """查询当前登录用户自己的订单状态与物流。

    :param order_no: 订单号；不传则返回最近的订单
    :param status: 可选，订单状态过滤，如 PAID / SHIPPED / REFUNDING，多个用逗号分隔
    :param limit: 不传订单号时最多返回几笔，默认 5（Java 侧上限 10）
    :return: 供主 agent 转述的订单摘要；查不到或出错时返回可读提示
    """
    # 模型传参不一定守类型：统一在这里收敛，避免工具抛异常打断整轮对话
    safe_order_no = str(order_no or "").strip()
    safe_status = str(status or "").strip()
    try:
        safe_limit = int(limit or 5)
    except (TypeError, ValueError):
        safe_limit = 5
    payload = {"orderNo": safe_order_no, "status": safe_status, "limit": safe_limit}
    data, error = call("/internal/agent/orders/query", payload)
    if error:
        return error

    orders = data or []
    if not orders:
        return "没有查到符合条件的订单。"
    return _format_orders(orders)


def _format_orders(orders: list) -> str:
    lines = [f"共查到 {len(orders)} 笔订单："]
    for idx, order in enumerate(orders, 1):
        if not isinstance(order, dict):
            continue
        items = [i for i in (order.get("items") or []) if isinstance(i, dict)]
        item_text = "；".join(_format_item(item) for item in items) or "无明细"
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


def _format_item(item: dict) -> str:
    text = item.get("productName") or "未知商品"
    spec = item.get("specText") or ""
    if spec:
        text += f"（{spec}）"
    return f"{text} ×{item.get('quantity') or 0}"


# ── 离线自测（项目约定：用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_order_service_tools() -> list:
    """订单查询工具：无票据不发请求、有票据带对头/体、返回格式化、异常降级。"""
    from app.agent.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post = httpx.post

    # ① 没有票据：不发请求，返回可读提示
    calls = {"n": 0}

    def _no_call(*a, **k):
        calls["n"] += 1
        raise AssertionError("没有票据时不应调用 Java")

    httpx.post = _no_call
    try:
        text = query_orders(order_no="NO123")
        if calls["n"] != 0:
            problems.append("无票据时仍发起了 HTTP 请求")
        if "权限" not in text:
            problems.append(f"无票据提示不清晰：{text!r}")
    finally:
        httpx.post = real_post

    # ② 有票据：校验请求头 / 请求体，并检查格式化结果
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 200, "data": [{
                "orderNo": "NO123",
                "status": "PAID",
                "statusLabel": "待发货",
                "merchantName": "测试店",
                "totalAmount": "199.00",
                "trackingNo": "",
                "createdAt": "2026-10-21 10:00:00",
                "paidAt": "2026-10-21 10:01:00",
                "shippedAt": "",
                "logistics": {
                    "status": "IN_TRANSIT", "statusLabel": "运输中",
                    "eta": "2026-10-22 12:00:00", "carrier": "买够速运",
                    "courierName": "王师傅", "progress": 42,
                },
                "items": [{"productName": "烫金机", "specText": "颜色:白",
                           "quantity": 1, "unitPrice": "199.00"}],
            }]}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    token = set_tool_context("ticket-abc", "42")
    httpx.post = _fake_post
    try:
        text = query_orders(order_no="NO123", status="PAID", limit=3)
        if captured.get("headers", {}).get("X-Agent-Ticket") != "ticket-abc":
            problems.append("请求未带 X-Agent-Ticket")
        if captured.get("json", {}).get("orderNo") != "NO123":
            problems.append("请求体未传订单号")
        if captured.get("json", {}).get("status") != "PAID":
            problems.append("请求体未传状态")
        for expected in ("NO123", "待发货", "烫金机", "测试店", "199.00",
                         "运输中", "预计 2026-10-22 12:00:00 送达"):
            if expected not in text:
                problems.append(f"格式化结果缺少 {expected!r}")
        for forbidden in ("13800000000", "杭州市西湖区"):
            if forbidden in text:
                problems.append(f"格式化结果泄露了 PII：{forbidden!r}")
    finally:
        httpx.post = real_post
        reset_tool_context(token)

    # ③ HTTP 异常：给出可读提示，不抛
    def _boom(*a, **k):
        raise RuntimeError("boom")

    token = set_tool_context("ticket", "1")
    httpx.post = _boom
    try:
        text = query_orders()
        if "暂时不可用" not in text:
            problems.append(f"HTTP 异常提示不清晰：{text!r}")
    finally:
        httpx.post = real_post
        reset_tool_context(token)

    return problems
