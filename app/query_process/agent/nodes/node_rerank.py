"""
重排序节点 (node_rerank)

作用：把本地知识库切片与联网搜索结果合并后统一打分重排，再用动态 Top-K 截断，
输出 reranked_docs 供答案生成使用。

三步走（与教程一致）：
1. step_1_merge_docs   —— 合并两路异构结果，统一成 {text, title, source, ...}
2. step_2_rerank_docs  —— 调重排模型给「问题-文档」对打分
3. step_3_topk         —— 按分数断崖动态截断

与教程的差异：打分改用 DashScope 重排 API（教程是本地 BGE），见 app/lm/reranker_utils.py。
"""
import sys

from app.core.error_policy import degrade
from app.core.logger import logger
from app.lm.reranker_utils import rerank
from app.query_process.agent.state import QueryGraphState
from app.utils.task_utils import add_running_task, add_done_task

# 节点名，与 main_graph.py 中 add_node 注册的名称保持一致，用于日志前缀
NODE_NAME = "node_rerank"

# -----------------------------
# 动态 TopK 参数
# -----------------------------
# 硬上限：最多取前 N 条
RERANK_MAX_TOPK = 10
# 硬下限：至少保留前 N 条。
# 教程取 1，但实测出现过「首尾分差过大导致只剩 1 条上下文」的情况，
# 而答案生成只有一条切片支撑显然不够，故抬到 3。
RERANK_MIN_TOPK = 3
# 断崖阈值：相邻分数的相对跌幅超过它就截断。
#
# 教程还配了个绝对阈值 GAP_ABS=0.5，但那是按本地 BGE 的**无界 logits** 定的；
# 本项目改用的 DashScope gte-rerank-v2 返回 **0~1 归一化分数**（实测相邻最大落差
# 仅约 0.14），绝对阈值永远不会触发，是死参数，因此这里直接去掉，只保留相对阈值。
RERANK_GAP_RATIO = 0.25

# -----------------------------
# 知识库配额
# -----------------------------
# 最终 Top-K 里至少给**本地切片**留几个位。
#
# 为什么需要（实测，见 HANDOFF §3.18）：「Brother HAK 180 烫金机长什么样」那一轮，
# 本地那条「HAK 180设备外观示意图」重排得 0.3384，5 条联网新闻得 0.6760~0.8886 ——
# **本地 0 条进最终 Top-K**，答案 100% 由百度新闻拼出来，而知识库里明明有那张图。
# 重排模型对「自然语言提问 vs 新闻散文」天然给高分，对说明书那种短促、带图注的切片
# 给低分；**这不是阈值卡掉的**，放宽截断也轮不到本地。
#
# 配**硬配额**而不是给本地加权：语义上「这个领域里知识库是权威，联网只是补充」，
# 该表达成「保证留位」而不是「稍微加点分」—— 靠加分翻不过 2 倍的差距。
LOCAL_MIN_SLOTS = 2
# 但本地也不能乱塞：低于这个门槛的宁可不占位。
# 实测「打印质量不清晰怎么办？」本地最佳只有 0.1808（说明书里确实没对应内容），
# 那种情况留给联网是对的。
# 0.25 是照这批实测分定的：该救回来的（外观图 0.3384、产品简介 0.8090）都在它之上，
# 该挡掉的（0.1808 / 0.1605 / 0.1227 / 0.0059）都在它之下。
LOCAL_MIN_SCORE = 0.25


