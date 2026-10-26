"""
LLM-as-Judge 的模型配置（评估路线 P3）

judge 模型必须和生成模型不同：本项目生成侧是 `qwen-plus`（RAG）+
`deepseek-flash`（主 agent），默认 judge 用 `qwen-max`。
可用 `JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` 覆盖；
后两者缺省回退到百炼的 `OPENAI_*`。
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

from app.conf.lm_config import lm_config

load_dotenv()


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    temperature: float


judge_config = JudgeConfig(
    model=os.getenv("JUDGE_MODEL") or "qwen-max",
    base_url=os.getenv("JUDGE_BASE_URL") or lm_config.base_url or "",
    api_key=os.getenv("JUDGE_API_KEY") or lm_config.api_key or "",
    # 0 保证评分可复现；与生成侧的温度解耦
    temperature=float(os.getenv("JUDGE_TEMPERATURE") or 0),
)
