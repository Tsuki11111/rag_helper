"""
评测用例集的人工标注脚手架（评估路线 P2 的**工具**）

**它只提供工具，不做标注。** 「哪条切片才是正确答案」是人判断的 —— 这里做的是让人能
高效、无歧义地做那件事的四件事：

```bash
.venv/Scripts/python.exe -m app.core.eval_cases check                    # 校验格式 + gold 有效性 + 标注进度
.venv/Scripts/python.exe -m app.core.eval_cases candidates               # 从真实会话里导出候选提问（挑那 25 条用）
.venv/Scripts/python.exe -m app.core.eval_cases recall "问题"             # 真跑一次检索，列出可召回的切片（挑 gold 用）
.venv/Scripts/python.exe -m app.core.eval_cases show 469415858335087550  # 打印切片正文（它到底是不是 gold）
.venv/Scripts/python.exe -m app.core.eval_cases rounds eval/cases.round2.yaml   # 两轮标注一致率（规范要求 ≥ 90%）
```

用例集在 [`eval/cases.yaml`](../../eval/cases.yaml) —— **格式与标注规范都写在那份文件的头部注释里**，
这里不重复（只改一处，免得两边打架）。

**为什么单独做这几个命令**：
- `recall` + `show` 是配套的 —— 标注时先看「这个问题能召回哪些」，再逐条读正文判断谁是 gold；
  不给这两个，标注的人只能靠猜 id
- `candidates` 是因为 §3.3 要求用例**从真实提问里挑**，不是凭空造题
- `rounds` 是规范要求的自检：**隔一天重标一遍**，一致率 < 90% 说明规范没写清，先改规范
"""
import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pymilvus.exceptions import MilvusException
from pymongo.errors import PyMongoError

from app.clients.milvus_utils import dense_search, fetch_chunks_by_chunk_ids, get_milvus_client
from app.conf.milvus_config import milvus_config
from app.core.logger import logger
from app.utils.path_util import PROJECT_ROOT

# 用例集文件（可用 --file 覆盖）
DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "cases.yaml"

# 问题类型。前六个是**检索维度**（evaluation-plan.md §3.2），后两个是另外两个维度：
# `sensitive` 对应教程四维度里的「安全合规」，`boundary` 是形态边界（只有产品名、空串、超长…）
CATEGORIES = {"fact", "steps", "descriptive", "anaphora", "cross_doc", "out_of_kb",
              "sensitive", "boundary"}
SOURCES = {"real", "constructed"}

# 每条用例必须有的字段。**不含 `doc`** —— 「库外」那类用例本来就没有对应文档（留空是正常的）
REQUIRED_FIELDS = ("id", "question", "category", "source")


