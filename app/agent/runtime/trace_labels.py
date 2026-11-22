"""执行过程的中文标签：只给用户看“在做什么”，不暴露工具名/参数/原文。"""

TRACE_TOOL_LABELS = {
    "search_products": "查询在售商品",
    "semantic_search_products": "语义检索商品",
    "list_categories": "查询商品分类",
    "query_orders": "查询订单",
    "query_order": "查询订单",
    "list_addresses": "查询收货地址",
    "prepare_order": "整理下单草稿",
    "prepare_refund": "整理退款草稿",
    "prepare_urge_shipment": "整理催发货草稿",
    "prepare_urge_refund": "整理催退款草稿",
    "prepare_refunds": "批量整理退款草稿",
    "prepare_urge_shipments": "批量整理催发货草稿",
    "prepare_urge_refunds": "批量整理催退款草稿",
    "get_consumption_report": "汇总消费报告",
    "search_tone_faq": "匹配话术参考",
    "search_platform_policy": "检索平台政策",
    "get_user_profile": "读取用户偏好",
    "request_refund_reason_form": "收集退款原因",
    "request_address_edit_form": "收集新地址",
}
TRACE_MAIN_AGENT = "买小购"
TRACE_SUBAGENT_NAMES = {
    "product-kb": "产品知识库智能体",
    "product-guide": "商品导购智能体",
    "policy-agent": "平台政策智能体",
    "order-service": "订单服务智能体",
}
TRACE_SUBAGENT_ACTIONS = {
    "product-kb": "检索资料",
    "product-guide": "推荐商品",
    "policy-agent": "查询政策",
    "order-service": "处理您的订单",
}


def trace_tool_label(name: str) -> str:
    return TRACE_TOOL_LABELS.get(str(name or ""), "处理请求")


def trace_subagent_name(name: str) -> str:
    return TRACE_SUBAGENT_NAMES.get(str(name or ""), TRACE_MAIN_AGENT)


def trace_subagent_action(name: str) -> str:
    return TRACE_SUBAGENT_ACTIONS.get(str(name or ""), "处理请求")
