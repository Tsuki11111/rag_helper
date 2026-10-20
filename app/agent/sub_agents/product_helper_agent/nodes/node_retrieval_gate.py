"""
检索充分性判定节点 (node_retrieval_gate)

位置：`node_rerank` 之后。决定「用这批证据作答」还是「换检索词再检索一轮」。

三条设计红线（2026-10-20，见 HANDOFF §3.52）：
1. **永远对 `original_query` 判定**，不对本轮的 `retrieval_query` 判定 ——
   改写只是探针，不能反过来定义「用户想问什么」
2. **正常轮次不调模型**：只有红灯（无证据 / 纯联网 / 重排降级 / 本地证据过少）
   才调 LLM；判不出或调用失败一律按「充分」处理（fail-open），
   绝不因为判定器坏了把本来能答的题拖进重试
3. 循环耗尽时**用第一轮证据作答**（`anchor_docs`）—— 一次糟糕的改写最多浪费成本，
   不会把最终答案带到漂移后的方向

⚠️ 它不是评估体系里的 LLM-as-Judge（P3）：判定结果只用于控制流，
**不得拿去算检索质量指标**（见 HANDOFF §3.52 与 evolution-plan §8）。
"""
import json
import sys

from langchain.messages import HumanMessage

from app.agent.sub_agents.product_helper_agent.state import (
    QueryGraphState,
    resolve_retrieval_query,
)
from app.conf.pipeline_config import pipeline_config
from app.core.budget import remaining_wall_clock
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.retry import invoke_with_retry
from app.lm.lm_utils import get_llm_client
from app.utils.task_utils import add_done_task, add_running_task

# 节点名，与 main_graph.py 中 add_node 注册的名称保持一致，用于日志前缀
NODE_NAME = "node_retrieval_gate"

# 给判定器看的证据：前几条、每条截断多长（够判方向即可，不把整篇塞进提示词）
EVIDENCE_MAX_DOCS = 3
EVIDENCE_MAX_CHARS = 300


def _is_red_flagged(docs: list, web_only: bool) -> tuple:
    """
    返回 `(是否需要 LLM 判定, 原因)`

    这些信号只是「可疑」，不是「一定不行」—— 所以只负责触发判定，
    最终由模型对原始问题做语义判断。全部健康时**一次模型调用都不发生**。
    """
    if pipeline_config.retrieval_judge_always:
        return True, "always"
    if not docs:
        return True, "no_docs"
    if web_only:
        return True, "web_only"
    scores = [float(d.get("score") or 0.0) for d in docs if isinstance(d, dict)]
    if scores and all(s == 0.0 for s in scores):
        return True, "rerank_degraded"
    n_local = sum(1 for d in docs if isinstance(d, dict) and d.get("source") == "local")
    if n_local < pipeline_config.rerank_min_topk:
        return True, "few_local"
    return False, ""


def _evidence_brief(docs: list) -> str:
    """把证据压成判定器能快速读懂的短清单（标题 + 前 300 字）"""
    parts = []
    for i, d in enumerate(
            [x for x in (docs or []) if isinstance(x, dict)][:EVIDENCE_MAX_DOCS], 1):
        title = (d.get("title") or "").strip()
        text = (d.get("text") or "").strip().replace("\n", " ")
        parts.append(
            f"[{i}] 来源={d.get('source') or 'local'} 标题={title[:80]}\n"
            f"{text[:EVIDENCE_MAX_CHARS]}"
        )
    return "\n\n".join(parts) or "（无）"


def _parse_json_object(content: str) -> dict:
    """剥掉 ```json 外壳后解析；形状不对返回空 dict（调用方按 fail-open 处理）"""
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.replace("```json", "").replace("```", "").strip()
    result = json.loads(content)
    return result if isinstance(result, dict) else {}


