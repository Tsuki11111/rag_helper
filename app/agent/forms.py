"""会话内表单（2026-11-07，评审通过；协议见 proto/agent.proto + docs/form-protocol-review.md）

agent 侧职责只有两件事：
1. **下发**：主 agent 调用表单工具 → 表单进 ContextVar 收集器 → 随 final 事件/历史下发；
2. **回传处理**：Java 把用户提交的 `FormResult` 放回下一轮 Converse → 这里按 `form_type`
   **直接调工具**（不经过模型猜参数），并校验 form_id「本会话下发过、未过期、只用一次」。

首批只实现 `refund_reason`（最小闭环，不依赖新的业务接口）。
"""
import json
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from app.core.logger import logger

# 表单有效期（秒）：与 review 稿一致，前端/服务端共用这个默认值
FORM_TTL_SECONDS = 600

# 当前这一轮要下发的表单（最多 1 张）。None 表示当前不在收集上下文（如子图/CLI）
_FORMS: ContextVar[Optional[list]] = ContextVar("agent_forms", default=None)


def reset_forms():
    """开一轮新的收集器（run_main_agent 用），返回 token 供 finally 复位。"""
    return _FORMS.set([])


def reset_forms_token(token) -> None:
    _FORMS.reset(token)


def add_form(form: Dict[str, Any]) -> None:
    """把表单放进当前轮的收集器；不在收集上下文时忽略（例如离线调用）。"""
    bucket = _FORMS.get()
    if bucket is None:
        logger.warning("[forms] 当前不在 run_main_agent 上下文，表单未收集")
        return
    if bucket:
        # 契约约定一轮最多 1 张：后来者丢弃，避免前端同时弹多张
        logger.warning("[forms] 本轮已有表单，忽略第二张（契约：最多 1 张）")
        return
    bucket.append(form)


def current_forms() -> List[Dict[str, Any]]:
    return list(_FORMS.get() or [])


