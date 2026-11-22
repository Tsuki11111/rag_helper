"""所有暴露给模型的工具必须有 docstring（LangChain 用它生成工具描述）。"""
from app.agent.runtime.main_agent import MAIN_AGENT_TOOLS
from app.agent.subagents.order_service.api.tools import ORDER_TOOLS
from app.agent.subagents.policy_agent.api.tools import search_platform_policy
from app.agent.subagents.product_guide.api.tools import PRODUCT_GUIDE_TOOLS


def test_all_tools_have_docstrings():
    groups = {
        "order": ORDER_TOOLS,
        "guide": PRODUCT_GUIDE_TOOLS,
        "policy": [search_platform_policy],
        "main": MAIN_AGENT_TOOLS,
    }
    missing = [
        f"{group}.{getattr(tool, '__name__', tool)!r}"
        for group, tools in groups.items()
        for tool in tools
        if not (getattr(tool, "__doc__", None) or "").strip()
    ]
    assert not missing, f"缺少 docstring 的工具：{missing}"
