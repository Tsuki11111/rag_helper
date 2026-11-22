"""政策子智能体端口：应用服务只依赖这个协议，不依赖 Milvus 实现。"""
from typing import Protocol

from app.conf.policy_config import policy_config

DEFAULT_TOP_K = policy_config.default_top_k


class PolicyRepositoryPort(Protocol):
    def search(self, query: str, limit: int = DEFAULT_TOP_K) -> list[dict]:
        """按语义检索政策章节，返回含 entity 的命中列表。"""
        ...
