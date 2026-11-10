"""导入平台政策章节到 Milvus（幂等，可重复执行）。

用法：
    .venv/Scripts/python.exe -m app.agent.tools.policy_import

- 事实源：Java `GET /api/policies` + `GET /api/policies/{key}`（MySQL 章节原文）；
- 集合：`kb_platform_policy`（可用 PLATFORM_POLICY_COLLECTION 覆盖）；
- 主键：`section_id` = `policy_key#section_no`；每次按 policy_key 先删后写，避免旧章节残留；
- 改政策后重跑本脚本即可同步向量库。
"""
import sys

import httpx
from pymilvus import DataType

from app.agent.tools.policy_kb import COLLECTION_NAME
from app.clients.milvus_clients import get_milvus_client
from app.conf.embedding_config import embedding_config
from app.conf.its_mygo_config import its_mygo_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings

MAX_SECTION_ID = 128
MAX_POLICY_KEY = 64
MAX_TITLE = 255
MAX_HEADING = 512
MAX_CONTENT = 16384
MAX_VERSION = 32


def _ensure_collection(client) -> None:
    if client.has_collection(COLLECTION_NAME):
        client.load_collection(COLLECTION_NAME)
        return
    dimension = embedding_config.dimension
    if not dimension or dimension <= 0:
        raise ValueError(f"向量维度不合法：{dimension}")
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("section_id", DataType.VARCHAR, max_length=MAX_SECTION_ID, is_primary=True)
    schema.add_field("policy_key", DataType.VARCHAR, max_length=MAX_POLICY_KEY)
    schema.add_field("policy_title", DataType.VARCHAR, max_length=MAX_TITLE)
    schema.add_field("section_no", DataType.INT64)
    schema.add_field("heading", DataType.VARCHAR, max_length=MAX_HEADING)
    schema.add_field("content", DataType.VARCHAR, max_length=MAX_CONTENT)
    schema.add_field("version", DataType.VARCHAR, max_length=MAX_VERSION)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=dimension)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense_vector",
        index_name="dense_vector_index",
        index_type="HNSW",
        metric_type=milvus_config.metric_type,
        params={"M": 16, "efConstruction": 200},
    )
    client.create_collection(collection_name=COLLECTION_NAME, schema=schema,
                             index_params=index_params)
    client.load_collection(COLLECTION_NAME)
    logger.info(f"政策库集合已创建：{COLLECTION_NAME}（dim={dimension}）")


def _fetch_policies() -> list[dict]:
    base = its_mygo_config.base_url
    with httpx.Client(timeout=15) as client:
        response = client.get(f"{base}/api/policies")
        response.raise_for_status()
        payload = response.json()
    policies = payload.get("data") or []
    if not policies:
        raise RuntimeError("Java 没有返回任何政策，请先确认政策已入库")
    details = []
    with httpx.Client(timeout=15) as client:
        for item in policies:
            key = item["policyKey"]
            response = client.get(f"{base}/api/policies/{key}")
            response.raise_for_status()
            details.append(response.json().get("data") or {})
    return details


def main() -> int:
    try:
        policies = _fetch_policies()
    except Exception as exc:
        print(f"读取政策失败：{exc}")
        return 1

    rows = []
    for policy in policies:
        policy_key = policy.get("policyKey") or ""
        title = policy.get("title") or policy_key
        version = policy.get("version") or "1.0"
        for section in policy.get("sections") or []:
            content = (section.get("content") or "").strip()
            heading = (section.get("heading") or "").strip()
            if not content and not heading:
                continue
            rows.append({
                "section_id": f"{policy_key}#{section.get('sectionNo')}",
                "policy_key": policy_key,
                "policy_title": title,
                "section_no": int(section.get("sectionNo") or 0),
                "heading": heading,
                "content": content,
                "version": version,
            })
    if not rows:
        print("政策章节为空，退出")
        return 1

    texts = [
        f"《{row['policy_title']}》{row['heading']}\n{row['content']}"
        for row in rows
    ]
    embeddings = generate_embeddings(texts) or {}
    vectors = embeddings.get("dense") or []
    if len(vectors) != len(rows):
        print(f"向量数量不匹配：{len(vectors)} != {len(rows)}")
        return 1

    client = get_milvus_client()
    if client is None:
        print("Milvus 不可用，请先启动中间件")
        return 1
    _ensure_collection(client)

    for row in {item["policy_key"] for item in rows}:
        try:
            client.delete(collection_name=COLLECTION_NAME,
                          filter=f'policy_key == "{row}"')
        except Exception as exc:
            logger.warning(f"清理旧章节失败（policy_key={row}）：{exc}")
    data = [{**row, "dense_vector": vector} for row, vector in zip(rows, vectors)]
    client.upsert(collection_name=COLLECTION_NAME, data=data)
    client.flush(COLLECTION_NAME)
    count = client.query(collection_name=COLLECTION_NAME, filter='section_id != ""',
                         output_fields=["count(*)"])
    total = count[0].get("count(*)") if count else "?"
    print(f"政策库导入完成：{len(data)} 个章节（集合 {COLLECTION_NAME}，当前 {total} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
