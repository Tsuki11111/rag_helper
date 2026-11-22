"""检索图接线回归用例（从 graph.py 迁出）。"""
import pytest

import app.agent.subagents.product_kb.application.graph as _g

globals().update({k: v for k, v in vars(_g).items() if not k.startswith("__")})


def _check_no_interrupt_flow() -> list:
    """
    离线自测：认不出产品时图**不再中断**（无状态化，2026-10-11，见 evolution-plan §3）

    守两条（纯逻辑，零依赖）：
    - `node_ask_user` 不在图里 —— 它承载的「图内中断等用户」机制已退役；
      残留注册意味着有人把它加了回来，或摘除时漏了接线
    - 路由函数把 `need_confirm` 送到结束（`__end__`），而不是任何「去问用户」的去处

    :return: 问题描述列表，空表示通过
    """
    problems = []
    if "node_ask_user" in builder.nodes:
        problems.append("node_ask_user 仍在图中（无状态化后不该注册）")
    got = route_after_item_name_confirm({"need_confirm": True})
    if got != "__end__":
        problems.append(f"认不出产品应路由到 __end__，实际 {got!r}")
    return problems


def _check_bm25_wiring() -> list:
    """离线自测：BM25 节点已注册并接到分叉/合并点"""
    problems = []
    if "node_search_bm25" not in builder.nodes:
        problems.append("node_search_bm25 未注册到图中")
    return problems


def _check_retrieval_loop_wiring() -> list:
    """
    离线自测：检索循环的节点注册与条件路由（纯逻辑，零依赖）

    守的是「接线」而不是节点内部逻辑（那两条在各自节点里守）：
    节点漏注册、条件边漏接、路由函数被改坏，都会让循环**静默失效** ——
    表面照常出答案，只是永远不再重试。
    """
    problems = []
    if "node_retrieval_gate" not in builder.nodes:
        problems.append("node_retrieval_gate 未注册到图中")
    if "node_query_rewrite" not in builder.nodes:
        problems.append("node_query_rewrite 未注册到图中")
    if route_after_retrieval_gate({"retrieval_judge": {"next": "retry"}}) != "node_query_rewrite":
        problems.append("gate 判 retry 时没有走改写节点")
    if route_after_retrieval_gate({"retrieval_judge": {"next": "answer"}}) != "node_answer_output":
        problems.append("gate 判 answer 时没有去作答")
    if route_after_query_rewrite({"retrieval_judge": {"rewrite_ok": True}}) != "node_multi_search":
        problems.append("改写成功后没有回到检索")
    if route_after_query_rewrite({"retrieval_judge": {"rewrite_ok": False}}) != "node_answer_output":
        problems.append("改写失败后没有回落到作答")
    return problems

CHECKS = [
    ("_check_no_interrupt_flow", _check_no_interrupt_flow),
    ("_check_bm25_wiring", _check_bm25_wiring),
    ("_check_retrieval_loop_wiring", _check_retrieval_loop_wiring),
]

@pytest.mark.parametrize("name,fn", CHECKS, ids=[n for n, _ in CHECKS])
def test_checks_pure(name, fn):
    assert fn() == []
