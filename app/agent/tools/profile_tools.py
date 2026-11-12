"""用户画像工具：把 Java 聚合出的购买/浏览偏好翻译成导购可用的中文摘要。

数据源是 Java 内部接口（只读）：
`GET /internal/agent/profile`，只返回当前票据用户的聚合事实，
不返回订单号、地址或聊天原文。本工具不做任何金额推算。

画像只用于导购筛选与排序；回复用户时不要直接报出累计消费/订单明细。
"""
from app.agent.subagents.order_service.tools.its_mygo_client import call


def _names(items, limit: int = 3) -> str:
    names = []
    for item in items or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        if len(names) >= limit:
            break
    return "、".join(names)


def _price_band(band) -> str:
    if not isinstance(band, dict):
        return ""
    low, high = band.get("min"), band.get("max")
    if low is None or high is None:
        return ""
    return f"¥{low}~¥{high}"


def get_user_profile() -> str:
    """查询当前登录用户的消费/浏览画像，用于导购推荐。

    :return: 给模型看的画像摘要；没有记录/接口异常时返回可读提示
    """
    data, error = call("/internal/agent/profile", method="GET", read_only=True)
    if error:
        return error
    if not isinstance(data, dict):
        return "暂时没拿到用户画像，先按用户这次说的预算和用途推荐。"

    if data.get("coldStart"):
        return "用户还没有足够的购买/浏览记录（冷启动），先问清预算和用途再推荐，不要臆测偏好。"

    lines = []
    order_count = int(data.get("orderCount") or 0)
    if order_count > 0:
        lines.append(f"用户有 {order_count} 笔已支付订单。")
    categories = _names(data.get("topCategories"))
    if categories:
        lines.append(f"常买品类：{categories}。")
    brands = _names(data.get("topBrands"))
    if brands:
        lines.append(f"常买品牌：{brands}。")
    band = _price_band(data.get("priceBand"))
    if band:
        lines.append(f"常买价格带：{band}（优先在这个带内挑，不要硬拒绝更便宜或略贵的）。")
    views = _names(data.get("recentViews"))
    if views:
        lines.append(f"近期浏览品类：{views}。")
    favorites = _names(data.get("favoriteCategories"))
    if favorites:
        lines.append(f"收藏品类：{favorites}。")
    if data.get("avgRating") is not None:
        lines.append(f"历史评价均分：{data['avgRating']}。")
    lines.append("推荐时优先匹配以上品类/品牌/价格带；对用户不要直接复述累计消费金额或订单明细。")
    return "\n".join(lines)


# ── 离线自测（registry 见 app/core/regression.py）──

def _check_user_profile_tool() -> list:
    """回归：路径/只读、正常摘要、冷启动与错误降级。"""
    problems: list = []
    real_call = call
    captured: dict = {}

    def _fake(path, method="POST", read_only=False, params=None, **kwargs):
        captured.update(path=path, method=method, read_only=read_only)
        return {
            "coldStart": False, "orderCount": 3,
            "topCategories": [{"categoryId": 1, "name": "办公设备", "count": 2}],
            "topBrands": [{"name": "H3C", "count": 2}],
            "priceBand": {"min": "40.00", "avg": "50.00", "max": "60.00"},
            "recentViews": [{"categoryId": 2, "name": "打印耗材", "count": 1}],
            "favoriteCategories": [{"categoryId": 3, "name": "数码配件", "count": 1}],
            "avgRating": 4.8,
        }, ""

    globals()["call"] = _fake
    try:
        text = get_user_profile()
        if captured.get("path") != "/internal/agent/profile":
            problems.append(f"接口路径不对：{captured}")
        if captured.get("method") != "GET" or not captured.get("read_only"):
            problems.append(f"应走只读 GET：{captured}")
        for expected in ("3 笔已支付订单", "办公设备", "H3C", "¥40.00~¥60.00", "4.8"):
            if expected not in text:
                problems.append(f"画像摘要缺少 {expected!r}：{text!r}")

        globals()["call"] = lambda *a, **k: ({"coldStart": True}, "")
        cold = get_user_profile()
        if "冷启动" not in cold:
            problems.append(f"冷启动提示不对：{cold!r}")

        globals()["call"] = lambda *a, **k: (None, "平台服务暂时不可用，请稍后再试。")
        err = get_user_profile()
        if "暂时不可用" not in err:
            problems.append(f"错误未转述：{err!r}")
    finally:
        globals()["call"] = real_call
    return problems
