"""客服话术库配置。"""
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


class ToneFaqConfig:
    def __init__(self):
        self.collection_name = os.getenv("TONE_FAQ_COLLECTION", "kb_tone_faq")
        self.default_top_k = _int("TONE_FAQ_DEFAULT_TOP_K", 3)
        self.max_top_k = max(self.default_top_k, _int("TONE_FAQ_MAX_TOP_K", 5))
        self.min_score = _num("TONE_FAQ_MIN_SCORE", 0.30)


tone_faq_config = ToneFaqConfig()