def step_1_merge_docs(state: QueryGraphState) -> list:
    """
    阶段一：合并本地切片与联网结果

    两路字段结构完全不同（本地是 Milvus 切片实体，联网是标题+摘要+链接），
    统一成同一格式，其中 text 是后续打分的依据。
    """
    rrf_docs = state.get("rrf_chunks") or []
    web_docs = state.get("web_search_docs") or []
    logger.info(
        f"[{NODE_NAME}] 合并输入：本地 RRF {len(rrf_docs)} 条，联网 {len(web_docs)} 条"
    )

    doc_items = []

    # 本地切片：text 取 content
    for i, doc in enumerate(rrf_docs):
        if not isinstance(doc, dict):
            logger.warning(f"[{NODE_NAME}] 跳过结构异常的本地文档(index={i})：{type(doc).__name__}")
            continue
        content = (doc.get("content") or "").strip()
        if not content:
            continue
        doc_id = doc.get("chunk_id") or doc.get("id")
        doc_items.append({
            "text": content,
            "doc_id": doc_id,
            "chunk_id": doc_id,
            "title": doc.get("title") or doc.get("item_name") or "",
            "url": "",
            "source": "local",
        })

    # 联网结果：text 取 snippet，天然没有切片主键
    for i, doc in enumerate(web_docs):
        if not isinstance(doc, dict):
            logger.warning(f"[{NODE_NAME}] 跳过结构异常的联网结果(index={i})：{type(doc).__name__}")
            continue
        text = (doc.get("snippet") or doc.get("content") or "").strip()
        if not text:
            continue
        doc_items.append({
            "text": text,
            "doc_id": None,
            "chunk_id": None,
            "title": (doc.get("title") or "").strip(),
            "url": (doc.get("url") or "").strip(),
            "source": "web",
        })

    logger.info(f"[{NODE_NAME}] 合并完成，共 {len(doc_items)} 条待打分")
    return doc_items


def step_2_rerank_docs(state: QueryGraphState, doc_items: list) -> list:
    """
    阶段二：调重排模型打分并按分数降序

    重排失败时降级为原始顺序（分数记 0），保证链路不中断。
    """
    question = state.get("rewritten_query") or state.get("original_query") or ""
    if not doc_items or not question:
        logger.warning(f"[{NODE_NAME}] 无文档或无问题，跳过重排")
        return []

    texts = [d["text"] for d in doc_items]
    try:
        scored = rerank(question, texts)
    except Exception as e:
        # 重排失败降级为原始顺序（分数记 0），保证链路不中断
        return degrade(NODE_NAME, "重排打分", [{**d, "score": 0.0} for d in doc_items], e)

    # 接口按 index 指回入参下标，这里还原成完整文档
    out = [{**doc_items[s["index"]], "score": s["score"]} for s in scored]
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def step_3_topk(scored_docs: list) -> list:
    """
    阶段三：动态 Top-K

    不用机械的「取前 N 条」，而是在 [MIN_TOPK, MAX_TOPK] 区间内找分数断崖：
    相邻两条落差过大说明相关性骤降，就在那里截断，避免低分文档混入候选。
    """
    if not scored_docs:
        return []

    max_topk = min(RERANK_MAX_TOPK, len(scored_docs))
    topk = max_topk  # 没触发断崖就取满上限

    if topk > RERANK_MIN_TOPK:
        # 从 MIN_TOPK 之后开始探测相邻落差（索引从 0 起，故起点为 MIN_TOPK-1），
        # 这样 MIN_TOPK 就是硬地板，断崖再陡也不会把上下文截到它以下
        for i in range(RERANK_MIN_TOPK - 1, max_topk - 1):
            s1 = scored_docs[i].get("score") or 0.0
            s2 = scored_docs[i + 1].get("score") or 0.0
            gap = s1 - s2  # 已降序，gap 恒 >= 0
            rel = gap / (abs(s1) + 1e-6)  # 1e-6 防除零
            if rel >= RERANK_GAP_RATIO:
                logger.info(
                    f"[{NODE_NAME}] 触发断崖截断 @ 第 {i + 1} 条 "
                    f"(score {s1:.4f} -> {s2:.4f}, gap={gap:.4f}, rel={rel:.3f})"
                )
                topk = i + 1
                break

    return _apply_local_quota(scored_docs[:topk], scored_docs[topk:])


