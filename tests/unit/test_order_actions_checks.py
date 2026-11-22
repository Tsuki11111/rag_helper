"""订单动作工具回归用例（从 api/actions.py 迁出）。"""
import httpx
import pytest

import app.agent.subagents.order_service.api.actions as _aa

globals().update({k: v for k, v in vars(_aa).items() if not k.startswith("__")})


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

    from app.agent.runtime.tool_context import reset_tool_context, set_tool_context

    problems = []
    real_post, real_get = httpx.post, httpx.get
    state = {"status": "PAID", "catalog_stock": 10, "last": {}}

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
        state["last"] = {"url": url, "json": json}
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
                            "statusLabel": "待发货", "refundReason": "商品有瑕疵"},
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
        text = prepare_refund("NO123", reason="商品有瑕疵")
        if "尚未提交" not in text:
            problems.append(f"退款草稿口径不清晰：{text!r}")
        if (state["last"].get("json") or {}).get("reason") != "商品有瑕疵":
            problems.append(f"退款原因未传给 Java：{state['last'].get('json')}")
        if "商品有瑕疵" not in text:
            problems.append(f"退款原因未回显：{text!r}")

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

    from app.agent.runtime.tool_context import reset_tool_context, set_tool_context

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

    from app.agent.runtime.tool_context import reset_tool_context, set_tool_context

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

CHECKS = [
    ("_check_order_actions_tools", _check_order_actions_tools),
    ("_check_order_precheck", _check_order_precheck),
    ("_check_batch_actions", _check_batch_actions),
]

@pytest.mark.parametrize("name,fn", CHECKS, ids=[n for n, _ in CHECKS])
def test_order_actions_pure(name, fn):
    assert fn() == []
