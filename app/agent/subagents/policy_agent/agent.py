"""平台政策子智能体组合根。"""
from app.agent.subagents.policy_agent.api.tools import search_platform_policy
from app.core.load_prompt import load_prompt


def build_policy_agent_subagent() -> dict:
    return {
        "name": "policy-agent",
        "description": load_prompt("policy_agent_description").strip(),
        "system_prompt": load_prompt("policy_agent_system").strip(),
        "tools": [search_platform_policy],
    }


POLICY_SUBAGENT = build_policy_agent_subagent()
