"""三个进程入口的监听地址与端口。"""
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


class ServiceConfig:
    def __init__(self):
        # 本地直接跑默认回环；容器内由 compose 显式设 SERVICE_HOST/GRPC_HOST=0.0.0.0
        self.host = os.getenv("SERVICE_HOST", "127.0.0.1")
        self.grpc_host = os.getenv("GRPC_HOST", "127.0.0.1")
        # 注意：这里是**进程内部监听端口**；compose 的 RAG_*_PORT 是宿主机映射，不能复用
        self.import_port = _int("SERVICE_IMPORT_PORT", 8001)
        self.http_port = _int("SERVICE_HTTP_PORT", 8002)
        self.grpc_port = _int("SERVICE_GRPC_PORT", 8003)


service_config = ServiceConfig()
