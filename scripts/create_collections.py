import sys

from pymilvus import DataType, MilvusClient

from app.conf.embedding_config import embedding_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger

# 切片文本字段的最大长度（Milvus VARCHAR 上限 65535）
TEXT_FIELD_MAX_LENGTH = 65535
# item_name 集合的主键长度：产品名称作为主键，512 足够且索引更省空间
ITEM_NAME_KEY_MAX_LENGTH = 512
# 建库与检索必须使用同一度量方式，COSINE 与 .env 的 MILVUS_METRIC_TYPE 保持一致
METRIC_TYPE = "COSINE"


def step1_validate_config() -> int:
    """
    1.校验建集合所需的配置
    :return: 向量维度
    """
    function_name = sys._getframe().f_code.co_name
    if not milvus_config.item_name_collection:
        logger.error(f"[{function_name}] 缺少ITEM_NAME_COLLECTION配置")
        raise Exception("缺少ITEM_NAME_COLLECTION配置")
    if not milvus_config.chunks_collection:
        logger.error(f"[{function_name}] 缺少CHUNKS_COLLECTION配置")
        raise Exception("缺少CHUNKS_COLLECTION配置")
    dimension = embedding_config.dimension
    if not dimension or dimension <= 0:
        logger.error(f"[{function_name}] 向量维度不合法: {dimension}")
        raise Exception(f"向量维度不合法: {dimension}")
    logger.info(f"[{function_name}] 配置校验通过，向量维度={dimension}")
    return dimension


def build_item_name_schema(client, dimension: int):
    """
    构建 kb_item_names 的 schema
    字段：item_name(主键) / file_title / dense_vector
    item_name 作为主键：产品名称天然唯一，重复导入时可用upsert直接覆盖，实现幂等。
    注意：upsert 是「标记删除 + 插入」，get_collection_stats 的 row_count 会包含已删除记录，
    判断实际记录数请用 query，不要用 row_count。
    :param client: MilvusClient实例
    :param dimension: 向量维度
    :return: CollectionSchema
    """
    function_name = sys._getframe().f_code.co_name
    # enable_dynamic_field=True：后续想加字段不必重建集合
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)

    # 主键：产品名称本身，不再额外引入自增id
    schema.add_field(
        field_name="item_name",
        datatype=DataType.VARCHAR,
        max_length=ITEM_NAME_KEY_MAX_LENGTH,
        is_primary=True,
    )
    # 产品名称所属的源文件标题，便于反查产品来自哪篇文档
    schema.add_field(
        field_name="file_title",
        datatype=DataType.VARCHAR,
        max_length=TEXT_FIELD_MAX_LENGTH,
    )
    # item_name 只做产品名匹配，仍用 qwen3.7-text-embedding 的稠密向量
    schema.add_field(
        field_name="dense_vector",
        datatype=DataType.FLOAT_VECTOR,
        dim=dimension,
    )
    logger.info(f"[{function_name}] schema构建完成，主键=item_name，向量维度={dimension}")
    return schema


def build_chunks_schema(client, dimension: int):
    """
    构建 kb_chunks v2 的 schema

    稳定业务键 `chunk_key` 做主键；`chunk_id` 仅保留为旧接口兼容的 64 位
    数值。文档、产品、章节、版本、分页等字段用于过滤、引用与增量更新。

    :param client: MilvusClient实例
    :param dimension: 向量维度
    :return: CollectionSchema
    """
    function_name = sys._getframe().f_code.co_name
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)

    # 稳定业务键：doc_id + section_id + chunk_index 的哈希
    schema.add_field(field_name="chunk_key", datatype=DataType.VARCHAR, max_length=128, is_primary=True)
    # 兼容旧接口的 64 位数值 id；新代码不要用它做稳定引用
    schema.add_field(field_name="chunk_id", datatype=DataType.INT64)
    # 文档身份与版本
    schema.add_field(field_name="doc_id", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="doc_version", datatype=DataType.VARCHAR, max_length=32)
    # 产品/租户；当前单租户 merchant_id 为空，先留字段
    schema.add_field(field_name="product_id", datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name="merchant_id", datatype=DataType.VARCHAR, max_length=64)
    # 章节结构
    schema.add_field(field_name="section_id", datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name="section_path", datatype=DataType.VARCHAR, max_length=1024)
    schema.add_field(field_name="section_index", datatype=DataType.INT32)
    schema.add_field(field_name="page_no", datatype=DataType.INT32)
    schema.add_field(field_name="chunk_index", datatype=DataType.INT32)
    # 正文与内容指纹
    schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=TEXT_FIELD_MAX_LENGTH)
    schema.add_field(field_name="content_hash", datatype=DataType.VARCHAR, max_length=64)
    # 展示与兼容字段
    schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=TEXT_FIELD_MAX_LENGTH)
    schema.add_field(field_name="parent_title", datatype=DataType.VARCHAR, max_length=TEXT_FIELD_MAX_LENGTH)
    schema.add_field(field_name="part", datatype=DataType.INT32)
    schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=TEXT_FIELD_MAX_LENGTH)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=TEXT_FIELD_MAX_LENGTH)
    # 生命周期与来源
    schema.add_field(field_name="source_type", datatype=DataType.VARCHAR, max_length=32)
    schema.add_field(field_name="is_active", datatype=DataType.BOOL)
    schema.add_field(field_name="embedding_model", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="metadata", datatype=DataType.JSON)
    # 向量
    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=dimension)
    schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
    logger.info(
        f"[{function_name}] schema构建完成，主键=chunk_key，"
        f"向量维度={dimension}，含 sparse_vector 与章节/版本字段"
    )
    return schema


