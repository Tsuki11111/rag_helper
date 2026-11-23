"""产品实体解析：依赖不可用不能伪装成 no_match。"""
import pytest

import app.agent.subagents.product_kb.application.product_resolver as pr
from app.agent.subagents.product_kb.application.nodes.node_item_name_confirm import (
    step_6_check_confirmation,
)
from app.agent.subagents.product_kb.domain.outcome import (
    OUTCOME_ERROR,
    SERVICE_UNAVAILABLE_ANSWER,
    judge_outcome,
)


def test_milvus_down_returns_unavailable(monkeypatch):
    monkeypatch.setattr(pr, "get_kb_cache_version", lambda: 0)
    monkeypatch.setattr(pr, "get_milvus_client", lambda: None)
    pr._CACHE.update({"created_at": 0.0, "version": -1, "resolver": None})

    result = pr.resolve_product("HAK180 怎么安装")

    assert result["status"] == "unavailable"
    assert result["item_names"] == []


def test_programming_error_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(pr, "get_resolver", lambda: (_ for _ in ()).throw(NameError("bad")))

    with pytest.raises(NameError):
        pr.resolve_product("HAK180 怎么安装")


def test_unavailable_branch_answers_service_error():
    result = step_6_check_confirmation(
        {"status": "unavailable", "confirmed_item_names": [], "candidates": [], "near": []},
        "s1", "HAK180 怎么安装", "HAK180 怎么安装")

    assert result["answer"] == SERVICE_UNAVAILABLE_ANSWER
    assert not result.get("need_confirm")
    assert judge_outcome({"answer": result["answer"], "reranked_docs": []}) == OUTCOME_ERROR