def load_cases(path: Path = None) -> List[Dict[str, Any]]:
    """读用例集；读不到或不是列表就直接抛（这是标注者的输入错误，不该被吞掉）"""
    path = Path(path or DEFAULT_CASES_PATH)
    if not path.exists():
        raise FileNotFoundError(f"用例集不存在：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"用例集顶层应当是列表，实为 {type(data).__name__}：{path}")
    return data


def validate(cases: List[Dict[str, Any]], *, check_gold_exists: bool = True) -> List[str]:
    """
    校验用例集，返回问题清单（空 = 通过）

    抽成函数是为了两边共用：`check` 命令与回归用例 `_check_cases_file` ——
    **这份文件是手写的**，格式写错、gold 指向不存在的切片，都该在跑评测**之前**被拦下。
    """
    problems: List[str] = []
    seen_ids = set()
    gold_ids: List[int] = []

    for idx, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            problems.append(f"第 {idx} 条不是字典：{type(case).__name__}")
            continue
        cid = case.get("id")
        tag = cid or f"第 {idx} 条"

        for field in REQUIRED_FIELDS:
            if not case.get(field):
                problems.append(f"{tag}：缺字段 `{field}`")
        if cid:
            if cid in seen_ids:
                problems.append(f"{tag}：id 重复")
            seen_ids.add(cid)
        if case.get("category") and case["category"] not in CATEGORIES:
            problems.append(f"{tag}：category `{case['category']}` 不在 {sorted(CATEGORIES)} 里")
        # 指代类没有上文 = 既标不了 gold、评测时也复现不了（「它」指谁无从判定）
        if case.get("category") == "anaphora" and not case.get("context"):
            problems.append(f"{tag}：指代类（anaphora）必须写 `context`（上一轮的用户提问）")
        if case.get("source") and case["source"] not in SOURCES:
            problems.append(f"{tag}：source `{case['source']}` 不在 {sorted(SOURCES)} 里")

        gold = case.get("gold_chunks") or []
        if not isinstance(gold, list):
            problems.append(f"{tag}：gold_chunks 应当是列表")
            continue
        for c in gold:
            if not isinstance(c, int):
                problems.append(f"{tag}：gold_chunks 里的 {c!r} 不是整数（Milvus 主键是 INT64）")
            else:
                gold_ids.append(c)

    # gold 指向的切片必须真的在库里 —— 否则用例是坏的，而且**跑起来才会以「召回率 0」的样子暴露**
    if gold_ids and check_gold_exists:
        client = get_milvus_client()
        rows = fetch_chunks_by_chunk_ids(
            client, milvus_config.chunks_collection, sorted(set(gold_ids)),
            output_fields=["chunk_id", "file_title", "title"])
        found = {r["chunk_id"] for r in rows}
        missing = sorted(set(gold_ids) - found)
        for c in missing:
            problems.append(f"gold_chunks 里的 {c} 在 Milvus 里不存在（切片被重导过？）")
        # 第一次真用就踩到了：把工作表里的**序号**当成 chunk_id 填了进来（1、3、7…）。
        # 序号都是小数字，而真实主键是 18 位的 —— 据此给一句人话提示
        if missing and all(c <= 100 for c in missing):
            problems.append(
                "**看起来填的是「序号」不是 chunk_id** —— 工作表里 `### 1.` 的 1 只是候选编号，"
                "`gold_chunks` 要填它后面 `` `4695…` `` 那串数字（`worksheet` 命令末尾有现成的对照表）")
    return problems


def _check_cases_file() -> List[str]:
    """
    回归用例：**手写的用例集本身没被改坏**（依赖 Milvus，因为要验 gold 是否存在）

    守两件在评测里都会**静默**出事的东西：
    - 格式写坏（缺字段、id 重复）——`load_cases` 之后的断言会莫名全红，或者干脆少跑几条
    - **gold 指向已不存在的切片**（切片被重导过就会这样）——Recall@K 会算成 0，
      看起来像「检索变差了」，其实是标注坏了

    :return: 问题描述列表，空表示通过
    """
    try:
        return validate(load_cases())
    except FileNotFoundError:
        return []          # 还没开始标注：这份文件不存在不算失败
    except Exception as e:
        return [f"读用例集失败：{type(e).__name__}: {e}"]


# ---------------------------
# check：格式 + gold 是否存在 + 进度
# ---------------------------
def cmd_check(args) -> int:
    """校验用例集，并报「标到哪了」"""
    cases = load_cases(args.file)
    problems = validate(cases)

    by_category: Dict[str, int] = {}
    for case in cases:
        if isinstance(case, dict):
            key = case.get("category") or "?"
            by_category[key] = by_category.get(key, 0) + 1

    marked = sum(1 for c in cases if isinstance(c, dict) and (c.get("gold_chunks") or []))
    print(f"\n用例 {len(cases)} 条，已标 gold {marked} 条（未标 {len(cases) - marked} 条）")
    print(f"  按类型：{by_category}")
    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("  格式与 gold 有效性：全部通过\n")
    return 0


# ---------------------------
# candidates：从真实会话里导出候选提问
# ---------------------------
# 分档：**越靠前越该被挑成用例**（§3.3 要求用例从真实提问里挑，而「答得不好的」才可能发现问题）
_TIER_TITLES = {
    0: "① 没答上来 / 出错（最该挑）",
    1: "② 认不出产品、转去问用户（也是一次没答好，而且可复现）",
    2: "③ 本地一条没命中、靠联网兜底",
    3: "④ 本地只命中 1 条（召回偏弱）",
    4: "⑤ 正常答出来的",
    5: "⑥ 无运行记录（运行记录是 2026-10-08 才有的，更早的会话查不到当时表现）",
}


def _tier_of(info: Dict[str, Any]) -> int:
    """按「答得好不好」分档；info 为 None 表示这条提问没有对应的运行记录"""
    if info is None:
        return 5
    outcome = info.get("outcome")
    if outcome in ("no_match", "error", "blocked"):
        return 0
    # 认不出产品 → 图中断问用户。**它压根没跑到检索**，所以不能按 topk_local 判它召回弱
    if outcome == "waiting_user":
        return 1
    if info.get("web_only"):
        return 2
    if (info.get("topk_local") or 0) <= 1:
        return 3
    return 4


def cmd_candidates(args) -> int:
    """
    列出真实提问，**按「答得好不好」排序**（答得差的在前）

    数据来自两处，合起来才全：
    - `query_runs`：运行记录（2026-10-08 起）——**直接带着结局**：outcome / web_only / topk_local
    - `chat_message`：会话历史（更早的提问只有这里有）

    同一个问题问过多次时取**最差**的那次 —— 挑用例就是要挑出问题来。
    """
    from app.clients.mongo_history_utils import get_history_mongo_tool
    from app.clients.mongo_run_utils import get_query_run_tool

    # ① 运行记录：问题 → 最差表现
    best: Dict[str, Dict[str, Any]] = {}
    for r in get_query_run_tool().collection.find(
            {}, {"question": 1, "outcome": 1, "web_only": 1, "topk_local": 1, "ts": 1}):
        q = (r.get("question") or "").strip()
        if not q:
            continue
        if _tier_of(r) < _tier_of(best.get(q)):
            best[q] = r

    # ② 会话历史：更早的提问（运行记录里没有的）
    seen, ordered = set(), []
    for r in get_history_mongo_tool().db["chat_message"].find({"role": "user"}, {"text": 1}).sort("_id", 1):
        text = (r.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)

    # 两侧合并：历史里出现过的全要（用运行记录补表现），运行记录独有的也补上
    questions = list(ordered) + [q for q in best if q not in seen]

    # 注入尝试不是「真实提问」—— 它们是 P5 的对抗用例素材，混进来只会把这份检索评测集搅浑
    from app.core.input_guard import check_user_input
    attacks = [q for q in questions if check_user_input(q)]
    questions = [q for q in questions if not check_user_input(q)]

    buckets: Dict[int, List[tuple]] = {t: [] for t in _TIER_TITLES}
    for q in questions:
        info = best.get(q)
        buckets[_tier_of(info)].append((q, info))

    total = sum(len(v) for v in buckets.values())
    print(f"\n真实提问 {total} 条（去重后，**按「答得好不好」排序**）：\n")
    shown = 0
    for tier, title in _TIER_TITLES.items():
        items = buckets[tier]
        if not items:
            continue
        print(f"  ── {title}（{len(items)} 条）──")
        for q, info in items:
            if shown >= args.limit:
                break
            detail = ""
            if info is not None:
                detail = f"  ← {info.get('outcome')}"
                if info.get("web_only"):
                    detail += "·纯联网"
                elif (info.get("topk_local") or 0) > 0:
                    detail += f"·本地{info.get('topk_local')}条"
            # 很短的提问多半是多轮里的追问（指代类）—— 单独拿来当用例会缺上文
            hint = "　（像是多轮追问，挑之前先看上文）" if len(q) <= 6 else ""
            print(f"    [{len(q):>4} 字] {' '.join(q.split())[:64]}{detail}{hint}")
            shown += 1
        print()
        if shown >= args.limit:
            print(f"  （已显示 {shown} 条，用 --limit 调）\n")
            break
    if attacks:
        print(f"  另跳过 **{len(attacks)} 条注入尝试** —— 那是 P5 的对抗用例素材，不属于这份检索评测集\n")
    print("  挑法：**从上面往下挑**，一档挑几条；每一档都挑一些，别只挑最差的那档 ——\n"
          "  全是「本来就答不上来」的题，测不出检索变没变好。\n")
    return 0


# ---------------------------
# recall：真跑一次检索，列出候选切片
# ---------------------------
def cmd_recall(args) -> int:
    """
    把问题向量化后直接检索（**不走整图**：标注要看的是「哪些切片可被召回」，
    走整图还要烧重排与生成的配额）
    """
    from app.lm.embedding_utils import generate_embeddings

    question = args.question
    vector = generate_embeddings([question])["dense"][0]
    client = get_milvus_client()
    # dense_search 返回「**每条**查询向量的结果列表」—— 这里只有一条查询，取 [0]
    results = dense_search(
        client, milvus_config.chunks_collection, vector,
        limit=args.top,
        output_fields=["chunk_id", "title", "file_title", "item_name"],
    )
    hits = results[0] if results else []

    print(f"\n「{question}」可召回的切片（Top {args.top}，**不带产品过滤**）：\n")
    for i, h in enumerate(hits, 1):
        # Milvus 的命中结构：分数在 `distance`，`output_fields` 里的字段嵌在 `entity` 里
        # （与 node_search_embedding 的取法一致 —— 那边也是 `h.get("distance")` + `h["entity"]["title"]`）
        entity = h.get("entity") or {}
        title = (entity.get("title") or "").strip().lstrip("#").strip()
        print(f"  {i:>2}. id={entity.get('chunk_id')}  相似度={h.get('distance', 0):.4f}  "
              f"[{entity.get('file_title') or '?'}] {title[:44]}")
    print("\n挑出 gold 后：`eval_cases show <id>` 读正文确认，再填进 cases.yaml 的 gold_chunks\n")
    return 0


# ---------------------------
# show：打印切片正文
# ---------------------------
def cmd_show(args) -> int:
    """按 chunk_id 打印切片，供判断「它是不是能支撑答案」"""
    client = get_milvus_client()
    rows = fetch_chunks_by_chunk_ids(
        client, milvus_config.chunks_collection, [int(c) for c in args.chunk_ids],
        output_fields=["chunk_id", "title", "parent_title", "file_title", "item_name", "content"])
    found = {r["chunk_id"] for r in rows}
    for c in args.chunk_ids:
        if int(c) not in found:
            print(f"\n── {c} ── 在 Milvus 里不存在（切片被重导过？）")

    for r in rows:
        content = (r.get("content") or "").strip()
        title = (r.get("title") or "").strip().lstrip("#").strip()
        print(f"\n── {r['chunk_id']} ── [{r.get('file_title') or '?'}] {title}")
        print(f"   产品：{r.get('item_name') or '?'}　正文 {len(content)} 字")
        limit = args.chars
        body = content[:limit].replace("\n", "\n   ")
        if len(content) > limit:
            body += f"\n   ……（还有 {len(content) - limit} 字没显示，加 --chars {len(content)} 看全文）"
        print("   " + body)
    print()
    return 0


# ---------------------------
# rounds：两轮标注的一致率
# ---------------------------
def cmd_rounds(args) -> int:
    """
    比两份标注的 gold 一致率（规范要求：隔一天重标一遍，< 90% 说明规范没写清）

    只比 `gold_chunks` —— 那才是主观判断的部分；`question`/`expect` 那些是抄来的，不会变
    """
    first = {c["id"]: set(c.get("gold_chunks") or []) for c in load_cases(args.file) if c.get("id")}
    second = {c["id"]: set(c.get("gold_chunks") or []) for c in load_cases(args.other) if c.get("id")}

    both = sorted(set(first) & set(second))
    only_first = sorted(set(first) - set(second))
    only_second = sorted(set(second) - set(first))
    if not both:
        print("\n两份文件没有共同的用例 id，没法比\n")
        return 1

    diff = [(cid, first[cid], second[cid]) for cid in both if first[cid] != second[cid]]
    agree = (len(both) - len(diff)) / len(both)

    print(f"\n两轮标注一致率：{agree:.1%}（{len(both) - len(diff)}/{len(both)} 条一致）")
    if only_first or only_second:
        print(f"  仅第一轮有：{only_first or '（无）'}")
        print(f"  仅第二轮有：{only_second or '（无）'}")
    if diff:
        print("\n不一致的用例（先看这些是不是**规范没说清**，别急着改标注）：")
        for cid, a, b in diff:
            print(f"  · {cid}：第一轮 {sorted(a)}　第二轮 {sorted(b)}")
    print(f"\n判据：一致率 < 90% 就先改规范再重标（见 evaluation-plan.md §3.4）\n")
    return 0 if agree >= 0.9 else 1


# ---------------------------
# worksheet：把标注要看的材料一次性跑出来
# ---------------------------
# 切片正文里的图片是 `![描述](http://…很长的URL)`；标注看的是**描述**（导入时视觉模型写的），
# URL 只会把文件撑长，所以压成 `[图：描述]`
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)\n]+\)")


