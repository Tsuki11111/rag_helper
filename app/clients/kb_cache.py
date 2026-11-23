"""知识库缓存版本号（Redis 单一数字，跨进程共享）

检索侧有三处进程内缓存（重排结果、BM25 索引、产品目录解析器），它们各自按 TTL 过期。
导入完成或文档撤回后，旧切片已经不在 Milvus 里，但缓存仍可能在 TTL 内被命中，
导致用户继续看到已下线文档的答案。

版本号是这些缓存的共同失效信号：每次写库后自增，缓存键/缓存项带上版本，
版本变了自然 miss。Redis 不可用时退回 0，缓存只能等 TTL 过期 —— 这是降级，
不是错误（缓存本来就是优化项）。
"""
from app.clients.redis_clients import (
    REDIS_DOWN_ERRORS,
    TaskStoreUnavailable,
    get_redis,
    note_store_failure,
)
from app.core.logger import logger

VERSION_KEY = "rag:kb:cache-version"


def get_kb_cache_version() -> int:
    """读当前版本；Redis 不可用返回 0（调用方照常使用缓存）"""
    try:
        raw = get_redis().get(VERSION_KEY)
        return int(raw or 0)
    except TaskStoreUnavailable:
        return 0
    except REDIS_DOWN_ERRORS as e:
        note_store_failure(e)
        return 0
    except (TypeError, ValueError):
        # 键被外部改成非数字：当作 0，不阻断检索
        logger.warning("[kb_cache] 缓存版本号不是整数，按 0 处理")
        return 0
    except Exception as e:
        # 缓存版本只是失效优化，任何异常都不能拖垮检索
        logger.warning(f"[kb_cache] 读取缓存版本失败，按 0 处理：{type(e).__name__}: {e}")
        return 0


def bump_kb_cache_version() -> int:
    """写库成功后自增版本；Redis 不可用时返回 0（缓存只能等 TTL）"""
    try:
        version = int(get_redis().incr(VERSION_KEY))
        logger.info(f"[kb_cache] 知识库缓存版本已更新：{version}")
        return version
    except TaskStoreUnavailable:
        logger.warning("[kb_cache] Redis 不可用，知识库缓存版本未更新")
        return 0
    except REDIS_DOWN_ERRORS as e:
        note_store_failure(e)
        logger.warning(f"[kb_cache] Redis 不可用，知识库缓存版本未更新：{e}")
        return 0
    except Exception as e:
        logger.warning(f"[kb_cache] 更新缓存版本失败：{type(e).__name__}: {e}")
        return 0
