"""
产品确认节点 (node_item_name_confirm)

节点作用：判断用户问的是**哪个产品**，以便定位到对应手册。

流程（4 步，2026-11-16 实体链接改造）：
1. LLM 只做 query 改写（代词消解），不再让模型从 catalog 里挑产品名
2. 产品身份交给 product_resolver：别名/型号/部件映射 + BM25 候选
3. resolved:
   - confirmed → 写入 state，继续检索
   - candidates → 候选卡片，need_confirm，本轮结束
   - none → 固定 NO_MATCH_ANSWER，按 no_match 结束
   - confirmed 但缺规格（如烫金膜盒的全幅/半幅）→ 询问规格
4. 证据检索用 product_id 过滤，不再让 LLM 猜 item_names。

**子图不读写会话历史**（无状态化收尾，2026-10-12）：历史归主 agent，
本轮上下文由主 agent 在 task 描述里携带。
"""
import json
import re
import sys
import unicodedata
from typing import Any, Dict, List

from langchain.messages import HumanMessage, SystemMessage

from app.clients.milvus_clients import dense_search, get_milvus_client
from app.conf.milvus_config import milvus_config
from app.core.load_prompt import load_prompt
from app.core.error_policy import (
    CONTENT_REJECTED_ANSWER,
    ErrorKind,
    degrade,
    degrade_dependency,
    is_content_rejected,
)
from app.core.input_guard import INPUT_GUARD_ANSWER, check_user_input
from app.core.retry import invoke_with_retry
from app.core.logger import logger
from app.conf.pipeline_config import pipeline_config
from app.lm.embedding_utils import generate_embeddings
from app.lm.lm_utils import get_llm_client
from app.agent.subagents.product_kb.domain.outcome import SERVICE_UNAVAILABLE_ANSWER
from app.agent.subagents.product_kb.domain.state import QueryGraphState
from app.agent.subagents.product_kb.application.product_resolver import resolve_product
from app.utils.task_utils import add_running_task, add_done_task

# 节点名，与 main_graph.py 中 add_node 注册的名称保持一致，用于日志前缀
NODE_NAME = "node_item_name_confirm"

# --- 配置参数 ---
# 相似度阈值 / 候选数 / 检索条数等调参集中在 app/conf/pipeline_config.py（.env 可覆盖）：
#   confirm_score_threshold（≥ 确认）、candidate_score_threshold（≥ 候选）、
#   max_candidate_options、max_near_options、item_search_limit

# 认不出产品时的提示语（有候选时由调用方展示给用户）
CLARIFY_QUESTION = "「{query}」没能锁定到具体产品，你想问的是下面哪一个？"
# 连近似候选都没有时的固定答复：未知产品直接按 no_match 结束，不再伪装成「等用户确认」
NO_MATCH_ANSWER = "抱歉，没有找到与该问题匹配的产品资料。你可以确认一下产品名称或型号，或换个说法再问。"


# 型号识别：字母开头 + 2~4 位数字（`HAK 180` / `Z35` / `hl3040` / `RS-12` / `CB304n`）。
# 纯中文产品名（如只写「烫金机」）不在守卫范围，那部分交给 prompt 约束。
_MODEL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,6}[\s\-]?\d{2,4}[A-Za-z0-9]*(?![A-Za-z0-9])")


def _model_tokens(text: str) -> List[str]:
    """抽出文本里出现的产品型号（只认字母+数字形态，避免把普通词当型号）"""
    return [m.group(0).strip() for m in _MODEL_TOKEN_RE.finditer(text or "")]


def _keep_history_subject(rewritten_query: str, query: str, history_text: str) -> str:
    """
    改写结果丢了历史里的产品型号时，把型号原样补回句首（纯函数，离线可测）

    规则（三条，任一不满足就不改）：
    1. 当前问题自己写了型号 → 不干预（用户显式指定的产品优先，见 D21）
    2. 历史里没有型号 → 不干预（没有可搬的东西）
    3. 改写结果已含历史型号 → 不干预（模型自己保住了）

    :return: 处理后的改写问题；只做「从历史原文搬运型号」，不猜产品、不补品牌
    """
    rewritten = (rewritten_query or "").strip()
    if not rewritten:
        return rewritten
    if _model_tokens(query):
        return rewritten
    history_models = _model_tokens(history_text)
    if not history_models:
        return rewritten
    norm = lambda t: unicodedata.normalize("NFKC", t).casefold()
    if {norm(t) for t in _model_tokens(rewritten)} & {norm(t) for t in history_models}:
        return rewritten
    # 历史里最后一次出现的型号最贴近当前轮
    return f"{history_models[-1]} {rewritten}"


