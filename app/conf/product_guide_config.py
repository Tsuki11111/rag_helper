"""导购配置：默认值集中在 app/conf，agent 目录不再自建 config。"""
import os

from dotenv import load_dotenv

load_dotenv()


def _num(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    return max(1, int(_num(name, default)))


class ProductGuideConfig:
    def __init__(self):
        self.default_limit = _int("PRODUCT_GUIDE_DEFAULT_LIMIT", 5)
        self.max_limit = max(self.default_limit, _int("PRODUCT_GUIDE_MAX_LIMIT", 10))
        self.semantic_ttl_sec = _num("PRODUCT_SEMANTIC_TTL_SEC", 300)
        self.semantic_min_score = _num("PRODUCT_SEMANTIC_MIN_SCORE", 0.15)
        self.semantic_relative_score = _num("PRODUCT_SEMANTIC_RELATIVE_SCORE", 0.8)


product_guide_config = ProductGuideConfig()
