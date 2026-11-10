"""平台政策导入：从 Java 拉取政策章节，转成可向量化的 chunks。

政策原文以 Java MySQL 为准；本节点不解析 Markdown，只把已经入库的章节转成
导入图统一使用的 chunks 结构，后面的向量化 / Milvus 写入节点可以复用。
"""
import sys

import httpx

from app.conf.its_mygo_config import its_mygo_config
from app.core.logger import logger
from app.importing.state import ImportGraphState
from app.utils.task_utils import add_done_task, add_running_task

NODE_NAME = "node_policy_chunks"


def build_policy_chunks(detail: dict, policy_key: str) -> list:
    """把 Java 政策详情转成 chunks；纯函数便于回归测试。"""
    title = (detail.get("title") or policy_key).strip()
    version = (detail.get("version") or "").strip()
    chunks = []
    for section in detail.get("sections") or []:
        content = (section.get("content") or "").strip()
        heading = (section.get("heading") or "").strip()
        section_no = int(section.get("sectionNo") or 0)
        # 空正文章节没有可召回语义，跳过；MySQL 仍保留它供管理端阅读
        if not content:
            continue
        chunks.append({
            "title": heading or title,
            "parent_title": title,
            "part": section_no,
            "file_title": title,
            "policy_key": policy_key,
            "policy_title": title,
            "section_no": section_no,
            "heading": heading,
            "version": version,
            "content": content,
            # 政策章节的嵌入文本带上标题与章节名，召回时语义更完整
            "embedding_text": f"《{title}》{heading}\n{content}",
        })
    return chunks


def _fetch_policy_detail(policy_key: str) -> dict:
    base = its_mygo_config.base_url.rstrip("/")
    url = f"{base}/api/policies/{policy_key}"
    with httpx.Client(timeout=15) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    detail = payload.get("data") or {}
    if not detail:
        raise ValueError(f"平台政策不存在或未生效：{policy_key}")
    return detail


def node_policy_chunks(state: ImportGraphState) -> ImportGraphState:
    """节点：拉取政策详情 → 生成章节 chunks。"""
    function_name = sys._getframe().f_code.co_name
    task_id = state.get("task_id", "")
    policy_key = (state.get("policy_key") or "").strip()
    logger.info(f"[{NODE_NAME}] [{function_name}] 开始处理政策：{policy_key}")
    add_running_task(task_id, function_name)
    try:
        if not policy_key:
            raise ValueError("政策库导入缺少 policy_key")
        detail = _fetch_policy_detail(policy_key)
        chunks = build_policy_chunks(detail, policy_key)
        if not chunks:
            raise ValueError(f"政策没有可导入的章节：{policy_key}")
        state["chunks"] = chunks
        state["file_title"] = detail.get("title") or policy_key
        logger.info(f"[{NODE_NAME}] [{function_name}] 政策章节转换完成，共 {len(chunks)} 条")
    finally:
        add_done_task(task_id, function_name)
    return state


def _check_policy_import_chunks() -> list:
    """回归：政策详情 → chunks 的字段与嵌入文本。"""
    problems = []
    detail = {
        "policyKey": "refund",
        "title": "退换货与售后政策",
        "version": "1.0",
        "sections": [
            {"sectionNo": 1, "heading": "3. 退款时效",
             "content": "商家审核通过后 1-3 个工作日原路退回。"},
            {"sectionNo": 2, "heading": "2. 申请流程", "content": "在订单详情页提交申请。"},
        ],
    }
    chunks = build_policy_chunks(detail, "refund")
    if len(chunks) != 2:
        problems.append(f"政策章节数不对：{len(chunks)}")
    first = chunks[0] if chunks else {}
    for field in ("title", "content", "policy_key", "section_no", "embedding_text"):
        if not first.get(field):
            problems.append(f"政策 chunk 缺少字段：{field}")
    if "《退换货与售后政策》3. 退款时效" not in (first.get("embedding_text") or ""):
        problems.append("政策 embedding_text 没有带政策标题/章节名")
    return problems
