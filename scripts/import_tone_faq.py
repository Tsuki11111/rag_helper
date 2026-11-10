"""导入客服话术 FAQ 到 Milvus（幂等，可重复执行）。

用法：
    .venv/Scripts/python.exe -m scripts.import_tone_faq

- 数据源：`data/tone_faq.json`（场景 / 用户说法 / 建议回复 / 标签）；
- 集合：`kb_tone_faq`（可用 TONE_FAQ_COLLECTION 覆盖），与知识库切片完全隔离；
- 主键：`faq_id`，重复导入直接 upsert 覆盖，不改动其他集合。
"""
import json
import sys
from pathlib import Path

from pymilvus import DataType

from app.agent.tools.tone_faq import COLLECTION_NAME
from app.clients.milvus_clients import get_milvus_client
from app.conf.embedding_config import embedding_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings

DATA_FILE = Path(__file__).resolve().parents[3] / "data" / "tone_faq.json"
MAX_FAQ_ID = 64
MAX_SCENARIO = 256
MAX_QUESTION = 1024
MAX_ANSWER = 4096
MAX_TAGS = 512


def _ensure_collection(client) -> None:
    """集合不存在才创建：HNSW + COSINE，与检索侧参数保持一致。"""
    if client.has_collection(COLLECTION_NAME):
        client.load_collection(COLLECTION_NAME)
        return
    dimension = embedding_config.dimension
    if not dimension or dimension <= 0:
        raise ValueError(f"向量维度不合法：{dimension}")
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("faq_id", DataType.VARCHAR, max_length=MAX_FAQ_ID, is_primary=True)
    schema.add_field("scenario", DataType.VARCHAR, max_length=MAX_SCENARIO)
    schema.add_field("question", DataType.VARCHAR, max_length=MAX_QUESTION)
    schema.add_field("answer", DataType.VARCHAR, max_length=MAX_ANSWER)
    schema.add_field("tags", DataType.VARCHAR, max_length=MAX_TAGS)
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
    logger.info("话术库集合已创建：%s（dim=%s）", COLLECTION_NAME, dimension)


def main() -> int:
    entries = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if not entries:
        print("话术种子为空，退出")
        return 1
    # 用「场景 + 用户说法」做向量，检索时用户原话更接近
    texts = [f"{item['scenario']}。{item['question']}" for item in entries]
    embeddings = generate_embeddings(texts) or {}
    vectors = embeddings.get("dense") or []
    if len(vectors) != len(entries):
        print(f"向量数量不匹配：{len(vectors)} != {len(entries)}")
        return 1

    client = get_milvus_client()
    if client is None:
        print("Milvus 不可用，请先启动中间件")
        return 1
    _ensure_collection(client)

    data = [
        {
            "faq_id": item["id"],
            "scenario": item["scenario"],
            "question": item["question"],
            "answer": item["answer"],
            "tags": ",".join(item.get("tags") or []),
            "dense_vector": vector,
        }
        for item, vector in zip(entries, vectors)
    ]
    client.upsert(collection_name=COLLECTION_NAME, data=data)
    client.flush(COLLECTION_NAME)
    count = client.query(collection_name=COLLECTION_NAME, filter='faq_id != ""',
                         output_fields=["count(*)"])
    rows = count[0].get("count(*)") if count else "?"
    print(f"话术库导入完成：{len(data)} 条（集合 {COLLECTION_NAME}，当前 {rows} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
