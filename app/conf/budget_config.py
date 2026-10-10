"""
超时与预算配置

两类数值，都是为了**别让人无限等**：

1. **各客户端单次调用的超时** —— 此前多数外部客户端压根没设：LLM / 嵌入 / 视觉走 openai SDK
   的隐式 600 秒，MinerU 的 `requests` 更是**彻底无限等待**。一个卡住的调用会让整轮问答一直挂着，
   用户只能刷新页面。
2. **一整轮的预算**（wall-clock / token）—— 超了就中止本轮，而不是无限跑下去。

数值全走 `.env`，这里只给保守默认值；**调这些值不用改代码**。

**为什么用「客户端超时」而不是「节点级超时」**：LangGraph 的 `add_node(timeout=...)`
对**同步节点不可用**（实测报 `Node timeouts are only supported for async nodes because
sync Python execution cannot be safely cancelled in-process`）。本项目 16 个节点全是同步的，
而客户端超时能让**每一路各自降级**，正好接上既有的 `error_policy`（timeout → RETRYABLE → 降级）。
"""
from dataclasses import dataclass
import os

from dotenv import load_dotenv

# 与 app/conf/ 下其它配置模块一致：先加载 .env，再读环境变量
load_dotenv()


def _num(name: str, default: float) -> float:
    """读数值型环境变量；没配或配成非数字就退回默认值（不让一个笔误把服务搞挂）"""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class BudgetConfig:
    # --- 各客户端单次调用超时（秒）---
    llm_timeout: float              # LLM 与视觉模型（视觉复用同一个客户端）
    embedding_timeout: float        # 嵌入
    milvus_timeout: float           # Milvus 单次操作
    neo4j_connect_timeout: float    # Neo4j 建连
    neo4j_tx_retry_time: float      # Neo4j 事务总重试时间
    minio_connect_timeout: float    # MinIO 建连
    minio_read_timeout: float       # MinIO 读
    mineru_connect_timeout: float   # MinerU 建连
    mineru_read_timeout: float      # MinerU 创建任务 / 轮询状态
    mineru_transfer_timeout: float  # MinerU 大文件传输：上传 PDF 与下载结果包

    # --- 整轮预算（目前只给查询图用）---
    # 判定用的是 `is not None` + `>=`：**给了值就一律执行**，且**达到即止**
    #（所以设 0 = 在第一个节点前就中止）。想放宽就把数值调大。
    # 导入侧根本不注入这两个值，所以那边完全不受影响。
    query_wall_clock_budget: float  # 一次问答最多耗时（秒）
    query_token_budget: float       # 一次问答最多消耗 token

    # --- 故障分类重试（见 app/core/retry.py，只对 RETRYABLE 生效）---
    # 数值先取保守默认：**实测暂时性故障极少**（7 天 1066 次调用里只有 1 次超时），
    # 拿不出分布来调参，所以这里只保证「有重试、别重试太久」，等真出问题再改数字
    retry_max_attempts: int         # 总共尝试几次（2 = 原调用 + 重试 1 次）
    retry_base_delay_sec: float     # 退避基数（指数增长 + 抖动）
    retry_max_delay_sec: float      # 单次退避上限（429 的 Retry-After 也受它约束）


budget_config = BudgetConfig(
    llm_timeout=_num("LLM_TIMEOUT_SEC", 120.0),
    embedding_timeout=_num("EMBEDDING_TIMEOUT_SEC", 60.0),
    milvus_timeout=_num("MILVUS_TIMEOUT_SEC", 10.0),
    neo4j_connect_timeout=_num("NEO4J_CONNECT_TIMEOUT_SEC", 5.0),
    neo4j_tx_retry_time=_num("NEO4J_TX_RETRY_TIMEOUT_SEC", 10.0),
    minio_connect_timeout=_num("MINIO_CONNECT_TIMEOUT_SEC", 5.0),
    minio_read_timeout=_num("MINIO_READ_TIMEOUT_SEC", 60.0),
    # MinerU 走的是国内 CDN，握手偶尔偏慢：连接超时给宽一点（30 秒）
    mineru_connect_timeout=_num("MINERU_CONNECT_TIMEOUT_SEC", 30.0),
    mineru_read_timeout=_num("MINERU_READ_TIMEOUT_SEC", 60.0),
    mineru_transfer_timeout=_num("MINERU_TRANSFER_TIMEOUT_SEC", 300.0),
    query_wall_clock_budget=_num("QUERY_WALL_CLOCK_BUDGET_SEC", 180.0),
    query_token_budget=_num("QUERY_TOKEN_BUDGET", 80000.0),
    retry_max_attempts=max(1, int(_num("RETRY_MAX_ATTEMPTS", 2))),
    retry_base_delay_sec=_num("RETRY_BASE_DELAY_SEC", 0.5),
    retry_max_delay_sec=_num("RETRY_MAX_DELAY_SEC", 5.0),
)
