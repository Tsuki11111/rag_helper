"""
管道调参配置：检索 / 重排 / 切片 / 识别

此前这些数值散在各节点文件里硬编码（`DEFAULT_MAX_CONTENT_LENGTH`、`RERANK_GAP_RATIO` …），
调一次要翻好几个文件、改完还得重新提交。现在全部走 `.env`；**默认值与原硬编码完全一致** ——
不配 `.env` 时行为与从前一模一样，配了就生效、不用改代码（与 `budget_config` 同一条理念）。

分两组：
- **查询侧**（product_helper 检索图）：产品名对齐阈值 / 各路召回 TOP_K / RRF / 重排 / 生成上下文
- **导入侧**（import 图）：切片长度 / 图谱抽取批次 / 产品名识别上下文 / 入库批次

**只收「运行时可调」的参数**；文案、字段名、状态枚举这类代码逻辑常量不在此列。
"""
from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


def _num(name: str, default: float) -> float:
    """读数值型环境变量；没配或配成非数字就退回默认值（不让一个笔误把管道搞挂）"""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    return max(1, int(_num(name, default)))


@dataclass
class PipelineConfig:
    # --- 查询侧：产品名对齐（node_item_name_confirm）---
    confirm_score_threshold: float    # ≥ 该值直接确认
    candidate_score_threshold: float  # ≥ 该值作为候选问用户
    max_candidate_options: int
    max_near_options: int
    item_search_limit: int            # 产品名检索的匹配数

    # --- 查询侧：四路召回 ---
    dense_top_k: int                  # 向量 / HyDE 每路召回数
    search_ef: int                    # HNSW 检索参数
    kg_top_k: int                     # 图谱路返回切片数
    rrf_k: int
    rrf_max_results: int
    rrf_weight_vector: float
    rrf_weight_hyde: float
    rrf_weight_kg: float

    # --- 查询侧：重排与生成 ---
    rerank_max_topk: int
    rerank_min_topk: int              # 断崖截断的硬地板
    rerank_gap_ratio: float           # 相对断崖阈值（分数尺度差异见 HANDOFF §8.9）
    web_max_slots: int                # 联网结果至多补几条
    mcp_max_results: int              # MCP 联网搜索取前几条交给下游重排
    max_context_chars_per_doc: int    # 单个切片拼进上下文的上限

    # --- 导入侧 ---
    split_max_content_length: int     # 切片二次切分长度
    split_min_content_length: int     # 短切片合并门槛
    kg_batch_size: int                # 图谱抽取每批切片数
    kg_max_chunk_chars: int
    item_name_chunk_k: int
    item_name_single_chunk_max_len: int
    item_name_context_max_chars: int
    milvus_insert_batch_size: int
    max_file_size_mb: int


pipeline_config = PipelineConfig(
    # 查询侧：产品名对齐
    confirm_score_threshold=_num("CONFIRM_SCORE_THRESHOLD", 0.85),
    candidate_score_threshold=_num("CANDIDATE_SCORE_THRESHOLD", 0.6),
    max_candidate_options=_int("MAX_CANDIDATE_OPTIONS", 3),
    max_near_options=_int("MAX_NEAR_OPTIONS", 3),
    item_search_limit=_int("ITEM_SEARCH_LIMIT", 5),
    # 查询侧：四路召回
    dense_top_k=_int("DENSE_TOP_K", 5),
    search_ef=_int("SEARCH_EF", 64),
    kg_top_k=_int("KG_TOP_K", 5),
    rrf_k=_int("RRF_K", 60),
    rrf_max_results=_int("RRF_MAX_RESULTS", 10),
    rrf_weight_vector=_num("RRF_WEIGHT_VECTOR", 1.0),
    rrf_weight_hyde=_num("RRF_WEIGHT_HYDE", 1.0),
    rrf_weight_kg=_num("RRF_WEIGHT_KG", 1.0),
    # 查询侧：重排与生成
    rerank_max_topk=_int("RERANK_MAX_TOPK", 10),
    rerank_min_topk=_int("RERANK_MIN_TOPK", 3),
    rerank_gap_ratio=_num("RERANK_GAP_RATIO", 0.25),
    web_max_slots=_int("WEB_MAX_SLOTS", 2),
    mcp_max_results=_int("MCP_MAX_RESULTS", 5),
    max_context_chars_per_doc=_int("MAX_CONTEXT_CHARS_PER_DOC", 1200),
    # 导入侧
    split_max_content_length=_int("SPLIT_MAX_CONTENT_LENGTH", 2000),
    split_min_content_length=_int("SPLIT_MIN_CONTENT_LENGTH", 500),
    kg_batch_size=_int("KG_BATCH_SIZE", 5),
    kg_max_chunk_chars=_int("KG_MAX_CHUNK_CHARS", 800),
    item_name_chunk_k=_int("ITEM_NAME_CHUNK_K", 5),
    item_name_single_chunk_max_len=_int("ITEM_NAME_SINGLE_CHUNK_MAX_LEN", 800),
    item_name_context_max_chars=_int("ITEM_NAME_CONTEXT_MAX_CHARS", 2500),
    milvus_insert_batch_size=_int("MILVUS_INSERT_BATCH_SIZE", 200),
    max_file_size_mb=_int("MAX_FILE_SIZE_MB", 200),
)
