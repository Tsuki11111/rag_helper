import httpx

from app.conf.budget_config import budget_config
from app.conf.embedding_config import embedding_config
from app.core.logger import logger
from app.core.retry import retry_call
from app.core.usage_tracker import Timer, record

# 文本上限参考：qwen3.7-text-embedding 支持 128K token；这里沿用字符截断，
# 避免单条切片异常超长时把整批请求拖垮。
MAX_INPUT_LENGTH = 2048

# DashScope 原生文本嵌入接口。不能走 /compatible-mode/v1：兼容端点不支持
# output_type=dense&sparse，传 sparse 会返回 200 但 embedding 为空。
NATIVE_EMBEDDING_PATH = "/services/embeddings/text-embedding/text-embedding"

# 客户端单例对象，避免重复初始化
_client = None


def get_client():
    """
    获取 DashScope 原生 embedding HTTP 客户端单例。

    只负责连接复用与超时；鉴权头与请求体在 _batch_encode 里拼。
    """
    global _client
    if _client is not None:
        return _client

    if not embedding_config.api_key:
        logger.error("未配置 DashScope API Key，请设置 EMBEDDING_API_KEY 或 OPENAI_API_KEY")
        raise ValueError("缺少 DashScope API Key")

    base_url = (embedding_config.base_url or "").rstrip("/")
    if not base_url:
        logger.error("未配置 EMBEDDING_BASE_URL")
        raise ValueError("缺少 EMBEDDING_BASE_URL")

    logger.info(
        "开始初始化 DashScope embedding 客户端",
        extra={
            "base_url": base_url,
            "model": embedding_config.model,
            "dimension": embedding_config.dimension,
        },
    )
    try:
        _client = httpx.Client(timeout=budget_config.embedding_timeout)
        logger.success("DashScope embedding 客户端初始化成功")
        return _client
    except Exception as e:
        logger.error(f"DashScope embedding 客户端初始化失败：{str(e)}", exc_info=True)
        raise


def _endpoint() -> str:
    """
    拼接原生嵌入接口地址。

    允许 EMBEDDING_BASE_URL 配到 /api/v1，也允许直接配完整接口地址；
    兼容端点直接报错，避免悄悄退化成 dense-only。
    """
    base_url = (embedding_config.base_url or "").rstrip("/")
    if "compatible-mode" in base_url:
        raise ValueError(
            "qwen3.7-text-embedding 的 sparse 输出只支持 DashScope 原生接口，"
            "请把 EMBEDDING_BASE_URL 改为 https://dashscope.aliyuncs.com/api/v1"
        )
    if base_url.endswith(NATIVE_EMBEDDING_PATH):
        return base_url
    if base_url.endswith("/api/v1"):
        return f"{base_url}{NATIVE_EMBEDDING_PATH}"
    raise ValueError(
        "EMBEDDING_BASE_URL 需配置为 https://dashscope.aliyuncs.com/api/v1"
    )


def _truncate(text):
    """将超长文本截断到 MAX_INPUT_LENGTH 字符，避免超出单条文本长度上限。"""
    if len(text) > MAX_INPUT_LENGTH:
        truncated = text[:MAX_INPUT_LENGTH]
        logger.warning(f"文本超长（{len(text)}字），已截断到{MAX_INPUT_LENGTH}字")
        return truncated
    return text


def _to_sparse_dict(items) -> dict:
    """
    把 DashScope 的 sparse_embedding 列表转成 Milvus 需要的 {token_id: weight}。

    响应元素形如 {"index": 123, "token": "退货", "value": 0.42}；
    Milvus 只认 index/value，token 仅用于排查。
    """
    sparse = {}
    for item in items or []:
        try:
            index = int(item.get("index"))
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if value:
            sparse[index] = value
    return sparse


