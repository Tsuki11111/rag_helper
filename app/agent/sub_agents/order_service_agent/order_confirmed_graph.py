"""下单确认内部事件（M9 二期）。

Java 在用户确认订单草稿、真正创建订单后，通过 gRPC 内部事件
`order-confirmed` 触发本模块。订单已经在 Java 侧创建完成，agent 不再调用任何工具，
只负责把结果写成一条 assistant 历史，让聊天里能看到「下单成功 + 订单号 + 去支付」。

内部事件不保存用户消息；和催发货 / 催退款一样，用户历史里只出现买小购的回复。
"""
from app.clients.mongo_history_clients import save_chat_message


def _format_order(order: dict, index: int) -> str:
    order_no = str(order.get("orderNo") or "").strip()
    amount = order.get("totalAmount")
    status = str(order.get("statusLabel") or "待支付").strip()
    title = f"订单 {order_no}" if order_no else f"第 {index} 笔订单"
    amount_text = f"¥{amount}" if amount not in (None, "") else "金额以订单页为准"
    return f"- {title}：应付 {amount_text}，当前{status}"


def run_order_confirmed(session_id: str, user_id: str, tenant_id: str,
                        orders: list | None = None) -> str:
    orders = orders or []
    if orders:
        lines = [f"下单成功！已为您生成 {len(orders)} 笔待支付订单：", ""]
        lines.extend(_format_order(order, index)
                     for index, order in enumerate(orders, 1)
                     if isinstance(order, dict))
        lines.extend([
            "",
            "请在订单详情页完成支付；支付后商家才会发货。",
        ])
    else:
        lines = [
            "下单成功！订单已经生成，请到「我的订单」查看。",
            "",
            "请在订单详情页完成支付；支付后商家才会发货。",
        ]
    reply = "\n".join(lines)
    save_chat_message(
        session_id,
        "assistant",
        reply,
        user_id=user_id or None,
        tenant_id=tenant_id or None,
    )
    return reply


# ── 离线自测（registry 见 app/core/regression.py）──

def _check_order_confirmed_reply() -> list:
    """下单成功回复：列出订单号/金额/状态，并且只写一条 assistant 历史。"""
    import app.agent.sub_agents.order_service_agent.order_confirmed_graph as module

    problems = []
    saved = []
    original_save = module.save_chat_message
    module.save_chat_message = lambda *args, **kwargs: saved.append((args, kwargs))
    try:
        reply = run_order_confirmed(
            "sid-order", "42", "tenant-a",
            [
                {"orderNo": "NO20261025001", "totalAmount": "199.00",
                 "statusLabel": "待支付"},
                {"orderNo": "NO20261025002", "totalAmount": "88.00",
                 "statusLabel": "待支付"},
            ],
        )
        for expected in ("下单成功", "NO20261025001", "NO20261025002",
                         "199.00", "待支付"):
            if expected not in reply:
                problems.append(f"成功回复缺少「{expected}」：{reply!r}")
        if len(saved) != 1:
            problems.append(f"成功回复应只写 1 条历史，实际 {len(saved)} 条")
        elif saved[0][0][1] != "assistant":
            problems.append(f"回复写入角色不是 assistant：{saved[0][0][1]!r}")

        saved.clear()
        reply = run_order_confirmed("sid-order", "42", "tenant-a", [])
        if "下单成功" not in reply or "我的订单" not in reply:
            problems.append(f"空订单兜底回复不完整：{reply!r}")
        if len(saved) != 1:
            problems.append(f"空订单也应只写 1 条历史，实际 {len(saved)} 条")
    finally:
        module.save_chat_message = original_save
    return problems
