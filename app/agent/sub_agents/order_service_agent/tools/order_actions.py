"""
M9 / M10：下单与退款申请的工具层。

**这些工具只生成「草稿」（PENDING_CONFIRM），不会真正执行**——
执行由用户在聊天里的确认卡片点击确认后，Java 按服务端快照调用业务 Service。
所以在工具返回里必须明确说「尚未下单 / 尚未提交」，主 agent 不许声称已成功。

2026-11-07「行动能力」改造：
- **动作前预检**（只读、不产生草稿）：下单先核对 SKU / 库存 / 地址，退款与催办先核对
  订单状态；不通过就返回「预检未通过：…」，**不调用**写接口，减少确认卡片被 Java 拒绝；
- **批量草稿**：多个订单号时循环调用单条工具，逐单生成草稿（确认仍需逐张点），
  单条失败不影响其余，最后给「成功 N / 失败 M」汇总。
"""
from app.agent.sub_agents.order_service_agent.tools.its_mygo_client import call

# 批量操作单次上限：再多就该引导用户分批，避免一次弹十几张卡片
BATCH_LIMIT = 10

# 各动作允许的订单状态（与 Java 侧校验口径一致；预检只是提前拦，最终仍由 Java 判定）
REFUNDABLE_STATUSES = {"PAID", "SHIPPED", "COMPLETED"}
URGE_SHIPPABLE_STATUSES = {"PAID"}
URGE_REFUNDABLE_STATUSES = {"REFUNDING"}


def list_addresses() -> str:
    """列出当前用户的收货地址（含地址 id，供 prepare_order 使用）。"""
    data, error = call("/internal/agent/catalog/addresses", method="GET", read_only=True)
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


# ── 动作前预检（只读） ──

def _precheck_address(address_id: int) -> str:
    """校验收货地址 id 存在；通过返回空串，不通过返回给用户看的原因。"""
    data, error = call("/internal/agent/catalog/addresses", method="GET", read_only=True)
    if error:
        return f"预检失败：{error}"
    ids = {a.get("id") for a in (data or []) if isinstance(a, dict)}
    if address_id not in ids:
        return f"预检未通过：收货地址 id={address_id} 不存在，请重新选择收货地址。"
    return ""


def _precheck_items(normalized: list[dict]) -> str:
    """
    校验商品行：商品在售、SKU 存在、库存足够。

    数据源用 `GET /catalog/all`（Java 侧目前只支持关键词搜索，没有按 productId 查；
    目录条数超过单次上限时**跳过缺失商品的预检**，交给 Java 最终校验，不误拦）。
    """
    data, error = call("/internal/agent/catalog/all", method="GET", read_only=True,
                       params={"limit": 200})
    if error:
        return f"预检失败：{error}"
    products = [p for p in (data or []) if isinstance(p, dict)]
    capped = len(products) >= 200
    index = {}
    for product in products:
        skus = {s.get("skuId"): s for s in (product.get("skus") or []) if isinstance(s, dict)}
        index[product.get("productId")] = {"name": product.get("name") or "未知商品",
                                           "skus": skus}
    for item in normalized:
        product = index.get(item["productId"])
        if product is None:
            if capped:
                continue    # 目录可能没取全，别因为「没取到」误拦
            return (f"预检未通过：商品 id={item['productId']} 不在售（可能已下架），"
                    f"请重新选择商品。")
        sku = product["skus"].get(item["skuId"])
        if sku is None:
            return (f"预检未通过：商品「{product['name']}」没有 skuId={item['skuId']} "
                    f"这个规格，请重新选择规格。")
        try:
            stock = int(sku.get("stock") or 0)
        except (TypeError, ValueError):
            stock = 0
        if stock < item["quantity"]:
            spec = sku.get("specText") or "默认规格"
            return (f"预检未通过：商品「{product['name']}」（{spec}）库存只剩 {stock} 件，"
                    f"不够 {item['quantity']} 件；可以少买几件或换个规格。")
    return ""


