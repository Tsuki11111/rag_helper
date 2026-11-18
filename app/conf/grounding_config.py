"""终稿接地校验配置（运行时可调；默认开启，失败不阻断主流程）"""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return default


class GroundingConfig:
    """终稿接地校验的开关与尺寸上限"""

    def __init__(self):
        self.enabled = _bool("REPLY_GROUNDING_ENABLED", True)
        # 与主 agent 同为 DeepSeek flash；不用 v4-pro（用户明确要求）
        self.verifier_model = os.getenv("REPLY_VERIFIER_MODEL") or "deepseek-flash"
        # 只有台账里存在「实质工具返回」才值得跑模型校验，避免每轮都加延迟
        self.min_tool_chars = _int("REPLY_VERIFIER_MIN_TOOL_CHARS", 80)
        self.min_reply_chars = _int("REPLY_VERIFIER_MIN_REPLY_CHARS", 120)
        self.max_ledger_chars = _int("REPLY_VERIFIER_MAX_LEDGER_CHARS", 12000)
        self.max_tool_chars = _int("REPLY_VERIFIER_MAX_TOOL_CHARS", 4000)


grounding_config = GroundingConfig()