def merge_forms(forms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去重保序（按 form_id）；空值过滤。"""
    out, seen = [], set()
    for form in forms or []:
        if not isinstance(form, dict):
            continue
        fid = form.get("form_id")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        out.append(form)
    return out


def build_form(form_type: str, title: str, fields: List[Dict[str, Any]],
               meta: Dict[str, Any] = None, description: str = "",
               submit_label: str = "提交", ttl: int = FORM_TTL_SECONDS) -> Dict[str, Any]:
    """构造一张表单（字段/选项结构按 proto FormRequest 的 JSON 形状）。"""
    now = int(time.time())
    return {
        "form_id": "form_" + uuid.uuid4().hex[:16],
        "form_type": form_type,
        "title": title,
        "description": description,
        "fields": fields,
        "submit_label": submit_label,
        "expires_in_seconds": int(ttl),
        "meta": {str(k): str(v) for k, v in (meta or {}).items()},
        # created_at 只在服务端用：过期判断；前端可以忽略
        "created_at": now,
    }


# ── 具体表单：首发退款原因 + 改地址 ──

def request_refund_reason_form(order_no: str) -> str:
    """给用户下发「退款原因」表单（**只下发表单，不办理退款**）。

    用户要申请退款、但还没说原因时调用：先把订单号带上，用户选完后系统会
    直接生成退款草稿（不再让用户手打原因）。一轮只问这一件事，下发后不要继续办理。

    **选项由平台补全**（评审结论 §9）：本仓库不硬编码退款原因字典，下发时 options 为空，
    Java 在 SSE 转发时用售后原因接口（`AfterSaleService.reasons()`）填入。

    :param order_no: 要申请退款的订单号（来自 query_orders 的真实订单）
    """
    no = (order_no or "").strip()
    if not no:
        return "请先确认要退款的订单号，再下发退款原因表单。"
    form = build_form(
        form_type="refund_reason",
        title="选择退款原因",
        description="选一个最符合的原因，提交后会为您生成退款申请草稿（还需您在确认卡片上确认）。",
        fields=[{
            "name": "reason", "label": "退款原因", "type": "select", "required": True,
            "placeholder": "请选择退款原因", "options": [],
        }],
        meta={"orderNo": no},
        submit_label="提交退款原因",
    )
    add_form(form)
    return (f"已下发退款原因表单（订单 {no}）。请让用户在下方表单里选择原因并提交；"
            f"提交后会直接生成退款草稿，**现在还没有申请退款**。")


def request_address_edit_form(address_id: int) -> str:
    """给用户下发「修改收货地址」表单（**只下发表单，不直接改地址**）。

    用户要改收货地址时调用：先确认是哪条地址（可先让 order-service 用 list_addresses
    列出，用户选定后把 id 传进来）。用户提交后系统会直接更新该地址。

    :param address_id: 要修改的收货地址 id（来自 list_addresses 的真实地址）
    """
    try:
        aid = int(address_id)
    except (TypeError, ValueError):
        return "收货地址 id 不正确，请先让用户确认要改哪条地址。"
    form = build_form(
        form_type="address_edit",
        title="修改收货地址",
        description="核对并修改后提交，提交后会直接更新这条收货地址。",
        fields=[
            {"name": "receiver", "label": "收货人", "type": "text", "required": True},
            {"name": "phone", "label": "手机号", "type": "text", "required": True},
            {"name": "province", "label": "省份", "type": "text", "required": True},
            {"name": "city", "label": "城市", "type": "text", "required": True},
            {"name": "district", "label": "区县", "type": "text", "required": True},
            {"name": "detail", "label": "详细地址", "type": "text", "required": True},
        ],
        meta={"addressId": aid},
        submit_label="保存地址",
    )
    add_form(form)
    return (f"已下发收货地址表单（地址 id={aid}）。请让用户修改后提交；"
            f"提交后会直接更新这条地址，**现在还没有改**。")


# ── 回传处理：按 form_type 直接调工具 ──

def _find_pending_form(history: List[Dict[str, Any]], form_id: str) -> Optional[Dict[str, Any]]:
    """在会话历史里找这张表单；找不到/已用过/已过期都返回 None。"""
    form = None
    used = False
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("form_result_for") == form_id:
            used = True
        for f in (msg.get("forms") or []):
            if isinstance(f, dict) and f.get("form_id") == form_id:
                form = f
    if used or form is None:
        return None
    created_at = int(form.get("created_at") or 0)
    ttl = int(form.get("expires_in_seconds") or FORM_TTL_SECONDS)
    if created_at and time.time() - created_at > ttl:
        return None
    return form


def dispatch_form_result(form_id: str, values: Dict[str, str],
                         history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    处理用户提交的表单：校验 → 按 form_type 直接调用工具。

    :return: {"ok": bool, "answer": 给用户的最终回答, "kind": dispatched/expired/invalid/unsupported,
              "tool": 实际调用的工具名（可选）, "form": 命中的表单}
    """
    fid = (form_id or "").strip()
    if not fid:
        return {"ok": False, "kind": "invalid", "answer": "表单信息不完整，请重新发起一次。"}
    form = _find_pending_form(history, fid)
    if form is None:
        return {"ok": False, "kind": "expired",
                "answer": "这张表单已经失效或提交过了，请重新告诉我要办理的业务。"}
    form_type = form.get("form_type") or ""
    meta = form.get("meta") or {}
    clean_values = {str(k): str(v) for k, v in (values or {}).items()}

    if form_type == "refund_reason":
        reason = (clean_values.get("reason") or "").strip()
        order_no = str(meta.get("orderNo") or "").strip()
        if not reason:
            return {"ok": False, "kind": "invalid", "form": form,
                    "answer": "退款原因还没有选，请重新选一个原因后提交。"}
        if not order_no:
            return {"ok": False, "kind": "invalid", "form": form,
                    "answer": "表单里缺少订单信息，请重新发起退款申请。"}
        # 局部导入：避免 main_agent ↔ order_actions 的循环依赖
        from app.agent.sub_agents.order_service_agent.tools.order_actions import prepare_refund
        text = prepare_refund(order_no, reason=reason)
        failed_prefix = ("预检未通过", "预检失败", "操作未完成", "平台服务暂时不可用", "平台工具凭证")
        ok = not text.startswith(failed_prefix)
        logger.info(f"[forms] refund_reason 表单已回传：订单={order_no} 原因={reason} 成功={ok}")
        return {"ok": ok, "kind": "dispatched", "answer": text, "tool": "prepare_refund",
                "form": form}

    if form_type == "address_edit":
        try:
            address_id = int(meta.get("addressId"))
        except (TypeError, ValueError):
            return {"ok": False, "kind": "invalid", "form": form,
                    "answer": "表单里缺少地址信息，请重新发起修改地址。"}
        required = ("receiver", "phone", "province", "city", "district", "detail")
        missing = [k for k in required if not (clean_values.get(k) or "").strip()]
        if missing:
            return {"ok": False, "kind": "invalid", "form": form,
                    "answer": "地址信息不完整，请把必填项都填上再提交。"}
        from app.agent.sub_agents.order_service_agent.tools.its_mygo_client import call
        data, error = call("/internal/agent/actions/address", {
            "addressId": address_id, "receiver": clean_values["receiver"].strip(),
            "phone": clean_values["phone"].strip(), "province": clean_values["province"].strip(),
            "city": clean_values["city"].strip(), "district": clean_values["district"].strip(),
            "detail": clean_values["detail"].strip(),
        })
        if error:
            return {"ok": False, "kind": "dispatched", "answer": error, "tool": "update_address",
                    "form": form}
        address = ((data or {}).get("summary") or {}).get("address") or {}
        answer = (f"收货地址已更新：{address.get('receiver') or clean_values['receiver']}，"
                  f"{address.get('address') or ''}。")
        logger.info(f"[forms] address_edit 表单已回传：地址={address_id} 成功=True")
        return {"ok": True, "kind": "dispatched", "answer": answer, "tool": "update_address",
                "form": form}

    return {"ok": False, "kind": "unsupported", "form": form,
            "answer": "暂不支持这种表单，请用文字说明。"}


# ── 离线自测 ──

def _check_forms_roundtrip() -> list:
    """回归：下发 → 回传 → 按 form_type 调工具；过期/重复/缺值都拒绝。"""
    problems = []
    real_bucket = _FORMS.set([])
    try:
        text = request_refund_reason_form("20261107164942602122")
        forms = current_forms()
        if len(forms) != 1:
            problems.append(f"表单未入收集器：{forms}")
        if "表单" not in text or "还没有申请退款" not in text:
            problems.append(f"下发文案口径不对：{text!r}")
        form = forms[0]
        if form.get("form_type") != "refund_reason":
            problems.append(f"form_type 不对：{form.get('form_type')}")
        if (form.get("meta") or {}).get("orderNo") != "20261107164942602122":
            problems.append(f"meta 没带订单号：{form.get('meta')}")
        # 评审结论 §9：原因选项由平台补全，rag_helper 不硬编码
        if (form.get("fields") or [{}])[0].get("options"):
            problems.append("退款原因选项不应由 rag_helper 硬编码")
    finally:
        _FORMS.reset(real_bucket)

    # 回传：直接调工具（打桩 prepare_refund，验证参数是表单里的 reason）
    import app.agent.sub_agents.order_service_agent.tools.order_actions as oa
    real_prepare = oa.prepare_refund
    calls = []

    def _fake_prepare(order_no, reason=""):
        calls.append((order_no, reason))
        return "退款申请草稿已生成（尚未提交）。"

    oa.prepare_refund = _fake_prepare
    try:
        form = build_form(form_type="refund_reason", title="t",
                          fields=[{"name": "reason", "type": "select"}],
                          meta={"orderNo": "NO1"})
        history = [{"role": "assistant", "forms": [form]}]
        result = dispatch_form_result(form["form_id"], {"reason": "商品有瑕疵"}, history)
        if not result.get("ok") or calls != [("NO1", "商品有瑕疵")]:
            problems.append(f"回传未直接调工具：result={result} calls={calls}")
        if "退款申请草稿" not in result.get("answer", ""):
            problems.append(f"回传回答不对：{result.get('answer')!r}")

        # 已用过（历史里出现 form_result_for）→ 拒绝
        used_hist = [{"role": "assistant", "forms": [form]},
                     {"role": "assistant", "form_result_for": form["form_id"]}]
        again = dispatch_form_result(form["form_id"], {"reason": "其他"}, used_hist)
        if again.get("kind") != "expired":
            problems.append(f"重复提交未被拒绝：{again}")

        # 过期 → 拒绝
        old = build_form(form_type="refund_reason", title="t", fields=[], meta={"orderNo": "NO1"})
        old["created_at"] = int(time.time()) - FORM_TTL_SECONDS - 10
        expired = dispatch_form_result(old["form_id"], {"reason": "其他"},
                                       [{"role": "assistant", "forms": [old]}])
        if expired.get("kind") != "expired":
            problems.append(f"过期表单未被拒绝：{expired}")

        # 缺原因 → 拒绝
        missing = dispatch_form_result(form["form_id"], {}, [{"role": "assistant", "forms": [form]}])
        if missing.get("kind") != "invalid":
            problems.append(f"缺值未被拒绝：{missing}")

        # 改地址：下发 + 回传直接调 Java 内部接口（打桩）
        real_bucket = _FORMS.set([])
        try:
            addr_text = request_address_edit_form(1590)
            addr_forms = current_forms()
        finally:
            _FORMS.reset(real_bucket)
        if not addr_forms or addr_forms[0].get("form_type") != "address_edit":
            problems.append(f"地址表单未下发：{addr_forms}")
        addr_form = addr_forms[0] if addr_forms else {}
        import app.agent.sub_agents.order_service_agent.tools.its_mygo_client as imc
        real_call = imc.call
        sent = {}

        def _fake_call(path, payload=None, **kwargs):
            sent.update(path=path, payload=payload)
            return {"summary": {"address": {"receiver": "李四", "address": "杭州市西湖区"}}}, ""

        imc.call = _fake_call
        try:
            addr_result = dispatch_form_result(
                addr_form.get("form_id"),
                {"receiver": "李四", "phone": "13800002222", "province": "浙江省",
                 "city": "杭州市", "district": "西湖区", "detail": "文三路 1 号"},
                [{"role": "assistant", "forms": [addr_form]}])
        finally:
            imc.call = real_call
        if not addr_result.get("ok") or sent.get("path") != "/internal/agent/actions/address":
            problems.append(f"改地址未走内部接口：{addr_result} / {sent}")
        if addr_result.get("ok") and "已更新" not in addr_result.get("answer", ""):
            problems.append(f"改地址回答不对：{addr_result.get('answer')!r}")
        # 必填缺失 → 拒绝且不调接口
        sent.clear()
        imc.call = _fake_call
        try:
            addr_missing = dispatch_form_result(
                addr_form.get("form_id"), {"receiver": "李四"},
                [{"role": "assistant", "forms": [addr_form]}])
        finally:
            imc.call = real_call
        if addr_missing.get("kind") != "invalid" or sent:
            problems.append(f"地址必填缺失未拦下：{addr_missing} / {sent}")
    finally:
        oa.prepare_refund = real_prepare
    return problems


if __name__ == "__main__":
    problems = _check_forms_roundtrip()
    if problems:
        for p in problems:
            print("FAIL", p)
        raise SystemExit(1)
    print("OK 表单回传自测通过")
