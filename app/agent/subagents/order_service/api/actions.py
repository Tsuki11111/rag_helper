"""
M9 / M10：下单与退款申请的工具适配层。

业务在 `application/order_action_service.py`；这里只保留模型工具签名与导出，
方便测试继续从 `api.actions` 取名字。**这些工具只生成草稿，不执行**。
"""
from app.agent.subagents.order_service.application.order_action_service import (
    OrderActionService,
)
from app.agent.subagents.order_service.domain.rules import (
    BATCH_LIMIT,
    REFUNDABLE_STATUSES,
    URGE_REFUNDABLE_STATUSES,
    URGE_SHIPPABLE_STATUSES,
)
from app.clients.its_mygo_client import call
from app.conf.order_service_config import order_service_config


def _service() -> OrderActionService:
    # 每次按当前 call 构造：测试会 monkeypatch 模块级 call
    return OrderActionService(call_fn=call)


def list_addresses() -> str:
    return _service().list_addresses()


def _precheck_items(normalized: list[dict]) -> str:
    return _service().precheck_items(normalized)


def prepare_order(items: list[dict], address_id: int | None = None) -> str:
    return _service().prepare_order(items, address_id=address_id)


def prepare_refund(order_no: str, reason: str = "") -> str:
    return _service().prepare_refund(order_no, reason=reason)


def prepare_urge_shipment(order_no: str) -> str:
    return _service().prepare_urge_shipment(order_no)


def prepare_urge_refund(order_no: str) -> str:
    return _service().prepare_urge_refund(order_no)


def prepare_refunds(order_nos) -> str:
    return _service().prepare_refunds(order_nos)


def prepare_urge_shipments(order_nos) -> str:
    return _service().prepare_urge_shipments(order_nos)


def prepare_urge_refunds(order_nos) -> str:
    return _service().prepare_urge_refunds(order_nos)


# ── 离线自测（用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

class _FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._body
