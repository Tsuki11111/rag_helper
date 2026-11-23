"""订单动作应用服务：预检、草稿文案与批量编排。

只生成 PENDING_CONFIRM 草稿，不执行；执行由 Java 在用户确认后完成。
"""
from app.agent.runtime.tool_errors import is_failure_text
from app.agent.subagents.order_service.domain.ports import OrderGateway
from app.agent.subagents.order_service.domain.rules import (
    BATCH_LIMIT,
    REFUNDABLE_STATUSES,
    URGE_REFUNDABLE_STATUSES,
    URGE_SHIPPABLE_STATUSES,
)
from app.clients.its_mygo_client import call
from app.conf.order_service_config import order_service_config

# 与 Java AgentOrderDraftBuilder 的约束一致：购买数量 1–99
MAX_ITEM_QUANTITY = 99


class OrderActionService:
    def __init__(self, call_fn: OrderGateway | None = None):
        self._call = call_fn or call

    def list_addresses(self) -> str:
        """列出当前用户的收货地址（含地址 id，供 prepare_order 使用）。"""
        data, error = self._call("/internal/agent/catalog/addresses",
                                 method="GET", read_only=True)
        if error:
            return error
        addresses = data or []
        if not addresses:
            return "当前用户还没有收货地址，请先到用户中心添加。"

        lines = [f"共 {len(addresses)} 个收货地址："]
        for idx, address in enumerate(addresses, 1):
            if not isinstance(address, dict):
                continue
            default = "（默认）" if address.get("isDefault") else ""
            lines.append(
                f"{idx}. id={address.get('id')}，{address.get('receiver') or ''} "
                f"{address.get('phone') or ''}，{address.get('address') or ''}{default}")
        return "\n".join(lines)

    def precheck_address(self, address_id: int) -> str:
        """校验收货地址 id 存在；通过返回空串。"""
        data, error = self._call("/internal/agent/catalog/addresses",
                                 method="GET", read_only=True)
        if error:
            return f"预检失败：{error}"
        ids = {a.get("id") for a in (data or []) if isinstance(a, dict)}
        if address_id not in ids:
            return f"预检未通过：收货地址 id={address_id} 不存在，请重新选择收货地址。"
        return ""

    def precheck_items(self, normalized: list[dict]) -> str:
        """校验商品行：商品在售、SKU 存在、库存足够。"""
        data, error = self._call(
            "/internal/agent/catalog/all", method="GET", read_only=True,
            params={"limit": order_service_config.catalog_page_limit})
        if error:
            return f"预检失败：{error}"
        products = [p for p in (data or []) if isinstance(p, dict)]
        capped = len(products) >= order_service_config.catalog_page_limit
        index = {}
        for product in products:
            skus = {s.get("skuId"): s for s in (product.get("skus") or [])
                    if isinstance(s, dict)}
            index[product.get("productId")] = {
                "name": product.get("name") or "未知商品", "skus": skus}
        for item in normalized:
            product = index.get(item["productId"])
            if product is None:
                if capped:
                    continue
                return (f"预检未通过：商品 id={item['productId']} 不在售（可能已下架），"
                        f"请重新选择商品。")
            sku = product["skus"].get(item["skuId"])
            if sku is None:
                return (f"预检未通过：商品「{product['name']}」没有 skuId={item['skuId']} "
                        f"这个规格，请重新选择规格。")
            try:
                stock = int(sku.get("stock") or 0)
            except (TypeError, ValueError):
                stock = 0
            if stock < item["quantity"]:
                spec = sku.get("specText") or "默认规格"
                return (f"预检未通过：商品「{product['name']}」（{spec}）库存只剩 {stock} 件，"
                        f"不够 {item['quantity']} 件；可以少买几件或换个规格。")
        return ""

    def precheck_order_status(self, order_no: str, allowed: set, action_cn: str) -> str:
        """校验订单状态是否允许该动作；通过返回空串。"""
        data, error = self._call("/internal/agent/orders/query",
                                 {"orderNo": order_no, "limit": 1}, read_only=True)
        if error:
            return f"预检失败：{error}"
        orders = [o for o in (data or []) if isinstance(o, dict)]
        if not orders:
            return f"预检未通过：没查到你名下的订单 {order_no}，请核对订单号。"
        order = orders[0]
        status = str(order.get("status") or "")
        if status not in allowed:
            label = order.get("statusLabel") or status or "未知状态"
            return f"预检未通过：订单 {order_no} 当前是「{label}」，不能{action_cn}。"
        return ""

    def prepare_order(self, items: list[dict], address_id: int | None = None) -> str:
        """准备下单草稿（不会真正下单）。"""
        if not items:
            return "请先确认要购买的商品和数量。"

        normalized = []
        for raw in items:
            if not isinstance(raw, dict):
                return "商品行格式不正确，请提供 productId / skuId / quantity。"
            try:
                raw_quantity = raw.get("quantity")
                # 未填数量默认 1；显式 0/负数必须拒绝，不能悄悄改成 1 或放行
                quantity = 1 if raw_quantity in (None, "") else int(raw_quantity)
                if not 1 <= quantity <= MAX_ITEM_QUANTITY:
                    return f"购买数量需在 1–{MAX_ITEM_QUANTITY} 之间。"
                normalized.append({
                    "productId": int(raw.get("productId")),
                    "skuId": int(raw.get("skuId")),
                    "quantity": quantity,
                })
            except (TypeError, ValueError):
                return "商品行格式不正确，请提供数字类型的 productId / skuId / quantity。"

        payload: dict = {"items": normalized}
        if address_id is not None:
            try:
                payload["addressId"] = int(address_id)
            except (TypeError, ValueError):
                return "收货地址 id 不正确。"
            precheck = self.precheck_address(payload["addressId"])
            if precheck:
                return precheck

        precheck = self.precheck_items(normalized)
        if precheck:
            return precheck

        data, error = self._call("/internal/agent/actions/order", payload)
        if error:
            return error
        summary = (data or {}).get("summary") or {}
        address = summary.get("address") or {}
        return (
            "订单草稿已生成（尚未下单）。请让用户在下方确认卡片里核对并点击「确认下单」；"
            "确认后才会生成待支付订单，支付仍需用户到订单详情页完成。\n"
            f"摘要：共 {summary.get('itemCount', 0)} 件，"
            f"应付 ¥{summary.get('totalAmount', '?')}；"
            f"收货人 {address.get('receiver') or '未知'}，"
            f"地址 {address.get('address') or '未知'}。")

    def prepare_refund(self, order_no: str, reason: str = "") -> str:
        """准备退款申请草稿（不会真正提交）。"""
        no = (order_no or "").strip()
        if not no:
            return "请提供要退款的订单号。"
        precheck = self.precheck_order_status(no, REFUNDABLE_STATUSES, "申请退款")
        if precheck:
            return precheck

        payload = {"orderNo": no}
        clean_reason = (reason or "").strip()
        if clean_reason:
            payload["reason"] = clean_reason[:200]
        data, error = self._call("/internal/agent/actions/refund", payload)
        if error:
            return error
        summary = (data or {}).get("summary") or {}
        return (
            "退款申请草稿已生成（尚未提交）。请让用户在下方确认卡片里核对并点击"
            "「确认提交退款申请」；确认后才会提交给商家。\n"
            f"订单 {summary.get('orderNo') or no}，"
            f"金额 ¥{summary.get('totalAmount', '?')}，"
            f"当前状态 {summary.get('statusLabel') or '未知'}，"
            f"退款原因 {summary.get('refundReason') or clean_reason or '未填写'}。")

    def prepare_urge_shipment(self, order_no: str) -> str:
        """准备催发货消息草稿（不会直接发送）。"""
        no = (order_no or "").strip()
        if not no:
            return "请提供要催发货的订单号。"
        precheck = self.precheck_order_status(no, URGE_SHIPPABLE_STATUSES, "催发货")
        if precheck:
            return precheck

        data, error = self._call("/internal/agent/actions/urge-shipment", {"orderNo": no})
        if error:
            return error
        summary = (data or {}).get("summary") or {}
        return (
            "催发货消息草稿已生成（尚未发送）。请让用户在下方确认卡片里核对并点击"
            "「确认催发货」；确认后才会通知商家。\n"
            f"订单 {summary.get('orderNo') or no}，"
            f"店铺 {summary.get('merchantName') or '未知'}，"
            f"当前状态 {summary.get('statusLabel') or '未知'}。")

    def prepare_urge_refund(self, order_no: str) -> str:
        """准备催退款消息草稿（不会直接发送）。"""
        no = (order_no or "").strip()
        if not no:
            return "请提供要催退款的订单号。"
        precheck = self.precheck_order_status(no, URGE_REFUNDABLE_STATUSES, "催退款")
        if precheck:
            return precheck

        data, error = self._call("/internal/agent/actions/urge-refund", {"orderNo": no})
        if error:
            return error
        summary = (data or {}).get("summary") or {}
        return (
            "催退款消息草稿已生成（尚未发送）。请让用户在下方确认卡片里核对并点击"
            "「确认催退款」；确认后才会提醒商家处理退款。\n"
            f"订单 {summary.get('orderNo') or no}，"
            f"店铺 {summary.get('merchantName') or '未知'}，"
            f"当前状态 {summary.get('statusLabel') or '未知'}，"
            f"退款原因 {summary.get('refundReason') or '未填写'}。")

    @staticmethod
    def clean_order_nos(order_nos) -> list:
        """把模型可能传成字符串/列表的订单号收敛成去重列表（保持原顺序）。"""
        if isinstance(order_nos, str):
            raw_items = order_nos.replace("，", ",").replace("、", ",").replace(" ", ",").split(",")
        elif isinstance(order_nos, (list, tuple)):
            raw_items = list(order_nos)
        else:
            return []
        cleaned, seen = [], set()
        for item in raw_items:
            no = str(item or "").strip()
            if not no or no in seen:
                continue
            seen.add(no)
            cleaned.append(no)
        return cleaned

    def batch_result(self, action_cn: str, order_nos, single) -> str:
        """批量执行单条草稿工具并汇总；单条失败不中断其余。"""
        cleaned = self.clean_order_nos(order_nos)
        if not cleaned:
            return f"请提供至少一个要{action_cn}的订单号。"
        if len(cleaned) > BATCH_LIMIT:
            return f"一次最多处理 {BATCH_LIMIT} 单，请分批发给我。"
        succeeded, failed = [], []
        for no in cleaned:
            text = single(no)
            if is_failure_text(text):
                failed.append(f"- {no}：{text}")
            else:
                succeeded.append(no)
        lines = [f"批量{action_cn}：成功 {len(succeeded)} 单，失败 {len(failed)} 单。"]
        if succeeded:
            lines.append("已生成草稿（**尚未发送**，请逐张确认）：" + "、".join(succeeded))
        if failed:
            lines.append("未生成草稿：")
            lines.extend(failed)
        lines.append("每单都需要用户在对应的确认卡片上点击确认后才会真正执行。")
        return "\n".join(lines)

    def prepare_refunds(self, order_nos) -> str:
        return self.batch_result("退款申请", order_nos, self.prepare_refund)

    def prepare_urge_shipments(self, order_nos) -> str:
        return self.batch_result("催发货", order_nos, self.prepare_urge_shipment)

    def prepare_urge_refunds(self, order_nos) -> str:
        return self.batch_result("催退款", order_nos, self.prepare_urge_refund)