def _history_product_subject(history_product: Dict[str, Any]) -> str:
    """结构化历史产品主体的检索用写法：优先型号，其次标准名（纯中文名也可用）"""
    if not isinstance(history_product, dict):
        return ""
    return str(history_product.get("target_model")
               or history_product.get("item_name") or "").strip()


def _with_history_product(rewritten_query: str, history_product: Dict[str, Any],
                          current_status: str = "none") -> str:
    """
    当前问题没有锁定产品时，把上一轮的真实产品主体前置到检索问题。

    只在当前问题 `resolve_product` 返回 none 时生效；当前问题已确认产品或出现候选，
    说明用户可能在问新产品/存在歧义，不能拿历史产品覆盖。
    """
    if current_status != "none":
        return rewritten_query
    subject = _history_product_subject(history_product)
    if not subject:
        return rewritten_query
    if subject.casefold() in (rewritten_query or "").casefold():
        return rewritten_query
    return f"{subject} {rewritten_query}"


def _history_subject_hint(rewritten_query: str, query: str, history_text: str) -> str:
    """
    当前问题没有型号时，从主 agent 的任务描述里补回历史产品型号。

    与 `_keep_history_subject` 的区别：这里**不要求**出现代词或改写发生变化，
    覆盖「怎么进行烫金」这类省略主语的续问。只在当前问题自身没有型号时生效；
    用户显式写了型号就一律以当前问题为准。
    """
    if _model_tokens(rewritten_query):
        return rewritten_query
    return _keep_history_subject(rewritten_query, query, history_text)


def _rewrite_resolution_wins(original: Dict[str, Any], rewritten: Dict[str, Any]) -> bool:
    """
    改写结果是否该覆盖原话的解析结果（纯函数，离线可测）

    只在两种情况同时成立时为真：原话没锁到型号（部件映射/模糊/无命中），
    且改写结果给出**显式型号命中**。原话已经是型号命中时一律保留原话 ——
    用户说的产品不能被模型改写出来的产品替换（D21、D28）。
    """
    if original.get("provenance") == "model":
        return False
    return rewritten.get("provenance") == "model" and rewritten.get("status") == "confirmed"


