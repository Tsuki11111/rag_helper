"""检索缓存：键必须带知识库版本与联网开关；降级重排不得写缓存。"""
import app.agent.subagents.product_kb.application.nodes.node_rerank as nr


def _state(**overrides):
    state = {
        "session_id": "s1",
        "user_question": "HAK180 怎么装烫金膜盒",
        "rewritten_query": "HAK180 怎么装烫金膜盒",
        "item_names": ["Brother HAK 180 烫金机"],
        "enable_web_search": False,
        "retrieval_round": 0,
        "is_stream": False,
    }
    state.update(overrides)
    return state


def test_cache_key_changes_with_version_and_web(monkeypatch):
    monkeypatch.setattr(nr, "get_kb_cache_version", lambda: 1)
    base = nr._retrieval_cache_key(_state())

    monkeypatch.setattr(nr, "get_kb_cache_version", lambda: 2)
    assert nr._retrieval_cache_key(_state()) != base

    monkeypatch.setattr(nr, "get_kb_cache_version", lambda: 1)
    assert nr._retrieval_cache_key(_state(enable_web_search=True)) != base


def test_rerank_failure_marks_degraded(monkeypatch):
    def _boom(question, texts):
        raise RuntimeError("rerank down")

    monkeypatch.setattr(nr, "rerank", _boom)
    docs, degraded = nr.step_2_rerank_docs(_state(), [{"text": "x", "source": "local"}])

    assert degraded is True
    assert docs[0]["score"] == 0.0


def test_degraded_result_is_not_cached(monkeypatch):
    saved = []
    monkeypatch.setattr(nr, "_load_cached_retrieval", lambda key: None)
    monkeypatch.setattr(nr, "_save_cached_retrieval", lambda *args: saved.append(args))
    monkeypatch.setattr(nr, "get_kb_cache_version", lambda: 1)
    monkeypatch.setattr(nr, "step_1_merge_docs", lambda state: [{"text": "x", "source": "local"}])
    monkeypatch.setattr(nr, "step_2_rerank_docs",
                        lambda state, items: ([{**items[0], "score": 0.0}], True))
    monkeypatch.setattr(nr, "step_3_topk", lambda docs: (docs, False))

    nr.node_rerank(_state())

    assert saved == []
