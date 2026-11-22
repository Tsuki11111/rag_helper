"""子图记账：mark/since 只返回增量，父账仍保留全部调用。"""
from app.core.usage_tracker import UsageAccumulator


def test_mark_since_returns_delta_and_parent_keeps_total():
    acc = UsageAccumulator()
    acc.add({"ok": True, "prompt_tokens": 10, "completion_tokens": 5,
             "total_tokens": 15, "cost": 0.10, "node": "parent", "kind": "llm"})
    mark = acc.mark()
    acc.add({"ok": True, "prompt_tokens": 20, "completion_tokens": 8,
             "total_tokens": 28, "cost": 0.25, "node": "rag_subagent", "kind": "embedding"})

    delta = acc.since(mark)
    assert delta["calls"] == 1
    assert delta["total_tokens"] == 28
    assert abs(delta["cost"] - 0.25) < 1e-9
    assert {n["node"] for n in delta["by_node"]} == {"rag_subagent"}

    total = acc.summary()
    assert total["calls"] == 2
    assert abs(total["cost"] - 0.35) < 1e-9
