"""
检索词改写节点 (node_query_rewrite)

只在 gate 判「不足且可以重试」时运行。改写的是 `retrieval_query`（探针），
**绝不碰 `rewritten_query`**（意图锚）—— 这是防目标漂移的边界：
检索可以试探，作答与重排始终对着用户原意。

三条硬约束（任一不过就放弃本轮重试，回落到第一轮证据）：
1. 必须原样包含已锁定产品的标准名
2. 不得与历史检索词重复（规范化后比较）
3. 长度不得超过原问题的两倍（下限 60 字符，给标准名留空间）
"""
import json
import re
import sys
import unicodedata

from langchain.messages import HumanMessage

from app.agent.sub_agents.product_helper_agent.state import (
    QueryGraphState,
    resolve_retrieval_query,
)
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.retry import invoke_with_retry
from app.lm.lm_utils import get_llm_client
from app.utils.task_utils import add_done_task, add_running_task

# 节点名，与 main_graph.py 中 add_node 注册的名称保持一致，用于日志前缀
NODE_NAME = "node_query_rewrite"

# NFKC + casefold + 去分隔符/标点 —— 与产品名对齐同一套口径
_SEP_RE = re.compile(r"[\s\-_/·.,，。()（）\[\]【】\"'“”‘’?!？！:：;；、]+")


def _normalize(text: str) -> str:
    return _SEP_RE.sub("", unicodedata.normalize("NFKC", text or "").casefold())


def _history_queries(state: QueryGraphState) -> list:
    """本轮之前用过的检索词：轮次历史 + 当前探针 + 意图锚 + 原始问题"""
    out = []
    for h in state.get("retrieval_history") or []:
        if isinstance(h, dict) and h.get("query"):
            out.append(str(h["query"]))
    for q in (state.get("retrieval_query"), state.get("rewritten_query"),
              state.get("original_query")):
        if q:
            out.append(str(q))
    return out


def _rewrite_guard_reason(new_query: str, state: QueryGraphState) -> str:
    """
    改写词的硬约束（纯函数，离线可测）

    :return: 拒绝原因；空串表示通过
    """
    new = (new_query or "").strip()
    if not new:
        return "改写为空"

    original = (state.get("original_query") or "").strip()
    limit = max(2 * len(original), 60)
    if len(new) > limit:
        return f"过长（{len(new)} > {limit}）"

    item_names = [str(n).strip() for n in (state.get("item_names") or []) if str(n).strip()]
    if item_names and not any(_normalize(n) in _normalize(new) for n in item_names):
        return "丢失已锁定产品名"

    seen = {_normalize(q) for q in _history_queries(state) if _normalize(q)}
    if _normalize(new) in seen:
        return "与历史检索词重复"

    return ""


def _llm_rewrite(state: QueryGraphState) -> str:
    """调模型改写检索词；失败返回空串（调用方据此放弃重试）"""
    try:
        titles = "\n".join(
            f"- {(d.get('title') or '').strip()}"
            for d in (state.get("reranked_docs") or [])
            if isinstance(d, dict) and (d.get("title") or "").strip()
        )[:600] or "（无）"
        judge = state.get("retrieval_judge") or {}
        prompt = load_prompt(
            "retrieval_rewrite",
            original_query=state.get("original_query") or "",
            item_names="、".join(state.get("item_names") or []) or "（无）",
            retrieval_query=resolve_retrieval_query(state),
            missing=judge.get("missing") or "（无）",
            rewrite_hint=judge.get("rewrite_hint") or "（无）",
            titles=titles,
        )
        client = get_llm_client(json_mode=True)
        response = invoke_with_retry(
            client, [HumanMessage(content=prompt)], "改写检索词")
        content = (response.content or "").strip()
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()
        result = json.loads(content)
        if not isinstance(result, dict):
            return ""
        return str(result.get("query") or "").strip()
    except Exception as e:
        logger.warning(
            f"[{NODE_NAME}] 改写失败，放弃本轮重试：{type(e).__name__}: {e}")
        return ""


def node_query_rewrite(state: QueryGraphState) -> QueryGraphState:
    """
    节点: 改写检索词 (node_query_rewrite)

    :param state: 需包含 session_id / original_query / item_names / retrieval_judge
    :return: 成功 → 新 `retrieval_query` + `rewrite_ok=True`；
             失败 → `rewrite_ok=False` 并回落到第一轮证据
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{NODE_NAME}] [{function_name}] 开始处理")
    add_running_task(state["session_id"], function_name, state.get("is_stream"))

    judge = dict(state.get("retrieval_judge") or {})
    try:
        new_query = _llm_rewrite(state)
        reason = _rewrite_guard_reason(new_query, state)
        if reason:
            logger.warning(
                f"[{NODE_NAME}] [{function_name}] 改写被守卫拒绝（{reason}），"
                f"放弃重试、回落到第一轮证据：{new_query[:80]!r}")
            judge["rewrite_ok"] = False
            return {
                "retrieval_judge": judge,
                "reranked_docs": list(state.get("anchor_docs") or []),
                "web_only": bool(state.get("anchor_web_only")),
            }

        judge["rewrite_ok"] = True
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 改写通过："
            f"{resolve_retrieval_query(state)!r} → {new_query!r}")
        return {"retrieval_query": new_query, "retrieval_judge": judge}

    finally:
        add_done_task(state["session_id"], function_name, state.get("is_stream"))
        logger.info(f"[{NODE_NAME}] [{function_name}] 处理结束")


def _check_rewrite_guard() -> list:
    """
    离线自测：改写词的三条硬约束（纯逻辑，不调模型）

    守四件事：通过；丢产品名；与历史重复（含大小写/分隔符变体）；超长/为空。
    守卫漏掉任何一条，「一次糟糕的改写」就会把答案带偏或让循环空转。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    state = {
        "original_query": "烫金机怎么装膜？",
        "rewritten_query": "Brother HAK 180 烫金机怎么装膜？",
        "retrieval_query": "",
        "item_names": ["Brother HAK 180 烫金机"],
        "retrieval_history": [{"round": 1, "query": "Brother HAK 180 烫金机怎么装膜？"}],
    }

    ok = "Brother HAK 180 烫金机 烫金膜盒 安装步骤"
    reason = _rewrite_guard_reason(ok, state)
    if reason:
        problems.append(f"合法改写被误拒：{reason}")

    if not _rewrite_guard_reason("烫金膜盒安装步骤", state):
        problems.append("丢失产品名的改写没被拒绝")
    if not _rewrite_guard_reason("Brother HAK 180 烫金机怎么装膜？", state):
        problems.append("与历史完全重复的改写没被拒绝")
    if not _rewrite_guard_reason("brother-hak180 烫金机 怎么装膜", state):
        problems.append("大小写/分隔符变体的重复改写没被拒绝")
    if not _rewrite_guard_reason("Brother HAK 180 烫金机" + "x" * 100, state):
        problems.append("超长改写没被拒绝")
    if not _rewrite_guard_reason("   ", state):
        problems.append("空改写没被拒绝")

    return problems