def _apply_local_quota(picked: list, remaining: list) -> list:
    """
    给本地切片保底：最终结果里本地不足 `LOCAL_MIN_SLOTS` 条时，
    把**分数够线**（≥ `LOCAL_MIN_SCORE`）的最佳本地切片补进来，
    替换掉分数最低的联网条目（本地已经不多了，就别再拿本地开刀）。

    :param picked: 断崖截断后选中的文档（已按分降序）
    :param remaining: 被截掉的那部分（同样已按分降序），从这里捞本地切片
    :return: 补过配额后的文档列表（仍按分降序）
    """
    n_local = sum(1 for d in picked if d.get("source") == "local")
    if n_local >= LOCAL_MIN_SLOTS:
        return picked

    # 够线的最佳本地切片（remaining 已降序，取前几条就是最像的）
    rescue = [d for d in remaining
              if d.get("source") == "local" and (d.get("score") or 0.0) >= LOCAL_MIN_SCORE]
    if not rescue:
        return picked
    rescue = rescue[:LOCAL_MIN_SLOTS - n_local]

    out = list(picked)
    for r in rescue:
        victims = [i for i, d in enumerate(out) if d.get("source") == "web"]
        if victims:
            # 让出分数最低的那条联网结果
            lowest = min(victims, key=lambda i: out[i].get("score") or 0.0)
            out[lowest] = r
        else:
            # 没有联网条目可让位（比如联网整个被关掉了）：直接追加，不替换任何东西
            out.append(r)

    out.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
    logger.info(
        f"[{NODE_NAME}] 知识库配额：本地原 {n_local} 条不足 {LOCAL_MIN_SLOTS}，"
        f"补入 {len(rescue)} 条本地切片"
    )
    return out


