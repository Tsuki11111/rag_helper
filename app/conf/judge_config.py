"""
LLM-as-Judge 的模型配置（评估路线 P3）

judge 模型默认必须和生成模型不同：本项目生成侧是 `qwen-plus`（RAG）+
`deepseek-flash`（主 agent），默认 judge 用 **DeepSeek `deepseek-v4-pro`**
（同供应商、不同模型，复用 `DEEPSEEK_API_KEY`）。
可用 `JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` 覆盖。

想图便宜用 `deepseek-flash` 当 judge 时要显式设 `JUDGE_ALLOW_SAME_MODEL=1`：
`validate_judge_config` 会把「与主 agent 同模型」从错误降为警告，
代价是自评偏差，指标只能自己跟自己比。
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    temperature: float
    allow_same_model: bool = False


judge_config = JudgeConfig(
    model=os.getenv("JUDGE_MODEL") or "deepseek-v4-pro",
    base_url=(os.getenv("JUDGE_BASE_URL")
              or os.getenv("MAIN_AGENT_BASE_URL")
              or "https://api.deepseek.com"),
    api_key=(os.getenv("JUDGE_API_KEY")
             or os.getenv("DEEPSEEK_API_KEY")
             or os.getenv("MAIN_AGENT_API_KEY")
             or ""),
    # 0 保证评分可复现；与生成侧的温度解耦
    temperature=float(os.getenv("JUDGE_TEMPERATURE") or 0),
    allow_same_model=(os.getenv("JUDGE_ALLOW_SAME_MODEL") or "").strip().lower()
    in {"1", "true", "yes", "on"},
)
