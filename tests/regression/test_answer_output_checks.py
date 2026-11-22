"""node_answer_output 回归用例（从节点文件迁出）。"""
import json
import logging
import os
import re
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import app.agent.subagents.product_kb.application.nodes.node_answer_output as _nao

globals().update({k: v for k, v in vars(_nao).items() if not k.startswith("__")})


def _check_answer_mode_classification() -> list:
    """离线自测：步骤/事实/描述三类路由不误判。"""
    problems = []
    cases = [
        ("HAK180 烫金机的烫金膜盒怎么安装", "steps"),
        ("烫金温度怎么设置", "steps"),
        ("操作面板和指示灯怎么看", "steps"),
        ("说明书里支持哪些验证和加密方式", "fact"),
        ("打印机状态灯闪烁代表什么", "fact"),
        ("烫金膜盒有几种规格", "fact"),
        ("HAK180 长什么样", "descriptive"),
        ("企业级路由器主要用在什么场景", "descriptive"),
        ("烫金机是做什么用的", "descriptive"),
        ("你好", "general"),
    ]
    for question, expect in cases:
        got = classify_answer_mode(question)
        if got != expect:
            problems.append(f"{question!r} 期望 {expect}，实得 {got}")
    if not ANSWER_MODE_RULES.get("steps") or not ANSWER_MODE_RULES.get("fact"):
        problems.append("回答模式规则缺失")
    rendered = load_prompt(
        "answer_out", tone_guide="t", answer_mode_rule="MODE_RULE",
        image_catalog="", context="", prior_answer="", history="",
        item_names="", question="",
    )
    if "MODE_RULE" not in rendered:
        problems.append("answer_out 没有渲染 answer_mode_rule")
    return problems


