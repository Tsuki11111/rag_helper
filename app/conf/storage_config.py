"""存储连接配置：Mongo / Redis / Neo4j 的环境变量统一在这里读取。"""
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


class StorageConfig:
    def __init__(self):
        self.mongo_url = os.getenv("MONGO_URL") or "mongodb://127.0.0.1:27017"
        self.mongo_db_name = os.getenv("MONGO_DB_NAME") or "rag_helper"
        self.redis_url = os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        self.redis_key_ttl_sec = max(60, _int("REDIS_KEY_TTL_SEC", 86400))
        self.neo4j_uri = os.getenv("NEO4J_URI") or "bolt://127.0.0.1:7687"
        self.neo4j_username = os.getenv("NEO4J_USERNAME") or "neo4j"
        self.neo4j_password = os.getenv("NEO4J_PASSWORD") or ""
        self.neo4j_database = os.getenv("NEO4J_DATABASE") or "neo4j"

    @property
    def neo4j_enabled(self) -> bool:
        """每次读取环境变量：自测会在运行时临时开关 NEO4J_ENABLED。"""
        raw = os.getenv("NEO4J_ENABLED", "false")
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}


storage_config = StorageConfig()
