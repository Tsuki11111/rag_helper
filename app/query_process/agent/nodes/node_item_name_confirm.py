"""
产品确认节点 (node_item_name_confirm)

节点作用：判断用户问的是**哪个产品**，以便定位到对应手册。

流程（7 步）：
1. 取历史会话
2. 保存当前问题
3. LLM 提取产品名 + 改写问题（处理代词消解）
4. 产品名向量化，在 kb_item_names 中检索标准产品名
5. 按相似度对齐（≥0.85 确认 / 0.6~0.85 候选 / <0.6 记为「近似候选」）
6. 两分支处理：
   A 有确认产品 → 写入 state，继续检索
   B/C 认不出来（只有候选，或连候选都没有）→ 置 need_confirm，把卡片内容写进
       state['clarify']，主图条件边转到 node_ask_user **中断去问用户**
       （早先是生成一句反问当答案直接输出：用户看不到选项、也没法从断点继续。
        改成中断后，用户选完会回到同一个 thread 接着检索）
7. 持久化历史记录

注意：本项目嵌入模型为 DashScope（仅稠密向量），因此第 4 步用 dense_search，
而非教程中的稠密+稀疏混合检索。
"""
import json
import sys
from typing import Any, Dict, List

from langchain.messages import HumanMessage, SystemMessage

from app.clients.milvus_utils import dense_search, get_milvus_client
from app.clients.mongo_history_utils import (
    get_recent_messages,
    save_chat_message,
    update_message_item_names,
)
from app.conf.milvus_config import milvus_config
from app.core.load_prompt import load_prompt
from app.core.error_policy import (
    CONTENT_REJECTED_ANSWER,
    ErrorKind,
    degrade,
    degrade_dependency,
    is_content_rejected,
)
from app.core.logger import logger
from app.lm.embedding_utils import generate_embeddings
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.state import QueryGraphState
from app.utils.task_utils import add_running_task, add_done_task

# 节点名，与 main_graph.py 中 add_node 注册的名称保持一致，用于日志前缀
NODE_NAME = "node_item_name_confirm"

# --- 配置参数 ---
# 参与上下文的历史消息条数
HISTORY_LIMIT = 10
# 相似度阈值：≥该值认为与库中标准产品名是同一个（教程代码用 0.85）
CONFIRM_SCORE_THRESHOLD = 0.85
# 候选阈值：≥该值但低于确认阈值时，作为候选让用户选择
CANDIDATE_SCORE_THRESHOLD = 0.6
# 反问时最多列出的候选数量
MAX_CANDIDATE_OPTIONS = 3
# 候选之外，再给几个「近似」的（低于候选线但排名靠前），让用户有东西可选
MAX_NEAR_OPTIONS = 3
# 检索时对每个产品名取回的匹配数
SEARCH_LIMIT = 5

# 向用户确认时的提示语（卡片标题，选项由 node_ask_user 展示）
CLARIFY_QUESTION = "「{query}」没能锁定到具体产品，你想问的是下面哪一个？"
# 连近似候选都没有时的提示语（卡片仍会弹出，只是只有自由输入）
NO_MATCH_QUESTION = "没能从「{query}」里认出产品，请补充准确的产品型号。"


def step_1_get_history(session_id: str) -> List[Dict[str, Any]]:
    """
    步骤 1: 取该会话的最近若干条历史记录，用于代词消解与上下文理解
    :param session_id: 会话ID
    :return: 历史消息列表，失败返回空列表
    """
    function_name = sys._getframe().f_code.co_name
    try:
        history = get_recent_messages(session_id, limit=HISTORY_LIMIT)
    except Exception as e:
        # 历史读取失败退化为无上下文，不中断流程；编程错误由 degrade 上抛
        history = degrade(NODE_NAME, "读取会话历史", [], e)
    logger.info(f"[{NODE_NAME}] [{function_name}] 取到{len(history)}条历史消息")
    return history


def step_2_save_user_message(session_id: str, original_query: str) -> str:
    """
    步骤 2: 先保存用户当前问题，拿到消息ID供后续步骤7补充改写结果
    :return: 消息ID；保存失败返回空字符串
    """
    function_name = sys._getframe().f_code.co_name
    try:
        message_id = save_chat_message(session_id, "user", original_query)
        logger.info(f"[{NODE_NAME}] [{function_name}] 用户消息已保存，ID={message_id}")
        return message_id
    except Exception as e:
        return degrade(NODE_NAME, "保存用户消息", "", e)