def _check_stream_boundary() -> list:
    """
    离线自测：流式推送的边界（不调任何接口，纯逻辑）

    2026-10-14 随「图片区块退役」收窄：配图改用正文编号引用（引用即附，见
    `_extract_images`），答案里不再有需要藏起来的标记，推送回归「逐块原样推」
    —— 曾经「扣住标记前缀防闪」的那套逻辑已删。

    不变量：**推给前端的 = 原文**（不吞字、不重复；编号引用是普通文字照常推）。
    用户中途暂停：已推的就是全部，不补发、不重推（作废的答案不得再吐回前端）。

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
            _nao.push_to_session = lambda sid, ev, data: deltas.append(data.get("delta"))
            _nao.get_llm_client = lambda *a, **k: _FakeLLM(pieces)
            _nao.is_stop_requested = lambda sid: False
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
            _nao.push_to_session = lambda sid, ev, data: deltas.append(data.get("delta"))
            _nao.get_llm_client = lambda *a, **k: _FakeLLM(pieces)

            def _fake_stop(sid):
                calls["n"] += 1
                return calls["n"] >= stop_from_call

            _nao.is_stop_requested = _fake_stop
            raw, stopped = _generate("boundary_test", [], True)
            return "".join(deltas), raw, stopped

        pushed, raw, stopped = _run(["安装步骤如下。", "说明。", "如图 2 所示，向下轻推。"])
        if pushed != raw or pushed != "安装步骤如下。说明。如图 2 所示，向下轻推。":
            problems.append(f"推送与原文不一致（吞字或重复）：{pushed!r} != {raw!r}")
        if stopped:
            problems.append("正常跑完不应被判为暂停")

        pushed, raw, _ = _run(["普通", "回答", "结束"])
        if pushed != raw or pushed != "普通回答结束":
            problems.append(f"切块推送与原文不一致：{pushed!r}")

        # --- 用户主动暂停 ---
        # 中途暂停：应中断、作废；逐块即推之后，已推的就是全部（pushed 应等于 raw）
        pushed, raw, stopped = _run_stop(["第一段。", "第二段。", "第三段。"], 3)
        if not stopped:
            problems.append("中途暂停未被识别")
        if raw != "第一段。":
            problems.append(f"暂停时应只保留已收到的内容，实际：{raw!r}")
        if pushed != raw:
            problems.append(f"暂停后推送与已收内容不一致：{pushed!r} != {raw!r}")

        # 开跑前就点了暂停（第一次查询即 True）：连生成都不该开
        pushed, raw, stopped = _run_stop(["第一段。", "第二段。"], 1)
        if not (stopped and raw == "" and pushed == ""):
            problems.append(
                f"开跑前暂停应直接作废，实际 raw={raw!r} pushed={pushed!r} stopped={stopped}"
            )
    finally:
        _nao.push_to_session, _nao.get_llm_client, _nao.is_stop_requested = \
            real_push, real_get, real_stop

    return problems


def _check_image_whitelist_space_url() -> list:
    """
    离线自测：**图片 URL 里带空格时，白名单也要认得**（纯逻辑，不调任何接口）

    踩过：导入时图片的 MinIO 对象名取自文档名，而文档名常带空格
    （如「Legion Y9000P IRX8 和 Legion R9000P ARX8 用户指南」）。白名单正则原先写的是
    `[^)\\s]+`，把空格排除在外 —— 这类文档的图片链接**一条都匹配不到**，白名单成了空的，
    模型给出的图全被当成「不在参考内容中」丢掉。实测 7 份文档里 4 份中招、178 处引用全废，
    而且不报错：答案照常生成，只是没有图。

    两个方向都要断言：**该认得必须认得**，**该挡的仍要挡住**（别为了修它把白名单放空）。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    url = ("http://127.0.0.1:9000/rag-helper-knowledge-files/upload-images/"
           "Legion Y9000P IRX8 和 Legion R9000P ARX8 用户指南-20231229_images/abc.jpg")
    docs = [{"title": "## 俯视 图", "text": f"电源键位置见下图。\n\n![电源键位置示意]({url})\n\n说完。"}]

    _, captions = _build_context(docs)
    if url not in captions:
        problems.append(f"白名单没认出带空格的图片 URL（只认出 {len(captions)} 条）")
    elif captions[url] != "电源键位置示意":
        problems.append(f"图注应取 alt 文本，实得 {captions[url]!r}")

    # 正文带编号引用（引用即附，见 _check_image_ref_extraction）——本条守的是
    # 目录/白名单对「带空格的 URL」的识别：带空格的图必须能进目录并被解析出来
    body, images = _extract_images("电源键位置如图 1 所示。", captions)
    if len(images) != 1 or images[0].get("url") != url:
        problems.append(f"带空格的图片没被认出来：{images!r}")
    if "如图 1 所示" not in body:
        problems.append(f"正文引用被改坏：{body!r}")

    # 反例：目录之外的编号（参考内容里没有的图）仍要被拦住
    _, others = _extract_images("电源键位置如图 9 所示。", captions)
    if others:
        problems.append(f"目录之外的编号被放行了：{others!r}")
    return problems


