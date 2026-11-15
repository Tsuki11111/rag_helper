"""清空知识库旧数据（Milvus / MongoDB 去重记录 / MinIO 源文件）。

用途：嵌入模型从 text-embedding-v2 切到 qwen3.7-text-embedding 后，
旧向量与新向量不可混用，必须全量重建。该脚本只删知识库资产，不动 MySQL 业务数据、
聊天记录和评测数据。

默认 dry-run，只打印将要删除的数量；确认后加 --yes 执行。

    PYTHONPATH=. .venv/Scripts/python.exe scripts/reset_knowledge_base.py
    PYTHONPATH=. .venv/Scripts/python.exe scripts/reset_knowledge_base.py --yes
"""
import argparse
import os

from pymongo import MongoClient

from app.clients.milvus_clients import get_milvus_client
from app.clients.minio_clients import get_minio_client
from app.conf.milvus_config import milvus_config
from app.conf.minio_config import minio_config

COLLECTIONS = [
    milvus_config.chunks_collection or "kb_chunks",
    milvus_config.item_name_collection or "kb_item_names",
    os.getenv("PLATFORM_POLICY_COLLECTION") or "kb_platform_policy",
    os.getenv("TONE_FAQ_COLLECTION") or "kb_tone_faq",
]


def reset_milvus(dry_run: bool) -> None:
    client = get_milvus_client()
    existing = set(client.list_collections())
    for name in COLLECTIONS:
        if name not in existing:
            print(f"[Milvus] {name}: 不存在，跳过")
            continue
        if dry_run:
            print(f"[Milvus] {name}: 将 drop_collection")
        else:
            client.drop_collection(name)
            print(f"[Milvus] {name}: 已删除")


def reset_mongo(dry_run: bool) -> None:
    client = MongoClient(os.getenv("MONGO_URL"), serverSelectionTimeoutMS=3000)
    db = client[os.getenv("MONGO_DB_NAME") or "rag_helper"]
    coll = db["imported_documents"]
    count = coll.count_documents({})
    if dry_run:
        print(f"[Mongo] imported_documents: 将删除 {count} 条")
    else:
        result = coll.delete_many({})
        print(f"[Mongo] imported_documents: 已删除 {result.deleted_count} 条")


def reset_minio(dry_run: bool) -> None:
    client = get_minio_client()
    if client is None:
        raise RuntimeError("MinIO 客户端未初始化，检查 MINIO_ENDPOINT / 容器")
    bucket = minio_config.bucket_name
    objects = list(client.list_objects(bucket, recursive=True))
    if dry_run:
        print(f"[MinIO] {bucket}: 将删除 {len(objects)} 个对象")
        return
    for obj in objects:
        client.remove_object(bucket, obj.object_name)
    print(f"[MinIO] {bucket}: 已删除 {len(objects)} 个对象")


def main() -> None:
    parser = argparse.ArgumentParser(description="清空知识库旧数据")
    parser.add_argument("--yes", action="store_true", help="确认执行删除；不加只打印")
    args = parser.parse_args()
    dry_run = not args.yes

    print(f"模式：{'dry-run（不删除）' if dry_run else '执行删除'}")
    print(f"Milvus 集合：{', '.join(COLLECTIONS)}")
    reset_milvus(dry_run)
    reset_mongo(dry_run)
    reset_minio(dry_run)
    print("完成。" + (" 加 --yes 才会真正删除。" if dry_run else ""))


if __name__ == "__main__":
    main()