def _precheck_order_status(order_no: str, allowed: set, action_cn: str) -> str:
    """校验订单状态是否允许该动作；通过返回空串。"""
    data, error = call("/internal/agent/orders/query",
                       {"orderNo": order_no, "limit": 1}, read_only=True)
    if error:
        return f"预检失败：{error}"
    orders = [o for o in (data or []) if isinstance(o, dict)]
    if not orders:
        return f"预检未通过：没查到你名下的订单 {order_no}，请核对订单号。"
    order = orders[0]
    status = str(order.get("status") or "")
    if status not in allowed:
        label = order.get("statusLabel") or status or "未知状态"
        return f"预检未通过：订单 {order_no} 当前是「{label}」，不能{action_cn}。"
    return ""


# ── 单条草稿 ──

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
        precheck = _precheck_address(payload["addressId"])
        if precheck:
            return precheck

    precheck = _precheck_items(normalized)
    if precheck:
        return precheck

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
    no = (order_no or "").strip()
    if not no:
        return "请提供要退款的订单号。"
    precheck = _precheck_order_status(no, REFUNDABLE_STATUSES, "申请退款")
    if precheck:
        return precheck

    data, error = call("/internal/agent/actions/refund", {"orderNo": no})
    if error:
        return error
    summary = (data or {}).get("summary") or {}
    return (
        "退款申请草稿已生成（尚未提交）。请让用户在下方确认卡片里核对并点击"
        "「确认提交退款申请」；确认后才会提交给商家。\n"
        f"订单 {summary.get('orderNo') or no}，"
        f"金额 ¥{summary.get('totalAmount', '?')}，"
        f"当前状态 {summary.get('statusLabel') or '未知'}。")


def prepare_urge_shipment(order_no: str) -> str:
    """准备催发货消息草稿（**不会直接发送**，必须用户在确认卡片里点确认）。"""
    no = (order_no or "").strip()
    if not no:
        return "请提供要催发货的订单号。"
    precheck = _precheck_order_status(no, URGE_SHIPPABLE_STATUSES, "催发货")
    if precheck:
        return precheck

    data, error = call("/internal/agent/actions/urge-shipment", {"orderNo": no})
    if error:
        return error
    summary = (data or {}).get("summary") or {}
    return (
        "催发货消息草稿已生成（尚未发送）。请让用户在下方确认卡片里核对并点击"
        "「确认催发货」；确认后才会通知商家。\n"
        f"订单 {summary.get('orderNo') or no}，"
        f"店铺 {summary.get('merchantName') or '未知'}，"
        f"当前状态 {summary.get('statusLabel') or '未知'}。")


def prepare_urge_refund(order_no: str) -> str:
    """准备催退款消息草稿（**不会直接发送**，必须用户在确认卡片里点确认）。"""
    no = (order_no or "").strip()
    if not no:
        return "请提供要催退款的订单号。"
    precheck = _precheck_order_status(no, URGE_REFUNDABLE_STATUSES, "催退款")
    if precheck:
        return precheck

    data, error = call("/internal/agent/actions/urge-refund", {"orderNo": no})
    if error:
        return error
    summary = (data or {}).get("summary") or {}
    return (
        "催退款消息草稿已生成（尚未发送）。请让用户在下方确认卡片里核对并点击"
        "「确认催退款」；确认后才会提醒商家处理退款。\n"
        f"订单 {summary.get('orderNo') or no}，"
        f"店铺 {summary.get('merchantName') or '未知'}，"
        f"当前状态 {summary.get('statusLabel') or '未知'}，"
        f"退款原因 {summary.get('refundReason') or '未填写'}。")


# ── 批量草稿（循环单条接口，逐单一张卡片） ──

def _clean_order_nos(order_nos) -> list:
    """把模型可能传成字符串/列表的订单号收敛成去重列表（保持原顺序）。"""
    if isinstance(order_nos, str):
        raw_items = order_nos.replace("，", ",").replace("、", ",").replace(" ", ",").split(",")
    elif isinstance(order_nos, (list, tuple)):
        raw_items = list(order_nos)
    else:
        return []
    cleaned, seen = [], set()
    for item in raw_items:
        no = str(item or "").strip()
        if not no or no in seen:
            continue
        seen.add(no)
        cleaned.append(no)
    return cleaned