def _check_image_ref_extraction() -> list:
    """
    离线自测：**引用即附** —— 配图由正文编号引用决定（纯逻辑，2026-10-14 定稿）

    由来：用户要求「图片应该在回答中添加引用，避免无关图片」，并要求说清图号。
    方案：模型只在正文写目录编号（【可用配图】给的，如「如图 3 所示」），平台按
    **引用首次出现的顺序**选出配图、并把正文重编号成 1 起 —— 正文引用与图卡编号
    由构造一致；没引用的图绝不出现，悬空引用不可能发生。
    守五件事：
    1. 引用即附：正文引了哪几张就附哪几张，顺序 = 首次出现顺序；
    2. 重编号：正文里的目录编号被改写成展示序号；
    3. 「第 N 张图」形态也认；重复引用只附一次、编号一致；
    4. 「试图 3」这类巧合数字不算引用（负向后顾）；
    5. 目录外的编号忽略（配图少挂总比挂错强）；没有编号引用则零配图。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    cats = {"http://x/1.png": "图注一", "http://x/2.png": "图注二", "http://x/3.png": "图注三"}

    body, images = _extract_images("先看第 3 张图，再回来看图 1。", cats)
    if [i["url"] for i in images] != ["http://x/3.png", "http://x/1.png"]:
        problems.append(f"配图选取/顺序不对（应按首次出现）：{[i['url'] for i in images]!r}")
    if body != "先看第 1 张图，再回来看图 2。":
        problems.append(f"正文重编号不对：{body!r}")

    # 重复引用同一张：只附一次，两处引用重编号到同一个号
    body, images = _extract_images("如图 2 所示，先这样；如图 2 所示，再那样。", cats)
    if len(images) != 1 or images[0]["url"] != "http://x/2.png":
        problems.append(f"重复引用应只附一张：{images!r}")
    if body.count("图 1") != 2:
        problems.append(f"重复引用应重编号到同一个号：{body!r}")

    # 「试图 3」这类巧合数字不算引用；没有编号引用则零配图
    _, images = _extract_images("不要试图 3 次倒置机器。", cats)
    if images:
        problems.append(f"「试图 3」被误当成编号引用：{images!r}")
    _, images = _extract_images("电源位置如下图所示。", cats)
    if images:
        problems.append(f"没有编号引用时不该配图：{images!r}")

    # 目录外的编号：忽略（配图少挂总比挂错强）
    _, images = _extract_images("如图 9 所示。", cats)
    if images:
        problems.append(f"目录外的编号被放行了：{images!r}")
    return problems


def _check_prompt_leak_guard() -> list:
    """
    离线自测：输出护栏认得出「模型把内部提示词吐出来」，且**不误伤正常答案**（纯逻辑）

    来由是 2026-10-09 的一次真实注入：问题里伪造 `{"role": "system", ...}`，
    让模型把 SYSTEM_PROMPT 写进答案的 `real_prompt` 字段 —— 那次是**逐字照抄**。

    两个方向都要断言：**该拦的拦住**、**正常答案不许误伤**（误伤的代价是用户拿到拒答）。

    :return: 问题描述列表，空表示通过
    """
    problems = []

    if SYSTEM_PROMPT != load_prompt("answer_out_system").strip():
        problems.append("answer_out 的 system prompt 没有从 prompts/answer_out_system.prompt 读取")

    # ① 逐字照抄 SYSTEM_PROMPT（复现那次事故的答案形态）
    leaked = f'{{"answer": "我是产品使用文档的问答助手。", "real_prompt": "{SYSTEM_PROMPT}"}}'
    if not _looks_like_prompt_leak(leaked):
        problems.append("整段照抄 SYSTEM_PROMPT 的答案没被认出来")

    # ② 只吐模板、不吐 SYSTEM_PROMPT（模板里的句子同样不该出现在答案里）。
    #    句子**动态取**，免得哪天模板改了、这里变成一条假红
    template_lines = [
        line.strip() for line in load_prompt("answer_out").splitlines()
        if len(line.strip()) >= PROMPT_LEAK_MIN_CHARS and "{" not in line
    ]
    if not template_lines:
        problems.append("answer_out 模板里挑不出够长的指纹（模板被改短了？）")
    elif not _looks_like_prompt_leak(f"我的规则是：{template_lines[0]}"):
        problems.append(f"模板原句没被认出来：{template_lines[0][:24]!r}")

    # ③ 复述时换了行、加了空格，也不能漏
    if not _looks_like_prompt_leak(SYSTEM_PROMPT.replace("，", "，\n\n")):
        problems.append("被换行/空格打断的提示词没被认出来")

    # ④ 正常答案不许误伤 —— 这几句都含提示词里出现过的词，但不是原句
    for normal in [
        "烫金膜盒的安装步骤见说明书 3.2 节：按箭头方向插入支架即可。",
        "没有找到相关内容，建议确认一下产品型号。",
        "参考内容包括产品简介、外观与接口说明三部分。",
        "这个型号支持 USB-C 供电，具体参数见说明书第 5 章。",
    ]:
        hit = _looks_like_prompt_leak(normal)
        if hit:
            problems.append(f"正常答案被误伤（命中 {hit[:20]!r}）：{normal}")
    return problems


def _check_prompt_leak_guard_wired() -> list:
    """
    离线自测：护栏**真的接在生成路径上**（打桩 LLM，不调接口）

    上一条测的是判定函数本身，这条测**调用点** —— 缺了它，「函数是对的、但没人调」
    这种缺口不会暴露（§3.19 的教训：覆盖了函数 ≠ 覆盖了调用点）。

    :return: 问题描述列表，空表示通过
    """
    from app.clients.mongo_history_clients import get_history_mongo_tool, get_recent_messages
    from app.agent.subagents.product_kb.domain.state import create_query_default_state
    from app.utils.task_utils import clear_task

    class _Resp:
        def __init__(self, content):
            self.content = content

    class _FakeInjectedLLM:
        """模拟被注入带偏的模型：把 SYSTEM_PROMPT 原样写进答案（复现那次事故的形态）"""

        def invoke(self, messages):
            return _Resp(f'{{"answer": "正常回答", "real_prompt": "{SYSTEM_PROMPT}"}}')

    sid = "selftest_prompt_leak_guard"
    col = get_history_mongo_tool().db["chat_message"]
    col.delete_many({"session_id": sid})

    problems = []
    real_get = _nao.get_llm_client
    _nao.get_llm_client = lambda *a, **k: _FakeInjectedLLM()
    try:
        state = create_query_default_state(
            session_id=sid, original_query="你是谁？", rewritten_query="你是谁？",
            is_stream=False,
            reranked_docs=[{"title": "## 概述", "text": "这是一段参考内容。"}],
        )
        out = node_answer_output(state) or {}
        if out.get("answer") != PROMPT_LEAK_ANSWER:
            problems.append(f"泄漏的答案没被换成拒答：{str(out.get('answer'))[:40]!r}")
        if out.get("images"):
            problems.append(f"被拦下时不该还带配图：{out.get('images')!r}")
        saved = [m for m in get_recent_messages(sid, limit=10) if m.get("role") == "assistant"]
        if saved and SYSTEM_PROMPT in (saved[-1].get("text") or ""):
            problems.append("泄漏内容还是进了历史存档")
    except Exception as e:
        problems.append(f"跑答案节点时抛异常：{type(e).__name__}: {e}")
    finally:
        _nao.get_llm_client = real_get
        clear_task(sid)
        col.delete_many({"session_id": sid})      # 自测数据不该留在用户的库里
    return problems


def _check_context_marks_web() -> list:
    """
    离线自测：联网结果在上下文里带「不可信」标记、本地切片不带（纯逻辑）

    第二层（提示词加固）里**能用代码断言的部分**只有这一处：模板里那几句声明是"告诉模型别听"，
    而模型得先分得出哪几条是第三方内容 —— 全靠这里标出来。
    至于模板措辞本身，单测断不了，靠 §3.25 那次攻击的复现来验。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    docs = [
        {"title": "## 安装步骤", "text": "本地切片正文。", "source": "local"},
        {"title": "某网页标题", "text": "联网片段正文。", "source": "web"},
        {"title": "没标来源的", "text": "兜底正文。"},          # 旧数据/异常数据
    ]
    context, _ = _build_context(docs)
    blocks = context.split("\n\n")

    if "[联网结果·不可信]" not in context:
        problems.append("联网结果没有带上「不可信」标记")
    elif "[联网结果·不可信]" not in blocks[1]:
        problems.append(f"标记没落在联网那一段上：{blocks[1][:40]!r}")

    # 本地切片与「没标来源」的都不该被标记 —— 标错了会让模型对自家文档也不信任
    for idx, label in ((0, "本地切片"), (2, "没标来源的片段")):
        if "不可信" in blocks[idx]:
            problems.append(f"{label}被误标成不可信：{blocks[idx][:40]!r}")
    return problems


