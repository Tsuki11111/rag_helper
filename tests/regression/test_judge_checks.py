"""judge 回归用例（从 app/eval/judge.py 迁出）。"""
import pytest

import app.eval.judge as _j

globals().update({k: v for k, v in vars(_j).items() if not k.startswith("__")})


def _check_judge_config_guard() -> list:
    from app.conf.judge_config import JudgeConfig
    problems = []
    if JUDGE_SYSTEM != load_prompt("judge_system").strip():
        problems.append("judge system prompt 没有从 prompts/judge_system.prompt 读取")
    generation = [("RAG 生成模型", "qwen-plus"), ("主 agent 模型", "deepseek-flash")]
    gen = JudgeConfig("qwen-plus", "http://ci.invalid", "k", 0)
    errors, _ = validate_judge_config(gen, generation, allow_same_model=False)
    if not any("不能与" in e for e in errors):
        problems.append("judge 与生成模型同名时没有拒绝")
    dated = JudgeConfig("qwen-plus-2025-04-28", "http://ci.invalid", "k", 0)
    errors, _ = validate_judge_config(dated, generation, allow_same_model=False)
    if not any("不能与" in e for e in errors):
        problems.append("judge 与生成模型同族日期快照时没有拒绝")
    ok = JudgeConfig("judge-different", "http://ci.invalid", "k", 0)
    errors, _ = validate_judge_config(ok, generation, allow_same_model=False)
    if any("不能与" in e for e in errors):
        problems.append(f"不同模型被误拒：{errors}")
    # 显式开开关时同模型只警告，用来跑便宜的日常口径
    errors, warnings = validate_judge_config(gen, generation, allow_same_model=True)
    if any("不能与" in e for e in errors):
        problems.append(f"开关打开后同模型仍被拒：{errors}")
    if not any("自评" in w for w in warnings):
        problems.append(f"开关打开后缺少自评偏差警告：{warnings}")
    return problems


def _check_judge_output_parsing() -> list:
    problems = []
    raw = ('```json\n{"claims":[{"claim":"A","supported":"true","evidence_ids":["S1"]},'
           '{"claim":"B","supported":false,"evidence_ids":[99]}],'
           '"answer_relevance":4,"is_refusal":"false","score":0.5}\n```')
    parsed = parse_judge_output(raw, evidence_count=1)
    if parsed["groundedness"] != 0.5 or parsed["total_claims"] != 2:
        problems.append(f"claims 重算不对：{parsed}")
    if parsed["claims"][1]["supported"] is not False:
        problems.append("越界 evidence_id 没有被强制 unsupported")
    if not parsed["warnings"]:
        problems.append("越界 evidence_id 没有告警")
    empty = parse_judge_output('{"claims":[],"answer_relevance":3,"is_refusal":true,"score":1}', 0)
    if empty["groundedness"] is not None:
        problems.append("空 claims 的 groundedness 应为 None")
    try:
        parse_judge_output("not json", 0)
        problems.append("坏 JSON 没有抛错")
    except JudgeParseError:
        pass
    return problems


def _check_advisory_claims() -> list:
    """回归：advisory 与 unsupported 可分，且 advisory 不计入幻觉率（requirements 2.21）。"""
    problems = []
    raw = ('{"claims":['
           '{"claim":"A","supported":true,"evidence_ids":["S1"]},'
           '{"claim":"B","claim_type":"advisory","hedged":true,'
           '"hedge_text":"仅供参考","evidence_ids":[]},'
           '{"claim":"C","claim_type":"advisory","hedged":false},'
           '{"claim":"D","claim_type":"advisory","hedged":true,"contradicted":true,'
           '"evidence_ids":["S2"]}],'
           '"answer_relevance":3,"is_refusal":false,"score":0.25}')
    parsed = parse_judge_output(raw, evidence_count=2)
    expect = {"grounded": 1, "advisory": 1, "unsupported": 1, "contradicted": 1}
    got = {}
    for claim in parsed["claims"]:
        got[claim["claim_type"]] = got.get(claim["claim_type"], 0) + 1
    if got != expect:
        problems.append(f"claim_type 归类不对：{got}")
    if (parsed["supported_claims"], parsed["advisory_claims"],
            parsed["unsupported_claims"], parsed["contradicted_claims"]) != (1, 1, 1, 1):
        problems.append(f"逐类计数不对：{parsed}")
    if parsed["silent_hallucination_claims"] != 2:
        problems.append(f"advisory 不应计入幻觉 claim：{parsed['silent_hallucination_claims']}")
    # 带「仅供」话术但引用了越界证据 → 仍算幻觉，不能靠免责声明豁免
    bad = parse_judge_output(
        '{"claims":[{"claim":"E","claim_type":"advisory","hedged":true,'
        '"hedge_text":"仅供参考","evidence_ids":[9]}]}', evidence_count=2)
    if bad["claims"][0]["claim_type"] != "unsupported":
        problems.append(f"越界证据的 advisory 未降级：{bad['claims'][0]['claim_type']}")
    rows = [{"case_id": "t", "category": "t", "judge": dict(parsed)}]
    summary = aggregate(rows)
    if summary["total_claims"] != 4 or summary["claim_cases"] != 1:
        problems.append(f"汇总 claim 计数不对：{summary['total_claims']}")
    for key, value in (("hallucination_claim_rate", 0.5), ("hallucination_answer_rate", 1.0),
                       ("advisory_claim_rate", 0.25), ("advisory_answer_rate", 1.0),
                       ("evidence_coverage_rate", 0.25)):
        if summary.get(key) != value:
            problems.append(f"{key} 应为 {value}：{summary.get(key)}")
    empty = aggregate([{"case_id": "e", "category": "e", "judge": {
        "parse_ok": True, "groundedness": None, "total_claims": 0}}])
    if empty["hallucination_claim_rate"] is not None:
        problems.append("无 claim 时幻觉率应为 None")
    return problems