def _batch_encode(client, texts, text_type: str, output_type: str):
    """
    分批调用 DashScope 原生 embedding 接口。

    :return: (dense_list, sparse_list, total_tokens)
             dense_list 与输入一一对应；sparse_list 仅在 output_type 含 sparse 时有值
    """
    dense_list = []
    sparse_list = []
    total_tokens = 0
    batch_size = embedding_config.batch_size
    endpoint = _endpoint()

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        logger.debug(f"正在编码第{i // batch_size + 1}批，共{len(batch)}条")
        payload = {
            "model": embedding_config.model,
            "input": {"texts": batch},
            "parameters": {
                "dimension": embedding_config.dimension,
                "output_type": output_type,
                "text_type": text_type,
            },
        }
        headers = {
            "Authorization": f"Bearer {embedding_config.api_key}",
            "Content-Type": "application/json",
        }

        # 每批各自重试：一批超时不该让整篇文档的嵌入白跑（导入侧一批 16 条）。
        # `raise_for_status` 必须放在重试闭包**里面**：429/5xx 是 HTTP 错误，
        # 在闭包外抛的话 retry_call 根本看不到，等于没有重试。
        def _post_batch():
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            return resp

        response = retry_call(_post_batch, what="生成嵌入")
        body = response.json()
        if body.get("code"):
            raise RuntimeError(f"嵌入接口返回错误：{body.get('code')} {body.get('message')}")

        usage = body.get("usage") or {}
        total_tokens += int(usage.get("total_tokens") or 0)
        items = (body.get("output") or {}).get("embeddings") or []
        if len(items) != len(batch):
            raise RuntimeError(
                f"嵌入接口返回数量不匹配：输入{len(batch)}条，返回{len(items)}条"
            )

        # 按 text_index 排序，保证顺序与输入一致（API 可能乱序返回）
        for item in sorted(items, key=lambda x: int(x.get("text_index", 0))):
            dense_list.append(item.get("embedding") or [])
            if "sparse" in output_type:
                sparse_list.append(_to_sparse_dict(item.get("sparse_embedding")))

    return dense_list, sparse_list, total_tokens


def generate_embeddings(texts, text_type="document", output_type="dense"):
    """
    为文本列表生成向量。

    :param texts: 要生成嵌入的文本列表，单文本也需封装为列表
    :param text_type: query | document；检索侧传 query，导入侧用默认 document
    :param output_type: dense | sparse | dense&sparse；默认只返回稠密向量
    :return: {"dense": [...]}；output_type 含 sparse 时额外带 "sparse": [{token_id: weight}]
    :raise: 向量生成过程中的异常，由调用方捕获处理
    """
    if not isinstance(texts, list) or len(texts) == 0:
        logger.warning("生成向量入参不合法，texts 必须为非空列表")
        raise ValueError("参数 texts 必须是包含文本的非空列表")

    if output_type not in {"dense", "sparse", "dense&sparse"}:
        raise ValueError(f"不支持的 output_type：{output_type}")
    if text_type not in {"query", "document"}:
        raise ValueError(f"不支持的 text_type：{text_type}")

    logger.info(f"开始为{len(texts)}条文本生成向量（text_type={text_type}, output_type={output_type}）")
    timer = Timer()
    try:
        with timer:
            client = get_client()
            texts = [_truncate(t) for t in texts]
            dense, sparse, total_tokens = _batch_encode(client, texts, text_type, output_type)
    except Exception:
        logger.error("文本向量生成失败", exc_info=True)
        # timer 的 __exit__ 已在异常穿出 with 时执行，此处读到的耗时是有效的
        record("embedding", model=embedding_config.model, latency_ms=timer.ms, ok=False,
               error="向量生成失败（详见日志）")
        raise  # 不吞异常，向上传递让调用方做重试/降级处理

    # 非 LangChain 调用，手工埋点：嵌入按输入 token 计费，分批合并成一条账
    record("embedding", model=embedding_config.model, prompt_tokens=total_tokens,
           latency_ms=timer.ms, items=len(texts))

    result = {"dense": dense}
    if "sparse" in output_type:
        result["sparse"] = sparse
    logger.success(f"{len(texts)}条文本向量生成完成，维度={embedding_config.dimension}")

    return result


def _check_embedding_config() -> list:
    """回归：原生端点校验 + sparse 响应转换。"""
    problems = []
    got = _to_sparse_dict([
        {"index": 12, "token": "退货", "value": 0.5},
        {"index": "34", "token": "退款", "value": "0.25"},
        {"index": 56, "value": 0},          # 零权重丢弃
        {"index": "bad", "value": 0.1},     # 非法 index 丢弃
    ])
    if got != {12: 0.5, 34: 0.25}:
        problems.append(f"sparse_embedding 转换不对：{got!r}")

    real_base = embedding_config.base_url
    try:
        embedding_config.base_url = "https://dashscope.aliyuncs.com/api/v1"
        if _endpoint() != f"https://dashscope.aliyuncs.com/api/v1{NATIVE_EMBEDDING_PATH}":
            problems.append(f"原生端点拼接不对：{_endpoint()!r}")
        embedding_config.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        try:
            _endpoint()
            problems.append("compatible-mode 没有被拒绝，sparse 会静默变空")
        except ValueError:
            pass
    finally:
        embedding_config.base_url = real_base
    return problems