def step_3_extract_info(query: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    步骤 3: 用大模型提取产品名并改写问题

    两个产出：
    - item_names：用户问的产品名（可能多个），用于后续向量对齐
    - rewritten_query：把「这个怎么装」这类指代不明的口语问题，改写成
      「XXX 怎么安装」这样的独立完整问题，提升后续检索召回率

    :return: {"item_names": [...], "rewritten_query": "..."}；失败时产品名为空、改写回退为原问题。
        被模型服务内容审核拒绝时额外带 `"rejected": True`，由入口决定短路
    """
    function_name = sys._getframe().f_code.co_name
    fallback = {"item_names": [], "rewritten_query": query}

    # 拼历史为「角色: 内容」的文本，供 LLM 做指代消解
    history_text = "".join(f"{m.get('role', '')}: {m.get('text', '')}\n" for m in history)

    try:
        # json_mode=True：让模型直接返回可解析的 JSON，避免额外剥壳
        client = get_llm_client(json_mode=True)
        prompt = load_prompt("rewritten_query_and_itemnames", history_text=history_text, query=query)
        messages = [
            SystemMessage(content="你是一个专业的客服助手，擅长理解用户意图和提取关键信息。"),
            HumanMessage(content=prompt),
        ]
        response = client.invoke(messages)
        content = (response.content or "").strip()

        # 兜底：部分模型仍会用 ```json 包裹
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()

        result = json.loads(content)
        # 字段兜底，避免模型漏字段导致下游 KeyError
        if not isinstance(result.get("item_names"), list):
            result["item_names"] = []
        if not result.get("rewritten_query"):
            result["rewritten_query"] = query

        logger.info(
            f"[{NODE_NAME}] [{function_name}] LLM提取完成："
            f"item_names={result['item_names']}，rewritten_query={result['rewritten_query']!r}"
        )
        return result

    except Exception as e:
        # LLM 失败或 JSON 解析失败都退化为「无产品名 + 原问题」，保证流程不中断。
        # 内容审核拒绝要多带一个 `rejected` 标记：它不是「提取失败」而是模型服务合规拒答，
        # 入口看到就短路给用户一句人话 —— 否则下游会拿原问题去检索、生成节点再被拒一次，
        # 用户最后看到的是一顿空答案，还不知道是为什么
        result = degrade(NODE_NAME, "LLM 提取产品名", fallback, e)
        if is_content_rejected(e):
            return {**result, "rejected": True}
        return result


def step_4_vectorize_and_query(item_names: List[str]) -> List[Dict[str, Any]]:
    """
    步骤 4: 把提取出的产品名向量化，在 kb_item_names 中检索库里的标准产品名

    批量生成向量以减少 API 调用；逐个检索以保证结果与产品名一一对应。
    本项目仅用稠密向量，故走 dense_search（教程的混合检索已在本项目移除）。

    :param item_names: step3 提取的产品名列表
    :return: [{"extracted_name": 提取名, "matches": [{"item_name": 标准名, "score": 相似度}]}]
    """
    function_name = sys._getframe().f_code.co_name
    results: List[Dict[str, Any]] = []

    client = get_milvus_client()
    if client is None:
        # 这条尤其要紧：产品名对齐挂掉会让所有提问都落到"拒识"，用户只会觉得"什么都查不到"
        return degrade_dependency(NODE_NAME, "产品名对齐", results, "Milvus 不可用")

    collection_name = milvus_config.item_name_collection
    if not collection_name:
        return degrade_dependency(NODE_NAME, "产品名对齐", results,
                                  "未配置 ITEM_NAME_COLLECTION", ErrorKind.BLOCKED)

    try:
        embeddings = generate_embeddings(item_names)
        dense_vectors = embeddings.get("dense") or []
    except Exception as e:
        return degrade(NODE_NAME, "产品名向量化", results, e)

    for idx, name in enumerate(item_names):
        try:
            if idx >= len(dense_vectors):
                logger.warning(f"[{NODE_NAME}] [{function_name}] 产品名[{name}]缺少对应向量，跳过")
                continue

            hits = dense_search(
                client, collection_name, dense_vectors[idx],
                # file_title 要一起取：它是候选卡片的「选了会怎样」—— 选了它就用这份手册回答
                limit=SEARCH_LIMIT, output_fields=["item_name", "file_title"],
                search_params={"ef": 64},
            )
            matches = []
            if hits and hits[0]:
                for hit in hits[0]:
                    entity = hit.get("entity") or {}
                    matches.append({
                        "item_name": entity.get("item_name"),
                        "file_title": entity.get("file_title") or "",
                        # 显式转 float：Milvus 回来的可能是 numpy 标量，
                        # 而这个值会跟着 clarify 进检查点走 msgpack 序列化，numpy 类型会直接报错
                        "score": float(hit.get("distance", 0.0) or 0.0),
                    })
            results.append({"extracted_name": name, "matches": matches})
            logger.info(f"[{NODE_NAME}] [{function_name}] [{name}] 检索到{len(matches)}个匹配")

        except Exception as e:
            # 单个产品名失败不影响其余产品名
            degrade(NODE_NAME, f"检索产品名[{name}]", None, e)

    return results


def step_5_align_item_names(query_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    步骤 5: 按相似度把提取名对齐到库中的标准产品名

    规则（优先级 a > b > c > d）：
      a 只有一个匹配 ≥0.85        → 确认为该产品
      b 多个匹配 ≥0.85           → 优先取与提取名完全相同的，否则取最高分
      c 无 ≥0.85 但有 ≥0.6       → 取分数最高的前几个作为候选，交由用户选择
      d 无 ≥0.6                  → 确认与候选均为空（视为未找到）

    :return: {
        "confirmed_item_names": [...],   # 已确认的标准产品名
        "candidates": [...],             # 候选（≥0.6）：每项 {item_name, file_title, score}
        "near": [...],                   # 连候选都没有时的近似项（<0.6、排名靠前），同结构
        "corrections": {提取名: 标准名},  # 发生的纠正，供 step6 同步修正改写问题
    }
    """
    function_name = sys._getframe().f_code.co_name
    confirmed: List[str] = []
    candidates: List[Dict[str, Any]] = []
    near: List[Dict[str, Any]] = []
    corrections: Dict[str, str] = {}

    for res in query_results:
        extracted_name = (res.get("extracted_name") or "").strip()
        matches = res.get("matches") or []
        if not matches:
            continue

        # 按分数降序，高分优先
        matches.sort(key=lambda x: x.get("score") or 0, reverse=True)
        high = [m for m in matches if (m.get("score") or 0) >= CONFIRM_SCORE_THRESHOLD]
        mid = [m for m in matches if (m.get("score") or 0) >= CANDIDATE_SCORE_THRESHOLD]

        # 规则 a：唯一高置信度
        if len(high) == 1:
            picked = high[0]["item_name"]
            confirmed.append(picked)
            if extracted_name and extracted_name != picked:
                corrections[extracted_name] = picked
            continue

        # 规则 b：多个高置信度，优先与提取名完全一致的那个
        if len(high) > 1:
            picked = next((m["item_name"] for m in high if m["item_name"] == extracted_name), None)
            if not picked:
                picked = high[0]["item_name"]
            confirmed.append(picked)
            if extracted_name and extracted_name != picked:
                corrections[extracted_name] = picked
            continue

        # 规则 c：无高置信度，取中置信度前几个作为候选
        if mid:
            candidates.extend(mid[:MAX_CANDIDATE_OPTIONS])
        # 规则 d：连候选都没有 —— 取分最低线以下、排名靠前的几个当「近似」，
        # 让用户至少有点可选的，而不是只给一句「没找到」
        else:
            low = [m for m in matches if (m.get("score") or 0) < CANDIDATE_SCORE_THRESHOLD]
            near.extend(low[:MAX_NEAR_OPTIONS])

    # 去重但保持顺序（list(set()) 会打乱顺序，候选展示需要稳定）
    def _dedup(seq):
        seen, out = set(), []
        for x in seq:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _dedup_opts(seq):
        """按 item_name 去重，保留首次出现的那个（带着它的 file_title 与分数）"""
        seen, out = set(), []
        for o in seq:
            name = o.get("item_name")
            if name and name not in seen:
                seen.add(name)
                out.append(o)
        return out

    result = {
        "confirmed_item_names": _dedup(confirmed),
        "candidates": _dedup_opts(candidates),
        "near": _dedup_opts(near),
        "corrections": corrections,
    }
    logger.info(
        f"[{NODE_NAME}] [{function_name}] 对齐结果：确认={result['confirmed_item_names']}，"
        f"候选={[c['item_name'] for c in result['candidates']]}，"
        f"近似={[c['item_name'] for c in result['near']]}，纠正={corrections}"
    )
    return result


def step_6_check_confirmation(
    align_result: Dict[str, Any],
    session_id: str,
    history: List[Dict[str, Any]],
    rewritten_query: str,
    original_query: str,
) -> Dict[str, Any]:
    """
    步骤 6: 按对齐结果决定流程走向

    分支 A 有确认产品：回填 state，并给历史中缺产品名的消息补上（上下文一致性），继续检索
    分支 B/C 认不出来（只有候选、或连候选都没有）：置 `need_confirm`、把卡片内容写进
    `state['clarify']`，由主图的 node_ask_user **中断去问用户**。

    **刻意不写 `answer`**：一是会把外部预置的 answer 覆盖掉（自测场景2 靠它短路），
    二是「要问用户」这件事应该由图去**中断**，而不是伪装成一句现成的答案 ——
    否则用户看不到选项，也没法从断点接着跑。

    另外修正一处不一致：step3 的 rewritten_query 是基于 LLM 当时提取的名字写的，
    若 step5 把名字对齐成了标准名（如 "HAK" → "Brother HAK 180 烫金机"），
    就把改写问题里的旧名换成标准名，避免后续检索用错名字。

    :return: 供 LangGraph 合并进 state 的字段字典
    """
    function_name = sys._getframe().f_code.co_name
    confirmed = align_result.get("confirmed_item_names") or []
    candidates = align_result.get("candidates") or []
    near = align_result.get("near") or []
    corrections = align_result.get("corrections") or {}

    # 用对齐后的标准名替换改写问题里的旧名
    for old, new in corrections.items():
        if old in rewritten_query:
            rewritten_query = rewritten_query.replace(old, new)
    if corrections:
        logger.info(f"[{NODE_NAME}] [{function_name}] 改写问题已同步纠正：{rewritten_query!r}")

    # 分支 A：确认了产品，继续走检索
    if confirmed:
        # 给历史中还没关联产品名的消息补上，保持上下文一致
        ids_to_update = [str(m["_id"]) for m in history if m.get("_id") and not m.get("item_names")]
        if ids_to_update:
            try:
                update_message_item_names(ids_to_update, confirmed)
                logger.info(f"[{NODE_NAME}] [{function_name}] 已为{len(ids_to_update)}条历史消息补上产品名")
            except Exception as e:
                # 历史回填失败不影响本次检索
                degrade(NODE_NAME, "回填历史产品名", None, e)

        logger.info(f"[{NODE_NAME}] [{function_name}] 分支A：已确认产品 {confirmed}")
        return {
            "item_names": confirmed,
            "rewritten_query": rewritten_query,
            "answer": "",   # 清空，避免残留答案让条件边误判为「已有答案」
        }

    # 分支 B/C：认不出来 —— 交给 node_ask_user 中断去问用户
    # 候选（≥0.6）排在前面，近似项（<0.6）标 near 让前端区别显示
    options = [{**c, "near": False} for c in candidates] + [{**c, "near": True} for c in near]
    question = (
        CLARIFY_QUESTION.format(query=original_query) if options
        else NO_MATCH_QUESTION.format(query=original_query)
    )
    logger.info(
        f"[{NODE_NAME}] [{function_name}] 认不出产品，转去询问用户："
        f"候选{len(candidates)}个、近似{len(near)}个"
    )
    return {
        "item_names": [],
        "rewritten_query": rewritten_query,
        "need_confirm": True,
        "clarify": {
            "question": question,
            "options": options,
            "allow_custom": True,
        },
    }


def step_7_write_history(
    session_id: str,
    original_query: str,
    rewritten_query: str,
    item_names: List[str],
    message_id: str,
    clarify: Dict[str, Any] = None,
) -> None:
    """
    步骤 7: 持久化本轮交互

    1. 本轮要**问用户**时写一条助手消息 ——
       卡片那句问题必须入历史，否则用户选完之后 `node_answer_output` 读到的上下文是断档的
    2. 更新用户那条消息，补上改写后的问题与识别出的产品名

    **这里刻意不存「答案」**（本节点产出 `answer` 的那条路 —— 内容审核拒绝 —— 也一样）。
    任何非空 `answer` 都会被 `route_after_item_name_confirm` 路由到 `node_answer_output`，
    而那个节点才是**唯一**负责存档最终答案的地方。两边都存就会在历史里留下两条一模一样的
    助手消息（2026-10-06 实测：审核拒绝那一轮存了两遍），下一轮的上下文因此重复。
    """
    function_name = sys._getframe().f_code.co_name

    # 助手侧只存档卡片那句问题；答案由 node_answer_output 存，见上面的说明
    assistant_text = (clarify or {}).get("question") or ""
    if assistant_text:
        try:
            save_chat_message(session_id, "assistant", assistant_text)
            logger.info(f"[{NODE_NAME}] [{function_name}] 助手消息已存档")
        except Exception as e:
            degrade(NODE_NAME, "助手消息存档", None, e)

    if message_id:
        try:
            save_chat_message(
                session_id, "user", original_query,
                rewritten_query, item_names, message_id=message_id,
            )
            logger.info(f"[{NODE_NAME}] [{function_name}] 用户消息已更新（改写结果+产品名）")
        except Exception as e:
            degrade(NODE_NAME, "用户消息更新", None, e)


def node_item_name_confirm(state: QueryGraphState) -> QueryGraphState:
    """
    节点入口：串联 7 个步骤

    :param state: 需包含 session_id / original_query / is_stream
    :return: 更新的字段（item_names / rewritten_query / answer / history）
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{NODE_NAME}] [{function_name}] 开始处理")
    session_id = state["session_id"]
    original_query = state.get("original_query", "")
    is_stream = state.get("is_stream", False)
    add_running_task(session_id, function_name, is_stream)

    # 1. 取历史会话
    history = step_1_get_history(session_id)

    # 2. 先保存用户当前问题，拿到消息ID
    message_id = step_2_save_user_message(session_id, original_query)

    # 3. LLM 提取产品名 + 改写问题
    extract_res = step_3_extract_info(original_query, history)
    item_names = extract_res.get("item_names") or []
    rewritten_query = extract_res.get("rewritten_query") or original_query
    rejected = bool(extract_res.get("rejected"))

    # 4 & 5. 有产品名才做向量检索与对齐
    align_result: Dict[str, Any] = {}
    if rejected:
        # 内容审核拒绝：**本轮到此为止**，别再往下检索 —— 检索回来的东西照样递不到用户手里，
        # 生成节点的 LLM 调用会被同样拒一次，最后只剩一顿空答案
        logger.info(f"[{NODE_NAME}] [{function_name}] 提问被模型服务内容审核拒绝，跳过检索")
    elif item_names:
        query_results = step_4_vectorize_and_query(item_names)
        align_result = step_5_align_item_names(query_results)
    else:
        logger.info(f"[{NODE_NAME}] [{function_name}] 未提取到产品名，跳过向量对齐")

    # 6. 按对齐结果决定分支；被拒绝时不走分支判断，直接置 answer 让条件边短路到输出节点
    if rejected:
        updates: Dict[str, Any] = {
            "item_names": [],
            "rewritten_query": rewritten_query,
            "answer": CONTENT_REJECTED_ANSWER,
        }
    else:
        updates = step_6_check_confirmation(
            align_result, session_id, history, rewritten_query, original_query,
        )

    # 7. 持久化（答案不在这里存 —— 归 node_answer_output，见 step_7 的说明）
    step_7_write_history(
        session_id=session_id,
        original_query=original_query,
        rewritten_query=updates.get("rewritten_query", rewritten_query),
        item_names=updates.get("item_names", []),
        message_id=message_id,
        clarify=updates.get("clarify") or {},
    )

    # **刻意不把 history 塞进 state**：里面每条都带 Mongo 的 `_id`（ObjectId），
    # 而检查点用 msgpack 序列化，ObjectId 不在可序列化类型里 —— 一塞进去，整轮问答就会
    # 报「Type is not msgpack serializable: ObjectId」（会话有历史时必现，全新的会话反而没事，
    # 所以很容易漏测）。反正下游也没人读它：node_answer_output 的 _build_history 是自己
    # 回 Mongo 现读的。要往下游传什么，传纯标量。

    add_done_task(session_id, function_name, is_stream)
    logger.info(
        f"[{NODE_NAME}] [{function_name}] 处理结束："
        f"item_names={updates.get('item_names')}，"
        f"是否直接出答案={bool(updates.get('answer'))}，"
        f"是否要问用户={bool(updates.get('need_confirm'))}"
    )
    return updates


if __name__ == '__main__':
    """
    本地测试：验证三个分支

    前置：Milvus 与 MongoDB 已启动
    """
    import time

    from app.query_process.agent.state import create_query_default_state
    from app.utils.task_utils import clear_task

    # 构造一个库里不存在的会话，确保历史为空，测试结果可预期
    test_session = f"confirm_test_{int(time.time())}"

    cases = [
        ("分支A 精确命中", "Brother HAK 180 烫金机怎么用？"),
        ("分支A 部分命中", "万用表怎么测量电压？"),
        ("分支B 模糊候选", "HAK180 怎么安装烫金膜盒？"),
        ("分支C 查无此人", "小米15 的电池怎么换？"),
    ]

    for label, query in cases:
        logger.info("=" * 70)
        logger.info(f"[测试] {label}：{query}")
        st = create_query_default_state(
            session_id=test_session + "_" + label[:4],
            original_query=query,
            is_stream=False,
        )
        try:
            result = node_item_name_confirm(st)
            logger.info(f"[测试] 产品名: {result.get('item_names')}")
            logger.info(f"[测试] 改写后: {result.get('rewritten_query')!r}")
            if result.get("answer"):
                logger.info(f"[测试] 直接答复: {result['answer']}")
        except Exception as e:
            logger.error(f"[测试] 执行失败：{e}", exc_info=True)
        finally:
            clear_task(st["session_id"])

    logger.info("=" * 70)
    logger.info("[测试] 内容审核拒绝：应短路给用户一句人话，不检索也不转问用户")

    class _FakeBadRequest(Exception):
        """模拟 DashScope 的 data_inspection_failed（报错体照抄真实响应）"""

        def __init__(self):
            super().__init__("400 - {'error': {'code': 'data_inspection_failed'}}")
            self.status_code = 400
            self.body = {"error": {"type": "data_inspection_failed",
                                   "code": "data_inspection_failed"}}

    class _FakeLLM:
        def invoke(self, messages):
            raise _FakeBadRequest()

    # 打桩替掉模块级名字，不真调模型；用完还原
    _real_get_llm_client = get_llm_client
    get_llm_client = lambda **kw: _FakeLLM()
    st = create_query_default_state(
        session_id=test_session + "_拒答", original_query="随便问一个", is_stream=False)
    try:
        result = node_item_name_confirm(st)
        if result.get("answer") != CONTENT_REJECTED_ANSWER:
            logger.error("[测试] [FAIL] 审核拒绝没有短路成一句人话")
        elif result.get("need_confirm") or result.get("item_names"):
            logger.error("[测试] [FAIL] 审核拒绝不该去检索、也不该转问用户")
        else:
            # 本节点**不该**存档助手消息：答案归 node_answer_output 存。
            # 两边都存会在历史里留下两条一样的助手消息（2026-10-06 实测踩到）
            from app.clients.mongo_history_utils import get_recent_messages
            msgs = get_recent_messages(st["session_id"], limit=10)
            dup = [m for m in msgs if m.get("role") == "assistant"]
            if dup:
                logger.error(f"[测试] [FAIL] 确认节点不该存助手消息，实际存了 {len(dup)} 条")
            else:
                logger.success("[测试] [PASS] 审核拒绝已短路给用户一句人话，且没重复存助手消息")
    except Exception as e:
        # 2026-10-06 的真实事故就发生在这里：degrade 的日志自己抛 KeyError，
        # 把「被审核拒绝」顶成了「检索图执行失败："'error'"」
        logger.error(f"[测试] [FAIL] 审核拒绝路径抛异常：{e}", exc_info=True)
    finally:
        get_llm_client = _real_get_llm_client
        clear_task(st["session_id"])

    logger.info("=" * 70)
    logger.info("[测试] 全部用例执行完毕")
