"""把旧 kb_chunks 迁移到 v2 schema（复用已有向量，不重新 embedding）。

用法：
    python scripts/migrate_chunks_v2.py            # 建 kb_chunks_v2 并迁移
    python scripts/migrate_chunks_v2.py --count    # 只打印迁移结果

迁移只复制数据，不删除/修改旧集合；确认新集合可检索后再切
CHUNKS_COLLECTION=kb_chunks_v2。
"""
from __future__ import annotations

import sys

from pymilvus import MilvusClient

from app.conf.embedding_config import embedding_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger
from app.utils.chunk_identity import build_chunk_fields
from scripts.create_collections import (
    build_chunks_schema,
    step3_build_index_params,
    step4_create_collection,
)

SOURCE = "kb_chunks"
TARGET = "kb_chunks_v2"
BATCH_SIZE = 200
DOC_VERSION = "legacy-20261117"


def _fetch_source(client: MilvusClient) -> list[dict]:
    return client.query(
        collection_name=SOURCE,
        filter="chunk_id >= 0",
        output_fields=["*"],
        limit=16384,
    )


def migrate() -> None:
    client = MilvusClient(uri=milvus_config.milvus_url, db_name=milvus_config.milvus_db_name)
    if not client.has_collection(SOURCE):
        raise SystemExit(f"源集合不存在：{SOURCE}")
    if client.has_collection(TARGET):
        raise SystemExit(f"目标集合已存在，先确认/删除：{TARGET}")

    rows = _fetch_source(client)
    logger.info(f"读取 {SOURCE}: {len(rows)} 条")

    schema = build_chunks_schema(client, embedding_config.dimension)
    index_params = step3_build_index_params(client, with_sparse=True)
    step4_create_collection(client, TARGET, schema, index_params)

    counters: dict[tuple[str, str, int], int] = {}
    prepared: list[dict] = []
    for row in rows:
        key = (
            str(row.get("file_title") or ""),
            str(row.get("parent_title") or row.get("title") or ""),
            int(row.get("part") or 0),
        )
        ordinal = counters.get(key, 0)
        counters[key] = ordinal + 1
        row = dict(row)
        row.update(build_chunk_fields(row, ordinal=ordinal, doc_version=DOC_VERSION))
        row["source_type"] = "legacy"
        prepared.append(row)

    allowed = {
        "chunk_key", "chunk_id", "doc_id", "doc_version", "product_id", "merchant_id",
        "section_id", "section_path", "section_index", "page_no", "chunk_index",
        "content", "content_hash", "title", "parent_title", "part", "file_title",
        "item_name", "source_type", "is_active", "embedding_model", "metadata",
        "dense_vector", "sparse_vector",
    }
    for start in range(0, len(prepared), BATCH_SIZE):
        batch = [{k: v for k, v in row.items() if k in allowed} for row in prepared[start:start + BATCH_SIZE]]
        client.insert(collection_name=TARGET, data=batch)
        logger.info(f"迁移 {start + 1}-{start + len(batch)} / {len(prepared)}")

    client.flush(TARGET)
    client.load_collection(TARGET)
    count = client.query(collection_name=TARGET, filter="chunk_id >= 0", output_fields=["count(*)"])
    logger.info(f"迁移完成，目标集合 {TARGET} 行数：{count}")
    client.close()


if __name__ == "__main__":
    try:
        migrate()
    except Exception as exc:
        logger.error(f"迁移失败：{exc}", exc_info=True)
        raise
