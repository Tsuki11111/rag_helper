"""把平台政策章节写入独立 Milvus 集合（政策导入简化链路的后半段）。

与产品文档的 node_import_milvus 分开：政策集合字段、幂等键（policy_key）都不同，
但复用同一个向量化节点产出的 dense_vector。
"""
import sys

from app.agent.tools.policy_kb import COLLECTION_NAME, ensure_collection
from app.clients.milvus_clients import get_milvus_client
from app.core.logger import logger
from app.import_process.agent.state import ImportGraphState
from app.utils.escape_milvus_string_utils import escape_milvus_string
from app.utils.task_utils import add_done_task, add_running_task

NODE_NAME = "node_import_policy_milvus"
REQUIRED_FIELDS = ["policy_key", "policy_title", "section_no", "content", "dense_vector"]


def build_policy_rows(chunks: list) -> list:
    """把导入图 chunks 转成政策集合行；纯函数便于回归。"""
    rows = []
    for chunk in chunks:
        policy_key = str(chunk.get("policy_key") or "").strip()
        section_no = int(chunk.get("section_no") or 0)
        rows.append({
            "section_id": f"{policy_key}#{section_no}",
            "policy_key": policy_key,
            "policy_title": str(chunk.get("policy_title") or "")[:255],
            "section_no": section_no,
            "heading": str(chunk.get("heading") or "")[:512],
            "content": str(chunk.get("content") or "")[:16384],
            "version": str(chunk.get("version") or "")[:32],
            "dense_vector": chunk.get("dense_vector") or [],
        })
    return rows


def node_import_policy_milvus(state: ImportGraphState) -> ImportGraphState:
    """节点：校验 → 确保集合 → 按 policy_key 清理 → upsert 章节向量。"""
    function_name = sys._getframe().f_code.co_name
    task_id = state.get("task_id", "")
    add_running_task(task_id, function_name)
    try:
        chunks = state.get("chunks") or []
        if not chunks:
            raise ValueError("政策章节为空，无法写入 Milvus")
        for idx, chunk in enumerate(chunks):
            missing = [field for field in REQUIRED_FIELDS if not chunk.get(field)]
            if missing:
                raise ValueError(f"第{idx + 1}条政策章节缺少字段：{missing}")

        client = get_milvus_client()
        if client is None:
            raise ValueError("Milvus 连接失败：get_milvus_client() 返回空")
        ensure_collection(client)

        policy_key = str(chunks[0].get("policy_key") or "")
        client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'policy_key == "{escape_milvus_string(policy_key)}"',
        )
        rows = build_policy_rows(chunks)
        client.upsert(collection_name=COLLECTION_NAME, data=rows)
        client.flush(COLLECTION_NAME)
        client.load_collection(COLLECTION_NAME)
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 政策 {policy_key} 写入完成，"
            f"共 {len(rows)} 个章节（集合 {COLLECTION_NAME}）"
        )
    finally:
        add_done_task(task_id, function_name)
    return state


def _check_policy_import_rows() -> list:
    """回归：chunks → 政策集合行的字段与幂等主键。"""
    problems = []
    rows = build_policy_rows([{
        "policy_key": "refund",
        "policy_title": "退换货与售后政策",
        "section_no": 3,
        "heading": "3. 退款时效",
        "content": "1-3 个工作日原路退回。",
        "version": "1.0",
        "dense_vector": [0.1, 0.2],
    }])
    row = rows[0] if rows else {}
    if row.get("section_id") != "refund#3":
        problems.append(f"政策主键不对：{row.get('section_id')}")
    if row.get("policy_key") != "refund" or row.get("section_no") != 3:
        problems.append("政策行缺少 policy_key/section_no")
    return problems