def _llm_verdict(state: QueryGraphState, docs: list) -> dict:
    """
    调 LLM 判充分性。

    **任何失败都 fail-open 成 sufficient**：判定器是优化项，不能成为新的故障点。
    要求重试却给不出缺失/改写建议的，也按充分处理 —— 没有方向的盲试只会白烧一轮。
    """
    fail_open = {"verdict": "sufficient", "missing": "", "rewrite_hint": "",
                 "reason": "judge_failed"}
    try:
        prompt = load_prompt(
            "retrieval_gate",
            original_query=state.get("original_query") or "",
            item_names="、".join(state.get("item_names") or []) or "（无）",
            retrieval_query=resolve_retrieval_query(state),
            evidence=_evidence_brief(docs),
        )
        client = get_llm_client(json_mode=True)
        response = invoke_with_retry(
            client, [HumanMessage(content=prompt)], "判定检索充分性")
        result = _parse_json_object(response.content)
        verdict = str(result.get("verdict") or "").strip().lower()
        if verdict not in ("sufficient", "retry"):
            logger.warning(
                f"[{NODE_NAME}] 判定器返回了无法识别的 verdict={verdict!r}，按充分处理")
            return {**fail_open, "reason": "judge_bad_verdict"}
        missing = str(result.get("missing") or "").strip()
        hint = str(result.get("rewrite_hint") or "").strip()
        if verdict == "retry" and not (missing or hint):
            logger.warning(
                f"[{NODE_NAME}] 判定器要求重试但没给 missing/rewrite_hint，按充分处理")
            return {**fail_open, "reason": "judge_empty_hint"}
        return {"verdict": verdict, "missing": missing, "rewrite_hint": hint,
                "reason": "llm"}
    except Exception as e:
        logger.warning(
            f"[{NODE_NAME}] 充分性判定失败，按充分处理（fail-open）："
            f"{type(e).__name__}: {e}")
        return fail_open


def _decide_next(*, round_no: int, max_rounds: int, verdict: str,
                 budget_ok: bool) -> str:
    """
    纯函数：本轮之后走哪条边（"retry" / "answer"）

    三个护栏按优先级：循环关闭 → 轮次用尽 → 剩余预算不够。
    任何一条成立都直接作答 —— **宁可答得保守，也不无限重试**。
    """
    if verdict != "retry":
        return "answer"
    if max_rounds <= 1:
        return "answer"
    if round_no >= max_rounds:
        return "answer"
    if not budget_ok:
        return "answer"
    return "retry"


def _choose_evidence(*, next_route: str, round_no: int, docs: list, web_only: bool,
                     best_docs: list, best_web_only: bool,
                     anchor_docs: list, anchor_web_only: bool):
    """
    纯函数：最终交给作答节点的证据 `(docs, web_only)`

    - 重试轮不改写状态里的证据（下一轮会覆盖），返回 `(None, None)`
    - 有「判定充分」的轮次 → 用那一轮
    - 循环耗尽 / 改写失败 → 用**第一轮**（意图锚），绝不采用漂移后的探针结果
    """
    if next_route != "answer":
        return None, None
    if best_docs:
        return best_docs, best_web_only
    if round_no > 1:
        return anchor_docs, anchor_web_only
    return docs, web_only