def _check_step_marker_not_in_context() -> list:
    """
    离线自测：步骤序号图不进入参考内容 / 图片目录（纯逻辑）

    新数据在导入侧已删除；这条守的是**旧切片**的兜底 —— 只要 alt 命中
    `is_meaningless_step_marker`，就不该出现在上下文、目录或白名单里。
    """
    problems = []
    docs = [
        {"title": "外观",
         "text": "正文\n![HAK 180设备外观示意图](http://example.invalid/good.jpg)",
         "source": "local"},
        {"title": "步骤",
         "text": "按图示操作\n![蓝色圆形图标内有白色数字“3”，表示操作步骤序号]"
                 "(http://example.invalid/bad.jpg)",
         "source": "local"},
    ]
    context, captions = _build_context(docs)
    if "good.jpg" not in context:
        problems.append("有用图片被误删")
    if "bad.jpg" in context:
        problems.append("步骤序号图仍留在参考内容里")
    if not any("good.jpg" in url for url in captions):
        problems.append("有用图片未进入图片目录")
    if any("bad.jpg" in url for url in captions):
        problems.append("步骤序号图仍进入图片目录")
    return problems


def _check_context_origin() -> list:
    """
    离线自测：上下文头部标注「手册名 · 章节路径」（requirements 2.23，纯逻辑）

    守四件事：手册名与章节路径都进头部；章节路径等于标题时不重复；
    Markdown 井号被清掉；没有这两个字段的联网结果退化成原格式、
    不出现孤零零的分隔符。
    """
    problems = []
    docs = [
        # 旧数据：title 本身就是章节标题，section_path 与它相同
        {"text": "正文一", "title": "## 3.4 安装",
         "file_title": "HAK180 说明书", "section_path": "## 3.4 安装",
         "parent_title": "## 3.4 安装", "source": "local"},
        # 新数据：父章节 > 本章节
        {"text": "正文二", "title": "## 3.4.2 装入半幅",
         "file_title": "HAK180 说明书", "section_path": "# 3.4 安装 > ## 3.4.2 装入半幅",
         "parent_title": "# 3.4 安装", "source": "local"},
        # 联网结果：两个字段都没有
        {"text": "网页正文", "title": "网页标题", "source": "web"},
    ]
    context, _ = _build_context(docs)
    heads = [line for line in context.split("\n") if line.startswith("[")]
    if len(heads) != 3:
        return [f"上下文头部数量不对：{heads}"]
    first, second, third = heads
    if first != "[1] HAK180 说明书 · 3.4 安装":
        problems.append(f"旧数据头部不对：{first!r}")
    if second != "[2] HAK180 说明书 · 3.4 安装 > 3.4.2 装入半幅":
        problems.append(f"章节路径没生效：{second!r}")
    if "#" in first + second:
        problems.append(f"Markdown 井号没清掉：{first!r} / {second!r}")
    if first.count("3.4 安装") != 1:
        problems.append(f"章节标题重复出现：{first!r}")
    if third != "[3] [联网结果·不可信] 网页标题":
        problems.append(f"联网结果头部不对：{third!r}")
    if " · " in third:
        problems.append(f"没有手册名时不该出现分隔符：{third!r}")

    long_docs = [{
        "text": "正文三", "title": "## 标题",
        "file_title": "很长的手册名" * 20, "section_path": "## 标题",
    }]
    long_head = _build_context(long_docs)[0].split("\n")[0]
    if not long_head.endswith("…"):
        problems.append(f"头部未按 {CONTEXT_ORIGIN_MAX_CHARS} 字符截断：{long_head!r}")
    return problems


