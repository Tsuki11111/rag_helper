# 导入核心依赖：数据类、环境变量读取、路径处理
from dataclasses import dataclass
import os
from dotenv import load_dotenv

# 提前加载.env配置文件（必须在读取环境变量前执行，确保os.getenv能获取到值）
# 若.env不在项目根目录，可指定路径：load_dotenv(dotenv_path=Path(__file__).parent / ".env")
load_dotenv()


# 定义minerU服务配置
@dataclass
class LLMConfig:
    base_url: str
    api_key : str
    lv_model: str
    llm_model: str
    llm_temperature: float
    # ── 主 agent（编排层）独立一组配置（2026-10-14，用户拍板）──
    # 走 **DeepSeek 官方 API**（不经百炼）。由来：派发可靠性是主 agent 的命门 ——
    # 实测同一提示词下 qwen-plus 只有 4/6 会派发检索（见 HANDOFF §3.44），
    # qwen-max 被否决，改用 DeepSeek V4.1 Flash 直连。
    # 注意官方模型 id 叫 `deepseek-flash`（报错原文列出支持名：deepseek-flash /
    # deepseek-v4-pro）；`deepseek-v4.1-flash` 是百炼侧的名字，官方 API 不认。
    # 密钥变量名沿用 DeepSeek 官方口径（DEEPSEEK_API_KEY），公司网关用户可用
    # MAIN_AGENT_* 覆盖
    main_agent_model: str
    main_agent_base_url: str
    main_agent_api_key: str

lm_config = LLMConfig(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    lv_model=os.getenv("VL_MODEL"),
    llm_model=os.getenv("LLM_DEFAULT_MODEL"),
    llm_temperature=float(os.getenv("LLM_DEFAULT_TEMPERATURE")),
    main_agent_model=os.getenv("MAIN_AGENT_MODEL") or "deepseek-flash",
    main_agent_base_url=os.getenv("MAIN_AGENT_BASE_URL") or "https://api.deepseek.com",
    main_agent_api_key=os.getenv("MAIN_AGENT_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or "",
)