def node_retrieval_gate(state: QueryGraphState) -> QueryGraphState:
    """
    节点: 判断检索充分性 (node_retrieval_gate)

    :param state: 需包含 session_id / original_query / rewritten_query / reranked_docs
    :return: 更新轮次、判定、历史与证据选择；`retrieval_judge.next` 决定路由
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{NODE_NAME}] [{function_name}] 开始处理")
    add_running_task(state["session_id"], function_name, state.get("is_stream"))

    try:
        docs = [d for d in (state.get("reranked_docs") or []) if isinstance(d, dict)]
        web_only = bool(state.get("web_only"))
        round_no = int(state.get("retrieval_round") or 0) + 1
        loop_enabled = pipeline_config.retrieval_max_rounds > 1

        # 第一轮证据固定下来：循环耗尽时只回落到它（防改写把答案带偏）
        anchor_docs = list(state.get("anchor_docs") or [])
        anchor_web_only = bool(state.get("anchor_web_only"))
        if round_no == 1:
            anchor_docs = [dict(d) for d in docs]
            anchor_web_only = web_only

        best_docs = list(state.get("best_docs") or [])
        best_web_only = bool(state.get("best_web_only"))

        # 判定：只有红灯轮次才调模型；循环关闭时直接判充分（零额外调用）
        flag, flag_reason = _is_red_flagged(docs, web_only)
        if loop_enabled and flag:
            judged = _llm_verdict(state, docs)
        else:
            judged = {"verdict": "sufficient", "missing": "", "rewrite_hint": "",
                      "reason": ("loop_disabled" if not loop_enabled
                                 else (flag_reason or "healthy"))}
        verdict = judged["verdict"]

        remaining = remaining_wall_clock()
        budget_ok = (remaining is None
                     or remaining >= pipeline_config.retrieval_retry_min_remaining_sec)
        next_route = _decide_next(
            round_no=round_no,
            max_rounds=pipeline_config.retrieval_max_rounds,
            verdict=verdict,
            budget_ok=budget_ok,
        )
        # verdict=retry 却去作答时，把「为什么停」写进日志 —— 否则只看到
        # 「下一步=作答」会误以为是判定器改主意了
        stop_reason = ""
        if verdict == "retry" and next_route == "answer":
            if pipeline_config.retrieval_max_rounds <= 1:
                stop_reason = "（循环关闭）"
            elif round_no >= pipeline_config.retrieval_max_rounds:
                stop_reason = "（已达轮次上限）"
            elif not budget_ok:
                stop_reason = "（剩余预算不足）"

        if verdict == "sufficient":
            best_docs = [dict(d) for d in docs]
            best_web_only = web_only

        chosen_docs, chosen_web_only = _choose_evidence(
            next_route=next_route, round_no=round_no, docs=docs, web_only=web_only,
            best_docs=best_docs, best_web_only=best_web_only,
            anchor_docs=anchor_docs, anchor_web_only=anchor_web_only,
        )

        n_local = sum(1 for d in docs if d.get("source") == "local")
        n_web = len(docs) - n_local
        history = list(state.get("retrieval_history") or [])
        history.append({
            "round": round_no,
            # 本轮实际使用的检索词（首轮可能是 rewritten_query 的回退）
            "query": resolve_retrieval_query(state),
            "verdict": verdict,
            "missing": judged.get("missing") or "",
            "top1_score": float(docs[0].get("score") or 0.0) if docs else 0.0,
            "n_local": n_local,
            "n_web": n_web,
        })

        judge = {
            "verdict": verdict,
            "missing": judged.get("missing") or "",
            "rewrite_hint": judged.get("rewrite_hint") or "",
            "reason": judged.get("reason") or "",
            "round": round_no,
            "next": next_route,
        }
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 第 {round_no} 轮：verdict={verdict}，"
            f"reason={judge['reason']}，证据 {len(docs)} 条"
            f"（本地 {n_local} / 联网 {n_web}），"
            f"下一步={'再检索一轮' if next_route == 'retry' else '作答'}{stop_reason}"
        )

        update = {
            "retrieval_round": round_no,
            "retrieval_judge": judge,
            "retrieval_history": history,
            "anchor_docs": anchor_docs,
            "anchor_web_only": anchor_web_only,
            "best_docs": best_docs,
            "best_web_only": best_web_only,
        }
        if chosen_docs is not None:
            update["reranked_docs"] = chosen_docs
            update["web_only"] = chosen_web_only
        return update

    finally:
        add_done_task(state["session_id"], function_name, state.get("is_stream"))
        logger.info(f"[{NODE_NAME}] [{function_name}] 处理结束")


def _check_gate_routing_and_evidence() -> list:
    """
    离线自测：检索循环的路由与证据回落（纯逻辑，零依赖、不调模型）

    守四件事：
    1. 充分 → 作答；不足且轮次/预算允许 → 重试
    2. 达上限 / 预算不足 / 循环关闭 → 一律作答（绝不无限循环）
    3. 充分轮用充分轮证据；循环耗尽用**第一轮**证据（防漂移的核心）
    4. 红灯判定覆盖：无证据 / 纯联网 / 重排降级 / 本地过少；健康轮次不触发判定

    :return: 问题描述列表，空表示通过
    """
    problems = []

    route_cases = [
        ("充分", dict(round_no=1, max_rounds=2, verdict="sufficient", budget_ok=True), "answer"),
        ("不足且可重试", dict(round_no=1, max_rounds=2, verdict="retry", budget_ok=True), "retry"),
        ("达轮次上限", dict(round_no=2, max_rounds=2, verdict="retry", budget_ok=True), "answer"),
        ("预算不足", dict(round_no=1, max_rounds=2, verdict="retry", budget_ok=False), "answer"),
        ("循环关闭", dict(round_no=1, max_rounds=1, verdict="retry", budget_ok=True), "answer"),
    ]
    for name, kwargs, expect in route_cases:
        got = _decide_next(**kwargs)
        if got != expect:
            problems.append(f"路由「{name}」：期望 {expect}，实得 {got}")

    docs1 = [{"text": "第一轮", "score": 0.5, "source": "local"}]
    docs2 = [{"text": "第二轮", "score": 0.9, "source": "local"}]
    # 重试轮不改写证据
    got, _ = _choose_evidence(
        next_route="retry", round_no=1, docs=docs1, web_only=False,
        best_docs=[], best_web_only=False, anchor_docs=docs1, anchor_web_only=False)
    if got is not None:
        problems.append(f"重试轮不该改写证据，实得 {got!r}")
    # 充分轮用充分轮证据（第二轮充分 → 第二轮）
    got, web = _choose_evidence(
        next_route="answer", round_no=2, docs=docs2, web_only=False,
        best_docs=docs2, best_web_only=False, anchor_docs=docs1, anchor_web_only=True)
    if got != docs2 or web is not False:
        problems.append(f"充分轮证据不对：{got!r} / web_only={web!r}")
    # 循环耗尽 → 第一轮证据（即使第二轮分数更高）
    got, web = _choose_evidence(
        next_route="answer", round_no=2, docs=docs2, web_only=False,
        best_docs=[], best_web_only=False, anchor_docs=docs1, anchor_web_only=True)
    if got != docs1 or web is not True:
        problems.append(f"耗尽应回落到第一轮证据，实得 {got!r} / web_only={web!r}")

    red_cases = [
        ("无证据", [], False, True),
        ("纯联网", [{"source": "web", "score": 0.8}], True, True),
        ("重排降级", [{"source": "local", "score": 0.0}] * 3, False, True),
        ("本地过少", [{"source": "local", "score": 0.8}], False, True),
        ("健康轮次",
         [{"source": "local", "score": 0.8}] * pipeline_config.rerank_min_topk,
         False, False),
    ]
    # 用例必须与用户 .env 无关：RETRIEVAL_JUDGE_ALWAYS=true 时健康轮次也会有红灯，
    # 那是配置行为、不是用例要守的东西 —— 这里临时关掉再判
    saved_always = pipeline_config.retrieval_judge_always
    pipeline_config.retrieval_judge_always = False
    try:
        for name, docs, web_only, expect in red_cases:
            got, reason = _is_red_flagged(docs, web_only)
            if bool(got) != expect:
                problems.append(f"红灯判定「{name}」：期望 {expect}，实得 {got}（{reason}）")
    finally:
        pipeline_config.retrieval_judge_always = saved_always

    # 检索词回退：探针 → 意图锚 → 原始问题
    fallback_cases = [
        ({"retrieval_query": "探针", "rewritten_query": "锚", "original_query": "原"}, "探针"),
        ({"rewritten_query": "锚", "original_query": "原"}, "锚"),
        ({"original_query": "原"}, "原"),
        ({}, ""),
    ]
    for state, expect in fallback_cases:
        got = resolve_retrieval_query(state)
        if got != expect:
            problems.append(f"检索词回退：期望 {expect!r}，实得 {got!r}")

    return problems