def _batch_result(action_cn: str, order_nos, single) -> str:
    """批量执行单条草稿工具并汇总；单条失败不中断其余。"""
    cleaned = _clean_order_nos(order_nos)
    if not cleaned:
        return f"请提供至少一个要{action_cn}的订单号。"
    if len(cleaned) > BATCH_LIMIT:
        return f"一次最多处理 {BATCH_LIMIT} 单，请分批发给我。"
    succeeded, failed = [], []
    for no in cleaned:
        text = single(no)
        if text.startswith("预检未通过") or text.startswith("预检失败") \
                or text.startswith("操作未完成") or text.startswith("平台服务暂时不可用") \
                or text.startswith("平台工具凭证") or "没有提交成功" in text:
            failed.append(f"- {no}：{text}")
        else:
            succeeded.append(no)
    lines = [f"批量{action_cn}：成功 {len(succeeded)} 单，失败 {len(failed)} 单。"]
    if succeeded:
        lines.append("已生成草稿（**尚未发送**，请逐张确认）：" + "、".join(succeeded))
    if failed:
        lines.append("未生成草稿：")
        lines.extend(failed)
    lines.append("每单都需要用户在对应的确认卡片上点击确认后才会真正执行。")
    return "\n".join(lines)


def prepare_refunds(order_nos) -> str:
    """批量准备退款申请草稿（逐单一张确认卡片，**不会提交**）。"""
    return _batch_result("退款申请", order_nos, prepare_refund)


def prepare_urge_shipments(order_nos) -> str:
    """批量准备催发货草稿（逐单一张确认卡片，**不会发送**）。"""
    return _batch_result("催发货", order_nos, prepare_urge_shipment)


def prepare_urge_refunds(order_nos) -> str:
    """批量准备催退款草稿（逐单一张确认卡片，**不会发送**）。"""
    return _batch_result("催退款", order_nos, prepare_urge_refund)


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

class _FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._body


