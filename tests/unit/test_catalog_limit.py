"""Java 目录上限：下单预检与导购全量目录都按 100 取数。"""
from app.agent.subagents.order_service.application.order_action_service import (
    OrderActionService,
)
from app.agent.subagents.product_guide.infrastructure.catalog_gateway import (
    JavaCatalogGateway,
)
from app.conf.order_service_config import order_service_config


def test_order_precheck_uses_java_limit_and_tolerates_capped_catalog():
    calls = []

    def fake_call(path, **kwargs):
        calls.append((path, kwargs))
        return [], None

    service = OrderActionService(call_fn=fake_call)
    service.precheck_items([{"productId": 1, "skuId": 1, "quantity": 1}])
    assert calls
    assert calls[0][1]["params"]["limit"] == order_service_config.catalog_page_limit
    assert calls[0][1]["read_only"] is True

    capped = [{"productId": i, "name": f"p{i}", "skus": []}
              for i in range(order_service_config.catalog_page_limit)]
    service = OrderActionService(call_fn=lambda *a, **k: (capped, None))
    text = service.precheck_items([{"productId": 99999, "skuId": 1, "quantity": 1}])
    assert "不在售" not in text


def test_guide_all_products_uses_explicit_limit_and_read_only():
    calls = []

    def fake_call(path, **kwargs):
        calls.append((path, kwargs))
        return [], None

    JavaCatalogGateway(call_fn=fake_call).all_products()
    assert calls
    assert calls[0][1]["params"]["limit"] == order_service_config.catalog_page_limit
    assert calls[0][1]["read_only"] is True
    assert 1 <= order_service_config.catalog_page_limit <= 100