def _check_core_claim_metrics() -> list:
    """回归：core 级幻觉与 unsupported/contradicted 拆分（requirements 2.29）。"""
    problems = []
    raw = ('{"claims":['
           '{"claim":"核心步骤","core":true,"claim_type":"grounded","supported":true,'
           '"evidence_ids":["S1"]},'
           '{"claim":"核心参数","core":true,"claim_type":"unsupported"},'
           '{"claim":"外观","core":false,"claim_type":"unsupported"}],'
           '"answer_relevance":5,"is_refusal":false,"score":0.6}')
    parsed = parse_judge_output(raw, evidence_count=1)
    summary = aggregate([{"case_id": "c", "category": "steps", "judge": dict(parsed)}])
    expect = {
        "core_claim_cases": 1,
        "core_total_claims": 2,
        "core_hallucination_claim_rate": 0.5,
        "core_hallucination_answer_rate": 1.0,
        "unsupported_answer_rate": 1.0,
        "contradicted_answer_rate": 0.0,
        "claims_per_answer_mean": 3.0,
    }
    for key, value in expect.items():
        if summary.get(key) != value:
            problems.append(f"{key} 应为 {value}：{summary.get(key)}")
    # 旧 JSONL 没有 core 字段：core 指标必须是 None，不能误报 0
    old = parse_judge_output(
        '{"claims":[{"claim":"x","claim_type":"unsupported"}]}', evidence_count=1)
    old_summary = aggregate([{"case_id": "o", "category": "steps", "judge": dict(old)}])
    for key in ("core_hallucination_claim_rate", "core_hallucination_answer_rate"):
        if old_summary.get(key) is not None:
            problems.append(f"旧数据 {key} 应为 None：{old_summary.get(key)}")
    return problems


def _check_evidence_recall_metrics() -> list:
    problems = []
    merged = merge_evidence([
        {"topk_local": 2, "topk_web": 0, "topk_total": 2,
         "topk_chunk_ids": ["1", "2"],
         "evidence": [{"source": "local", "chunk_id": 1, "text": "a"}]},
        {"topk_local": 1, "topk_web": 1, "topk_total": 2,
         "topk_chunk_ids": ["2", "3"],
         "evidence": [{"source": "web", "url": "u", "text": "b"}]},
    ])
    if merged["topk_chunk_ids"] != ["1", "2", "3"] or len(merged["evidence"]) != 2:
        problems.append(f"证据合并不对：{merged}")
    case = {"gold_chunks": [2, 3], "expect": {"outcome": "answered"}}
    metrics = recall_metrics(case, merged["topk_chunk_ids"])
    if not metrics["eligible"] or metrics["hit"] is not True or metrics["rr"] != 0.5:
        problems.append(f"Recall/MRR 不对：{metrics}")
    skipped = recall_metrics({"gold_chunks": [], "expect": {"outcome": "answered"}}, ["1"])
    if skipped["eligible"] or skipped["has_gold"]:
        problems.append("空 gold 不应参与 Recall")
    non_answer = recall_metrics({"gold_chunks": [1], "expect": {"outcome": "no_match"}}, ["1"])
    if non_answer["eligible"]:
        problems.append("非 answered 用例不应参与 Recall")
    # 同一套数据也守住主 agent 的多派发聚合（去重保序 / local+web 计数）
    from app.agent.runtime.main_agent import _aggregate_sub_evidence
    ids, local, web, total = _aggregate_sub_evidence([
        {"topk_chunk_ids": ["1", "2"], "topk_local": 2, "topk_web": 0, "topk_total": 2},
        {"topk_chunk_ids": ["2", "3"], "topk_local": 1, "topk_web": 1, "topk_total": 2},
    ])
    if ids != ["1", "2", "3"] or (local, web, total) != (3, 1, 4):
        problems.append(f"运行记录证据聚合不对：{ids}, {(local, web, total)}")
    return problems

CHECKS = [
    ("_check_judge_config_guard", _check_judge_config_guard),
    ("_check_judge_output_parsing", _check_judge_output_parsing),
    ("_check_advisory_claims", _check_advisory_claims),
    ("_check_core_claim_metrics", _check_core_claim_metrics),
    ("_check_evidence_recall_metrics", _check_evidence_recall_metrics),
]

@pytest.mark.parametrize("name,fn", CHECKS, ids=[n for n, _ in CHECKS])
def test_judge_pure(name, fn):
    assert fn() == []
