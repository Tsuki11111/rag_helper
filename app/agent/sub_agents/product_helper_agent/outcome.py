"""
RAG 子图的「结果解读」：结局口径 + 候选兜底文案

（2026-10-12 M4-T3 从 query_service 搬来，搬运本身零改动、仅换了家）
为什么在这里：这些是**对检索图产出物**的解读，随图走；主 agent 的适配层
（`_rag_task`）要用它们算子图结局、拼候选文案 —— 留在服务层会造成
main_agent ↔ query_service 的循环依赖。
"""
from app.agent.sub_agents.product_helper_agent.nodes.node_answer_output import FALLBACK_ANSWER
from app.core.error_policy import CONTENT_REJECTED_ANSWER
from app.core.input_guard import INPUT_GUARD_ANSWER

# ── 本轮结局（运行记录 query_runs 的核心字段）──
# `waiting_user` 不在 evaluation-plan 最初列的五个值里：图因缺信息**把候选带回来**时
# （2026-10-11 无状态化前是「中断挂起、等用户选」，现在是「直接结束、候选交调用方」——
# 这一段同样是终点，用户不接着问就没有下文），归进另外五个里任何一个都是说谎。
OUTCOME_ANSWERED = "answered"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_REJECTED = "rejected"
OUTCOME_BLOCKED = "blocked"
OUTCOME_PAUSED = "paused"
OUTCOME_WAITING_USER = "waiting_user"
OUTCOME_ERROR = "error"


def judge_outcome(state: dict) -> str:
    """
    从检索图的最终状态判断**这一段**的结局（纯函数，离线可测）

    判定顺序有意义：先看「要不要用户确认 / 被暂停」，再按答案的形态分类。依据都核实过：
    - 审核拒绝时 `node_item_name_confirm` 把 `CONTENT_REJECTED_ANSWER` 直接写进 answer
    - 输入护栏命中时同一个节点把 `INPUT_GUARD_ANSWER` 写进 answer（第三层护栏）
    - 一条参考都没有时 `node_answer_output` 用 `FALLBACK_ANSWER` 兜底，且**不调模型**
    - 认不出产品那条路**刻意不写 answer**（候选走 `clarify`、图直接结束），
      所以「有答案但没参考」只可能是上面几种
    """
    state = state or {}
    if state.get("need_confirm"):
        return OUTCOME_WAITING_USER
    if state.get("cancelled"):
        return OUTCOME_PAUSED
    answer = (state.get("answer") or "").strip()
    if answer == CONTENT_REJECTED_ANSWER:
        return OUTCOME_REJECTED
    # 输入护栏拦下的那类：答案同样是预设文本、参考切片也是空的 —— 不先判它就会被记成 no_match，
    # 而「有人往里打注入」和「库里没这个内容」是两件事，混在一起就没法统计了
    if answer == INPUT_GUARD_ANSWER:
        return OUTCOME_BLOCKED
    if not (state.get("reranked_docs") or []):
        return OUTCOME_NO_MATCH
    if not answer:
        # 有参考内容却没答案：生成那一环降级成了空答案（degraded_list 里能看到它）
        return OUTCOME_ERROR
    return OUTCOME_ANSWERED


def clarify_fallback_text(clarify: dict) -> str:
    """
    认不出产品时，给自用路径（SSE / 非流式）的一句人话。

    结构化候选（`clarify.options`）是给主 agent 链路消费的；
    这里把候选拼成人类可读的提示、写进 answer —— 前端不必为这一轮做特殊渲染。
    """
    options = [o.get("item_name") for o in ((clarify or {}).get("options") or [])
               if isinstance(o, dict) and o.get("item_name")]
    names = " / ".join(options[:5])
    if names:
        return f"没能锁定到具体产品。可能是：{names} —— 请带上产品型号重新提问。"
    return "没能锁定到具体产品，请带上产品型号重新提问。"


def _check_outcome_mapping() -> list:
    """
    离线自测：状态 → 结局的映射（纯逻辑，不碰任何服务）

    六种结局各一例，另加两条优先序。这几档一旦判错，评测拿到的就是**错的标签**，
    而且不会有任何报错 —— 报表只会安静地把通过率算错。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    cases = [
        ("认不出产品（带候选返回）",
         {"need_confirm": True, "clarify": {"options": []}, "answer": "", "reranked_docs": []},
         OUTCOME_WAITING_USER),
        ("用户暂停",
         {"cancelled": True, "answer": "", "reranked_docs": [{"source": "local"}]},
         OUTCOME_PAUSED),
        # 审核拒绝必须**先于**「没有参考内容」判：它的 answer 是预设的，
        # 参考切片也是空的，顺序反了就会被误记成 no_match
        ("审核拒绝",
         {"answer": CONTENT_REJECTED_ANSWER, "reranked_docs": []},
         OUTCOME_REJECTED),
        # 输入护栏拦下的同理：不先判就会混进 no_match，而「有人打注入」与「库里没有」是两件事
        ("输入护栏拦下",
         {"answer": INPUT_GUARD_ANSWER, "reranked_docs": []},
         OUTCOME_BLOCKED),
        ("一条参考都没有",
         {"answer": FALLBACK_ANSWER, "reranked_docs": []},
         OUTCOME_NO_MATCH),
        ("生成降级成空答案",
         {"answer": "", "reranked_docs": [{"source": "local"}]},
         OUTCOME_ERROR),
        ("正常回答",
         {"answer": "这是一段答案", "reranked_docs": [{"source": "local"}]},
         OUTCOME_ANSWERED),
        # 只有联网结果也算「有参考、有答案」—— 判据是 reranked_docs 非空，
        # 不是本地切片非空，否则纯联网那一轮会被误判成 no_match
        ("纯联网作答",
         {"answer": "来自网络的答案", "reranked_docs": [{"source": "web"}]},
         OUTCOME_ANSWERED),
        ("候选返回压过暂停",
         {"need_confirm": True, "cancelled": True, "answer": "",
          "reranked_docs": []},
         OUTCOME_WAITING_USER),
    ]
    for name, state, expect in cases:
        got = judge_outcome(state)
        if got != expect:
            problems.append(f"{name}：期望 {expect}，实得 {got}")

    # 空状态不能把服务炸掉（自测 / 命令行误用时可能传进来）
    try:
        judge_outcome(None)
    except Exception as e:
        problems.append(f"judge_outcome(None) 抛异常：{type(e).__name__}: {e}")
    return problems
