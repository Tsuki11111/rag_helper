"""
答案生成节点 (node_answer_output)

职责：
1. 前置节点已产出 answer（产品名反问 / 拒识 / 内容审核拒绝）时直接输出，跳过 LLM 生成
2. 否则用 `reranked_docs` 作参考内容调大模型生成答案
3. 流式模式下逐块推送 delta，结束后推送 final（含解析出的图片 URL）
4. 答案存档到 MongoDB，供 /history 接口读取

**图片的处理**：`prompts/answer_out.prompt` 要求模型在答案末尾追加一个【图片】区块，
本节点把它拆出来单独作为 `images` 返回（每项含 `url` 与图注），并从正文里去掉
（用户不必看到一堆裸链接）。**图注取自该图的 alt 文本**——那是导入时视觉模型写下的、
描述图里实际内容的文字；alt 为空或是占位（`图片`）时才退回用章节标题。

**只放行参考内容里真实出现过的 URL** —— 模型可能编造或改写链接，
直接透传会让前端显示一排破图。
"""
import re
import sys

from langchain.messages import HumanMessage, SystemMessage

from app.clients.mongo_history_utils import get_recent_messages, save_chat_message
from app.core.error_policy import (
    CONTENT_REJECTED_ANSWER,
    degrade,
    is_content_rejected,
    is_fatal,
)
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.usage_tracker import usage_context
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.state import QueryGraphState
from app.utils.sse_utils import push_to_session, SSEEvent
from app.utils.task_utils import (
    add_done_task,
    add_running_task,
    get_degraded_task_list,
    get_done_task_list,
    is_stop_requested,
)

# 节点名，与 main_graph.py 中 add_node 注册的名称保持一致，用于日志前缀
NODE_NAME = "node_answer_output"

# 带进提示词的历史消息条数
HISTORY_LIMIT = 6
# 单个切片拼进上下文时的上限，防止个别超长切片吃光窗口
MAX_CONTEXT_CHARS_PER_DOC = 1200
# 图片区块标记，与 prompts/answer_out.prompt 里的约定一致
IMAGE_MARKER = "【图片】"
# 从切片正文里抓 Markdown 图片链接：group(1)=alt 文本，group(2)=URL
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
# node_md_img 生成图片描述失败时会写入的占位 alt，这种要退回用章节标题当图注
IMAGE_ALT_PLACEHOLDER = "图片"
# 一片都没检索到时的兜底答复（此时调 LLM 只会让它凭空编）
FALLBACK_ANSWER = "抱歉，没有检索到与该问题相关的内容。可以换个说法，或确认一下产品型号。"

SYSTEM_PROMPT = "你是产品使用文档的问答助手。回答必须严格基于提供的参考内容，不确定就说明没有找到，不要编造。"


def _build_context(docs: list):
    """
    把重排后的切片拼成参考内容，同时记下每个图片 URL 出自哪条切片、图里画的是什么

    图注优先取**视觉模型写的 alt 文本**——导入时 `node_md_img` 已经让多模态模型看过每张图，
    描述的是图里实际有什么（如「打开烫金膜盒支架盖，按箭头方向将烫金膜盒插入支架」）。
    章节标题只作兜底：同一个章节下常有多张不同的图，用标题会导致它们图注一模一样。

    返回的 captions 一举两得：既当**白名单**（放行只在它里面出现过的 URL），又给前端提供**图注**。

    :return: (context 文本, {url: 图注})
    """
    parts, captions = [], {}
    for i, doc in enumerate(docs, 1):
        text = (doc.get("text") or "").strip()
        if not text:
            continue
        # 切片标题形如 "## 3.4.2 装入半幅烫金膜盒"，去掉井号当兜底图注更干净
        title = (doc.get("title") or "").strip().lstrip("#").strip()
        parts.append(f"[{i}] {title}\n{text[:MAX_CONTEXT_CHARS_PER_DOC]}")
        for m in MARKDOWN_IMAGE_RE.finditer(text):
            alt, url = m.group(1).strip(), m.group(2)
            caption = alt if alt and alt != IMAGE_ALT_PLACEHOLDER else title
            captions.setdefault(url, caption)
    return "\n\n".join(parts), captions