def _check_section_expand_order() -> list:
    """
    离线自测：章节证据扩展按 chunk_index 排序、按 chunk_id 找锚点（requirements 2.24）

    守三件事：chunk_id 数值大小与文档顺序相反时，仍按 chunk_index 排；
    锚点用 chunk_id 精确命中（不是「数值最接近」）；缺 chunk_index 的旧行退化为 0。
    """
    rows = [
        # 故意让 chunk_id 的数值顺序与文档顺序相反
        {"chunk_id": 900, "chunk_index": 0, "content": "第一片"},
        {"chunk_id": 20, "chunk_index": 1, "content": "第二片（锚点）"},
        {"chunk_id": 3, "chunk_index": 2, "content": "第三片"},
    ]
    problems = []
    ordered = _order_section_rows(rows)
    got = [r["content"] for r in ordered]
    if got != ["第一片", "第二片（锚点）", "第三片"]:
        problems.append(f"没有按 chunk_index 排序：{got}")
    anchor = _find_anchor_index(ordered, 20)
    if anchor != 1:
        problems.append(f"锚点位置不对：{anchor}")
    if _find_anchor_index(ordered, 999) != -1:
        problems.append("找不到的锚点应返回 -1")
    # 旧数据没有 chunk_index：退化为 0，再按 chunk_id 兜底，不能抛异常
    legacy = _order_section_rows([{"chunk_id": 30}, {"chunk_id": 20}, {"chunk_id": 20, "chunk_index": 0}])
    if len(legacy) != 3:
        problems.append(f"缺字段的行被丢掉了：{legacy}")
    return problems


