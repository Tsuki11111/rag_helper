"""LangChain 工具适配层：只做参数转换，业务在 PolicyService。"""
from app.agent.subagents.policy_agent.application.policy_service import PolicyService
from app.agent.subagents.policy_agent.domain.ports import DEFAULT_TOP_K
from app.clients.policy_repository import (
    COLLECTION_NAME,
    PolicyRepository,
    ensure_collection,
)

_service: PolicyService | None = None


def get_service() -> PolicyService:
    global _service
    if _service is None:
        _service = PolicyService(PolicyRepository())
    return _service


def search_platform_policy(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """检索平台政策条款，返回可引用的章节原文。

    用户问退款/退货/售后时效、隐私与账号、商家规则、发票、责任边界等政策问题时调用。
    回答必须基于命中原文；没命中就说没查到，不要自行解释或扩大承诺。

    :param query: 用户原话或要查的政策问题（保留口语）
    :param top_k: 返回条数，默认 3，最大 5
    :return: 政策参考文本；没命中或不可用时返回降级提示
    """
    return get_service().search(query, top_k)


__all__ = ["search_platform_policy", "ensure_collection", "COLLECTION_NAME"]