def _build_history(session_id: str) -> str:
    """
    把最近几轮对话拼成文本，供模型理解指代

    ⚠️ 读的是 `text` 字段（`save_chat_message` 存的就叫这个名）。
    这里曾写成 `m.get("content")` —— 库里压根没有 `content` 这个键，于是**永远返回「（无）」**：
    不报错、不降级，静默地把多轮上下文全丢了。`prompts/answer_out.prompt` 里那个 `{history}`
    槽一直是被喂「（无）」的。同项目的 `node_item_name_confirm` 读的是 `text`，是对的 ——
    改这里时对照着写。
    """
    function_name = sys._getframe().f_code.co_name
    try:
        msgs = get_recent_messages(session_id, limit=HISTORY_LIMIT)
    except Exception as e:
        logger.warning(f"[{NODE_NAME}] [{function_name}] 读取历史失败，将无历史继续：{e}")
        return "（无）"

    # 本轮问题已由 node_item_name_confirm 存入历史，去掉以免与【用户问题】重复
    if msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1]

    lines = []
    for m in msgs:
        content = (m.get("text") or "").strip()
        if content:
            lines.append(f"{'用户' if m.get('role') == 'user' else '助手'}：{content}")
    return "\n".join(lines) if lines else "（无）"


def _split_images(text: str, captions: dict):
    """
    拆出答案末尾的【图片】区块

    :param captions: {url: 图注}，同时充当白名单——只有参考内容里真实出现过的 URL 才放行
    :return: (去掉图片区块的正文, [{"url", "caption"}])
    """
    function_name = sys._getframe().f_code.co_name
    if IMAGE_MARKER not in text:
        return text.strip(), []

    answer, _, block = text.partition(IMAGE_MARKER)
    seen, kept = set(), []
    for line in block.splitlines():
        url = line.strip().strip("<>").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        if url in captions:
            kept.append({"url": url, "caption": captions[url]})

    dropped = len(seen) - len(kept)
    if dropped:
        logger.warning(
            f"[{NODE_NAME}] [{function_name}] 答案里有 {dropped} 个图片链接不在参考内容中，已过滤"
        )
    return answer.strip(), kept


def _generate(session_id: str, messages: list, is_stream: bool) -> tuple:
    """
    调 LLM 生成答案

    流式模式下逐块推送 delta。**图片区块不推给前端**——它只是给节点解析用的，
    一旦读到标记就停止推送（但仍继续累积原文，否则解析不出链接）。

    **标记可能被切在两个 chunk 之间**（先到「【」、下一块才是「图片】」），
    所以末尾要扣住 len(IMAGE_MARKER)-1 个字符不推：它们随时可能是标记的前缀。
    不扣的话，那个孤零零的「【」会跟着打字机一起闪过去。

    **用户主动暂停**：每收一块查一次取消标志（`is_stop_requested`），置位就跳出循环，
    本轮生成作废。中断时**不再补推扣住的那截尾巴**——答案已作废，再补字只会让人困惑。

    :return: (原文, 是否被用户中断)。中断时第一项是**作废的半截内容**，调用方不应使用
    """
    llm = get_llm_client()

    if not is_stream:
        # 非流式是同步 invoke，中途无处可断，故不参与暂停
        resp = llm.invoke(messages)
        return (getattr(resp, "content", "") or "").strip(), False

    # 检索阶段就被暂停了：连生成都不用开，省掉一次注定要作废的请求
    if is_stop_requested(session_id):
        logger.info(f"[{NODE_NAME}] [_generate] 生成开始前已收到暂停请求，跳过生成")
        return "", True

    hold = len(IMAGE_MARKER) - 1   # 可能是标记前缀的尾部字符数
    buf = ""
    pushed = 0          # 已推送给前端的字符数
    cut = None          # 图片区块的起始位置
    stopped = False     # 是否被用户中断
    stream_iter = llm.stream(messages)
    try:
        for chunk in stream_iter:
            # 每收一块查一次：用户点了暂停就跳出，本轮答案作废
            if is_stop_requested(session_id):
                stopped = True
                logger.info(f"[{NODE_NAME}] [_generate] 检测到用户暂停，已生成 {len(buf)} 字符后中断")
                break

            piece = getattr(chunk, "content", "") or ""
            if not piece:
                continue
            buf += piece
            if cut is None:
                idx = buf.find(IMAGE_MARKER)
                if idx >= 0:
                    cut = idx
            # 还没出现完整标记时，末尾 hold 个字符先按兵不动
            visible_end = cut if cut is not None else max(0, len(buf) - hold)
            if visible_end > pushed:
                push_to_session(session_id, SSEEvent.DELTA, {"delta": buf[pushed:visible_end]})
                pushed = visible_end
    finally:
        # 主动关掉生成器，不等 GC —— 否则 DashScope 那条 HTTP 流会一直挂着。
        # 关闭失败不影响结果（本轮答案已经作废/已完成），只记一条告警，不让它升级成报错
        try:
            stream_iter.close()
        except Exception as e:
            logger.warning(f"[{NODE_NAME}] [_generate] 关闭生成流时出错（忽略）：{e}")

    if stopped:
        return buf, True

    # 收尾：确认没有标记就把扣住的那截补推出去（有标记则正文已在 cut 处截断）
    if cut is None and len(buf) > pushed:
        push_to_session(session_id, SSEEvent.DELTA, {"delta": buf[pushed:]})
    return buf.strip(), False