def _check_insufficiency_note() -> list:
    """
    离线自测：证据不足时上下文要带「缺什么」的说明（requirements 2.26）

    守四件事：判定 retry 且有 missing → 追加说明；sufficient → 原样；
    retry 但没 missing → 原样；连判定都没有 → 原样。
    漏掉第一条，「证据不足」就只剩拒答或硬答两条路，静默幻觉率压不下来。
    """
    problems = []
    base = "[1] 手册 · 章节\n正文"

    got = _with_insufficiency_note(base, {"retrieval_judge": {"verdict": "retry", "missing": "烫金温度设定"}})
    if "烫金温度设定" not in got or "手册里没有写这部分" not in got:
        problems.append(f"不足时没追加说明：{got[-60:]!r}")
    if not got.startswith(base):
        problems.append("说明不该覆盖原有上下文")

    for judge, label in [
        ({"verdict": "sufficient", "missing": "烫金温度"}, "判定充分"),
        ({"verdict": "retry", "missing": "  "}, "retry 但没给缺失"),
        ({}, "没有判定"),
    ]:
        if _with_insufficiency_note(base, {"retrieval_judge": judge}) != base:
            problems.append(f"{label}时不该追加说明")
    if _with_insufficiency_note(base, {}) != base:
        problems.append("state 里没有 retrieval_judge 时不该追加说明")
    return problems


def _check_answer_intent_anchor() -> list:
    """
    离线自测：作答问题用**意图锚**，不用检索探针（纯逻辑）

    检索循环（2026-10-20）里 `retrieval_query` 是会被改写的探针；
    如果作答误用了它，一次跑偏的改写就会让回答也跑偏 —— 这是整个循环最关键的
    防漂移断言，必须钉住。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    got = _answer_question({
        "rewritten_query": "意图锚", "retrieval_query": "探针", "original_query": "原始"})
    if got != "意图锚":
        problems.append(f"有意图锚时应作答它，实得 {got!r}")
    got = _answer_question({"retrieval_query": "探针", "original_query": "原始"})
    if got != "原始":
        problems.append(f"没有意图锚时应回退原始问题，实得 {got!r}")
    got = _answer_question({"retrieval_query": "探针"})
    if got != "":
        problems.append(f"只有探针时不应拿它作答，实得 {got!r}")
    return problems

PURE_CHECKS = [
    ("_check_answer_mode_classification", _check_answer_mode_classification),
    ("_check_stream_boundary", _check_stream_boundary),
    ("_check_image_whitelist_space_url", _check_image_whitelist_space_url),
    ("_check_image_ref_extraction", _check_image_ref_extraction),
    ("_check_prompt_leak_guard", _check_prompt_leak_guard),
    ("_check_context_marks_web", _check_context_marks_web),
    ("_check_step_marker_not_in_context", _check_step_marker_not_in_context),
    ("_check_context_origin", _check_context_origin),
    ("_check_section_expand_order", _check_section_expand_order),
    ("_check_insufficiency_note", _check_insufficiency_note),
    ("_check_answer_intent_anchor", _check_answer_intent_anchor),
]

MONGO_CHECKS = [
    ("_check_prompt_leak_guard_wired", _check_prompt_leak_guard_wired),
]

@pytest.mark.parametrize("name,fn", PURE_CHECKS, ids=[n for n, _ in PURE_CHECKS])
def test_answer_output_pure(name, fn):
    assert fn() == []


@pytest.mark.integration
@pytest.mark.parametrize("name,fn", MONGO_CHECKS, ids=[n for n, _ in MONGO_CHECKS])
def test_answer_output_mongo(name, fn, mongo_ready):
    assert fn() == []
