"""final 事件契约的 pytest 用例。"""
from app.agent.runtime.events import FINAL_FIELDS, build_final_event


def test_final_event_contract():
    """final 事件必须带商品卡/表单到两个 transport 共用的 payload。"""
    ev = {
        "type": "final", "text": "给你推荐 2 款", "web_only": True,
        "images": [{"url": "http://img/1.png", "caption": "图 1"}],
        "products": [{"productId": "11", "name": "路由器"}],
        "forms": [{"form_id": "f1", "form_type": "refund_reason"}],
    }
    payload = build_final_event(ev)
    assert payload["text"] == "给你推荐 2 款"
    assert [p.get("productId") for p in payload["products"]] == ["11"]
    assert [f.get("form_id") for f in payload["forms"]] == ["f1"]
    assert payload["images"] == ev["images"]
    assert payload["web_only"] is True

    empty = build_final_event({"type": "final"})
    for key in FINAL_FIELDS:
        assert key in empty, f"final 空事件缺字段：{key}"
