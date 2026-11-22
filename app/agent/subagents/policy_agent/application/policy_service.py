"""政策应用服务：只负责格式化与降级，检索细节在 PolicyRepository。"""
from app.agent.subagents.policy_agent.infrastructure.policy_repository import (
    DEFAULT_TOP_K,
    PolicyRepository,
)
from app.core.logger import logger

_NOT_FOUND = "（政策库没有检索到相关条款，不要猜测；可建议用户查看对应政策页面或转人工。）"
_DEGRADED = "（政策库暂时不可用；如需引用政策，请让用户查看政策页面，不要编造条款。）"


class PolicyService:
    def __init__(self, repository: PolicyRepository):
        self._repository = repository

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> str:
        q = (query or "").strip()
        if not q:
            return "（没收到要检索的政策问题）"
        try:
            hits = self._repository.search(q, top_k)
        except Exception as exc:
            logger.warning(f"[policy_kb] 检索失败，降级：{exc}")
            return _DEGRADED
        if not hits:
            logger.info("[policy_kb] 无高相似政策条款，返回降级提示")
            return _NOT_FOUND
        logger.info(f"[policy_kb] 命中 {len(hits)} 条政策章节")
        return _format_hits(hits, top_k)


def _format_hits(hits: list[dict], limit: int) -> str:
    lines = ["平台政策参考（回答时注明政策标题与章节，不要编造未命中的条款）："]
    for index, hit in enumerate(hits[:limit], start=1):
        entity = hit.get("entity") or {}
        title = entity.get("policy_title") or "平台政策"
        heading = entity.get("heading") or ""
        version = entity.get("version") or ""
        content = (entity.get("content") or "").strip()
        lines.append(f"{index}) 《{title}》{f' · {heading}' if heading else ''}"
                     f"{f'（v{version}）' if version else ''}")
        if content:
            lines.append(f"   原文：{content}")
    return "\n".join(lines)


def _check_policy_format() -> list:
    """回归：政策检索结果排版 / 低分过滤 / 降级提示（纯逻辑）。"""
    problems: list = []
    hits = [
        {"distance": 0.82, "entity": {"policy_title": "退换货与售后政策",
                                      "heading": "三、申请退款", "version": "1.0",
                                      "content": "待发货订单可申请退款。"}},
    ]
    text = _format_hits(hits, 3)
    for marker in ("平台政策参考", "《退换货与售后政策》", "三、申请退款", "原文："):
        if marker not in text:
            problems.append(f"政策排版缺少：{marker}")

    class _Repo:
        def search(self, query, limit):
            raise RuntimeError("down")

    if "暂时不可用" not in PolicyService(_Repo()).search("退款"):
        problems.append("政策库故障时没有可读降级")
    if not PolicyService(PolicyRepository()).search("   ").startswith("（没收到"):
        problems.append("空 query 应返回可读提示")
    return problems
