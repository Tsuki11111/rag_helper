"""HTTP 非流式 /query 与 /history 必须带 products/forms，和 gRPC/SSE 对齐。"""
import asyncio

from fastapi import BackgroundTasks

import app.agent.api.query_service as qs


def test_query_response_includes_products_and_forms(monkeypatch):
    monkeypatch.setattr(qs, "_claim_session", lambda user, sid: "u1")
    monkeypatch.setattr(qs, "update_task_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(qs, "clear_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(qs, "run_main_agent", lambda *args, **kwargs: {
        "reply": "好的", "outcome": "answered", "error": "",
        "images": [], "web_only": False,
        "products": [{"productId": 1, "name": "游戏本"}],
        "forms": [{"form_id": "form_1", "form_type": "refund_reason"}],
        "usage": {},
    })

    result = asyncio.run(qs.query(
        BackgroundTasks(),
        qs.QueryRequest(query="你好", session_id="s1"),
        user={"user_id": "u1", "tenant_id": "t1", "role": "viewer"},
    ))

    assert result["products"] == [{"productId": 1, "name": "游戏本"}]
    assert result["forms"][0]["form_id"] == "form_1"


def test_history_includes_products_and_forms(monkeypatch):
    monkeypatch.setattr(qs, "_ensure_session_access", lambda user, sid: None)
    monkeypatch.setattr(qs, "get_recent_messages", lambda *args, **kwargs: [{
        "_id": "m1", "session_id": "s1", "role": "assistant", "text": "好的",
        "item_names": [], "images": [], "trace": [], "ts": 1,
        "products": [{"productId": 2, "name": "办公本"}],
        "forms": [{"form_id": "form_2", "form_type": "address_edit"}],
    }])

    result = asyncio.run(qs.get_history(
        "s1", user={"user_id": "u1", "tenant_id": "t1", "role": "viewer"}))

    assert result["items"][0]["products"][0]["productId"] == 2
    assert result["items"][0]["forms"][0]["form_id"] == "form_2"
