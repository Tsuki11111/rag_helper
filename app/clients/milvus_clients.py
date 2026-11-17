import os
from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker
from app.conf.budget_config import budget_config
from app.conf.milvus_config import milvus_config
from app.core.logger import logger

# 全局Milvus客户端实例，实现单例复用
_milvus_client = None


def get_milvus_client():
    """
    Milvus客户端单例获取方法
    实现客户端连接复用，避免重复创建连接消耗资源
    :return: MilvusClient实例，连接失败返回None
    """
    try:
        global _milvus_client
        # 单例判断：未初始化则创建新连接
        if _milvus_client is None:
            milvus_uri = milvus_config.milvus_url
            # 校验Milvus连接地址配置
            if not milvus_uri:
                logger.error("Milvus客户端连接失败：缺少MILVUS_URL环境变量配置")
                return None
            # 初始化Milvus客户端。timeout 显式写出来：pymilvus 默认也是 10 秒，
            # 但写在配置里才能统一调（见 app/conf/budget_config.py）
            # db_name 同理必须显式传：Milvus 是「库 → 集合」两级，不传会落到内置的 default 库
            _milvus_client = MilvusClient(
                uri=milvus_uri,
                db_name=milvus_config.milvus_db_name,
                timeout=budget_config.milvus_timeout,
            )
            logger.info("Milvus客户端连接成功")
        return _milvus_client
    except Exception as e:
        logger.error(f"Milvus客户端连接异常：{str(e)}", exc_info=True)
        return None


def _coerce_int64_ids(ids):
    """
    转换chunk_id为Milvus要求的INT64类型（主键字段schema为INT64）
    过滤无效ID，分离可转换/不可转换的ID
    :param ids: 待转换的chunk_id列表
    :return: 元组(ok_ids, bad_ids)，ok_ids为可转换的int64类型ID列表，bad_ids为无效ID列表
    """
    ok, bad = [], []
    for x in (ids or []):
        if x is None:
            continue
        try:
            ok.append(int(x))
        except Exception:
            bad.append(x)
    return ok, bad


def fetch_chunks_by_chunk_ids(
        client,
        collection_name: str,
        chunk_ids,
        *,
        output_fields=None,
        batch_size: int = 100,
):
    """
    通过业务 chunk_id 批量查询 Milvus 中的切片数据

    kb_chunks v2 的主键是稳定 `chunk_key`，`chunk_id` 只是普通 INT64 字段；
    不能再用 `client.get(ids=chunk_ids)` 按主键查，否则会拿数值去匹配
    chunk_key 而全部查不到。这里统一用 `chunk_id in [...]` 过滤。
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param chunk_ids: 待查询的chunk_id列表
    :param output_fields: 需要返回的字段列表，默认返回核心切片字段
    :param batch_size: 分批查询大小，避免单次查询数据量过大，默认100
    :return: List[dict]，Milvus实体字典列表，查询失败返回空列表
    """
    # 前置校验：客户端/集合名无效直接返回空
    if client is None:
        return []
    if not collection_name:
        return []
    # 默认返回字段：核心切片标识与内容字段
    if output_fields is None:
        output_fields = ["chunk_id", "content", "title", "parent_title", "item_name"]

    # 转换ID为INT64类型，分离有效/无效ID
    ok_ids, bad_ids = _coerce_int64_ids(chunk_ids)
    if bad_ids:
        # 记录无效ID，跳过查询
        logger.warning(f"存在无法转换为INT64的chunk_id，将跳过查询：{bad_ids}")

    # 无有效ID直接返回空
    if not ok_ids:
        return []

    results = []
    # 分批查询：按batch_size切分有效ID，循环查询
    for i in range(0, len(ok_ids), batch_size):
        batch = ok_ids[i: i + batch_size]
        try:
            expr = f"chunk_id in [{', '.join(str(x) for x in batch)}]"
            q = client.query(collection_name=collection_name, filter=expr, output_fields=output_fields)
            if q:
                results.extend(q)
        except Exception as e:
            logger.error(f"Milvus query方法批量查询chunk_id失败：{str(e)}", exc_info=True)

    return results


