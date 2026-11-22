"""
主 agent（deepagents）—— 买小购（一次买够平台客服智能体）的唯一「嘴」

进化形态见 evolution-plan §1：主 agent 用 deepagents 编排，RAG 检索图（本仓库的
查询链路）降为它的一个 subagent（`product-kb`）；将来的业务 subagent（查订单等）持续追加。

M2/M3 的范畴：本地 CLI 闭环 —— 问题 → 主 agent 派发 `task` → 各 subagent 干活
→ 主 agent 转述；并确认主 agent 自己的模型调用入账。

两个 subagent 各代表一种形态：
- `product-kb`：**CompiledSubAgent** —— 收一张现成编译好的图（检索链路）
- `order-service`：**字典式声明**（tools + system_prompt，框架自动编译）—— 普通「工具型」子 agent；
  M7 起工具回调 Java 的 `/internal/agent/orders/query` 查真实订单（票据经 metadata 下发）

运行（命令行闭环）：
    .venv/Scripts/python.exe -m app.agent.runtime.main_agent "烫金机盒怎么装膜?"
    .venv/Scripts/python.exe -m app.agent.runtime.main_agent "我买的烫金机不会用，订单 12345"
"""
import hashlib
import json
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from contextvars import ContextVar

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from deepagents import create_deep_agent

from app.clients.redis_clients import (
    REDIS_DOWN_ERRORS,
    TaskStoreUnavailable,
    get_redis,
    note_store_failure,
)
from app.agent.runtime.forms import (
    current_forms,
    dispatch_form_result,
    merge_forms,
    request_address_edit_form,
    request_refund_reason_form,
    reset_forms,
    reset_forms_token,
)
from app.conf.minio_config import normalize_image_url
from app.agent.subagents.order_service.application.graphs.shipment import run_urge_shipment
from app.agent.subagents.order_service.application.graphs.refund import run_urge_refund
from app.agent.subagents.order_service.application.graphs.order_confirmed import (
    run_order_confirmed,
)
from app.agent.subagents.order_service.agent import ORDER_SUBAGENT
from app.agent.subagents.policy_agent.agent import POLICY_SUBAGENT
from app.agent.subagents.policy_agent.api.tools import search_platform_policy
from app.agent.tools.report_tools import get_consumption_report
from app.agent.tools.tone_faq import search_tone_faq
from app.agent.subagents.product_guide.agent import PRODUCT_GUIDE_SUBAGENT
from app.agent.subagents.product_guide.infrastructure.card_sink import (
    reset_product_cards,
    set_product_cards,
)
from app.agent.runtime.grounding import ground_reply
from app.agent.runtime.answer_reconcile import (
    _merge_products,
    _merge_sub_images,
    _reconcile_reply_images,
    _reconcile_reply_products,
)
from app.agent.runtime.stream_consumer import (
    PausedByUser as _PausedByUser,
    consume_stream as _consume_stream,
    last_reply as _last_reply,
)
from app.agent.runtime.trace_labels import (
    TRACE_MAIN_AGENT as _TRACE_MAIN_AGENT,
    TRACE_SUBAGENT_ACTIONS as _TRACE_SUBAGENT_ACTIONS,
    TRACE_SUBAGENT_NAMES as _TRACE_SUBAGENT_NAMES,
    TRACE_TOOL_LABELS as _TRACE_TOOL_LABELS,
    trace_subagent_action as _trace_subagent_action,
    trace_subagent_name as _trace_subagent_name,
    trace_tool_label as _trace_tool_label,
)
from app.agent.runtime.tool_context import reset_tool_context, set_tool_context
from app.core import stage_timing
from app.clients.mongo_history_clients import (
    ensure_session_owner,
    get_recent_messages,
    save_chat_message,
)
from app.clients.mongo_run_clients import save_query_run
from app.conf.budget_config import budget_config
from app.conf.lm_config import lm_config
from app.conf.pipeline_config import pipeline_config
from app.core.input_guard import INPUT_GUARD_ANSWER, check_user_input
from app.core.safety_guard import SENSITIVE_REFUSAL_ANSWER, check_sensitive_topic
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.request_context import bind_context, raw_context, reset_context
from app.core.usage_tracker import usage_context
from app.lm.lm_utils import get_llm_client
from app.agent.subagents.product_kb.agent import build_product_kb_subagent
from app.agent.subagents.product_kb.application.graph import get_subagent_app
from app.agent.subagents.product_kb.application.product_resolver import resolve_product
from app.agent.subagents.product_kb.domain.outcome import (
    clarify_fallback_text,
    judge_outcome,
)
from app.agent.subagents.product_kb.domain.state import from_messages
from app.utils.task_utils import (
    clear_active_run,
    clear_task,
    is_stop_requested,
    set_active_run,
)

NODE_NAME = "main_agent"


def _clean_text(value) -> str:
    """把任意值转成可安全写入 JSON/Mongo 的字符串（去掉孤立代理字符）"""
    if value is None:
        return ""
    return str(value).encode("utf-8", "replace").decode("utf-8")


