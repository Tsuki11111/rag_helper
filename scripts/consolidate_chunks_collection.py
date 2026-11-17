"""把切片集合归一为 `kb_chunks`：删掉旧集合、按精简 schema 重建同名集合、拷回数据、删掉 v2。

背景：2026-11-17 先建了 `kb_chunks_v2` 做「新旧并存」的迁移，随后 schema 精简到 12 个字段；
现在确认 v2 是唯一在用的切片集合，于是去掉版本号后缀，回到单一集合名 `kb_chunks`。

做法（顺序不能改，Milvus 不支持 rename）：
1. 校验：两个集合都在、行数一致、`chunk_id` 集合一致（保证评测 gold 不会断链）；
2. drop 旧 `kb_chunks`（auto_id schema，数据已全部落在 v2 里）；
3. 用 `build_chunks_schema` 建同名 `kb_chunks`（chunk_key 主键 + dense/sparse 索引）；
4. 从 `kb_chunks_v2` 分批（200 条）拷回 12 个字段，向量原样复用，不重新 embedding；
5. 校验：行数、`chunk_key`/`chunk_id` 唯一、`sparse_vector` 非空、字段数 12；
6. drop `kb_chunks_v2`。

用法（必须用 `-m` 入口，直接跑脚本会找不到 `app` 包）：
    .venv\\Scripts\\python.exe -m scripts.consolidate_chunks_collection --dry-run
    .venv\\Scripts\\python.exe -m scripts.consolidate_chunks_collection
"""
import sys

from pymilvus import MilvusClient

from app.conf.embedding_config import embedding_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger
from scripts.create_collections import (
    build_chunks_schema,
    step3_build_index_params,
    step4_create_collection,
)

SOURCE = "kb_chunks_v2"
TARGET = "kb_chunks"
BATCH_SIZE = 200
# 精简后切片集合的全部字段（写入白名单，也是校验的期望集合）
FIELDS = [
    "chunk_key", "chunk_id", "file_title", "title", "parent_title", "section_path",
    "section_index", "chunk_index", "content", "item_name", "dense_vector", "sparse_vector",
]


def _fetch_all(client: MilvusClient, collection: str) -> list:
    """读出集合全部行（切片量级 1e3，一次 limit 16384 足够）。"""
    return client.query(
        collection_name=collection,
        filter="chunk_id >= 0",
        output_fields=FIELDS,
        limit=16384,
    )


def _count(client: MilvusClient, collection: str) -> int:
    rows = client.query(
        collection_name=collection, filter="chunk_id >= 0",
        output_fields=["count(*)"],
    )
    return int(rows[0]["count(*)"])


def consolidate(dry_run: bool = False) -> None:
    client = MilvusClient(uri=milvus_config.milvus_url, db_name=milvus_config.milvus_db_name)
    collections = client.list_collections()
    logger.info(f"[consolidate] 当前集合：{collections}")
    if SOURCE not in collections:
        raise SystemExit(f"缺少源集合 {SOURCE}，先确认迁移是否已做过")
    if TARGET not in collections:
        raise SystemExit(f"缺少旧集合 {TARGET}，本脚本只处理「两个集合并存」的状态")

    source_rows = _fetch_all(client, SOURCE)
    target_ids = {
        int(r["chunk_id"]) for r in client.query(
            collection_name=TARGET, filter="chunk_id >= 0", output_fields=["chunk_id"], limit=16384
        )
    }
    source_ids = {int(r["chunk_id"]) for r in source_rows}
    if len(source_rows) != len(target_ids) or source_ids != target_ids:
        raise SystemExit(
            f"两集合数据不一致，拒绝删除：v2 {len(source_rows)} 行 / 旧集合 {len(target_ids)} 行，"
            f"chunk_id 差集 {len(source_ids ^ target_ids)} 个"
        )

    logger.info(f"[consolidate] 校验通过：{len(source_rows)} 行、chunk_id 完全一致")
    if dry_run:
        logger.warning(f"[consolidate] --dry-run：将 drop {TARGET} → 按精简 schema 重建 → "
                       f"拷回 {len(source_rows)} 行 → drop {SOURCE}")
        client.close()
        return

    client.drop_collection(TARGET)
    logger.success(f"[consolidate] 已删除旧集合 {TARGET}")

    schema = build_chunks_schema(client, embedding_config.dimension)
    index_params = step3_build_index_params(client, with_sparse=True)
    step4_create_collection(client, TARGET, schema, index_params)

    for start in range(0, len(source_rows), BATCH_SIZE):
        batch = source_rows[start:start + BATCH_SIZE]
        payload = [{k: row.get(k) for k in FIELDS} for row in batch]
        client.insert(collection_name=TARGET, data=payload)
        logger.info(f"[consolidate] 已拷回 {min(start + BATCH_SIZE, len(source_rows))}/{len(source_rows)}")
    client.flush(TARGET)

    # 校验：行数、唯一性、稀疏向量、字段数
    problems = []
    rows = _count(client, TARGET)
    if rows != len(source_rows):
        problems.append(f"行数不对：{rows} != {len(source_rows)}")
    all_rows = _fetch_all(client, TARGET)
    keys = {r["chunk_key"] for r in all_rows}
    if len(keys) != len(all_rows):
        problems.append(f"chunk_key 不唯一：{len(keys)} / {len(all_rows)}")
    ids = {int(r["chunk_id"]) for r in all_rows}
    if len(ids) != len(all_rows):
        problems.append(f"chunk_id 不唯一：{len(ids)} / {len(all_rows)}")
    if ids != source_ids:
        problems.append(f"chunk_id 与源集合不一致：差集 {len(ids ^ source_ids)}")
    empty_sparse = sum(1 for r in all_rows if not r.get("sparse_vector"))
    if empty_sparse:
        problems.append(f"sparse_vector 为空 {empty_sparse} 条")
    fields = [f["name"] for f in client.describe_collection(TARGET)["fields"]]
    if set(fields) != set(FIELDS):
        problems.append(f"字段与设计不一致：{sorted(fields)}")
    if problems:
        client.close()
        raise SystemExit("校验失败，保留 v2 不删：" + "；".join(problems))

    logger.success(f"[consolidate] 校验通过：{rows} 行、字段 {len(fields)} 个、sparse 全非空")
    client.drop_collection(SOURCE)
    logger.success(f"[consolidate] 已删除 {SOURCE}，集合列表：{client.list_collections()}")
    client.close()


if __name__ == "__main__":
    consolidate(dry_run="--dry-run" in sys.argv)
