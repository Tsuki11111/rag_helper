"""导购子智能体组合根：只装配 spec，不写业务。"""
from app.agent.subagents.product_guide.api.tools import PRODUCT_GUIDE_TOOLS
from app.core.load_prompt import load_prompt


def build_product_guide_subagent() -> dict:
    return {
        "name": "product-guide",
        "description": load_prompt("product_guide_description").strip(),
        "system_prompt": load_prompt("product_guide_system").strip(),
        "tools": list(PRODUCT_GUIDE_TOOLS),
    }


PRODUCT_GUIDE_SUBAGENT = build_product_guide_subagent()