def dense_search(
        client,
        collection_name,
        dense_vector,
        limit=5,
        expr=None,
        output_fields=None,
        search_params=None,
):
    """
    基于稠密向量执行Milvus向量检索

    只使用稠密向量检索；需要 dense+sparse 融合时用 hybrid_search，
    旧集合没有 sparse_vector 时也回退到本函数。
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param dense_vector: 查询文本生成的稠密向量（单条，非嵌套列表）
    :param limit: 返回结果数量，默认5
    :param expr: 过滤表达式，如按 item_name 限定产品
    :param output_fields: 需要返回的字段列表，默认返回 item_name
    :param search_params: 搜索参数，如 HNSW 的 ef，默认 None
    :return: 搜索结果列表（外层对应每条查询向量），检索失败返回None
    """
    try:
        if client is None:
            logger.error(f"Milvus检索失败，集合[{collection_name}]：客户端不可用")
            return None

        # 默认返回字段：文档标识字段
        if output_fields is None:
            output_fields = ["item_name"]

        # 搜索参数：显式指定的话以传入为准，未指定时由客户端使用默认值
        params = {
            "metric_type": milvus_config.metric_type,
            "params": search_params or {},
        }

        res = client.search(
            collection_name=collection_name,
            data=[dense_vector],
            anns_field="dense_vector",
            limit=limit,
            filter=expr,
            output_fields=output_fields,
            search_params=params,
        )
        logger.info(f"Milvus稠密检索完成，集合[{collection_name}]共检索到{len(res[0])}条结果")
        return res
    except Exception as e:
        logger.error(f"Milvus稠密检索执行失败，集合[{collection_name}]：{str(e)}", exc_info=True)
        return None


def hybrid_search(
        client,
        collection_name,
        dense_vector,
        sparse_vector,
        limit=5,
        expr=None,
        output_fields=None,
        search_params=None,
        sparse_search_params=None,
        ranker_k=60,
):
    """
    dense + sparse 双路检索，用 Milvus RRFRanker 融合。

    sparse_vector 为空、集合不支持 sparse、或 Milvus 报错时返回 None，
    由调用方回退 dense_search（不把整条检索链路拖死）。

    :param dense_vector: 查询文本的稠密向量（单条）
    :param sparse_vector: 查询文本的稀疏向量 {token_id: weight}
    :param expr: 过滤表达式；hybrid_search 没有 filter 参数，需分别放进两个请求
    :param output_fields: 需要返回的字段列表
    :param search_params: HNSW 参数，如 {"ef": 64}
    :param sparse_search_params: 稀疏索引参数，默认空
    :param ranker_k: RRF 的 k，默认 60
    :return: 与 dense_search 相同的外层结构；失败返回 None
    """
    if client is None:
        logger.error(f"Milvus 混合检索失败，集合[{collection_name}]：客户端不可用")
        return None
    if not sparse_vector:
        logger.info(f"集合[{collection_name}]查询稀疏向量为空，跳过 hybrid_search")
        return None

    try:
        if output_fields is None:
            output_fields = ["item_name"]

        dense_req = AnnSearchRequest(
            data=[dense_vector],
            anns_field="dense_vector",
            param={
                "metric_type": milvus_config.metric_type,
                "params": search_params or {},
            },
            limit=limit,
            expr=expr,
        )
        sparse_req = AnnSearchRequest(
            data=[sparse_vector],
            anns_field="sparse_vector",
            param={
                "metric_type": "IP",
                "params": sparse_search_params or {},
            },
            limit=limit,
            expr=expr,
        )

        res = client.hybrid_search(
            collection_name=collection_name,
            reqs=[dense_req, sparse_req],
            ranker=RRFRanker(ranker_k),
            limit=limit,
            output_fields=output_fields,
        )
        logger.info(f"Milvus 混合检索完成，集合[{collection_name}]共检索到{len(res[0])}条结果")
        return res
    except Exception as e:
        logger.error(f"Milvus 混合检索执行失败，集合[{collection_name}]：{str(e)}", exc_info=True)
        return None


def upsert_item_name(client, collection_name, item_name, file_title, dense_vector):
    """
    写入（或覆盖）一条产品主体名称记录

    调用方为 node_item_name_recognition，集中在此处以复用同一套转义与字段约定
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param item_name: 产品主体名称，集合主键
    :param file_title: 产品名称所属的源文件标题
    :param dense_vector: 产品名称的稠密向量
    :return: True表示写入成功
    """
    data = {
        "item_name": item_name,
        "file_title": file_title,
        "dense_vector": dense_vector,
    }
    client.upsert(collection_name=collection_name, data=[data])
    client.flush(collection_name)
    logger.info(f"产品名称[{item_name}]已写入集合[{collection_name}]")
    return True


def _check_hybrid_search_fallback() -> list:
    """回归：sparse 为空或 Milvus 报错时必须返回 None，让调用方回退 dense。"""
    problems = []
    if hybrid_search(None, "kb_chunks", [0.1], {1: 0.5}) is not None:
        problems.append("客户端不可用时应返回 None")

    if hybrid_search(object(), "kb_chunks", [0.1], {}) is not None:
        problems.append("sparse 为空时应返回 None，不能发起请求")

    class _Boom:
        def hybrid_search(self, **_kwargs):
            raise RuntimeError("hybrid down")

    if hybrid_search(_Boom(), "kb_chunks", [0.1], {1: 0.5}) is not None:
        problems.append("hybrid_search 异常时应返回 None，不能把检索链路拖死")
    return problems