def _check_order_actions_tools() -> list:
    """下单/退款/催发货工具：无票据不发请求、参数正确、草稿口径明确、错误可读。"""
    import httpx

    from app.agent.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post, real_get = httpx.post, httpx.get
    state = {"status": "PAID", "catalog_stock": 10}

    def _fake_get(url, headers=None, timeout=None, params=None):
        if "/catalog/addresses" in url:
            return _FakeResp({"code": 200, "data": [{
                "id": 7, "receiver": "张三", "phone": "13800000000",
                "address": "浙江省杭州市", "isDefault": True,
            }]})
        if "/catalog/all" in url:
            return _FakeResp({"code": 200, "data": [{
                "productId": 1, "name": "测试商品",
                "skus": [{"skuId": 11, "specText": "颜色:白",
                          "price": "199.00", "stock": state["catalog_stock"]},
                         {"skuId": 2, "specText": "默认规格",
                          "price": "9.90", "stock": 999}],
            }]})
        raise AssertionError(f"unexpected GET {url}")

    def _fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/orders/query"):
            return _FakeResp({"code": 200, "data": [{
                "orderNo": (json or {}).get("orderNo") or "NO123",
                "status": state["status"], "statusLabel": "待发货",
                "merchantName": "平台自营", "totalAmount": "199.00",
                "trackingNo": "", "createdAt": "2026-11-07 10:00:00",
                "items": [],
            }]})
        if url.endswith("/actions/order"):
            return _FakeResp({"code": 200, "data": {
                "actionNo": "a1", "type": "CREATE_ORDER", "status": "PENDING_CONFIRM",
                "summary": {"itemCount": 2, "totalAmount": "398.00",
                            "address": {"receiver": "张三", "address": "杭州市"}},
            }})
        if url.endswith("/actions/refund"):
            return _FakeResp({"code": 200, "data": {
                "summary": {"orderNo": "NO123", "totalAmount": "199.00",
                            "statusLabel": "待发货"},
            }})
        if url.endswith("/actions/urge-shipment"):
            return _FakeResp({"code": 200, "data": {
                "summary": {"orderNo": "NO123", "merchantName": "平台自营",
                            "statusLabel": "待发货"},
            }})
        if url.endswith("/actions/urge-refund"):
            return _FakeResp({"code": 200, "data": {
                "summary": {"orderNo": "NO123", "merchantName": "平台自营",
                            "statusLabel": "退款中", "refundReason": "商品有瑕疵"},
            }})
        raise AssertionError(f"unexpected POST {url}")

    # ① 没有票据：不发请求
    calls = {"n": 0}

    def _no_call(*a, **k):
        calls["n"] += 1
        raise AssertionError("没有票据时不应调用 Java")

    httpx.post = httpx.get = _no_call
    try:
        for text in (prepare_order([{"productId": 1, "skuId": 2, "quantity": 1}]),
                     prepare_urge_shipment("NO123"),
                     prepare_urge_refund("NO123")):
            if "权限" not in text:
                problems.append(f"无票据提示不清晰：{text!r}")
        if calls["n"] != 0:
            problems.append("无票据时仍发起了 HTTP 请求")
    finally:
        httpx.post, httpx.get = real_post, real_get

    # ② 正常路径：地址 / 下单草稿 / 退款草稿 / 催办草稿
    token = set_tool_context("ticket-abc", "42")
    httpx.post, httpx.get = _fake_post, _fake_get
    try:
        text = prepare_order([{"productId": 1, "skuId": 11, "quantity": 2}],
                             address_id=7)
        if "尚未下单" not in text:
            problems.append(f"下单草稿口径不清晰：{text!r}")

        state["status"] = "PAID"
        text = prepare_refund("NO123")
        if "尚未提交" not in text:
            problems.append(f"退款草稿口径不清晰：{text!r}")

        text = prepare_urge_shipment("NO123")
        if "尚未发送" not in text:
            problems.append(f"催发货草稿口径不清晰：{text!r}")

        state["status"] = "REFUNDING"
        text = prepare_urge_refund("NO123")
        if "尚未发送" not in text or "确认催退款" not in text:
            problems.append(f"催退款草稿口径不清晰：{text!r}")

        text = list_addresses()
        if "id=7" not in text or "默认" not in text:
            problems.append(f"地址列表格式不正确：{text!r}")
    finally:
        httpx.post, httpx.get = real_post, real_get
        reset_tool_context(token)

    # ③ 业务错误：转成可读文本，不抛异常
    def _err_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/orders/query"):
            return _FakeResp({"code": 200, "data": [{
                "orderNo": "NO123", "status": "PAID", "statusLabel": "待发货"}]})
        return _FakeResp({"code": 400, "message": "库存不足"})

    token = set_tool_context("ticket", "1")
    httpx.post, httpx.get = _err_post, _fake_get
    try:
        text = prepare_order([{"productId": 1, "skuId": 2, "quantity": 1}])
        if "库存不足" not in text:
            problems.append(f"业务错误提示不清晰：{text!r}")
    finally:
        httpx.post, httpx.get = real_post, real_get
        reset_tool_context(token)

    # ④ 写操作超时：**只发一次请求、不重试**（否则会生成重复草稿）
    import httpx as _httpx
    wstate = {"writes": 0}

    def _timeout_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/actions/order"):
            wstate["writes"] += 1
            raise _httpx.ReadTimeout("timeout")
        return _FakeResp({"code": 200, "data": {}})

    token = set_tool_context("ticket", "1")
    httpx.post, httpx.get = _timeout_post, _fake_get
    try:
        text = prepare_order([{"productId": 1, "skuId": 11, "quantity": 1}])
        if wstate["writes"] != 1:
            problems.append(f"写操作超时应只调用 1 次，实为 {wstate['writes']} 次")
        if "没有提交成功" not in text:
            problems.append(f"写操作超时文案缺少「没有提交成功」：{text!r}")
    finally:
        httpx.post, httpx.get = real_post, real_get
        reset_tool_context(token)

    return problems