def node_rerank(state: QueryGraphState) -> QueryGraphState:
    """
    节点: 重排序 (node_rerank)

    :param state: 需包含 session_id，以及 rrf_chunks / web_search_docs 至少一路
    :return: {"reranked_docs": [带 score 的文档]}；无有效输入返回空列表
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{NODE_NAME}] [{function_name}] 开始处理")
    add_running_task(state["session_id"], function_name, state.get("is_stream"))

    try:
        doc_items = step_1_merge_docs(state)
        scored_docs = step_2_rerank_docs(state, doc_items)
        topk_docs = step_3_topk(scored_docs)

        logger.info(f"[{NODE_NAME}] [{function_name}] 最终输出 {len(topk_docs)} 条")
        for rank, d in enumerate(topk_docs, 1):
            logger.info(
                f"[{NODE_NAME}] [{function_name}]   {rank}. "
                f"[{d['source']}] score={d['score']:.4f} "
                f"{d['title'][:36]!r}"
            )
        return {"reranked_docs": topk_docs}

    except Exception as e:
        # 重排失败不中断链路：返回空结果，答案生成会拿到空上下文
        return degrade(NODE_NAME, "重排序", {"reranked_docs": []}, e)
    finally:
        add_done_task(state["session_id"], function_name, state.get("is_stream"))
        logger.info(f"[{NODE_NAME}] [{function_name}] 处理结束")


def _check_local_quota() -> list:
    """
    离线自测：知识库配额（纯逻辑，不调任何接口）

    用的是「Brother HAK 180 烫金机长什么样」那一轮的**真实分数**：
    5 条联网新闻 0.8886 / 0.8025 / 0.7577 / 0.7214 / 0.6760，
    而本地那条「HAK 180设备外观示意图」只有 0.3384、排在后面 ——
    改之前这一轮最终 5 条**全是联网**，知识库里那张外观图一条都没进来。

    :return: 问题描述列表，空表示通过
    """
    problems = []

    # 1. 该救的要救回来：分数够线的本地切片必须进最终结果
    web = [{"source": "web", "score": s, "title": f"新闻{i}"}
           for i, s in enumerate([0.8886, 0.8025, 0.7577, 0.7214, 0.6760], 1)]
    local_good = {"source": "local", "score": 0.3384, "title": "HAK 180设备外观示意图"}
    local_junk = {"source": "local", "score": 0.0059, "title": "(Start) （启动）。"}

    picked = step_3_topk([*web, local_good, local_junk])
    if local_good not in picked:
        problems.append("分数够线的本地切片没被保进来（配额没生效）")
    # 2. 不该救的别硬塞：分数不够线的碎片不能进来
    if local_junk in picked:
        problems.append("分数不够线的本地碎片被硬塞进来了（门槛没生效）")
    if len(picked) != 5:
        problems.append(f"补配额后条数变了：{len(picked)}，应仍是 5")
    if [d["score"] for d in picked] != sorted((d["score"] for d in picked), reverse=True):
        problems.append("补配额后没有保持按分降序")

    # 3. 本地本来就够时，一个字都不该改
    base = [{"source": "local", "score": 0.9}, {"source": "local", "score": 0.8},
            {"source": "web", "score": 0.7}, {"source": "web", "score": 0.6},
            {"source": "web", "score": 0.5}]
    if step_3_topk(list(base)) != base:
        problems.append("本地已经够 2 条时不该改动结果")

    # 4. 联网整路关掉（全是本地）时，没有可让位的联网条目，不该少条
    all_local = [{"source": "local", "score": 0.9}, {"source": "local", "score": 0.8},
                 {"source": "local", "score": 0.7}]
    if len(step_3_topk(list(all_local))) != 3:
        problems.append("全本地时条数被改了")
    return problems


if __name__ == '__main__':
    """
    本地测试：用伪造的两路结果走完整流程（会真实调用重排接口）

    重点验证：两路都进了合并、输出按分数降序、不超过硬上限、本地与联网都在结果里。
    """
    from app.query_process.agent.state import create_query_default_state
    from app.utils.task_utils import clear_task

    logger.info("=" * 70)
    logger.info("[测试] 知识库配额（离线纯逻辑，不调接口）")
    quota_problems = _check_local_quota()
    for p in quota_problems:
        logger.error(f"[测试] [FAIL] {p}")
    if not quota_problems:
        logger.success("[测试] [PASS] 配额把够线的本地切片保住了、把碎片挡住了")
    logger.info("=" * 70)

    rrf_chunks = [
        {"chunk_id": 101, "title": "## 装入全幅烫金膜盒",
         "content": "打开烫金膜盒支架盖，将烫金膜盒装入并向下轻推，直到它锁定到位。"},
        {"chunk_id": 102, "title": "## 更换电池",
         "content": "关闭设备并拔掉电源适配器，卸下后盖后更换电池。"},
        {"chunk_id": 103, "title": "## 使用设备",
         "content": "使用设备前确保已准备好所需材料并阅读安全须知。"},
    ]
    web_search_docs = [
        {"title": "HAK180 快速设置指导手册", "url": "https://example.com/a",
         "snippet": "烫金膜盒的安装步骤：先打开支架盖，再沿导轨推入并压紧。"},
        {"title": "烫金机保养常识", "url": "https://example.com/b",
         "snippet": "每日需检查电源与润滑部位，定期清理膜屑。"},
    ]

    session_id = "rerank_test"
    st = create_query_default_state(
        session_id=session_id,
        original_query="HAK 180 烫金机怎么安装烫金膜盒？",
        rewritten_query="HAK 180 烫金机怎么安装烫金膜盒？",
        is_stream=False,
        rrf_chunks=rrf_chunks,
        web_search_docs=web_search_docs,
    )

    try:
        # 合并阶段单独验证：动态 TopK 会截断，不能拿最终输出去判断合并有没有丢源
        merged = step_1_merge_docs(st)
        merged_sources = {d["source"] for d in merged}
        logger.info(f"[测试] 合并阶段 {len(merged)} 条，来源={merged_sources}")

        result = node_rerank(st)
        got = result.get("reranked_docs") or []
        logger.info(f"[测试] 最终输出 {len(got)} 条")
        for rank, d in enumerate(got, 1):
            logger.info(f"[测试]   {rank}. [{d['source']}] {d['score']:.4f} {d['title'][:32]!r}")

        problems = []
        if len(merged) != len(rrf_chunks) + len(web_search_docs):
            problems.append(f"合并条数不符：{len(merged)} != {len(rrf_chunks) + len(web_search_docs)}")
        if merged_sources != {"local", "web"}:
            problems.append(f"合并阶段丢失了某一路结果，来源={merged_sources}")

        if not got:
            problems.append("没有输出任何文档")
        if len(got) > RERANK_MAX_TOPK:
            problems.append(f"超过硬上限：{len(got)} > {RERANK_MAX_TOPK}")
        scores = [d["score"] for d in got]
        if scores != sorted(scores, reverse=True):
            problems.append("输出未按分数降序")
        # 每条结果的 text 都应能在原始输入里找到出处
        known_texts = {d["content"] for d in rrf_chunks} | {d["snippet"] for d in web_search_docs}
        if any(d["text"] not in known_texts for d in got):
            problems.append("存在 text 来源不明的结果")

        for p in problems:
            logger.error(f"[测试] [FAIL] {p}")
        if not problems:
            logger.success("[测试] [PASS] 重排节点验证通过")
    except Exception as e:
        logger.error(f"[测试] [FAIL] 执行失败：{e}", exc_info=True)
    finally:
        clear_task(session_id)
