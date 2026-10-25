"""
初始化 MinIO 桶（docker compose 的 minio-init 服务调用）

为什么要单独建桶：
- 知识库桶：应用首次 import 时也会懒创建，但导入服务启动前就该可用；
- Milvus binlog 桶：Milvus 不会替我们建，缺了它 standalone 起不来。

只建桶、不碰已有对象，重复执行安全。
"""
import json
import sys
import time

from minio import Minio

from app.conf.minio_config import minio_config
from app.core.logger import logger

# Milvus 自己的对象存储桶名（与 docker-compose.yml / milvus-compose.yml 保持一致）
MILVUS_BUCKET = "rag-helper-milvus-binlog"


def _client() -> Minio:
    return Minio(
        endpoint=minio_config.endpoint,
        access_key=minio_config.access_key,
        secret_key=minio_config.secret_key,
        secure=False,
    )


def _ensure_bucket(client: Minio, bucket: str) -> None:
    if client.bucket_exists(bucket):
        logger.info(f"MinIO桶[{bucket}]已存在")
        return
    client.make_bucket(bucket)
    logger.success(f"MinIO桶[{bucket}]创建完成")


def _public_read(client: Minio, bucket: str) -> None:
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket}/*"],
        }],
    }
    client.set_bucket_policy(bucket, json.dumps(policy))


def main() -> int:
    # MinIO 容器 healthy 后仍可能短暂拒绝写请求 —— 重试几轮，避免首次 up 偶发失败
    last_error = None
    for _ in range(30):
        try:
            client = _client()
            _ensure_bucket(client, minio_config.bucket_name)
            _ensure_bucket(client, MILVUS_BUCKET)
            _public_read(client, minio_config.bucket_name)
            logger.info("MinIO 初始化完成")
            return 0
        except Exception as exc:  # noqa: BLE001 —— 初始化脚本要把真实原因打出来再重试
            last_error = exc
            logger.warning(f"MinIO 初始化失败，2 秒后重试：{exc}")
            time.sleep(2)
    logger.error(f"MinIO 初始化重试耗尽：{last_error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