def _json_native(value):
    """把 numpy 标量 / 元组等转成 JSON-native，避免 save_query_run 静默失败"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_native(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_native(item())
        except Exception:
            pass
    return _clean_text(value)


def _doc_evidence(doc: dict, include_text: bool) -> dict:
    """从 reranked_docs 的一条记录提取评测证据（local 与 web 都保留）"""
    source = _clean_text(doc.get("source") or "local")
    chunk_id = doc.get("chunk_id")
    if chunk_id is not None:
        try:
            chunk_id = int(chunk_id)
        except (TypeError, ValueError):
            chunk_id = None
    text = _clean_text(doc.get("text") or doc.get("content") or doc.get("snippet") or "")
    if include_text:
        text = text[:pipeline_config.max_context_chars_per_doc]
    else:
        text = ""
    return {
        "source": source,
        "chunk_id": chunk_id,
        "title": _clean_text(doc.get("title") or doc.get("file_title") or ""),
        "score": _json_native(doc.get("score")),
        "url": _clean_text(doc.get("url") or ""),
        "text": text,
    }


def _aggregate_sub_evidence(sub_outcomes: list) -> tuple:
    """
    合并多次派发的证据：chunk_id 去重保序，累计 local/web/total。

    返回 `(topk_chunk_ids, topk_local, topk_web, topk_total)`；chunk_id 用字符串存
    （P0 的既有约定，便于和 gold 做集合比对）。
    """
    ids = []
    seen = set()
    local = web = total = 0
    for item in sub_outcomes or []:
        if not isinstance(item, dict):
            continue
        for cid in item.get("topk_chunk_ids") or []:
            key = str(cid)
            if key not in seen:
                seen.add(key)
                ids.append(key)
        local += int(item.get("topk_local") or 0)
        web += int(item.get("topk_web") or 0)
        total += int(item.get("topk_total") or 0)
    if not total:
        total = local + web
    return ids, local, web, total


def _strip_sub_outcome_evidence(sub_outcomes: list) -> list:
    """运行记录里不存证据正文（评测通过返回值拿），避免把切片正文写进 query_runs"""
    return [{k: v for k, v in item.items() if k != "evidence"}
            for item in (sub_outcomes or []) if isinstance(item, dict)]


def _rag_task(payload: dict) -> dict:
    """
    RAG 检索图作为 subagent 的适配层（deepagents 的 `task` 工具调它）

    输入契约（对照 deepagents 0.7.23 `middleware/subagents.py` 实测）：
      `{"messages": [HumanMessage(任务描述)], ...父 state 里未被排除的其余键}`
      —— task 工具固定塞一条 HumanMessage(description)，所以从 messages 里取
      最后一条 user/human 消息即任务描述。
    输出契约：**必须返回含 `messages` 的 dict**；主 agent 取其中**最后一条有文本的
      `AIMessage`** 作为工具结果（`_return_command_with_state_update`）。

    另外三条刻意的约定：
    - `is_stream=False`：子 agent 没有 SSE 队列，流式分支只会推给空气
    - 用 `get_subagent_app()`（**不带 checkpointer** 的编译版）：task 调用会带上
      主 agent 的 config（含 thread_id），共用 saver 会把两张图的检查点写进同一个 thread
    - 复用父累加器：账目与日志归因到这次子调用；子图记账同时并入整轮账本
      （没有父累加器时退回独立 `usage_context`）；session 是临时的，
      跑完 `clear_task` 清掉进度记录
    """
    state_in = from_messages(payload.get("messages") or [],
                             session_id=f"rag_sub_{uuid.uuid4().hex[:8]}",
                             is_stream=False,
                             enable_web_search=_ENABLE_WEB_SEARCH.get(),
                             user_question=_USER_QUESTION.get(),
                             history_product=_HISTORY_PRODUCT.get() or {},
                             prior_answer=_PRIOR_ANSWER.get())
    sid, question = state_in["session_id"], state_in["original_query"]
    logger.info(f"[{NODE_NAME}] [rag_task] 收到任务：{question[:60]!r}")

    stage_timing.start(sid)
    try:
        parent_acc = raw_context().get("acc")
        if parent_acc is not None:
            # 复用父累加器：record() 仍写进整轮账本、SSE usage 也带 RAG；
            # 用快照差值给 sub_outcomes 留一份「本次子调用单独花了多少」。
            usage_mark = parent_acc.mark()
            token = bind_context(node="rag_subagent")
            try:
                final = get_subagent_app().invoke(state_in)
                stage_ms = stage_timing.snapshot(sid)
            finally:
                reset_context(token)
            sub_usage = parent_acc.since(usage_mark)
        else:
            with usage_context(session_id=sid, node="rag_subagent") as child_acc:
                final = get_subagent_app().invoke(state_in)
                stage_ms = stage_timing.snapshot(sid)
                sub_usage = child_acc.summary()
        if stage_ms:
            logger.info(f"[{NODE_NAME}] [stage_trace] {stage_ms}")
        # 子图结局收集：透传给主 agent 的运行记录（「知识盲区」等信号的原料，M4-T3）。
        # 检索循环字段（2026-10-20）用于量「重试触发率 / 恢复率」，不参与结局判定。
        # reranked_docs 是 retrieval_gate 最终选中的 Top-K（检索指标口径）；生成侧
        # 可能另补同章节证据，放在 evidence_docs，只用于 judge 证据采集。
        collector = _SUB_OUTCOMES.get()
        if collector is not None:
            judge_meta = final.get("retrieval_judge") or {}
            rounds = int(final.get("retrieval_round") or 1)
            docs = [d for d in (final.get("reranked_docs") or []) if isinstance(d, dict)]
            # 生成侧可能补了同章节证据（answer 节点），它只用于 judge 证据采集，
            # 不能混进 docs 的 TopK/Recall 口径。
            evidence_docs = [
                d for d in (final.get("evidence_docs") or docs) if isinstance(d, dict)
            ]
            local_ids = []
            topk_local = topk_web = 0
            for doc in docs:
                if doc.get("source") == "web":
                    topk_web += 1
                else:
                    topk_local += 1
                    cid = doc.get("chunk_id")
                    if cid is not None:
                        try:
                            local_ids.append(int(cid))
                        except (TypeError, ValueError):
                            pass
            ids = list(dict.fromkeys(str(cid) for cid in local_ids))
            capture = bool(_CAPTURE_EVIDENCE.get())
            evidence = []
            if capture:
                seen_evidence = set()
                for doc in evidence_docs[:20]:
                    item = _doc_evidence(doc, include_text=True)
                    key = (item["source"], item["chunk_id"] or item["url"])
                    if key in seen_evidence:
                        continue
                    seen_evidence.add(key)
                    evidence.append(item)
            collector.append({
                "outcome": judge_outcome(final),
                "web_only": bool(final.get("web_only")),
                "rag_query": _clean_text(question),
                "answer_mode": final.get("answer_mode") or "",
                "rag_session_id": sid,
                "topk_total": len(docs),
                "topk_local": topk_local,
                "topk_web": topk_web,
                "topk_chunk_ids": ids,
                "evidence": evidence,
                "retrieval_rounds": rounds,
                "retrieval_verdict": judge_meta.get("verdict") or "",
                "retry_recovered": bool(
                    rounds > 1 and judge_meta.get("verdict") == "sufficient"),
                "retrieval_queries": [
                    h.get("query") for h in (final.get("retrieval_history") or [])
                    if isinstance(h, dict) and h.get("query")
                ],
                "usage": _json_native(sub_usage),
                "stage_timings": stage_ms,
            })
        # 结构化配图（含图注）交给收集器，随 final 事件与存档下发（M5）。
        # 不再以「[配图 N 张：url…]」文本附在答案尾部 —— 那种写法会逼主 agent
        # 把裸 URL 抄进回复，图注也在路上丢了；白名单过滤在上游 RAG 侧已完成
        bucket = _SUB_IMAGES.get()
        if bucket is not None:
            bucket.extend(final.get("images") or [])
        text = (final.get("answer") or "").strip()
        if not text and final.get("need_confirm"):
            # 没锁定产品：把候选交给主 agent，由它用对话向用户澄清（无状态化后 RAG 不再自己问）
            text = clarify_fallback_text(final.get("clarify") or {})
        if not text:
            text = "检索没有返回可用内容，请换一种问法或确认产品型号。"
        logger.info(
            f"[{NODE_NAME}] [rag_task] 完成，返回 {len(text)} 字符；子调用记账："
            f"{sub_usage.get('calls', 0)} 次，tokens "
            f"{sub_usage.get('prompt_tokens', 0)}+{sub_usage.get('completion_tokens', 0)}，"
            f"成本 {sub_usage.get('cost', 0):.6f} 元"
        )
        return {"messages": [AIMessage(content=text)]}
    finally:
        stage_timing.clear(sid)
        clear_task(sid)


RAG_SUBAGENT = build_product_kb_subagent(_rag_task)


def main_agent_system_prompt() -> str:
    """主 agent 系统提示 = 角色/工具规则（main_agent.prompt）+ 话术规范（单一来源）

    话术规范（prompts/tone_guide.prompt）同时注入检索图的 answer_out，两处共用一份，
    避免各写一套风格。入口抽成函数，回归用例可直接断言注入生效。
    """
    return load_prompt("main_agent").rstrip() + "\n\n" + load_prompt("tone_guide").strip() + "\n"


# 主 agent 直接挂的工具：表单 + 个人消费报告（后者不需要订单域上下文，直接回调 Java 聚合接口）
MAIN_AGENT_TOOLS = [request_refund_reason_form, request_address_edit_form,
                    get_consumption_report, search_tone_faq]


def build_main_agent():
    """组装主 agent。model 用带记账 callback 的 DeepSeek 官方 API 客户端 —— 主 agent 调用也入账

    走独立端点（不是百炼）：派发可靠性是这层的命门，qwen-plus 在同一提示词下
    会随机跳过检索（HANDOFF §3.44）；改用 deepseek-v4.1-flash 直连官方 API。
    """
    if not lm_config.main_agent_api_key:
        raise RuntimeError(
            "主 agent 配置缺失：请在 .env 里配置 DEEPSEEK_API_KEY（DeepSeek 官方 API 密钥）——"
            "主 agent 走官方直连，与百炼的 OPENAI_API_KEY 是两把钥匙")
    return create_deep_agent(
        model=get_llm_client(lm_config.main_agent_model,
                             base_url=lm_config.main_agent_base_url,
                             api_key=lm_config.main_agent_api_key,
                             retry_transient=True),   # 图内调用没有节点级 retry，见 lm_utils
        system_prompt=main_agent_system_prompt(),
        # 会话内表单（2026-11-07）：主 agent 自己下发表单，不经过子 agent
        tools=MAIN_AGENT_TOOLS,
        subagents=[RAG_SUBAGENT, PRODUCT_GUIDE_SUBAGENT, POLICY_SUBAGENT, ORDER_SUBAGENT],
    )


# ──────────────────────── 统一执行函数（HTTP / gRPC / CLI 共用）────────────────────────
# 一轮询问的骨架：会话读写、输入护栏、整轮归因与预算、事件流、运行记录 ——
# 平台 infra 的挂点都在这一层（见 evolution-plan §7「平台上移」对照）。

# 子图结局收集器（ContextVar）：run_main_agent 开一轮前置空 list，_rag_task 跑完子图后
# 往里 append —— 运行记录据此透传「知识盲区」等信号（M4-T3）。用可变 list：
# LangGraph copy_context() 到子线程后引用的是同一个对象，append 依然可见
_SUB_OUTCOMES: ContextVar = ContextVar("sub_outcomes", default=None)

# 结构化配图收集器（ContextVar）：_rag_task 把子图回传的配图（含图注）放进来，
# run_main_agent 收尾时合并去重、随 final 事件与会话存档下发（M5）。与 _SUB_OUTCOMES 同款
_SUB_IMAGES: ContextVar = ContextVar("sub_images", default=None)

# 联网开关（ContextVar）：/query 的 `enable_web_search` 随本轮透传给 RAG 子图。
# **必须走 ContextVar**：子图在 task 工具里被调起，参数不在调用栈上
# （M4-T3 迁移时这个开关曾静默断线 —— 前端在传、接口在收、没人读，M5 修复）
_ENABLE_WEB_SEARCH: ContextVar = ContextVar("enable_web_search", default=True)

# 用户原话（ContextVar）：子图收到的任务文本是主 agent 生成的，措辞每轮可能不同；
# 检索缓存要按用户实际问法命中，所以单独透传一份原话（见 node_rerank 的缓存键）
_USER_QUESTION: ContextVar = ContextVar("user_question", default="")

# 上一轮的真实产品主体（ContextVar）：主 agent 从 chat_message 历史里用
# product_resolver 解析，**不采信 task 描述里的自然语言推断**。子图用它补续问主体，
# 纯中文产品名也能覆盖（task 描述的型号正则覆盖不到）。
_HISTORY_PRODUCT: ContextVar = ContextVar("history_product", default=None)

# 同一问题的首次回答（ContextVar）：重复提问时透传给子图做一致性参考。
# **只写一次、永远指向第一次回答**，不随轮次滚动 —— 滚动传上一次的答案会形成
# 「传话游戏」，多轮后越漂越远。锚点只用来核对口径，证据变了仍以新证据为准。
_PRIOR_ANSWER: ContextVar = ContextVar("prior_answer", default="")

# 答案锚点（Redis）：key = 会话 + 规范化后的原问题，SETNX 写入，TTL 24 小时。
# Redis 不可用时静默降级 —— 少一个一致性提示，不影响作答。
ANSWER_ANCHOR_PREFIX = "rag:answer-anchor:v1"
ANSWER_ANCHOR_TTL = 24 * 3600
ANSWER_ANCHOR_MAX_CHARS = 1200


def _normalize_question(text: str) -> str:
    """只抹掉空白差异；不做同义改写 —— 锚点按「完全相同的问题」命中，宁可少命中"""
    return re.sub(r"\s+", "", text or "")


def _answer_anchor_key(session_id: str, message: str) -> str:
    digest = hashlib.sha1(_normalize_question(message).encode("utf-8")).hexdigest()
    return f"{ANSWER_ANCHOR_PREFIX}:{session_id}:{digest}"


def _load_prior_answer(session_id: str, message: str) -> str:
    try:
        raw = get_redis().get(_answer_anchor_key(session_id, message))
    except TaskStoreUnavailable:
        return ""
    except REDIS_DOWN_ERRORS as e:
        note_store_failure(e)
        return ""
    return (raw or "").strip()[:ANSWER_ANCHOR_MAX_CHARS]


def _save_prior_answer(session_id: str, message: str, answer: str) -> None:
    answer = (answer or "").strip()
    if not answer:
        return
    try:
        get_redis().set(
            _answer_anchor_key(session_id, message),
            answer[:ANSWER_ANCHOR_MAX_CHARS],
            nx=True,
            ex=ANSWER_ANCHOR_TTL,
        )
    except TaskStoreUnavailable:
        pass
    except REDIS_DOWN_ERRORS as e:
        note_store_failure(e)
    except Exception as e:
        logger.warning(f"[{NODE_NAME}] 答案锚点写入失败：{e}")

# 评测证据采集（ContextVar）：只有 eval_judge 回放时打开；生产默认关闭，
# 避免每轮把切片正文放进返回值/运行记录。topk_* 无论开关都会聚合。
_CAPTURE_EVIDENCE: ContextVar = ContextVar("capture_evidence", default=False)

_agent_cache = None


def _get_agent():
    """主 agent 惰性构建 + 进程内复用（编译不便宜，服务场景别每轮重建）"""
    global _agent_cache
    if _agent_cache is None:
        _agent_cache = build_main_agent()
    return _agent_cache


def _clip(text, n: int = 100) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def _safe_save(session_id: str, role: str, text: str, images: list = None,
               products: list = None, trace: list = None, forms: list = None,
               form_result_for: str = "",
               user_id: str = None, tenant_id: str = None) -> None:
    """写会话历史 —— 写不进去是历史的事，绝不能连带弄挂这一轮（与记账同一条规矩）"""
    try:
        save_chat_message(session_id, role, text, images=images, products=products,
                          trace=trace, forms=forms, form_result_for=form_result_for,
                          user_id=user_id, tenant_id=tenant_id)
    except Exception as e:
        logger.warning(f"[{NODE_NAME}] 会话历史写入失败（不影响本轮）：{e}")


def _parse_promotion_payload(payload: str) -> dict:
    """解析 Java 发来的智能推广内部事件 payload（纯函数，便于回归）。

    :return: {"text": 推广文案, "cards": [商品卡片 dict, ...]}；非法时 {"error": 原因}
    """
    try:
        data = json.loads(payload or "{}")
    except Exception:
        return {"error": "推广事件 payload 不是合法 JSON"}
    if not isinstance(data, dict):
        return {"error": "推广事件 payload 格式不对"}
    text = str(data.get("text") or "").strip()
    if not text:
        return {"error": "推广事件缺少文案"}
    raw_products = data.get("products")
    if raw_products is None:
        # 兼容旧版单商品 payload
        single = data.get("product")
        raw_products = [single] if isinstance(single, dict) else []
    if not isinstance(raw_products, list) or not raw_products:
        return {"error": "推广事件缺少商品"}
    cards = []
    for product in raw_products:
        if not isinstance(product, dict) or not str(product.get("productId") or "").strip():
            return {"error": "推广事件商品数据不完整"}
        cards.append({
            "productId": str(product.get("productId") or ""),
            "name": str(product.get("name") or ""),
            "image": str(product.get("image") or ""),
            "price": str(product.get("price") or ""),
            "stock": str(product.get("stock") or ""),
            "sales": str(product.get("sales") or ""),
            "rating": str(product.get("rating") or ""),
            "merchantName": str(product.get("merchantName") or ""),
            "specText": str(product.get("specText") or ""),
            "categoryName": str(product.get("categoryName") or ""),
        })
    return {"text": text, "cards": cards}


def _run_internal_event(session_id: str, event: str, payload: str,
                        user_id: str, tenant_id: str, run_id: str, emit) -> dict:
    """Java 发来的内部事件：不进模型、不存用户消息，只执行对应的动作图。

    目前有「催发货确认」「催退款确认」「下单确认」：
    - 催发货 / 催退款：agent 图先调用 Java 的发送工具（Java 校验并落库），
      再生成「完成 + 代商家致歉」回复，由 graph 写入 assistant 历史；
    - 下单确认：订单已由 Java 创建，agent 只把订单结果写成 assistant 历史。

    调用方（run_main_agent）已把 tool ticket 放进 ContextVar，工具回调会带回 Java。
    """
    # 智能推广：不跑模型，直接把一条 assistant 消息（带商品卡）写进客服会话历史，
    # 前端 AgentChatPanel 打开历史即可看到；**不进顾客—店铺聊天**
    if event == "promotion-push":
        parsed = _parse_promotion_payload(payload)
        if parsed.get("error"):
            emit({"type": "error", "detail": f"智能推广写入失败：{parsed['error']}"})
            return {"run_id": run_id, "reply": "", "outcome": "error",
                    "error": parsed["error"]}
        text, cards = parsed["text"], parsed["cards"]
        _safe_save(session_id, "assistant", text, products=cards,
                   user_id=user_id, tenant_id=tenant_id)
        emit({"type": "final", "text": text, "products": cards})
        logger.info(f"[{NODE_NAME}] 智能推广已写入客服会话：{len(cards)} 件商品")
        return {"run_id": run_id, "reply": text, "outcome": "internal_event",
                "products": cards}

    runners = {
        "urge-shipment-confirmed": (run_urge_shipment, "催发货"),
        "urge-refund-confirmed": (run_urge_refund, "催退款"),
    }
    entry = runners.get(event)
    if entry is None and event != "order-confirmed":
        detail = f"未知内部事件：{event}"
        logger.warning(f"[{NODE_NAME}] {detail}")
        emit({"type": "error", "detail": detail})
        return {"run_id": run_id, "reply": "", "outcome": "error", "error": detail}

    label = "下单确认" if event == "order-confirmed" else entry[1]
    try:
        data = json.loads(payload or "{}")
        if event == "order-confirmed":
            reply = run_order_confirmed(
                session_id=session_id,
                user_id=str(user_id or ""),
                tenant_id=str(tenant_id or ""),
                orders=data.get("orders") or [],
            )
        else:
            runner, _ = entry
            reply = runner(
                session_id=session_id,
                user_id=str(user_id or ""),
                tenant_id=str(tenant_id or ""),
                action_no=str(data.get("actionNo") or ""),
            )
        emit({"type": "final", "text": reply})
        return {"run_id": run_id, "reply": reply, "outcome": "internal_event"}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        logger.error(f"[{NODE_NAME}] {label}执行失败：{detail}", exc_info=True)
        emit({"type": "error", "detail": f"{label}执行失败"})
        return {"run_id": run_id, "reply": "", "outcome": "error", "error": detail}


@dataclass
class _TurnContext:
    """一轮对话的不可变输入 + 事件出口（拆掉 run_main_agent 的 11 个参数）。"""

    session_id: str
    message: str
    run_id: str
    emit: object
    tenant_id: str = None
    user_id: str = None
    on_usage: object = None
    enable_web_search: bool = True
    capture_evidence: bool = False
    tool_ticket: str = ""
    stream_output: bool = False
    trace_events: list = field(default_factory=list)


def _run_guard_branch(ctx: _TurnContext, guard_reason: str) -> dict:
    """输入护栏命中：拒答、不调模型，仍写用户消息与固定回答。"""
    logger.warning(f"[{NODE_NAME}] 输入护栏命中（{guard_reason}），拒答、不调模型")
    _safe_save(ctx.session_id, "user", ctx.message, user_id=ctx.user_id,
               tenant_id=ctx.tenant_id)
    _safe_save(ctx.session_id, "assistant", INPUT_GUARD_ANSWER,
               user_id=ctx.user_id, tenant_id=ctx.tenant_id)
    ctx.emit({"type": "final", "text": INPUT_GUARD_ANSWER})
    save_query_run({
        "trace_id": ctx.run_id, "segment": "start", "session_id": ctx.session_id,
        "tenant_id": ctx.tenant_id or "", "question": ctx.message, "outcome": "blocked",
        "answer_chars": len(INPUT_GUARD_ANSWER), "sub_outcomes": [],
        "done_list": [], "degraded_list": [], "ts": time.time(),
    })
    return {"run_id": ctx.run_id, "reply": INPUT_GUARD_ANSWER,
            "outcome": "blocked", "usage_text": "", "usage": {},
            "sub_outcomes": []}


def _run_safety_branch(ctx: _TurnContext, reason: str) -> dict:
    """敏感主题命中：固定拒答、不派发 RAG 子图，结局记 rejected。

    与输入护栏的 ``blocked`` 区分：这里不是注入攻击，而是业务上不回答的政策主题。
    """
    logger.warning(f"[{NODE_NAME}] 敏感主题命中（{reason}），拒答且不派发 RAG 子图")
    _safe_save(ctx.session_id, "user", ctx.message, user_id=ctx.user_id,
               tenant_id=ctx.tenant_id)
    _safe_save(ctx.session_id, "assistant", SENSITIVE_REFUSAL_ANSWER,
               user_id=ctx.user_id, tenant_id=ctx.tenant_id)
    ctx.emit({"type": "final", "text": SENSITIVE_REFUSAL_ANSWER})
    save_query_run({
        "trace_id": ctx.run_id, "segment": "start", "session_id": ctx.session_id,
        "tenant_id": ctx.tenant_id or "", "question": ctx.message, "outcome": "rejected",
        "answer_chars": len(SENSITIVE_REFUSAL_ANSWER), "sub_outcomes": [],
        "done_list": [], "degraded_list": [], "ts": time.time(),
    })
    return {"run_id": ctx.run_id, "reply": SENSITIVE_REFUSAL_ANSWER,
            "outcome": "rejected", "usage_text": "", "usage": {},
            "sub_outcomes": []}


def _run_form_branch(ctx: _TurnContext, form_result: dict) -> dict:
    """表单回传：按 form_type 直接调工具，不跑主 agent、不烧模型。"""
    fid = str((form_result or {}).get("form_id") or "").strip()
    values = (form_result or {}).get("values") or {}
    history_raw = get_recent_messages(ctx.session_id, limit=50, user_id=ctx.user_id)
    # 表单回传也要走 Java 工具回调（prepare_refund / 改地址），票据必须先进 ContextVar
    tool_token = set_tool_context(ctx.tool_ticket, ctx.user_id)
    try:
        dispatch = dispatch_form_result(fid, values, history_raw)
    finally:
        reset_tool_context(tool_token)
    reply = dispatch.get("answer") or "表单处理失败，请重新发起一次。"
    kind = dispatch.get("kind") or "invalid"
    consumed = fid if kind == "dispatched" else ""
    user_text = (ctx.message or "").strip() or "（提交表单）"
    ctx.emit({"type": "step", "kind": "form", "agent": _TRACE_MAIN_AGENT,
              "detail": "整理表单信息"})
    _safe_save(ctx.session_id, "user", user_text, user_id=ctx.user_id,
               tenant_id=ctx.tenant_id)
    _safe_save(ctx.session_id, "assistant", reply,
               trace=[{"kind": "form", "agent": _TRACE_MAIN_AGENT,
                       "detail": "整理表单信息"}],
               form_result_for=consumed,
               user_id=ctx.user_id, tenant_id=ctx.tenant_id)
    ctx.emit({"type": "final", "text": reply, "images": [], "products": [], "forms": []})
    save_query_run({
        "trace_id": ctx.run_id, "segment": "start", "session_id": ctx.session_id,
        "tenant_id": ctx.tenant_id or "", "question": user_text,
        "outcome": "answered" if kind == "dispatched" else "blocked",
        "answer_chars": len(reply), "form_result": {"form_id": fid, "kind": kind},
        "sub_outcomes": [], "done_list": [],
        "degraded_list": [] if kind == "dispatched" else [f"form:{kind}"],
        "ts": time.time(),
    })
    return {"run_id": ctx.run_id, "reply": reply, "outcome": "answered",
            "error": "", "usage_text": "", "usage": {}, "images": [],
            "products": [], "web_only": False, "forms": [], "sub_outcomes": []}


def _build_messages(session_id: str, user_id: str, message: str) -> list:
    """读历史组装上下文（读失败自然退化成本轮独立）。"""
    messages = []
    for m in get_recent_messages(session_id, user_id=user_id):
        text = (m.get("text") or "").strip()
        if not text:
            continue
        messages.append(HumanMessage(content=text) if m.get("role") == "user"
                        else AIMessage(content=text))
    messages.append(HumanMessage(content=message))
    return messages


def _find_history_product(messages: list) -> dict:
    """
    从真实会话历史里找最近一个能确认的产品主体（不读 task 描述）。

    只看用户消息、从最近一条往前找；最后一条是当前问题，跳过。用 product_resolver
    而不是型号正则，纯中文产品名（如「万用表RS-12」）也能命中。
    """
    user_texts = []
    for m in messages or []:
        if isinstance(m, HumanMessage):
            text = str(m.content or "").strip()
            if text:
                user_texts.append(text)
    for text in reversed(user_texts[:-1]):
        try:
            resolved = resolve_product(text)
        except Exception as e:
            logger.warning(f"[{NODE_NAME}] 解析历史产品失败（跳过）：{type(e).__name__}: {e}")
            continue
        if resolved.get("status") == "confirmed" and resolved.get("item_names"):
            return {
                "item_name": resolved["item_names"][0],
                "target_model": resolved.get("target_model") or "",
                "manual_id": resolved.get("manual_id") or "",
                "source_question": text[:200],
            }
    return {}


def _execute_turn(ctx: _TurnContext, messages: list, t0: float) -> dict:
    """跑一轮主 agent：暂停登记、子图收集器、整轮归因/预算、流消费。"""
    reply, error_msg, outcome = "", "", "answered"
    fact_ledger = [{"tool": "user", "content": ctx.message or ""}]
    grounding_meta: dict = {}
    sub_outcomes: list = []
    sub_images: list = []
    sub_products: list = []
    collected_forms: list = []   # 本轮下发的表单：必须在 finally 复位收集器**之前**取走
    tool_token = set_tool_context(ctx.tool_ticket, ctx.user_id)
    product_token = set_product_cards(sub_products)
    token = _SUB_OUTCOMES.set(sub_outcomes)
    img_token = _SUB_IMAGES.set(sub_images)
    ws_token = _ENABLE_WEB_SEARCH.set(bool(ctx.enable_web_search))
    uq_token = _USER_QUESTION.set(ctx.message or "")
    # 上一轮产品主体：从真实会话历史解析，供续问补主体（纯中文名也覆盖）
    hp_token = _HISTORY_PRODUCT.set(_find_history_product(messages))
    # 重复提问：取同一会话内该问题的**首次**回答做一致性参考（没有则为空）
    pa_token = _PRIOR_ANSWER.set(_load_prior_answer(ctx.session_id, ctx.message or ""))
    cap_token = _CAPTURE_EVIDENCE.set(bool(ctx.capture_evidence))
    # 会话内表单收集器：本轮主 agent 可能下发一张表单（最多 1 张）
    form_token = reset_forms()
    set_active_run(ctx.session_id, ctx.run_id)
    try:
        with usage_context(trace_id=ctx.run_id, session_id=ctx.session_id,
                           tenant_id=ctx.tenant_id, node=NODE_NAME,
                           on_usage=ctx.on_usage, user_id=ctx.user_id,
                           wall_clock_budget=budget_config.query_wall_clock_budget,
                           token_budget=budget_config.query_token_budget,
                           # 整轮起点：子图复用父 acc，但墙钟预算仍以整轮 t0 为基准
                           turn_started_at=t0) as acc:
            try:
                final_messages = _consume_stream(
                    _get_agent(), messages, ctx.emit,
                    deadline=t0 + budget_config.query_wall_clock_budget,
                    stop_check=lambda: is_stop_requested(ctx.session_id),
                    stream_output=ctx.stream_output,
                    fact_ledger=fact_ledger)
                reply = _last_reply(final_messages, since=len(messages))
                if not reply:
                    outcome, error_msg = "error", "主 agent 没有产出回复"
                # 终稿接地校验：只在这一轮确实有用户可见回复时跑；校验/修复都在
                # 本轮 usage_context 内，调用会入账并受整轮预算约束。
                if reply and outcome == "answered":
                    try:
                        reply, grounding_meta = ground_reply(
                            reply, fact_ledger, ctx.message)
                    except Exception as e:
                        # 校验器自身异常绝不能把能答的题拖成错误（fail-open）
                        logger.warning(
                            f"[{NODE_NAME}] 终稿接地校验异常（保留原文）："
                            f"{type(e).__name__}: {e}")
                        grounding_meta = {"error": f"{type(e).__name__}: {e}"}
                # 表单收集器在这里取走（下面 finally 会复位 ContextVar，复位后就读不到了）
                collected_forms = merge_forms(current_forms())
            except _PausedByUser:
                outcome = "paused"
                logger.info(f"[{NODE_NAME}] 本轮被用户暂停（run={ctx.run_id}）")
            except Exception as e:
                outcome = "error"
                error_msg = f"{type(e).__name__}: {e}"
                logger.error(f"[{NODE_NAME}] 本轮执行失败：{error_msg}", exc_info=True)
                ctx.emit({"type": "error", "detail": error_msg})
    finally:
        clear_active_run(ctx.session_id, ctx.run_id)
        reset_tool_context(tool_token)
        reset_product_cards(product_token)
        _SUB_OUTCOMES.reset(token)
        _SUB_IMAGES.reset(img_token)
        _ENABLE_WEB_SEARCH.reset(ws_token)
        _USER_QUESTION.reset(uq_token)
        _HISTORY_PRODUCT.reset(hp_token)
        _PRIOR_ANSWER.reset(pa_token)
        _CAPTURE_EVIDENCE.reset(cap_token)
        reset_forms_token(form_token)
    return {"reply": reply, "outcome": outcome, "error_msg": error_msg,
            "forms": collected_forms, "sub_outcomes": sub_outcomes,
            "sub_images": sub_images, "sub_products": sub_products, "acc": acc,
            "grounding": grounding_meta}


def _finalize_turn(ctx: _TurnContext, turn: dict) -> dict:
    """落库 + 收尾事件 + 运行记录（暂停轮不存半截回复 —— 本轮作废）。"""
    reply = turn["reply"]
    outcome = turn["outcome"]
    error_msg = turn["error_msg"]
    sub_outcomes = turn["sub_outcomes"]
    grounding = turn.get("grounding") or {}
    # 图片 URL 统一按当前 MINIO_PUBLIC_ENDPOINT 重写：知识库导入时固化的主机名
    # 可能在部署换 IP 后失效，但对象路径不变（见 minio_config.normalize_image_url）
    images = [
        {**img, "url": normalize_image_url(img.get("url") or "")}
        for img in _merge_sub_images(turn["sub_images"]) if isinstance(img, dict)
    ]
    products = _reconcile_reply_products(reply, _merge_products(turn["sub_products"]))
    forms = turn["forms"]
    web_only = any(bool(o.get("web_only")) for o in sub_outcomes if isinstance(o, dict))
    topk_ids, topk_local, topk_web, topk_total = _aggregate_sub_evidence(sub_outcomes)
    # 转述对账（2026-10-14）：检索侧对齐还不够，主 agent 改写句子时会漏掉部分
    # 「如图 N」—— 按**最终回复**的引用再裁一次图、重编号（见 _reconcile_reply_images）。
    # 多次派发两轮编号空间不同、按编号对不上，跳过（留给将来：让编号带轮次前缀）
    if images:
        if len(sub_outcomes) > 1:
            logger.warning(
                f"[{NODE_NAME}] 本轮派发过 {len(sub_outcomes)} 次 product-kb，"
                f"图卡跳过转述对账（编号可能对不上，见 HANDOFF §3.48）")
        else:
            reply, images = _reconcile_reply_images(reply, images)
    if outcome == "paused":
        ctx.emit({"type": "paused", "detail": "本轮已被暂停"})
    else:
        if reply:
            # 回复连配图、商品卡、表单与**执行流**一起存档：重开历史能还原答案与过程（M5.1）
            _safe_save(ctx.session_id, "assistant", reply, images=images, products=products,
                       forms=forms, trace=ctx.trace_events,
                       user_id=ctx.user_id, tenant_id=ctx.tenant_id)
            # 首次回答写入锚点（SETNX：后续轮次只读不覆盖），
            # 同一问题再问时用它核对口径，避免多轮滚动传答案造成的漂移
            if outcome == "answered":
                _save_prior_answer(ctx.session_id, ctx.message or "", reply)
        ctx.emit({"type": "final", "text": reply, "images": images,
                  "products": products, "forms": forms, "web_only": web_only})
    acc = turn["acc"]
    save_query_run({
        "trace_id": ctx.run_id, "segment": "start", "session_id": ctx.session_id,
        "tenant_id": ctx.tenant_id or "", "question": ctx.message, "rewritten_query": "",
        "item_names": [], "enable_web_search": bool(ctx.enable_web_search), "is_stream": True,
        "outcome": outcome, "error": error_msg,
        "answer_chars": len(reply), "images_count": len(images),
        "products_count": len(products), "web_only": web_only,
        "topk_total": topk_total, "topk_local": topk_local, "topk_web": topk_web,
        "topk_chunk_ids": topk_ids,
        "sub_outcomes": _strip_sub_outcome_evidence(sub_outcomes),
        "grounding": grounding,
        "done_list": [], "degraded_list": [],
        "usage": acc.summary(), "ts": time.time(),
    })
    return {"run_id": ctx.run_id, "reply": reply, "outcome": outcome,
            "error": error_msg, "usage_text": acc.text(), "usage": acc.summary(),
            "images": images, "products": products, "forms": forms,
            "web_only": web_only, "sub_outcomes": sub_outcomes,
            "grounding": grounding}


def _run_conversation(ctx: _TurnContext) -> dict:
    """普通问答路径：读历史 → 落用户消息 → 跑主 agent → 收尾。"""
    messages = _build_messages(ctx.session_id, ctx.user_id, ctx.message)
    # 用户消息先落库（哪怕这轮随后失败，历史里也看得见问了什么）
    _safe_save(ctx.session_id, "user", ctx.message, user_id=ctx.user_id,
               tenant_id=ctx.tenant_id)
    # 跑一轮（暂停登记 + 子图收集器 + 整轮归因/预算）
    t0 = time.time()
    turn = _execute_turn(ctx, messages, t0)
    return _finalize_turn(ctx, turn)


def run_main_agent(session_id: str, message: str, on_event=None,
                   tenant_id: str = None, user_id: str = None,
                   on_usage=None, run_id: str = None,
                   enable_web_search: bool = True,
                   capture_evidence: bool = False,
                   tool_ticket: str = "",
                   internal_event: str = "",
                   form_result: dict = None,
                   stream_output: bool = False) -> dict:
    """
    统一执行函数 —— 一轮询问的骨架（HTTP / gRPC / CLI 共用，见 evolution-plan §4 的 M4）

    职责（平台挂点，对照 §7「平台上移」）：
    - **输入护栏先过**：命中直接拒答，不烧主 agent 的模型调用
    - **整轮归因与预算**：`usage_context(trace_id=run_id, …)` 覆盖主 agent 与全部
      subagent；wall-clock 到点由 `_consume_stream` 中断；`on_usage` 透传累计用量
    - **暂停**：`set_active_run` 登记本轮，`_consume_stream` 每步查停止标记 ——
      与旧检索图同一套 `active/stop` 机制（只认 run_id，见 task_utils）
    - **会话状态 = `chat_message`**：读 `get_recent_messages` 组装上下文、
      写「用户消息 + 最终回复」（回复连**配图**一起存档，历史里图注能还原）
    - **运行记录 `query_runs`**：一轮一条，含子图结局 `sub_outcomes`
      （知识盲区等信号的原料，M4-T3）
    - **事件流**：`on_event({"type": "step" | "final" | "error" | "paused", …})`
      —— HTTP 推 SSE、gRPC 映射 Event、CLI 渲染灰字，三个消费者同一份原料；
      `final` 事件带结构化配图 `images`（M5）与商品卡 `products`（M11）

    具体分支已拆到 `_run_guard_branch` / `_run_form_branch` / `_run_conversation`，
    本函数只做身份校验与路由。

    :param tenant_id: 调用方租户（记账归因；HTTP / gRPC 层从身份取，CLI 不传）
    :param user_id: 平台顾客 ID（gRPC metadata 的 `x-user-id`）—— 归因留位，
      数据隔离阶段启用（evolution-plan §6「身份两维」）
    :param on_usage: 可选回调，每记完一笔账带累计汇总调一次（对应 SSE 的 usage 事件）
    :param run_id: 可选注入（不传则生成）——HTTP 层要先把 run_id 返回给前端做暂停令牌，
      注入进来保证「暂停令牌 = 日志 trace = 本轮登记」三者同 id
    :param enable_web_search: 联网搜索是否参与本轮（前端「联网」开关；
      经 ContextVar 透传给 RAG 子图，见 `_ENABLE_WEB_SEARCH`）
    :param capture_evidence: 是否让 RAG 子图把最终证据正文带回来（eval_judge 用）。
      生产默认 False；topk 统计不受影响
    :param tool_ticket: Java 在本次 Converse 里签发的工具票据（gRPC metadata
      `x-agent-tool-ticket`）—— order-service 回调 Java 内部接口时带回；
      只放 ContextVar，**不进模型上下文**
    :param internal_event: Java 动作确认后的内部事件（gRPC metadata
      `x-agent-internal-event`）。非空时不跑主 agent、不存用户消息，
      只执行对应的动作图（M8：催发货，图内调用 Java 发送工具）。
    :param stream_output: 是否回放主 agent 的 token 级增量（`{"type": "delta"}` 事件）。
      默认 False（与旧行为一致，只推 final）；前端「流式输出」开关打开时为 True。
    :param form_result: 会话内表单回传 `{form_id, values}`（2026-11-07）。
      非空时**不跑主 agent、不烧模型**：校验表单 → 按 form_type 直接调工具 → 回复走 final。
    :return: {"run_id", "reply", "outcome", "usage_text", "usage", "images", "web_only", "forms"}
    """
    run_id = run_id or uuid.uuid4().hex[:16]

    # ⓪ 会话归属：多用户隔离的唯一入口；冲突直接抛 PermissionError
    ensure_session_owner(session_id, user_id, tenant_id)

    trace_events: list = []          # 本轮执行流（step 事件原样收集）—— 随回复存档，重开历史可还原（M5.1）

    def _emit(ev: dict) -> None:
        if ev.get("type") == "step":
            trace_events.append({"kind": ev.get("kind"), "agent": ev.get("agent"),
                                 "detail": ev.get("detail")})
        (on_event or (lambda ev: None))(ev)

    if internal_event:
        tool_token = set_tool_context(tool_ticket, user_id)
        try:
            return _run_internal_event(session_id, internal_event, message,
                                       user_id, tenant_id, run_id, _emit)
        finally:
            reset_tool_context(tool_token)

    ctx = _TurnContext(
        session_id=session_id, message=message, run_id=run_id, emit=_emit,
        tenant_id=tenant_id, user_id=user_id, on_usage=on_usage,
        enable_web_search=enable_web_search, capture_evidence=capture_evidence,
        tool_ticket=tool_ticket, stream_output=stream_output, trace_events=trace_events,
    )

    # ① 输入护栏（统一入口先过）
    guard_reason = check_user_input(message)
    if guard_reason:
        return _run_guard_branch(ctx, guard_reason)

    # ①' 敏感主题（2026-11-16）：入口直接拒答，不派发 RAG 子图、不调模型
    safety_reason = check_sensitive_topic(message)
    if safety_reason:
        return _run_safety_branch(ctx, safety_reason)

    # ①'' 表单回传（2026-11-07）：按 form_type **直接调工具**，不跑主 agent、不烧模型。
    if form_result:
        return _run_form_branch(ctx, form_result)

    # ② 普通问答
    return _run_conversation(ctx)


def _dim(text: str) -> None:
    print(f"{_DIM}{text}{_RESET}")


def _render_event(ev: dict) -> None:
    """事件 → 灰字（CLI 的渲染器；M5 前端是另一套渲染器，消费同一份事件）"""
    if ev.get("type") == "step":
        kind = ev.get("kind")
        if kind == "delegate":
            _dim(f"  ⎿ 派发 → {ev.get('agent')}：{ev.get('detail')}")
        elif kind == "tool":
            _dim(f"  ⎿ 调用工具 {ev.get('agent')}：{ev.get('detail')}")
        elif kind == "result":
            _dim(f"  ⎿ 工具返回：{ev.get('detail')}")
        elif kind == "node":
            # 子图内部节点：缩进一级，与主 agent 的调度步骤区分（M5 起的 subgraphs 事件）
            _dim(f"  │ {ev.get('detail')}")
        else:
            _dim(f"  ⎿ {ev.get('detail')}")
    elif ev.get("type") == "error":
        _dim(f"  ⎿ [错误] {ev.get('detail')}")


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]).strip()
    if question:
        logger.info(f"[{NODE_NAME}] 单轮自测，问题：{question}")
        result = run_main_agent("main_agent_cli", question, on_event=_render_event)
        print("=" * 64)
        print("主 agent 最终回复：")
        print(result["reply"])
        print("=" * 64)
        print("本轮记账（主 agent + 子调用）：", result["usage_text"])
    else:
        # Windows 中文环境：管道 / 部分终端下 stdin 编码不一定是 UTF-8 —— 不重配的话，
        # Git Bash 里输入中文会带出代理字符（\udcXX），发给模型 API 时直接 UnicodeEncodeError。
        # 真控制台用的 WindowsConsoleIO 不支持 reconfigure，抛错即忽略 —— 它本来就能读中文。
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("交互模式 —— 直接输入问题；exit / 空行退出；调度过程为灰色小字。")
        while True:
            try:
                q = input("\n你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q.lower() in ("", "exit", "quit"):
                break
            result = run_main_agent("main_agent_cli", q, on_event=_render_event)
            print("\n客服 >", result["reply"] or "（本轮没有生成回复）")
            print(f"[本轮记账] {result['usage_text']}")
