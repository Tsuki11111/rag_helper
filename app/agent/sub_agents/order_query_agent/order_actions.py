"""
M9 / M10：下单与退款申请的工具层。

**这些工具只生成「草稿」（PENDING_CONFIRM），不会真正执行**——
执行由用户在聊天里的确认卡片点击确认后，Java 按服务端快照调用业务 Service。
所以在工具返回里必须明确说「尚未下单 / 尚未提交」，主 agent 不许声称已成功。
"""
from app.agent.sub_agents.order_query_agent.its_mygo_client import call


def list_addresses() -> str:
    """列出当前用户的收货地址（含地址 id，供 prepare_order 使用）。"""
    data, error = call("/internal/agent/catalog/addresses", method="GET")
    if error:
        return error
    addresses = data or []
    if not addresses:
        return "当前用户还没有收货地址，请先到用户中心添加。"

    lines = [f"共 {len(addresses)} 个收货地址："]
    for idx, address in enumerate(addresses, 1):
        if not isinstance(address, dict):
            continue
        default = "（默认）" if address.get("isDefault") else ""
        lines.append(
            f"{idx}. id={address.get('id')}，{address.get('receiver') or ''} "
            f"{address.get('phone') or ''}，{address.get('address') or ''}{default}")
    return "\n".join(lines)


def prepare_order(items: list[dict], address_id: int | None = None) -> str:
    """准备下单草稿（**不会真正下单**，必须用户在确认卡片里点确认）。

    :param items: 商品行列表，每行形如
        {"productId": 1, "skuId": 2, "quantity": 1}
    :param address_id: 收货地址 id；不传则用默认地址
    """
    if not items:
        return "请先确认要购买的商品和数量。"

    normalized = []
    for raw in items:
        if not isinstance(raw, dict):
            return "商品行格式不正确，请提供 productId / skuId / quantity。"
        try:
            normalized.append({
                "productId": int(raw.get("productId")),
                "skuId": int(raw.get("skuId")),
                "quantity": int(raw.get("quantity") or 1),
            })
        except (TypeError, ValueError):
            return "商品行格式不正确，请提供数字类型的 productId / skuId / quantity。"

    payload: dict = {"items": normalized}
    if address_id is not None:
        try:
            payload["addressId"] = int(address_id)
        except (TypeError, ValueError):
            return "收货地址 id 不正确。"

    data, error = call("/internal/agent/actions/order", payload)
    if error:
        return error
    summary = (data or {}).get("summary") or {}
    address = summary.get("address") or {}
    return (
        "订单草稿已生成（尚未下单）。请让用户在下方确认卡片里核对并点击「确认下单」；"
        "确认后才会生成待支付订单，支付仍需用户到订单详情页完成。\n"
        f"摘要：共 {summary.get('itemCount', 0)} 件，"
        f"应付 ¥{summary.get('totalAmount', '?')}；"
        f"收货人 {address.get('receiver') or '未知'}，"
        f"地址 {address.get('address') or '未知'}。")


def prepare_refund(order_no: str) -> str:
    """准备退款申请草稿（**不会真正提交**，必须用户在确认卡片里点确认）。"""
    if not (order_no or "").strip():
        return "请提供要退款的订单号。"

    data, error = call("/internal/agent/actions/refund",
                       {"orderNo": order_no.strip()})
    if error:
        return error
    summary = (data or {}).get("summary") or {}
    return (
        "退款申请草稿已生成（尚未提交）。请让用户在下方确认卡片里核对并点击"
        "「确认提交退款申请」；确认后才会提交给商家。\n"
        f"订单 {summary.get('orderNo') or order_no}，"
        f"金额 ¥{summary.get('totalAmount', '?')}，"
        f"当前状态 {summary.get('statusLabel') or '未知'}。")


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_order_actions_tools() -> list:
    """下单/退款工具：无票据不发请求、参数正确、草稿口径明确、错误可读。"""
    import httpx

    from app.agent.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post, real_get = httpx.post, httpx.get

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    # ① 没有票据：不发请求
    calls = {"n": 0}

    def _no_call(*a, **k):
        calls["n"] += 1
        raise AssertionError("没有票据时不应调用 Java")

    httpx.post = httpx.get = _no_call
    try:
        text = prepare_order([{"productId": 1, "skuId": 2, "quantity": 1}])
        if calls["n"] != 0:
            problems.append("无票据时仍发起了 HTTP 请求")
        if "权限" not in text:
            problems.append(f"无票据提示不清晰：{text!r}")
    finally:
        httpx.post, httpx.get = real_post, real_get

    # ② 正常路径：地址 / 下单草稿 / 退款草稿
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.clear()
        captured.update(url=url, json=json, headers=headers)
        if url.endswith("/actions/order"):
            return _Resp({"code": 200, "data": {
                "actionNo": "a1", "type": "CREATE_ORDER", "status": "PENDING_CONFIRM",
                "summary": {"itemCount": 2, "totalAmount": "398.00",
                            "address": {"receiver": "张三", "address": "杭州市"}},
                "result": None, "errorMessage": "",
                "expiresAt": "", "createdAt": "",
            }})
        if url.endswith("/actions/refund"):
            return _Resp({"code": 200, "data": {
                "actionNo": "a2", "type": "APPLY_REFUND", "status": "PENDING_CONFIRM",
                "summary": {"orderNo": "NO123", "totalAmount": "199.00",
                            "statusLabel": "已支付"},
                "result": None, "errorMessage": "",
                "expiresAt": "", "createdAt": "",
            }})
        raise AssertionError(f"unexpected url {url}")

    def _fake_get(url, headers=None, timeout=None):
        captured.clear()
        captured.update(url=url, headers=headers)
        return _Resp({"code": 200, "data": [{
            "id": 7, "receiver": "张三", "phone": "13800000000",
            "address": "浙江省杭州市", "isDefault": True,
        }]})

    token = set_tool_context("ticket-abc", "42")
    httpx.post, httpx.get = _fake_post, _fake_get
    try:
        text = prepare_order([{"productId": 1, "skuId": 11, "quantity": 2}],
                             address_id=7)
        if captured["json"].get("addressId") != 7:
            problems.append("下单草稿未带地址 id")
        if captured["json"]["items"][0].get("skuId") != 11:
            problems.append("下单草稿未带 skuId")
        if "尚未下单" not in text:
            problems.append(f"下单草稿口径不清晰：{text!r}")

        text = prepare_refund("NO123")
        if captured["json"].get("orderNo") != "NO123":
            problems.append("退款草稿未带订单号")
        if "尚未提交" not in text:
            problems.append(f"退款草稿口径不清晰：{text!r}")

        text = list_addresses()
        if "id=7" not in text or "默认" not in text:
            problems.append(f"地址列表格式不正确：{text!r}")
        if not captured["url"].endswith("/catalog/addresses"):
            problems.append("地址列表未走 GET 接口")
    finally:
        httpx.post, httpx.get = real_post, real_get
        reset_tool_context(token)

    # ③ 业务错误：转成可读文本，不抛异常
    def _err_post(url, json=None, headers=None, timeout=None):
        return _Resp({"code": 400, "message": "库存不足"})

    token = set_tool_context("ticket", "1")
    httpx.post = _err_post
    try:
        text = prepare_order([{"productId": 1, "skuId": 2, "quantity": 1}])
        if "库存不足" not in text:
            problems.append(f"业务错误提示不清晰：{text!r}")
    finally:
        httpx.post = real_post
        reset_tool_context(token)

    return problems
