"""订单服务配置：Java 目录接口的分页上限等运行时可调项。"""
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


class OrderServiceConfig:
    def __init__(self):
        # Java `AgentCatalogService.listAll` 实际把 limit clamp 到 1..100；
        # 这里必须与 Java 保持一致，超过 100 只会让「capped」判断失真。
        requested = max(1, _int("ORDER_CATALOG_PAGE_LIMIT", 100))
        self.catalog_page_limit = min(requested, 100)


order_service_config = OrderServiceConfig()