def step_3_rewrite_query(query: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    步骤 3: 只做 query 改写（代词消解）

    产品身份不在这里决定：LLM 不再看 catalog、也不再输出 item_names。
    下游由 product_resolver 用别名/型号/部件映射 + BM25 做实体链接。

    :return: {"rewritten_query": "..."}；失败时回退为原问题。
        被模型服务内容审核拒绝时额外带 `"rejected": True`，由入口决定短路
    """
    function_name = sys._getframe().f_code.co_name
    fallback = {"rewritten_query": query}

    # 拼历史为「角色: 内容」的文本，供 LLM 做指代消解
    history_text = "".join(f"{m.get('role', '')}: {m.get('text', '')}\n" for m in history)

    try:
        # temperature=0：改写结果是检索输入，同一问题必须稳定
        client = get_llm_client(json_mode=True, temperature=0)
        prompt = load_prompt("rewrite_query", history_text=history_text, query=query)
        messages = [
            SystemMessage(content=load_prompt("rewrite_query_system").strip()),
            HumanMessage(content=prompt),
        ]
        response = invoke_with_retry(client, messages, "改写问题")
        content = (response.content or "").strip()

        # 兜底：部分模型仍会用 ```json 包裹
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()

        result = json.loads(content)
        if not result.get("rewritten_query"):
            result["rewritten_query"] = query
        # 规则守卫：模型丢了历史里的产品型号时补回来（多轮指代最常见的失败，
        # 丢了型号就会退化成部件名，下游只能反问型号）
        kept = _keep_history_subject(result.get("rewritten_query") or "", query, history_text)
        if kept != result.get("rewritten_query"):
            logger.warning(
                f"[{NODE_NAME}] [{function_name}] 改写丢了历史产品主体，已补回："
                f"{result.get('rewritten_query')!r} → {kept!r}"
            )
        result["rewritten_query"] = kept

        logger.info(
            f"[{NODE_NAME}] [{function_name}] LLM改写完成："
            f"rewritten_query={result['rewritten_query']!r}"
        )
        return result

    except Exception as e:
        # LLM 失败或 JSON 解析失败都退化为原问题，保证流程不中断。
        # 内容审核拒绝要多带一个 `rejected` 标记：它不是「改写失败」而是模型服务合规拒答，
        # 入口看到就短路给用户一句人话。
        result = degrade(NODE_NAME, "LLM 改写问题", fallback, e)
        if is_content_rejected(e):
            return {**result, "rejected": True}
        return result


def step_4_vectorize_and_query(item_names: List[str]) -> List[Dict[str, Any]]:
    """
    步骤 4: 把提取出的产品名向量化，在 kb_item_names 中检索库里的标准产品名

    批量生成向量以减少 API 调用；逐个检索以保证结果与产品名一一对应。
    kb_item_names 只存稠密向量，故走 dense_search；正文切片才用混合检索。

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
                limit=pipeline_config.item_search_limit, output_fields=["item_name", "file_title"],
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
        high = [m for m in matches if (m.get("score") or 0) >= pipeline_config.confirm_score_threshold]
        mid = [m for m in matches if (m.get("score") or 0) >= pipeline_config.candidate_score_threshold]

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
            candidates.extend(mid[:pipeline_config.max_candidate_options])
        # 规则 d：连候选都没有 —— 取分最低线以下、排名靠前的几个当「近似」，
        # 让用户至少有点可选的，而不是只给一句「没找到」
        else:
            low = [m for m in matches if (m.get("score") or 0) < pipeline_config.candidate_score_threshold]
            near.extend(low[:pipeline_config.max_near_options])

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


# ──────────────────────── 字面匹配（字面优先，向量兜底）────────────────────────
# 由来（2026-10-11，见 evolution-plan §4 M3 修复）：用户确认产品后，主 agent 一字不差
# 回灌标准名，向量相似度却反复卡在 0.6~0.85 区间，形成「确认 → 又候选 → 再问」的循环。
# 库里的标准名本就只有个位数~几十条 —— 这一层不需要向量，「规范化字符串匹配」更准更稳。
# 规则取业界名字匹配的标准三板斧（rapidfuzz token_set / 电商 SKU 匹配的确定性版本），
# **模糊部分不叠 fuzzy 库**：向量层本身就是模糊匹配器，规则管「准」、向量管「糊」。

# 规范化：NFKC（全角→半角）+ casefold + 去掉所有非字母数字与汉字的字符
_NORM_STRIP_RE = re.compile(r"[^0-9a-z一-鿿]+")
# token 切分：连续字母数字 或 连续汉字（中英混合串靠它拆）
_TOKEN_RE = re.compile(r"[0-9a-z]+|[一-鿿]+")
# 直通的最小长度（规范化后，按字符数）——「烫金机」「键盘」这类泛词不直通，
# 交给向量 + 用户确认兜底，防误伤
_LITERAL_MIN_CHARS = 4
# 「特征 token」的最小长度：含数字的型号类 token（r9000p / gs3104t / er2100）才够格当
# 连接点；单个数字（「15」）不算特征，太容易误伤
_FEATURE_MIN_CHARS = 4


def _norm_name(s: str) -> str:
    """规范化：NFKC（全角→半角）→ casefold → 去分隔符（空格 / 连字符 / 下划线等）"""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return _NORM_STRIP_RE.sub("", s)


def _name_tokens(s: str) -> set:
    """token 化：连续字母数字一段、连续汉字一段（顺序无关比较用）"""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return set(_TOKEN_RE.findall(s))


def _feature_tokens(s: str) -> set:
    """提取名里的**特征型号 token**：含数字、长度达标的字母数字 token

    由来（2026-10-14，用户实测）：中文品牌写法对不上英文标准名时
    （「联想拯救者R9000P」vs「Lenovo Legion Y9000P IRX8 和 Legion R9000P ARX8」），
    型号是两边唯一可靠的连接点 —— `r9000p` 原样在名字里，但 token 集合包含
    要求**全部** token 命中，「联想拯救者」对不上就把整条否决了；向量也只有 0.55。
    """
    return {t for t in _name_tokens(s)
            if len(t) >= _FEATURE_MIN_CHARS and any(c.isdigit() for c in t)}


def _match_one(extracted: str, std_names: List[str]) -> str:
    """
    在全部标准名里给一个提取名找**唯一**的确定性命中；命中不了或不够唯一返回空串

    规则按置信度分级（命中即取最高级）：
      3 精确：规范化后完全相等（回灌标准名原文走这条）
      2 包含：一方是另一方的子串（`GS3104T-PRO` ⊂ `GANSS GS3104T-PRO 机械键盘`）
      1 token 集合包含：提取名的 token 都是标准名的 token（顺序无关，
        `RS-12 万用表` ↔ `万用表RS-12` 靠这条）；
        **或** 特征型号 token 命中：含数字的型号类 token（r9000p…）在标准名的
        token 里原样出现 —— 中文品牌写法对不上英文名时的连接点
    约束：规范化后长度 ≥ `_LITERAL_MIN_CHARS`；**同级多命中视为不唯一**（返回空，交回向量/用户）
    """
    ne = _norm_name(extracted)
    if len(ne) < _LITERAL_MIN_CHARS:
        return ""
    ne_tokens = _name_tokens(extracted)
    feats = _feature_tokens(extracted)
    best_level, hits = 0, []
    for sn in std_names:
        ns = _norm_name(sn)
        if ne == ns:
            level = 3
        elif ne in ns or ns in ne:
            level = 2
        elif ne_tokens and ne_tokens <= _name_tokens(sn):
            level = 1
        elif feats and (feats & _name_tokens(sn)):
            level = 1
        else:
            level = 0
        if level > best_level:
            best_level, hits = level, [sn]
        elif level == best_level and level > 0:
            hits.append(sn)
    if best_level == 0 or len(hits) != 1:
        return ""
    return hits[0]


def _load_std_names() -> List[str]:
    """全量读 kb_item_names 的标准名（量小，现拉现用——导入新文档立即生效，不做缓存）"""
    try:
        client = get_milvus_client()
        if client is None:
            return []
        rows = client.query(collection_name=milvus_config.item_name_collection,
                            filter="", output_fields=["item_name"], limit=1000)
        return [r.get("item_name") for r in rows if r.get("item_name")]
    except Exception as e:
        degrade(NODE_NAME, "字面匹配读取标准名", None, e)
        return []


def _literal_match(item_names: List[str]) -> tuple:
    """
    对提取出的产品名做字面匹配；返回 (align 形状的命中结果, 未命中、需转向量的名字列表)

    命中 `corrections` 供 step_6 把 rewritten_query 里的写法替换成标准名 ——
    下游的 item_name 过滤与 HyDE 都依赖标准名（对不上就是 0 条切片的静默归零）。
    """
    names = _load_std_names()
    if not names:
        return ({"confirmed_item_names": [], "candidates": [], "near": [],
                 "corrections": {}}, list(item_names))
    confirmed, corrections, remaining = [], {}, []
    for name in item_names:
        hit = _match_one(name, names)
        if hit:
            confirmed.append(hit)
            if hit != name:
                corrections[name] = hit
        else:
            remaining.append(name)
    logger.info(
        f"[{NODE_NAME}] 字面匹配：命中={confirmed}，转向量={remaining}"
    )
    return ({"confirmed_item_names": confirmed, "candidates": [], "near": [],
             "corrections": corrections}, remaining)


def step_6_check_confirmation(
    align_result: Dict[str, Any],
    session_id: str,
    rewritten_query: str,
    original_query: str,
) -> Dict[str, Any]:
    """
    步骤 6: 按对齐结果决定流程走向

    分支 A 有确认产品：回填 state，继续检索
    分支 B 有候选：置 `need_confirm`、把候选写进 `state['clarify']`，**本轮就此结束**
    —— 由调用方去问用户（无状态化后不再图内中断）。
    分支 C 连候选都没有：写固定 `NO_MATCH_ANSWER` 结束，按 no_match 处理 ——
    未知产品不该伪装成「等用户确认」，否则 outcome 会误判成 waiting_user。

    另外修正一处不一致：step3 的 rewritten_query 是基于 LLM 当时提取的名字写的，
    若 step5 把名字对齐成了标准名（如 "HAK" → "Brother HAK 180 烫金机"），
    就把改写问题里的旧名换成标准名，避免后续检索用错名字。

    :return: 供 LangGraph 合并进 state 的字段字典
    """
    function_name = sys._getframe().f_code.co_name
    status = align_result.get("status") or "none"
    confirmed = align_result.get("confirmed_item_names") or []
    manual_id = align_result.get("manual_id") or ""
    target_model = align_result.get("target_model") or ""
    candidates = align_result.get("candidates") or []
    near = align_result.get("near") or []
    corrections = align_result.get("corrections") or {}

    # 用对齐后的标准名替换改写问题里的旧名
    for old, new in corrections.items():
        if old in rewritten_query:
            rewritten_query = rewritten_query.replace(old, new)
    if corrections:
        logger.info(f"[{NODE_NAME}] [{function_name}] 改写问题已同步纠正：{rewritten_query!r}")

    # 依赖不可用：没有真正完成实体解析，不能按「目录里没这个产品」结束
    if status == "unavailable":
        logger.warning(
            f"[{NODE_NAME}] [{function_name}] 产品目录不可用，本轮按服务故障结束")
        return {
            "item_names": [],
            "rewritten_query": rewritten_query,
            "answer": SERVICE_UNAVAILABLE_ANSWER,
        }

    # 分支 A：确认了产品，继续走检索；但部件命中的产品可能还缺规格
    # （例如「烫金膜盒」只指向 HAK180，全幅/半幅步骤不同）。先问规格，
    # 不要直接拿通用步骤作答，也不要让模型自己猜。
    variant_options = align_result.get("variant_options") or []
    if confirmed and align_result.get("needs_variant"):
        question = f"您用的是{'还是'.join(variant_options)}？"
        logger.info(f"[{NODE_NAME}] [{function_name}] 产品已确认但缺规格：{confirmed}，"
                    f"询问 {variant_options}")
        return {
            "item_names": [],
            "manual_id": manual_id,
            "target_model": target_model,
            "rewritten_query": rewritten_query,
            "need_confirm": True,
            "clarify": {
                "question": question,
                "options": [],
                "allow_custom": True,
            },
        }

    if confirmed:
        logger.info(f"[{NODE_NAME}] [{function_name}] 分支A：已确认产品 {confirmed}")
        return {
            "item_names": confirmed,
            "manual_id": manual_id,
            "target_model": target_model,
            "rewritten_query": rewritten_query,
            "answer": "",   # 清空，避免残留答案让条件边误判为「已有答案」
        }

    # 分支 C：连候选都没有 —— 未知产品直接结束，按 no_match 处理。
    # 不再置 need_confirm，避免把「查无产品」误记成 waiting_user。
    options = [{**c, "near": False} for c in candidates] + [{**c, "near": True} for c in near]
    if not options:
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 无候选产品，按 no_match 直接结束："
            f"{original_query!r}"
        )
        return {
            "item_names": [],
            "rewritten_query": rewritten_query,
            "answer": NO_MATCH_ANSWER,
        }

    # 分支 B：有候选（≥0.6 排前面，近似项 <0.6 标 near 让前端区别显示）—— 交给调用方问用户
    question = CLARIFY_QUESTION.format(query=original_query)
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

    # 会话历史归主 agent（无状态化收尾，2026-10-12）：子图不读不写 chat_message，
    # 本轮上下文由主 agent 在 task 描述里携带 —— history 恒为空，防未来误用
    history: List[Dict[str, Any]] = []

    # ★ 输入护栏（纵深第二层，见 app/core/input_guard.py；第一层在主 agent 入口）：
    # 问题里像在「给模型下指令」（伪造的 role 结构、`<|...|>` 特殊 token、越狱模板）
    # 就直接拒答 —— **连提取那一次模型调用都不发**。
    # 历史存档归主 agent（无状态化），这里不再写库
    guard_reason = check_user_input(original_query)
    if guard_reason:
        logger.warning(
            f"[{NODE_NAME}] [{function_name}] 输入护栏命中（{guard_reason}），"
            f"跳过全部模型调用、直接拒答"
        )
        guarded: Dict[str, Any] = {
            "item_names": [],
            "rewritten_query": original_query,
            "answer": INPUT_GUARD_ANSWER,
        }
        add_done_task(session_id, function_name, is_stream)
        return guarded

    # 本轮用户原话优先；主 agent 透传的 original_query 可能是带上下文的 task 描述。
    # 产品实体链接只认用户原话，避免把 task 里被推断出的产品当成用户说的。
    user_question = (state.get("user_question") or "").strip() or original_query

    # 3. LLM 只改写问题，不再提取/裁决产品名
    extract_res = step_3_rewrite_query(user_question, history)
    rewritten_query = extract_res.get("rewritten_query") or user_question
    rejected = bool(extract_res.get("rejected"))
    history_product = state.get("history_product") or {}
    current_resolved = None
    # 结构化历史主体优先：主 agent 从真实会话历史解析出的产品，不采信 task 描述的推断。
    if not rejected and history_product and not _model_tokens(rewritten_query):
        current_resolved = resolve_product(user_question, context=original_query)
        hinted = _with_history_product(
            rewritten_query, history_product, current_resolved.get("status", "none"))
        if hinted != rewritten_query:
            logger.warning(
                f"[{NODE_NAME}] [{function_name}] 续问缺产品主体，从历史产品补回："
                f"{rewritten_query!r} → {hinted!r}"
            )
            rewritten_query = hinted
    # 兼容旧路径：主 agent 没给结构化主体时，仍可从 task 描述搬型号（只认型号形态）。
    if (not rejected and not history_product
            and not _model_tokens(rewritten_query)):
        hinted = _history_subject_hint(rewritten_query, user_question, original_query)
        if hinted != rewritten_query:
            logger.warning(
                f"[{NODE_NAME}] [{function_name}] 续问缺产品主体，从任务描述补回："
                f"{rewritten_query!r} → {hinted!r}"
            )
            rewritten_query = hinted

    # 4. 产品身份：别名/型号/部件映射 + BM25，不再走 LLM 选 catalog
    align_result: Dict[str, Any] = {}
    if rejected:
        # 内容审核拒绝：**本轮到此为止**，别再往下检索 —— 检索回来的东西照样递不到用户手里，
        # 生成节点的 LLM 调用会被同样拒一次，最后只剩一顿空答案
        logger.info(f"[{NODE_NAME}] [{function_name}] 提问被模型服务内容审核拒绝，跳过检索")
    else:
        # 规格追问（“全幅/半幅”）需要上一轮上下文兜底，但上下文只在该短回答时参与，
        # 避免把 task 描述里被推断出的产品重新带进来。
        resolved = current_resolved or resolve_product(user_question, context=original_query)
        # 用户原话是代词（「那它怎么装？」）时，原话里根本没有产品可认：
        # 改写结果已按历史补全了主体，只有当它给出**显式型号命中**才采信 —— 型号比
        # 部件映射确定（D21），也比「反问全幅/半幅」更接近用户本意。原话自带型号时不走这条
        # （先判 provenance 再决定要不要多花一次解析，正常提问不会被拖慢）。
        if (resolved.get("status") != "unavailable"
                and resolved.get("provenance") != "model"
                and rewritten_query != user_question):
            retried = resolve_product(rewritten_query, context=original_query)
            if _rewrite_resolution_wins(resolved, retried):
                logger.info(
                    f"[{NODE_NAME}] [{function_name}] 原话无法锁定产品（provenance="
                    f"{resolved.get('provenance')!r}），改用改写结果的显式型号："
                    f"{retried.get('target_model') or retried.get('item_names')}"
                )
                resolved = retried
        align_result = {
            "status": resolved.get("status") or "none",
            "confirmed_item_names": resolved.get("item_names") or [],
            "manual_id": resolved.get("manual_id") or "",
            "target_model": resolved.get("target_model") or "",
            "candidates": resolved.get("candidates") or [],
            "near": resolved.get("near") or [],
            "corrections": {},
            "variant_options": resolved.get("variant_options") or [],
            "needs_variant": bool(resolved.get("needs_variant")),
        }
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 产品解析：status={resolved.get('status')}，"
            f"确认={align_result['confirmed_item_names']}，"
            f"候选={[c.get('item_name') for c in align_result['candidates']]}，"
            f"缺规格={align_result['needs_variant']}"
        )

    # 5. 按解析结果决定分支；被拒绝时不走分支判断，直接置 answer 让条件边短路到输出节点
    if rejected:
        updates: Dict[str, Any] = {
            "item_names": [],
            "rewritten_query": rewritten_query,
            "answer": CONTENT_REJECTED_ANSWER,
        }
    else:
        updates = step_6_check_confirmation(
            align_result, session_id, rewritten_query, user_question,
        )

    # 持久化已随无状态化退役（2026-10-12）：历史归主 agent，子图不写库。
    # 旧注记保留一条教训：Mongo 文档（`_id` 是 ObjectId）**不能塞进 state** ——
    # 检查点走 msgpack 序列化，ObjectId 不是可序列化类型，会话一有历史整轮就报错。
    # 要往下游传什么，只传纯标量。

    add_done_task(session_id, function_name, is_stream)
    logger.info(
        f"[{NODE_NAME}] [{function_name}] 处理结束："
        f"item_names={updates.get('item_names')}，"
        f"是否直接出答案={bool(updates.get('answer'))}，"
        f"是否要问用户={bool(updates.get('need_confirm'))}"
    )
    return updates
