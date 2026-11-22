"""平台政策库配置：集合名、Top-K 与相似度阈值。"""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _num(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class PolicyConfig:
    def __init__(self):
        self.collection_name = os.getenv("PLATFORM_POLICY_COLLECTION", "kb_platform_policy")
        self.default_top_k = max(1, _int("POLICY_DEFAULT_TOP_K", 3))
        self.max_top_k = max(self.default_top_k, _int("POLICY_MAX_TOP_K", 5))
        self.min_score = _num("POLICY_MIN_SCORE", 0.35)


policy_config = PolicyConfig()