def _check_order_precheck() -> list:
    """动作前预检：库存不足 / 规格不存在 / 地址不存在 / 订单状态不允许时不创建草稿。"""
    import httpx

    from app.agent.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post, real_get = httpx.post, httpx.get
    state = {"stock": 1, "status": "PENDING_PAYMENT", "writes": []}

    def _get(url, headers=None, timeout=None, params=None):
        if "/catalog/addresses" in url:
            return _FakeResp({"code": 200, "data": [{"id": 7, "receiver": "张三",
                                                     "address": "杭州市", "isDefault": True}]})
        if "/catalog/all" in url:
            return _FakeResp({"code": 200, "data": [{
                "productId": 1, "name": "测试商品",
                "skus": [{"skuId": 11, "specText": "颜色:白", "stock": state["stock"]}],
            }]})
        raise AssertionError(f"unexpected GET {url}")

    def _post(url, json=None, headers=None, timeout=None):
        if url.endswith("/orders/query"):
            return _FakeResp({"code": 200, "data": [{
                "orderNo": "NO123", "status": state["status"], "statusLabel": "待支付"}]})
        state["writes"].append(url)
        return _FakeResp({"code": 200, "data": {"summary": {}}})

    token = set_tool_context("ticket", "1")
    httpx.post, httpx.get = _post, _get
    try:
        # ① 库存不足：拦下、不调用写接口
        text = prepare_order([{"productId": 1, "skuId": 11, "quantity": 2}], address_id=7)
        if "预检未通过" not in text or "库存" not in text:
            problems.append(f"库存不足未被预检拦下：{text!r}")
        if state["writes"]:
            problems.append(f"库存不足时仍调用了写接口：{state['writes']}")

        # ② 规格不存在
        state["stock"] = 99
        text = prepare_order([{"productId": 1, "skuId": 999, "quantity": 1}], address_id=7)
        if "预检未通过" not in text or "规格" not in text:
            problems.append(f"错误规格未被预检拦下：{text!r}")
        if state["writes"]:
            problems.append(f"规格错误时仍调用了写接口：{state['writes']}")

        # ③ 地址不存在
        text = prepare_order([{"productId": 1, "skuId": 11, "quantity": 1}], address_id=99)
        if "预检未通过" not in text or "地址" not in text:
            problems.append(f"错误地址未被预检拦下：{text!r}")

        # ④ 订单状态不允许退款：拦下、不调用写接口
        text = prepare_refund("NO123")
        if "预检未通过" not in text or "不能申请退款" not in text:
            problems.append(f"不可退款状态未被预检拦下：{text!r}")
        if state["writes"]:
            problems.append(f"不可退款状态仍调用了写接口：{state['writes']}")

        # ⑤ 状态允许时正常出草稿
        state["status"] = "SHIPPED"
        text = prepare_refund("NO123")
        if "尚未提交" not in text:
            problems.append(f"可退款状态没有生成草稿：{text!r}")
    finally:
        httpx.post, httpx.get = real_post, real_get
        reset_tool_context(token)
    return problems


def _check_batch_actions() -> list:
    """批量草稿：逐单调用、单条失败不影响其余、汇总成功/失败、逐张确认口径。"""
    import httpx

    from app.agent.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post, real_get = httpx.post, httpx.get
    state = {"urge_calls": []}

    def _get(url, headers=None, timeout=None, params=None):
        raise AssertionError(f"unexpected GET {url}")

    def _post(url, json=None, headers=None, timeout=None):
        if url.endswith("/orders/query"):
            return _FakeResp({"code": 200, "data": [{
                "orderNo": (json or {}).get("orderNo"), "status": "PAID",
                "statusLabel": "待发货"}]})
        if url.endswith("/actions/urge-shipment"):
            no = (json or {}).get("orderNo")
            state["urge_calls"].append(no)
            if no == "NO2":
                return _FakeResp({"code": 400, "message": "该订单不在可催范围"})
            return _FakeResp({"code": 200, "data": {
                "summary": {"orderNo": no, "merchantName": "平台自营",
                            "statusLabel": "待发货"}}})
        raise AssertionError(f"unexpected POST {url}")

    token = set_tool_context("ticket", "1")
    httpx.post, httpx.get = _post, _get
    try:
        text = prepare_urge_shipments("NO1, NO2，NO1")
        if state["urge_calls"] != ["NO1", "NO2"]:
            problems.append(f"批量去重/逐单调用不对：{state['urge_calls']}")
        for expected in ("成功 1 单", "失败 1 单", "逐张确认", "该订单不在可催范围"):
            if expected not in text:
                problems.append(f"批量汇总缺少 {expected!r}：{text!r}")
        if not text.startswith("批量催发货"):
            problems.append(f"批量汇总标题不对：{text!r}")

        # 超过上限直接拒绝（不发请求）
        state["urge_calls"].clear()
        many = ",".join(f"NO{i}" for i in range(BATCH_LIMIT + 1))
        text = prepare_urge_shipments(many)
        if "最多处理" not in text or state["urge_calls"]:
            problems.append(f"批量上限未生效：{text!r} / {state['urge_calls']}")
    finally:
        httpx.post, httpx.get = real_post, real_get
        reset_tool_context(token)
    return problems
