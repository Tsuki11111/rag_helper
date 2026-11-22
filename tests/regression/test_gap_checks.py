"""知识缺口回归用例（从 app/core/knowledge_gaps.py 迁出）。"""
import pytest

import app.core.knowledge_gaps as _kg

globals().update({k: v for k, v in vars(_kg).items() if not k.startswith("__")})


def _check_gap_aggregation_pure() -> List[str]:
    """缺口口径：两种结局都算、注入不算、同问题合并、产品归因、min_count 过滤"""
    problems: List[str] = []
    runs = _sample_runs()
    gaps = aggregate_gaps(runs)
    by_question = {g["question"]: g for g in gaps}

    if len(gaps) != 1:
        problems.append(f"应聚合出 1 个缺口（注入与正常命中都不算），实为 {len(gaps)}："
                        f"{[g['question'] for g in gaps]}")
    merged = by_question.get("HAK 180 可以连接 WiFi 吗？")
    if merged is None:
        problems.append("no_match 的缺口没被聚合出来")
    elif merged["count"] != 2:
        problems.append(f"同一问题（大小写/空格/标点不同）应合并计数为 2，实为 {merged['count']}")
    elif merged["products"] != ["Brother HAK 180 烫金机"]:
        problems.append(f"产品归因不对：{merged['products']}")
    if any("系统提示词" in g["question"] for g in gaps):
        problems.append("注入尝试被算成了知识缺口")
    if any("万用表校准" == g["question"] or "万用表怎么校准" == g["question"] for g in gaps):
        problems.append("本地命中过的正常提问被算成了缺口")
    if len(aggregate_gaps(runs, min_count=2)) != 1:
        problems.append("min_count=2 应按频次过滤掉只出现一次的问题")

    # gap_id 稳定性：同问题不同写法必须同 id
    if make_gap_id("HAK 180 可以连接 WiFi 吗？") != make_gap_id("hak180 可以连接 wifi 吗"):
        problems.append("gap_id 对大小写/空格/标点不归一，状态会挂不住")
    return problems


def _cluster_test_gaps() -> List[Dict[str, Any]]:
    """聚类测试用：凑 3 个不同产品的缺口（保证有足够的向量可比）"""
    runs = _sample_runs() + [
        {"question": "RS-12 万用表怎么校准", "outcome": "no_match",
         "item_names": ["万用表RS-12"], "topk_local": 0, "topk_web": 0,
         "datetime": "2026-11-05 09:00:00"},
        {"question": "P3030 怎么换硒鼓", "outcome": "no_match",
         "item_names": ["奔图 P3030"], "topk_local": 0, "topk_web": 0,
         "datetime": "2026-11-05 10:00:00"},
    ]
    return aggregate_gaps(runs)


def _check_gap_cluster_fallback() -> List[str]:
    """聚类：embedding 失败不报错（每条自成一簇）；相似向量能合簇且不丢问题"""
    problems: List[str] = []
    gaps = _cluster_test_gaps()
    if len(gaps) != 3:
        return [f"聚类测试数据异常：应 3 个缺口，实为 {len(gaps)}"]

    try:
        fallback = cluster_gaps(gaps, embed_fn=lambda ts: (_ for _ in ()).throw(RuntimeError("embed down")))
    except Exception as e:
        return [f"embedding 失败时聚类抛了异常：{e}"]
    if len(fallback) != len(gaps):
        problems.append(f"降级时应每条自成一簇（{len(gaps)}），实为 {len(fallback)}")
    if sorted(x for c in fallback for x in c["gap_ids"]) != sorted(g["gap_id"] for g in gaps):
        problems.append("降级簇丢掉了缺口 id")

    # 相似向量：两条几乎同向 → 合簇；第三条正交 → 单独成簇
    if len(gaps) >= 3:
        vecs = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]
        merged = cluster_gaps(gaps, embed_fn=lambda ts: vecs)
        if len(merged) != 2:
            problems.append(f"相似向量应聚成 2 簇，实为 {len(merged)}")
        if sorted(x for c in merged for x in c["gap_ids"]) != sorted(g["gap_id"] for g in gaps):
            problems.append("合簇后缺口 id 丢失或重复")
    return problems


def _check_gap_report_and_draft() -> List[str]:
    """周报结构 + 草稿的「待人工核实」强制标记（打桩模型，不调外部）"""
    problems: List[str] = []
    gaps = aggregate_gaps(_sample_runs())
    clusters = cluster_gaps(gaps, embed_fn=lambda ts: [[1.0, 0.0] for _ in ts])
    report = render_report(gaps, clusters, days=7)

    for marker in ("# 知识缺口周报", "## 按产品分布", "## Top 缺口", "## 建议"):
        if marker not in report:
            problems.append(f"周报缺少段落：{marker}")
    if "HAK 180" not in report:
        problems.append("周报里看不到具体缺口问题")
    if render_report([], [], days=1).count("知识缺口周报") != 1:
        problems.append("空缺口时周报无法渲染")

    draft = generate_draft(gaps, llm_invoke=lambda prompt: "这是模型写的草稿正文")
    if "待人工核实" not in draft:
        problems.append("草稿缺少「待人工核实」标记（验收项）")
    if not draft.startswith("#"):
        problems.append("草稿没有一级标题，不能直接导入")
    draft2 = generate_draft(gaps, llm_invoke=lambda prompt: "# 自定义标题\n\n> 待人工核实：已确认。")
    if not draft2.startswith("# 自定义标题"):
        problems.append("模型已给标题时被覆盖")
    return problems


def _check_gap_status_roundtrip() -> List[str]:
    """状态写入/读回（Mongo）；用完即删，不污染真实数据"""
    problems: List[str] = []
    gid = "gap_selftest_knowledge_gap"
    try:
        set_status([gid], STATUS_RESOLVED, note="自测")
        got = load_status([gid]).get(gid) or {}
        if got.get("status") != STATUS_RESOLVED:
            problems.append(f"状态未按预期写入/读回：{got}")
        set_status([gid], STATUS_IGNORED)
        if (load_status([gid]).get(gid) or {}).get("status") != STATUS_IGNORED:
            problems.append("状态覆盖写没生效")
        try:
            set_status([gid], "not-a-status")
        except ValueError:
            pass
        else:
            problems.append("非法状态没有被拒绝")
    except KnowledgeGapStoreUnavailable as e:
        problems.append(f"状态库不可用：{e}")
    finally:
        try:
            _get_status_collection().delete_many({"gap_id": gid})
        except Exception:
            pass
    return problems


# ── CLI ──

PURE_CHECKS = [
    ("_check_gap_aggregation_pure", _check_gap_aggregation_pure),
    ("_check_gap_cluster_fallback", _check_gap_cluster_fallback),
    ("_check_gap_report_and_draft", _check_gap_report_and_draft),
]

MONGO_CHECKS = [
    ("_check_gap_status_roundtrip", _check_gap_status_roundtrip),
]

@pytest.mark.parametrize("name,fn", PURE_CHECKS, ids=[n for n, _ in PURE_CHECKS])
def test_gaps_pure(name, fn):
    assert fn() == []


@pytest.mark.integration
@pytest.mark.parametrize("name,fn", MONGO_CHECKS, ids=[n for n, _ in MONGO_CHECKS])
def test_gaps_mongo(name, fn, mongo_ready):
    assert fn() == []
