"""pytest 公共夹具：真实依赖探测，缺服务时 SKIP 而不是 FAIL。"""
import pytest

from app.conf.storage_config import storage_config


def _mongo_ok() -> bool:
    try:
        from pymongo import MongoClient
        MongoClient(storage_config.mongo_url, serverSelectionTimeoutMS=1000).admin.command("ping")
        return True
    except Exception:
        return False


def _redis_ok() -> bool:
    try:
        from app.clients.redis_clients import is_available
        return bool(is_available())
    except Exception:
        return False


def _milvus_ok() -> bool:
    try:
        from app.clients.milvus_clients import get_milvus_client
        return get_milvus_client() is not None
    except Exception:
        return False


@pytest.fixture(scope="session")
def mongo_ready():
    if not _mongo_ok():
        pytest.skip("MongoDB 不可用")
    return True


@pytest.fixture(scope="session")
def redis_ready():
    if not _redis_ok():
        pytest.skip("Redis 不可用")
    return True


@pytest.fixture(scope="session")
def milvus_ready():
    if not _milvus_ok():
        pytest.skip("Milvus 不可用")
    from app.clients.milvus_clients import get_milvus_client
    return get_milvus_client()
