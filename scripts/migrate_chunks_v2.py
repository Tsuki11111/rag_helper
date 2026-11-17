"""把旧 kb_chunks 迁移到 v2 schema（复用已有向量，不重新 embedding）。

2026-11-17 起 v2 是精简版 12 字段，脚本同时负责「重建目标集合」：

    python scripts/migrate_chunks_v2.py            # 目标集合已存在时直接退出
    python scripts/migrate_chunks_v2.py --recreate  # 先 drop 目标集合再重建迁移

迁移只复制数据，不删除/修改旧集合；确认可检索后再切
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
ALLOWED_FIELDS = {
    "chunk_key", "chunk_id", "file_title", "title", "parent_title", "section_path",
    "section_index", "chunk_index", "content", "item_name",
    "dense_vector", "sparse_vector",
}


def _fetch_source(client: MilvusClient) -> list[dict]:
    return client.query(
        collection_name=SOURCE,
        filter="chunk_id >= 0",
        output_fields=["*"],
        limit=16384,
    )


def migrate(recreate: bool = False) -> None:
    client = MilvusClient(uri=milvus_config.milvus_url, db_name=milvus_config.milvus_db_name)
    if not client.has_collection(SOURCE):
        raise SystemExit(f"源集合不存在：{SOURCE}")
    if client.has_collection(TARGET):
        if not recreate:
            raise SystemExit(f"目标集合已存在，加 --recreate 重建，或先确认/删除：{TARGET}")
        logger.warning(f"--recreate：先删除已存在的目标集合 {TARGET}")
        client.drop_collection(TARGET)

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
        row.update(build_chunk_fields(row, ordinal=ordinal))
        prepared.append(row)

    for start in range(0, len(prepared), BATCH_SIZE):
        batch = [
            {k: v for k, v in row.items() if k in ALLOWED_FIELDS}
            for row in prepared[start:start + BATCH_SIZE]
        ]
        client.insert(collection_name=TARGET, data=batch)
        logger.info(f"迁移 {start + 1}-{start + len(batch)} / {len(prepared)}")

    client.flush(TARGET)
    client.load_collection(TARGET)
    count = client.query(collection_name=TARGET, filter="chunk_id >= 0", output_fields=["count(*)"])
    logger.info(f"迁移完成，目标集合 {TARGET} 行数：{count}")
    client.close()


if __name__ == "__main__":
    try:
        migrate(recreate="--recreate" in sys.argv)
    except Exception as exc:
        logger.error(f"迁移失败：{exc}", exc_info=True)
        raise
