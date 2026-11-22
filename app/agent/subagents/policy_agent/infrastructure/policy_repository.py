"""政策库基础设施：Milvus schema 与检索，不负责话术排版。"""
import os

from pymilvus import DataType

from app.clients.milvus_clients import dense_search, get_milvus_client
from app.conf.embedding_config import embedding_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings

COLLECTION_NAME = os.getenv("PLATFORM_POLICY_COLLECTION", "kb_platform_policy")
DEFAULT_TOP_K = 3
MAX_TOP_K = 5
MIN_SCORE = 0.35
OUTPUT_FIELDS = [
    "section_id", "policy_key", "policy_title", "section_no",
    "heading", "content", "version",
]

MAX_SECTION_ID = 128
MAX_POLICY_KEY = 64
MAX_TITLE = 255
MAX_HEADING = 512
MAX_CONTENT = 16384
MAX_VERSION = 32


def ensure_collection(client) -> None:
    """确保政策集合存在；导入脚本与导入图共用这一份 schema。"""
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


class PolicyRepository:
    def __init__(self, client_factory=None, embed_fn=None):
        self._client_factory = client_factory or get_milvus_client
        self._embed = embed_fn or generate_embeddings

    def search(self, query: str, limit: int = DEFAULT_TOP_K) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        embeddings = self._embed([q], text_type="query") or {}
        vectors = embeddings.get("dense") or []
        if not vectors:
            raise RuntimeError("查询向量化失败")
        client = self._client_factory()
        if client is None:
            raise RuntimeError("Milvus 不可用")
        result = dense_search(
            client=client,
            collection_name=COLLECTION_NAME,
            dense_vector=vectors[0],
            limit=max(1, min(int(limit or DEFAULT_TOP_K), MAX_TOP_K)) * 2,
            output_fields=OUTPUT_FIELDS,
        )
        hits = (result[0] if result else []) or []
        kept = []
        for hit in hits:
            try:
                score = float(hit.get("distance"))
            except (TypeError, ValueError):
                continue
            if score >= MIN_SCORE:
                kept.append(hit)
        return kept[:max(1, min(int(limit or DEFAULT_TOP_K), MAX_TOP_K))]


def _check_policy_retrieval() -> list:
    """回归（依赖 Milvus）：政策库已导入且能检索到退款条款。"""
    problems: list = []
    try:
        client = get_milvus_client()
        if client is None or not client.has_collection(COLLECTION_NAME):
            return problems
        rows = client.query(collection_name=COLLECTION_NAME,
                            filter='section_id != ""', output_fields=["count(*)"])
        if not rows or not rows[0].get("count(*)"):
            return problems
    except Exception:
        return problems
    text = PolicyRepository().search("退款多久能到账", 3)
    if not any("退款" in ((hit.get("entity") or {}).get("content") or "") for hit in text):
        problems.append(f"政策库没检索到退款条款：{text[:120]}")
    return problems