def _tidy(text: str) -> str:
    """把正文里的图片压成 `[图：描述]`，其余原样"""
    return _IMAGE_RE.sub(lambda m: f"[图：{(m.group(1) or '').strip() or '无描述'}]", text or "")


def cmd_worksheet(args) -> int:
    """
    生成「标注工作表」：每条用例 → 可召回的候选切片（含正文摘要）→ 一个 Markdown 文件

    **为什么要有它**：标注真正费时的是**来回敲命令** —— 每条用例先 `recall` 一次，
    再对每条候选 `show` 一次。这里一次跑完，人打开文件就能判断；
    **判断仍然是人的事，工具只负责把材料摆到眼前。**

    候选**不按产品过滤**（与 `recall` 一致）—— 跨文档用例本来就该看到别的文档的切片；
    但每条会标出「同文档 / 别的文档」，方便你决定。
    """
    from app.lm.embedding_utils import generate_embeddings

    cases = load_cases(args.file)
    out: List[str] = [
        "# 标注工作表（`eval_cases worksheet` 生成，可随时重跑）",
        "",
        "**怎么用**：逐条读下面的候选切片，判断「**光凭它自己**能不能答完这个问题的全部要点」，",
        "把够格的 `id` 填回 `eval/cases.yaml` 的 `gold_chunks`。只沾边的**不填**；",
        "够格的有几条就填几条（Recall@K 看「有没有捞到」，不是「恰好捞到那一条」）。",
        "",
        "**别拿第一条当答案** —— 那等于把系统的输出当标准，评测就变成自己考自己。",
        "判断不了的用例就跳过，别硬标。",
        "",
        f"⚠️ **正文按 {args.chars} 字截断**。被截断的那几条**会明确写出「还有多少字没显示」** —— "
        "看到就按提示跑 `show <id> --chars <更大>` 看全文再判。",
        "（**这不是形式主义**：答案可能就藏在被截掉的那部分 —— 一张快捷键表里的「音量」就藏在表格中部，",
        "有人据此错判成「文档里没这块内容」、差点删掉一条好用例。）",
        "",
        "---",
        "",
    ]
    skipped = 0
    mapping: List[str] = []          # 末尾的「序号 → id 对照」，防止把序号当 id 填进 gold
    for case in cases:
        cid = case.get("id")
        if not cid:
            continue
        question = case.get("question") or ""
        doc = case.get("doc") or ""
        per_case: List[str] = []     # 这条用例的「序号 = id」，末尾汇总成对照表
        marks = [f"`{cid}`", f"类型 {case.get('category') or '?'}"]
        if doc:
            marks.append(f"文档 {doc}")
        out.append(f"## {question}")
        out.append("")
        out.append("　".join(marks))
        if case.get("gold_chunks"):
            out.append("")
            out.append(f"> 已标：{case['gold_chunks']}（想复核就往下看候选）")
        out.append("")

        vector = generate_embeddings([question])["dense"][0]
        results = dense_search(
            get_milvus_client(), milvus_config.chunks_collection, vector,
            limit=args.top,
            output_fields=["chunk_id", "title", "file_title", "item_name", "content"])
        hits = results[0] if results else []
        if not hits:
            out.append("（一条候选都没有 —— 这本身是个发现：问题该归到「库外」，或知识库缺这块内容）")
            out.append("")
            skipped += 1
            continue

        for i, h in enumerate(hits, 1):
            entity = h.get("entity") or {}
            title = (entity.get("title") or "").strip().lstrip("#").strip()
            same = "同文档" if doc and entity.get("file_title") == doc else "别的文档"
            # 标出 item_name：那才是检索链路**真正用来过滤**的条件（不是相似度阈值 ——
            # 本项目没有相似度阈值，`.env` 里那个 MILVUS_MIN_COSINE_SCORE 没有任何代码在读）
            out.append(f"### {i}. `{entity.get('chunk_id')}`　相似度 {h.get('distance', 0):.4f}　"
                       f"[{entity.get('file_title') or '?'}·{same}｜产品 {entity.get('item_name') or '?'}] "
                       f"{title}")
            per_case.append(f"{i} = `{entity.get('chunk_id')}`")
            out.append("")
            content = _tidy((entity.get("content") or "").strip())
            out.append("```text")
            if len(content) > args.chars:
                out.append(content[: args.chars])
                # **不能只写「截断」**：2026-10-10 有人（我）就是把截断当全文，
                # 判定「文档里没有这块内容」，差点删掉一条真能命中的用例。
                # 所以这里明确写「还差多少字」+ 给出看全文的命令
                out.append(f"……（**这条正文还有 {len(content) - args.chars} 字没显示**，"
                           f"判定前请先跑："
                           f"`eval_cases show {entity.get('chunk_id')} --chars {len(content)}`）")
            else:
                out.append(content)
            out.append("```")
            out.append("")
        out.append("---")
        out.append("")
        if per_case:
            mapping.append(f"**{question}**")
            mapping.append("　".join(per_case))
            mapping.append("")

    # 末尾的对照表：**第一次真用就有人把序号当 id 填进了 gold_chunks** ——
    # 候选标题里的 `### 1.` 是编号，真正要填的是它后面那串 18 位数字
    if mapping:
        out += ["---", "", "## 附：序号 → chunk_id 对照",
                "", "填 `gold_chunks` 时**抄右边那串数字，不是左边的序号**：", ""]
        out += mapping

    target = Path(args.out)
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"\n工作表已写入 {target}（{len(cases)} 条用例、候选每条截 {args.chars} 字）")
    if skipped:
        print(f"  其中 {skipped} 条一条候选都没有 —— 那些值得单独看一眼")
    print("  下一步：读它、判断、把 gold 填回 eval/cases.yaml，再跑 check\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="app.core.eval_cases",
        description="评测用例集的人工标注脚手架（格式与规范见 eval/cases.yaml 头部）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # `--file` 挂在子命令上（不是全局参数）—— 全局参数必须写在子命令**前面**，
    # 而人下意识会写 `check --file xxx.yaml`，那样会被 argparse 拒掉
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", type=Path, default=DEFAULT_CASES_PATH, help="用例集文件")

    sub.add_parser("check", parents=[common], help="校验格式、gold 是否存在、标注进度")

    p_cand = sub.add_parser("candidates", help="导出真实提问（挑用例用）")
    p_cand.add_argument("--limit", type=int, default=60)

    p_recall = sub.add_parser("recall", help="检索一遍，列出可召回的切片（挑 gold 用）")
    p_recall.add_argument("question")
    p_recall.add_argument("--top", type=int, default=10)

    p_show = sub.add_parser("show", help="打印切片正文（判断它是不是 gold）")
    p_show.add_argument("chunk_ids", nargs="+")
    p_show.add_argument("--chars", type=int, default=600, help="每条正文打印多少字")

    p_rounds = sub.add_parser("rounds", parents=[common], help="两轮标注的一致率")
    p_rounds.add_argument("other", type=Path, help="第二轮标注的文件")

    p_sheet = sub.add_parser("worksheet", parents=[common],
                             help="生成标注工作表（候选 + 正文，落成 Markdown）")
    p_sheet.add_argument("--top", type=int, default=8, help="每条用例列几个候选")
    p_sheet.add_argument("--chars", type=int, default=500, help="每条候选正文截多少字")
    p_sheet.add_argument("--out", type=Path, default=PROJECT_ROOT / "eval" / "worksheet.md")

    args = ap.parse_args(argv)
    handlers = {
        "check": cmd_check, "candidates": cmd_candidates,
        "recall": cmd_recall, "show": cmd_show, "rounds": cmd_rounds,
        "worksheet": cmd_worksheet,
    }
    try:
        return handlers[args.cmd](args)
    except FileNotFoundError as e:
        print(f"\n{e}\n")
        return 2
    except PyMongoError as e:
        # 依赖没起是环境问题、不是这个工具的 bug —— 给人话，别甩 traceback
        print(f"\n连不上 MongoDB：{type(e).__name__}。确认容器起了：docker start mongo\n")
        return 2
    except MilvusException as e:
        print(f"\n连不上 Milvus：{type(e).__name__}。确认容器起了：docker start milvus-standalone\n")
        return 2
    except Exception as e:
        logger.error(f"[标注脚手架] {args.cmd} 执行失败：{e}", exc_info=True)
        print(f"\n执行失败：{type(e).__name__}: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
