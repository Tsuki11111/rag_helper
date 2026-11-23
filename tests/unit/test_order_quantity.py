"""下单数量：显式 0/负数/超过上限必须拒绝，不能静默改成 1。"""
import pytest

from app.agent.subagents.order_service.application.order_action_service import (
    OrderActionService,
)


@pytest.mark.parametrize("quantity", [0, -1, 100])
def test_invalid_quantity_rejected(quantity):
    calls = []
    service = OrderActionService(call_fn=lambda *args, **kwargs: calls.append(args) or ({}, ""))

    text = service.prepare_order(
        [{"productId": 1, "skuId": 2, "quantity": quantity}])

    assert "购买数量" in text
    assert calls == []


def test_missing_quantity_defaults_to_one(monkeypatch):
    calls = []

    def _call(path, payload=None, **kwargs):
        calls.append((path, payload))
        return {"summary": {"itemCount": 1, "totalAmount": 1,
                            "address": {}, "items": []}}, ""

    service = OrderActionService(call_fn=_call)
    monkeypatch.setattr(service, "precheck_items", lambda items: "")
    service.prepare_order([{"productId": 1, "skuId": 2}])

    assert calls
    assert calls[0][1]["items"][0]["quantity"] == 1
