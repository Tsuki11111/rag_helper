"""消费报告工具：把平台侧聚合的「个人消费报告」翻译成用户能听懂的摘要。

数据源是 Java 内部接口（只读）：
`GET /internal/agent/reports/consumption?period=month|year&date=YYYY-MM|YYYY`
返回个人维度的金额/订单数/品类/店铺/商品排行；本工具只做中文排版，
**不做任何金额推算**（所有数字必须来自接口返回）。

用于客服对话里的「我今年花了多少钱 / 帮我看看这个月消费」这类问题。
"""
from app.agent.sub_agents.order_service_agent.tools.its_mygo_client import call

_PERIOD_LABEL = {"month": "本月", "year": "本年", "all": "全部"}


def get_consumption_report(period: str = "month", date: str = "") -> str:
    """查询当前登录用户的个人消费报告（金额、订单数、品类/店铺/商品排行）。

    :param period: month（按月，默认）/ year（按年）/ all（全部时间）
    :param date: 指定月份 `YYYY-MM` 或年份 `YYYY`；不传则取最近一个月/年
    :return: 给用户看的消费摘要；出错时返回可读提示
    """
    clean_period = (period or "month").strip().lower()
    if clean_period not in ("month", "year", "all"):
        clean_period = "month"
    params = {"period": clean_period}
    clean_date = (date or "").strip()
    if clean_date:
        params["date"] = clean_date
    # 只读调用：允许超时/5xx 有限重试（见 its_mygo_client 的失败三分）
    data, error = call("/internal/agent/reports/consumption", method="GET",
                       read_only=True, params=params)
    if error:
        return error
    if not isinstance(data, dict):
        return "暂时没拿到消费报告，请稍后再试。"

    label = data.get("label") or _PERIOD_LABEL.get(clean_period, "本期")
    order_count = int(data.get("orderCount") or 0)
    if order_count <= 0:
        return f"{label}还没有已支付的订单，暂无消费报告。"

    lines = [
        f"{label}消费报告：共 {order_count} 笔订单，"
        f"实付 ¥{data.get('paidAmount') or '0.00'}，"
        f"退款 ¥{data.get('refundedAmount') or '0.00'}，"
        f"净支出 ¥{data.get('netAmount') or '0.00'}。",
    ]
    if data.get("discountAmount") and str(data["discountAmount"]) not in ("0.00", "0"):
        lines.append(f"优惠共省 ¥{data['discountAmount']}；单笔均价 ¥{data.get('avgOrderAmount') or '—'}。")

    categories = [c for c in (data.get("categories") or []) if isinstance(c, dict)][:3]
    if categories:
        text = "、".join(f"{c.get('name') or '其他'} ¥{c.get('amount')}" for c in categories)
        lines.append(f"花得最多的品类：{text}。")

    merchants = [m for m in (data.get("merchants") or []) if isinstance(m, dict)][:3]
    if merchants:
        text = "、".join(f"{m.get('name') or '店铺'} ¥{m.get('amount')}" for m in merchants)
        lines.append(f"常买的店铺：{text}。")

    products = [p for p in (data.get("topProducts") or []) if isinstance(p, dict)][:3]
    if products:
        text = "、".join(f"{p.get('name') or '商品'}（{p.get('count') or 0} 件）" for p in products)
        lines.append(f"买得最多的商品：{text}。")

    monthly = [m for m in (data.get("monthly") or []) if isinstance(m, dict)]
    if clean_period == "year" and monthly:
        peak = max(monthly, key=lambda m: float(m.get("amount") or 0))
        lines.append(f"全年 {len(monthly)} 个月有消费，最高是 {peak.get('month')}"
                     f"（¥{peak.get('amount')}）。")
    return "\n".join(lines)


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_consumption_report() -> list:
    """回归：参数正确、金额只来自接口、空数据/错误都有可读文案。"""
    problems: list = []
    real_call = call
    captured: dict = {}

    def _fake(path, method="POST", read_only=False, params=None, **kwargs):
        captured.update(path=path, method=method, read_only=read_only, params=params)
        return {
            "label": "2026年11月", "orderCount": 3,
            "paidAmount": "358.00", "refundedAmount": "0.00", "netAmount": "358.00",
            "discountAmount": "20.00", "avgOrderAmount": "119.33",
            "categories": [{"name": "办公设备", "amount": "199.00", "count": 2}],
            "merchants": [{"name": "平台自营", "amount": "358.00"}],
            "topProducts": [{"name": "RS-12 数字万用表", "amount": "318.00", "count": 2}],
            "monthly": [{"month": "2026-11", "amount": "358.00"}],
        }, ""

    globals()["call"] = _fake
    try:
        text = get_consumption_report("month", "2026-11")
        if captured.get("path") != "/internal/agent/reports/consumption":
            problems.append(f"接口路径不对：{captured}")
        if captured.get("method") != "GET" or not captured.get("read_only"):
            problems.append(f"应走只读 GET：{captured}")
        if (captured.get("params") or {}).get("period") != "month":
            problems.append(f"参数不对：{captured.get('params')}")
        for expected in ("共 3 笔订单", "¥358.00", "办公设备", "平台自营", "RS-12"):
            if expected not in text:
                problems.append(f"摘要缺少 {expected!r}：{text!r}")

        # 无订单 → 明确说没有
        globals()["call"] = lambda *a, **k: ({"label": "2026年11月", "orderCount": 0}, "")
        empty = get_consumption_report("month")
        if "还没有已支付" not in empty:
            problems.append(f"空报告文案不对：{empty!r}")

        # 接口错误 → 原样转述可读提示
        globals()["call"] = lambda *a, **k: (None, "平台服务暂时不可用，请稍后再试。")
        err = get_consumption_report("month")
        if "暂时不可用" not in err:
            problems.append(f"错误未转述：{err!r}")
    finally:
        globals()["call"] = real_call
    return problems
