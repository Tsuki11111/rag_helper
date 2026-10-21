# 平台（ItsMyGo）回调配置：agent 的工具通过内部接口查订单。
# 只在内网使用；票据由 Java 在每次 Converse 时签发，经 gRPC metadata 下发，
# 不经过模型，也不从环境变量读用户身份。
from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ItsMyGoConfig:
    base_url: str
    tool_timeout_seconds: float


its_mygo_config = ItsMyGoConfig(
    base_url=(os.getenv("ITS_MYGO_BASE_URL") or "http://127.0.0.1:8080").rstrip("/"),
    tool_timeout_seconds=float(os.getenv("ITS_MYGO_TOOL_TIMEOUT_SECONDS") or 5),
)