def step3_build_index_params(client, with_sparse: bool = False):
    """
    3.构建索引参数
    稠密向量用HNSW索引（检索快、召回高），度量方式与建库/检索保持一致；
    kb_chunks 额外为稀疏向量建 SPARSE_INVERTED_INDEX（内积）。
    :param client: MilvusClient实例
    :param with_sparse: 是否同时为 sparse_vector 建索引
    :return: IndexParams
    """
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense_vector",
        index_name="dense_vector_index",
        index_type="HNSW",
        metric_type=METRIC_TYPE,
        # M: 图中每个节点的最大连接数；efConstruction: 建索引时的搜索范围
        params={"M": 16, "efConstruction": 200},
    )
    if with_sparse:
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_vector_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
        )
    logger.info(
        f"[{sys._getframe().f_code.co_name}] 索引参数构建完成，"
        f"dense=HNSW/{METRIC_TYPE}，sparse={'SPARSE_INVERTED_INDEX/IP' if with_sparse else '无'}"
    )
    return index_params


def step4_create_collection(client, collection_name, schema, index_params) -> bool:
    """
    4.创建集合并加载到内存
    已存在则跳过，避免重复建库覆盖数据
    :return: True表示本次新建，False表示已存在跳过
    """
    function_name = sys._getframe().f_code.co_name
    if client.has_collection(collection_name):
        logger.warning(f"[{function_name}] 集合[{collection_name}]已存在，跳过创建")
        return False
    client.create_collection(
        collection_name=collection_name,
        schema=schema,
        index_params=index_params,
    )
    # 加载后才能被检索到
    client.load_collection(collection_name)
    logger.success(f"[{function_name}] 集合[{collection_name}]创建并加载完成")
    return True


def create_item_name_collection(client, dimension: int) -> None:
    """
    创建 kb_item_names 集合（产品主体名称）
    """
    collection_name = milvus_config.item_name_collection
    schema = build_item_name_schema(client, dimension)
    index_params = step3_build_index_params(client)
    step4_create_collection(client, collection_name, schema, index_params)


def create_chunks_collection(client, dimension: int) -> None:
    """
    创建 kb_chunks 集合（文档切片）
    """
    collection_name = milvus_config.chunks_collection
    schema = build_chunks_schema(client, dimension)
    index_params = step3_build_index_params(client, with_sparse=True)
    step4_create_collection(client, collection_name, schema, index_params)


def ensure_database() -> None:
    """
    确保 Milvus 库存在

    全新部署时 Milvus 只有内置的 default 库；直接以 db_name=rag_helper 连接不会自动建库，
    后续建集合会报 database not found。这里先连默认库补建，已存在则跳过。
    """
    function_name = sys._getframe().f_code.co_name
    db_name = milvus_config.milvus_db_name
    if not db_name or db_name == "default":
        return
    client = MilvusClient(uri=milvus_config.milvus_url)
    if db_name in client.list_databases():
        logger.info(f"[{function_name}] Milvus库[{db_name}]已存在，跳过创建")
        return
    client.create_database(db_name)
    logger.success(f"[{function_name}] Milvus库[{db_name}]创建完成")


def create_all_collections() -> None:
    """
    创建全部集合（kb_item_names + kb_chunks）
    流程：校验配置 -> 确保库存在 -> 连接Milvus -> 逐个建表并加载
    说明：文档指纹（去重记录）存在 MongoDB 里，不是 Milvus 集合
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{function_name}] 开始创建Milvus集合")

    try:
        # 1.校验配置
        dimension = step1_validate_config()
        # 2.全新 Milvus 先补建库
        ensure_database()
        client = MilvusClient(uri=milvus_config.milvus_url,
                              db_name=milvus_config.milvus_db_name)
        # 3.建 kb_item_names
        create_item_name_collection(client, dimension)
        # 4.建 kb_chunks
        create_chunks_collection(client, dimension)
        logger.info(f"[{function_name}] 集合创建流程结束，当前集合列表: {client.list_collections()}")
    except Exception as e:
        logger.error(f"[{function_name}] 创建失败，错误信息为{e}", exc_info=True)
        raise e


def _check_chunks_schema() -> list:
    """回归：kb_chunks v2 必须有稳定主键、章节字段与 dense/sparse 向量。"""
    client = MilvusClient(uri=milvus_config.milvus_url)
    schema = build_chunks_schema(client, embedding_config.dimension)
    client.close()
    fields = {f.name: f for f in schema.fields}
    problems = []
    expected = {
        "chunk_key", "chunk_id", "doc_id", "doc_version", "product_id", "merchant_id",
        "section_id", "section_path", "section_index", "page_no", "chunk_index",
        "content", "content_hash", "title", "parent_title", "part", "file_title",
        "item_name", "source_type", "is_active", "embedding_model", "metadata",
        "dense_vector", "sparse_vector",
    }
    missing = expected - set(fields)
    if missing:
        problems.append(f"kb_chunks 缺少字段：{sorted(missing)}")
    if not fields.get("chunk_key") or not fields["chunk_key"].is_primary:
        problems.append("kb_chunks 的 chunk_key 必须是主键")
    if fields.get("dense_vector") is None:
        problems.append("kb_chunks 缺少 dense_vector")
    if fields.get("sparse_vector") is None or fields["sparse_vector"].dtype != DataType.SPARSE_FLOAT_VECTOR:
        problems.append(f"kb_chunks 的 sparse_vector 类型不对：{fields.get('sparse_vector')!r}")
    if fields.get("part") is not None and fields["part"].dtype != DataType.INT32:
        problems.append(f"kb_chunks 的 part 类型应为 INT32：{fields['part'].dtype!r}")
    return problems


if __name__ == '__main__':
    """
    本地测试入口：需要本地Milvus已启动
    """
    create_all_collections()
