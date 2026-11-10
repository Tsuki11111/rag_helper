"""final 事件契约的回归用例（从 app/agent/events.py 迁出）。"""
from app.agent.events import FINAL_FIELDS, build_final_event


def _check_final_event_contract() -> list:
    """回归：final 事件必须带商品卡/表单到两个 transport 共用的 payload。"""
    problems: list = []
    ev = {
        "type": "final", "text": "给你推荐 2 款", "web_only": True,
        "images": [{"url": "http://img/1.png", "caption": "图 1"}],
        "products": [{"productId": "11", "name": "路由器"}],
        "forms": [{"form_id": "f1", "form_type": "refund_reason"}],
    }
    payload = build_final_event(ev)
    if payload["text"] != "给你推荐 2 款":
        problems.append("final 文案丢失")
    if [p.get("productId") for p in payload["products"]] != ["11"]:
        problems.append("final 商品卡丢失")
    if [f.get("form_id") for f in payload["forms"]] != ["f1"]:
        problems.append("final 表单丢失")
    if payload["images"] != ev["images"] or not payload["web_only"]:
        problems.append("final 图片/联网标记丢失")
    # 空事件也要给出稳定骨架，避免下游 KeyError
    empty = build_final_event({"type": "final"})
    for key in FINAL_FIELDS:
        if key not in empty:
            problems.append(f"final 空事件缺字段：{key}")
    return problems