def node_answer_output(state: QueryGraphState) -> QueryGraphState:
    """
    节点: 生成答案 (node_answer_output)

    :param state: 需包含 session_id；有 answer 则直接输出，否则用 reranked_docs 生成
    :return: {"answer": 最终答案文本}
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f"[{NODE_NAME}] [{function_name}] 节点处理开始")
    add_running_task(state["session_id"], function_name, state.get("is_stream"))

    session_id = state["session_id"]
    is_stream = state.get("is_stream", True)
    preset = (state.get("answer") or "").strip()
    docs = state.get("reranked_docs") or []
    streamed = False    # 是否已经通过 delta 推过内容
    cancelled = False   # 是否被用户主动暂停（finally 里要据此决定算不算「完成」）

    try:
        if preset:
            # 前置节点（产品名反问 / 拒识）已经给出答复，不必再调模型
            final_text, images = preset, []
            logger.info(f"[{NODE_NAME}] [{function_name}] 前置节点已产出答案，跳过 LLM 生成")
        elif not docs:
            final_text, images = FALLBACK_ANSWER, []
            logger.warning(f"[{NODE_NAME}] [{function_name}] 没有可用参考切片，返回兜底答复")
        else:
            context, captions = _build_context(docs)
            prompt = load_prompt(
                "answer_out",
                context=context,
                history=_build_history(session_id),
                item_names="、".join(state.get("item_names") or []) or "（无）",
                question=state.get("rewritten_query") or state.get("original_query") or "",
            )
            messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]
            logger.info(
                f"[{NODE_NAME}] [{function_name}] 参考切片 {len(docs)} 条，"
                f"上下文 {len(context)} 字符，开始生成"
            )
            raw, cancelled = _generate(session_id, messages, is_stream)

            if cancelled:
                # 用户主动暂停：本轮答案作废，流程照常走到 END。
                # 不推 FINAL、不存档 —— 那半截内容已经通过 delta 给到前端，只作显示用；
                # 存进历史会让下一轮把半句话当成完整回答去理解。
                # **不反问用户**：想调整什么，用户自己重新提问即可。
                # （图因缺信息需要用户确认而中断是另一条线，靠 interrupt + checkpointer）
                logger.info(f"[{NODE_NAME}] [{function_name}] 本轮被用户暂停，答案作废")
                return {"answer": "", "cancelled": True}

            streamed = is_stream
            final_text, images = _split_images(raw, captions)
            logger.info(
                f"[{NODE_NAME}] [{function_name}] 生成完成，答案 {len(final_text)} 字符，"
                f"配图 {len(images)} 张"
            )

        if is_stream:
            # 非 LLM 路径没有逐块推送，这里整段补一次 delta
            if not streamed:
                push_to_session(session_id, SSEEvent.DELTA, {"delta": final_text})
            # 先把「生成答案」标成完成（顺带推一次 progress），**再**推 FINAL：
            # 否则 FINAL 先到时前端已经关掉事件流，泳道收尾就只能靠猜 —— 以前是拿
            # 全量节点兜底，结果把压根没跑过的节点也点亮了（见 chat.html 的收尾注释）。
            # add_done_task 幂等，finally 里那次重复调用无害
            add_done_task(session_id, function_name, is_stream)
            push_to_session(
                session_id,
                SSEEvent.FINAL,
                {
                    "answer": final_text,
                    "status": "completed",
                    "images": images,
                    # 把**真实进度**一并交给前端，让它收尾时不必再猜：
                    # done_list 是实际跑过的节点，degraded_list 是其中降级返回的
                    "done_list": get_done_task_list(session_id),
                    "degraded_list": get_degraded_task_list(session_id),
                },
            )

        # 存档助手这一轮的答案，供 /history 接口读取
        try:
            save_chat_message(session_id, "assistant", final_text)
            logger.info(f"[{NODE_NAME}] [{function_name}] 助手消息已存档")
        except Exception as e:
            # 存档失败不应影响答案返回，但编程错误要被上抛（存档写错也是 bug）
            degrade(NODE_NAME, "助手消息存档", None, e)

        return {"answer": final_text, "images": images, "cancelled": False}

    except Exception as e:
        # 最后一环，没有可降级的兜底内容：编程错误直接上抛，
        # 其余的给用户一条明确的失败提示（流式下推 error 事件）
        if is_fatal(e):
            raise
        if is_content_rejected(e):
            # 生成阶段才被审核拒绝（提取阶段没拦住的那些）：照旧给一句人话。
            # 流式靠 error 事件送达；**非流式必须把话写进 answer** ——
            # 这条路的响应体除此之外没有别的地方能带上原因，否则用户只收到一个空字符串
            if is_stream:
                push_to_session(session_id, SSEEvent.ERROR, {"error": CONTENT_REJECTED_ANSWER})
            return degrade(NODE_NAME, "答案生成", {"answer": CONTENT_REJECTED_ANSWER}, e)
        if is_stream:
            push_to_session(session_id, SSEEvent.ERROR, {"error": f"答案生成失败：{e}"})
        return degrade(NODE_NAME, "答案生成", {"answer": ""}, e)
    finally:
        # 被暂停的节点不算「已完成」：否则泳道会谎报「生成答案 ✓」，
        # 而实际上一句话都没生成完。前端在 paused 事件里按真实进度渲染
        if not cancelled:
            add_done_task(state["session_id"], function_name, state.get("is_stream"))
        logger.info(f"[{NODE_NAME}] [{function_name}] 节点处理结束")


def _check_stream_boundary() -> list:
    """
    离线自测：图片标记的流式边界（不调任何接口，纯逻辑）

    这块曾经有 bug：模型把「【图片】」切成「【」+「图片】」两块时，第一个 chunk
    处理完时 `buf.find()` 还找不到完整标记，那个孤零零的「【」就被推给前端、
    跟着打字机闪过去。修法是推之前先扣住 len(IMAGE_MARKER)-1 个字符。

    不变量：**推给前端的正文，必须等于最终答案里图片区块之前的部分**——
    少推会吞字，多推会漏出标记或裸 URL。
    用户中途暂停时例外：此时答案作废，**已推出去的就是全部**，扣住的那截尾巴不得补推，
    否则会把作废的半截答案又吐回前端。

    :return: 问题描述列表，空表示全部通过
    """
    problems = []

    class _FakeChunk:
        def __init__(self, content):
            self.content = content

    class _FakeLLM:
        def __init__(self, pieces):
            self.pieces = pieces

        def stream(self, messages):
            for p in self.pieces:
                yield _FakeChunk(p)

    real_push, real_get, real_stop = push_to_session, get_llm_client, is_stop_requested
    try:
        def _run(pieces):
            """正常跑完一轮：取消标志恒为 False"""
            deltas = []
            globals()["push_to_session"] = lambda sid, ev, data: deltas.append(data.get("delta"))
            globals()["get_llm_client"] = lambda *a, **k: _FakeLLM(pieces)
            globals()["is_stop_requested"] = lambda sid: False
            raw, stopped = _generate("boundary_test", [], True)   # 必须先跑完再 join
            return "".join(deltas), raw, stopped

        def _run_stop(pieces, stop_from_call):
            """
            模拟用户中途点暂停：第 stop_from_call 次查询起返回 True。

            `_generate` 的查询次数是「开跑前 1 次 + 每收一块 1 次」，
            所以 stop_from_call=3 表示处理完第 1 块之后中断。
            """
            deltas = []
            calls = {"n": 0}
            globals()["push_to_session"] = lambda sid, ev, data: deltas.append(data.get("delta"))
            globals()["get_llm_client"] = lambda *a, **k: _FakeLLM(pieces)

            def _fake_stop(sid):
                calls["n"] += 1
                return calls["n"] >= stop_from_call

            globals()["is_stop_requested"] = _fake_stop
            raw, stopped = _generate("boundary_test", [], True)
            return "".join(deltas), raw, stopped

        pushed, raw, stopped = _run(["安装步骤如下。", "说明。", "【", "图片", "】", "http://a/1.jpg"])
        if "【" in pushed:
            problems.append(f"标记被切块时前缀泄漏：{pushed!r}")
        if pushed != "安装步骤如下。说明。":
            problems.append(f"切块场景正文推送不完整：{pushed!r}")
        if "【图片】" not in raw:
            problems.append("原文应保留标记，否则节点解析不出图片")
        if stopped:
            problems.append("正常跑完不应被判为暂停")

        pushed, raw, _ = _run(["普通", "回答", "结束"])
        if pushed != raw or pushed != "普通回答结束":
            problems.append(f"无标记时尾部被吞或与原文不一致：{pushed!r}")

        pushed, _, _ = _run(["答案正文", "【图片】http://a/2.jpg"])
        if pushed != "答案正文":
            problems.append(f"标记整块到达时推送不对：{pushed!r}")

        # 流恰好停在半个标记上：它是真实正文（final 里也有），必须补推，前后一致
        pushed, raw, _ = _run(["答案", "【图"])
        if pushed != raw:
            problems.append(f"流结束时推送与原文不一致：{pushed!r} != {raw!r}")

        # --- 用户主动暂停 ---
        # 中途暂停：应中断、报 cancelled，且**不得补推**扣住的那截尾巴
        # （补了的话 pushed 就会等于 raw，把作废的答案又吐给前端）
        pushed, raw, stopped = _run_stop(["第一段。", "第二段。", "第三段。"], 3)
        if not stopped:
            problems.append("中途暂停未被识别")
        if raw != "第一段。":
            problems.append(f"暂停时应只保留已收到的内容，实际：{raw!r}")
        if pushed == raw:
            problems.append(f"暂停后仍把扣住的尾巴补推了：{pushed!r}")

        # 开跑前就点了暂停（第一次查询即 True）：连生成都不该开
        pushed, raw, stopped = _run_stop(["第一段。", "第二段。"], 1)
        if not (stopped and raw == "" and pushed == ""):
            problems.append(
                f"开跑前暂停应直接作废，实际 raw={raw!r} pushed={pushed!r} stopped={stopped}"
            )
    finally:
        globals()["push_to_session"], globals()["get_llm_client"], globals()["is_stop_requested"] = \
            real_push, real_get, real_stop

    return problems


def _check_build_history() -> list:
    """
    离线自测：`_build_history` 真能把历史读出来（只碰 Mongo，不调模型）

    这里曾把字段名写成 `content`，而库里存的是 `text` —— 于是**永远返回「（无）」**：
    不报错、不降级，静默丢掉全部多轮上下文，`answer_out.prompt` 的 `{history}` 槽一直是空的。
    所以断言的是「写进去的历史必须读得出来」，等于同时守住「存/读两边字段名一致」这条不变量
    —— 而不是把 `text` 这个键名硬编码进用例（换个写法就会连用例一起改错）。

    :return: 问题描述列表，空表示通过
    """
    from app.clients.mongo_history_utils import get_history_mongo_tool

    problems = []
    sid = "selftest_build_history"
    col = get_history_mongo_tool().db["chat_message"]
    col.delete_many({"session_id": sid})   # 清掉上次残留，保证从零开始
    try:
        save_chat_message(sid, "user", "HAK180 怎么安装烫金膜盒？")
        save_chat_message(sid, "assistant", "第一步打开支架盖。")

        got = _build_history(sid)
        if got == "（无）":
            problems.append("历史读成了「（无）」—— 多半是字段名又写错了（应读 text）")
        else:
            if "HAK180 怎么安装烫金膜盒" not in got:
                problems.append(f"用户那轮没进历史：{got!r}")
            if "第一步打开支架盖" not in got:
                problems.append(f"助手那轮没进历史：{got!r}")

        # 末尾那条用户消息 = 本轮问题（已由确认节点存入），要裁掉以免与【用户问题】重复
        save_chat_message(sid, "user", "本轮的问题")
        got2 = _build_history(sid)
        if "本轮的问题" in got2:
            problems.append(f"末尾那条用户消息没被裁掉，会和【用户问题】重复：{got2!r}")
    except Exception as e:
        problems.append(f"读历史时抛异常：{type(e).__name__}: {e}")
    finally:
        col.delete_many({"session_id": sid})   # 测试产物不该留在用户的库里
    return problems


if __name__ == '__main__':
    """
    本地测试：先跑离线的流式边界用例，再走真实检索 → 真实生成

    前置：Milvus / Neo4j / MongoDB 均在运行
    """
    from app.clients.mongo_checkpoint_utils import graph_config
    from app.query_process.agent.main_graph import get_query_app
    from app.query_process.agent.state import create_query_default_state
    from app.utils.task_utils import clear_task

    logger.info("=" * 70)
    logger.info("[测试] 图片标记流式边界（离线，不调接口）")
    boundary_problems = _check_stream_boundary()
    for p in boundary_problems:
        logger.error(f"[测试] [FAIL] {p}")
    if not boundary_problems:
        logger.success("[测试] [PASS] 流式边界四个场景全部通过")

    logger.info("[测试] _build_history 真能读到历史（离线，只碰 Mongo）")
    history_problems = _check_build_history()
    for p in history_problems:
        logger.error(f"[测试] [FAIL] {p}")
    if not history_problems:
        logger.success("[测试] [PASS] 历史读写字段一致、末尾用户消息会被裁掉")

    cases = [
        ("正常问答", "Brother HAK 180 烫金机怎么安装烫金膜盒？"),
        ("库中无此产品", "小米15怎么开机？"),
    ]

    for label, question in cases:
        session_id = f"answer_test_{label}"
        logger.info("=" * 70)
        logger.info(f"[测试] {label}：{question}")
        st = create_query_default_state(
            session_id=session_id, original_query=question, is_stream=False,
        )
        try:
            # 命令行跑图也包一层：日志能按 trace 串起来、账目能归到这一次运行
            with usage_context(session_id=session_id) as acc:
                result = get_query_app().invoke(st, graph_config(f"selftest_{session_id}"))
            logger.info(f"[测试] 本次记账：{acc.text()}")
            answer = (result.get("answer") or "").strip()
            docs = result.get("reranked_docs") or []
            logger.info(f"[测试] 参考切片 {len(docs)} 条")
            logger.info(f"[测试] 答案（{len(answer)} 字符）：\n{answer[:400]}")
            if not answer:
                logger.error("[测试] [FAIL] 答案为空")
            if label == "正常问答":
                if not docs:
                    logger.error("[测试] [FAIL] 正常问答没有检索到切片")
                if "【图片】" in answer:
                    logger.error("[测试] [FAIL] 图片区块没被拆掉，仍留在正文里")
                if "测试回答" in answer or "打字机" in answer:
                    logger.error("[测试] [FAIL] 仍是占位文本")
        except Exception as e:
            logger.error(f"[测试] [FAIL] 执行失败：{e}", exc_info=True)
        finally:
            clear_task(session_id)

    # 收尾：这个自测会真跑图、节点也真落库（两个 answer_test_* 会话），跑完清掉。
    # 不清的话每跑一次就往用户库里留两个会话（2026-10-06 发现库里一直积着它们）
    try:
        from app.clients.mongo_history_utils import get_history_mongo_tool
        deleted = get_history_mongo_tool().db["chat_message"].delete_many(
            {"session_id": {"$regex": "^answer_test_"}}).deleted_count
        logger.info(f"[测试] 已清理自测会话记录 {deleted} 条")
    except Exception as e:
        logger.warning(f"[测试] 清理自测会话记录失败（不影响结论）：{e}")

    logger.info("=" * 70)
    failed = len(boundary_problems) + len(history_problems)
    if failed:
        logger.error(f"[测试] 有 {failed} 项离调用例未通过（见上文）")
    else:
        logger.info("[测试] 全部用例执行完毕")
