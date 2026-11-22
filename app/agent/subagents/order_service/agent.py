"""订单子智能体组合根。"""
from app.agent.subagents.order_service.api.tools import ORDER_TOOLS
from app.core.load_prompt import load_prompt


def build_order_subagent() -> dict:
    return {
        "name": "order-service",
        "description": load_prompt("order_service_description").strip(),
        "system_prompt": load_prompt("order_service_system").strip(),
        "tools": list(ORDER_TOOLS),
    }


ORDER_SUBAGENT = build_order_subagent()
